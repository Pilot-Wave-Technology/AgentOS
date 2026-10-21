"""E14 — the harness's long tools: job_start, job_poll, job_cancel.

THE SHAPE THE MODEL ALREADY HAS. `bash_background` → `bash_output` →
`bash_kill` works: the job outlives the call, the caller polls, and a
twelve-minute build is watched rather than waited on. The HARNESS had
one blocking call with a hard ceiling — `env_setup`, `env_install`, a
check run — and three costs came with it. The ceiling is a CLIFF (a
monorepo install past it reports `TimeoutError`, which reads as a broken
manifest and is not one; KER25 lost a story to the previous cliff at
120s). The call holds a tool-pool thread for its whole duration, which
is the sizing argument D5 retires. And a worker that restarts mid-install
loses the install AND leaves it running.

SO THIS IS NOT `tools_jobs.py` WITH DIFFERENT NAMES, and the difference
is the whole reason for a second module:

  * `bash_output` RENDERS FOR A MODEL. Its exit code lives in a prose
    header, beside notes about dropped bytes. Parsing that
    programmatically is fault-10 shaped — it is how a 45MB capture came
    back as "stored nothing". Two consumers, two formats.
  * `state` and `exit_code` are SEPARATE FIELDS here. A running job has
    `exit_code: null` — never 0, never -1. `kernel/drivers/k8s_outcome.py`
    learned that at length: a placeholder exit code is read as a result,
    and a fabricated 1 becomes "the work is wrong".
  * OUTPUT IS BASE64 AT A BYTE OFFSET, for the reason
    `kernel/verifier/runner.py` states: the result stays one line and
    stays parseable whatever the command emitted — a control character,
    half a UTF-8 sequence, an ANSI escape, a NUL.

THE BUDGET IS ENFORCED HERE, IN THE POD, by a watchdog thread — never by
the caller's HTTP timeout. That is what makes every bridge call short: a
thirty-minute install is a thirty-minute install and the harness holds a
thread for a poll, not for a run.

A JOB OUTLIVES ITS POLLER. State lives in this process, keyed by job id,
exactly as `bash_background`'s does. `key` is how a restarted worker
finds it again without a database column: starting with the same key
while the job runs RE-ATTACHES instead of starting a second one — which
is not hypothetical, execution 200 ran a second `pip` against one tree
because the first caller stopped listening.
"""

import base64
import os
import time

import errors
import jobcore
import limits

# The tail we keep per job. Generous, because a poller reads this
# INCREMENTALLY and only falls behind if it stops polling; small enough
# that four stuck jobs cannot be the reason a sandbox is OOM-killed.
MAX_BUFFER = 4 * 1024 * 1024

# One poll's answer. Base64 inflates by a third and the whole body is
# one JSON line, so an unbounded first poll on a noisy install would be
# a multi-megabyte response to a 10s call. `offset` is the next cursor,
# so the rest arrives on the next poll -- which the caller was going to
# make anyway.
POLL_BYTES = 1024 * 1024

# Harness jobs are started by the harness, not by a model: env setup, an
# install, a check run. More than a handful at once means something is
# starting them in a loop.
MAX_JOBS = 4

LANE = "harness"

# THE REGISTRY MOVED TO `jobcore`. The drain thread, the group kill, the
# reap grace, the budget watchdog and the `ended` reason all used to live
# here and nowhere else -- which is exactly why the model's side went
# without them. They are shared now; what stays in this file is the
# harness's own presentation: base64 at a byte offset, `state` and
# `exit_code` as separate fields, and a `key` that re-attaches.


def _result(payload: dict) -> dict:
    """The structured answer, under its own key.

    NESTED RATHER THAN MERGED, because the envelope already has an
    `exit_code` and it means something else: the TOOL's. A job's exit
    code sharing that name is the confusion `k8s_outcome` documents --
    "the poll succeeded" and "the install failed" must never be the
    same field.
    """
    return errors.ok("", result=payload)


def _cwd(raw, workspace: str):
    """(directory, refusal). A cwd that is not there is never silently
    swapped for another one: an install that ran in the wrong directory
    reports success and changes nothing anybody can find."""
    if not raw:
        return (workspace if os.path.isdir(workspace) else None), None
    path = raw if os.path.isabs(raw) else os.path.join(workspace, raw)
    if not os.path.isdir(path):
        return None, errors.fail("E_PATH_NOT_FOUND",
                                 f"cwd {path!r} is not a directory")
    return path, None


def tool_start(args: dict, workspace: str, safe_env: dict) -> dict:
    """Start a job and return its id. Never waits for the work."""
    command = args.get("command") or ""
    if not command:
        return errors.fail("E_BAD_ARGUMENT", "command is required")
    try:
        budget = limits.job_budget(args)
    except limits.BadTimeout as bad:
        return errors.fail("E_BAD_ARGUMENT", str(bad))
    cwd, refusal = _cwd(args.get("cwd"), workspace)
    if refusal:
        return refusal
    env = dict(safe_env)
    for name, value in (args.get("env") or {}).items():
        env[str(name)] = str(value)

    job_id = jobcore.new_id("hjob", args.get("key"))
    prior = jobcore.snapshot(job_id)
    if prior is not None and prior["running"]:
        # THE RESTART CASE, and the reason `key` exists. Starting a
        # second `pip` against one tree is what execution 200 did when
        # the first caller's HTTP timeout fired.
        jobcore.log(LANE, "reattach", job_id)
        return _result({"job": job_id, "reattached": True})
    if prior is not None:
        # Finished. A deliberate re-run must not read back the previous
        # run's exit code as its own.
        jobcore.forget(job_id)

    live = jobcore.live_in_lane(LANE)
    if live >= MAX_JOBS:
        return errors.fail(
            "E_TOO_LARGE",
            f"too many harness jobs ({live}); cancel one or wait")

    failed = jobcore.start(job_id, command, cwd=cwd, env=env,
                           budget_s=budget, cap=MAX_BUFFER, lane=LANE)
    if failed:
        return errors.fail("E_TRANSIENT", failed)
    return _result({"job": job_id, "reattached": False})


def _unknown(job_id: str) -> dict:
    known = ", ".join(jobcore.known(LANE)) or "none"
    return errors.fail("E_BAD_ARGUMENT",
                       f"unknown job {job_id!r}; known: {known}")


def tool_poll(args: dict) -> dict:
    """What the job has produced since `since`. Returns what exists NOW."""
    job_id = args.get("job") or ""
    raw = args.get("since")
    since = max(0, raw) if isinstance(raw, int) and not isinstance(
        raw, bool) else 0
    snap = jobcore.snapshot(job_id, since=since, window=POLL_BYTES)
    if snap is None:
        return _unknown(job_id)
    running = snap["running"]
    return _result({
        "state": "running" if running else "exited",
        # NULL WHILE RUNNING. Not 0, not -1: a placeholder exit code is
        # read as a result (k8s_outcome), and this field is the one a
        # caller branches on.
        "exit_code": None if running else snap["exit_code"],
        "bytes": base64.b64encode(snap["chunk"]).decode("ascii"),
        "offset": snap["offset"],
        "elapsed_s": round(
            (snap["finished_at"] or time.time()) - snap["started_at"], 3),
        # Why it stopped: "exit", "budget" or "cancel". A signal death
        # cannot say which on its own, and "your install ran out of
        # time" is a different sentence from "your install failed".
        "ended": snap["ended"],
        # Bytes between the caller's cursor and what is still held. Only
        # a SUPPLIED cursor can be outrun: `since = 0` is "give me what
        # you have", which on a long job is the ordinary first poll and
        # not a poller who fell behind.
        "dropped": max(0, snap["kept_from"] - since) if since else 0,
    })


def tool_cancel(args: dict) -> dict:
    """Stop a job. Idempotent: cancelling a finished job is not an error."""
    job_id = args.get("job") or ""
    found, was_running = jobcore.cancel(job_id, "cancel")
    if not found:
        return _unknown(job_id)
    return _result({"state": "cancelled", "was_running": was_running})
