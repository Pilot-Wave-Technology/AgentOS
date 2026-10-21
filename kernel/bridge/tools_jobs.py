"""The model's background shell tools: bash_background, bash_output, bash_kill.

Why this exists. A foreground `bash` running a build or a test suite blocks
the step, and a suite that takes longer than the leg's wall clock ends the
leg on the lease predicate with nothing to show for the work. Our own gate
takes twelve minutes. Without this an executor cannot run its own tests,
which is most of what a coding agent does between edits.

Jobs live in this process, which is the sandbox. That is deliberate: a job
must not outlive the sandbox it was started in, or a killed leg leaves a
compiler running against a workspace the next leg is editing.

A PRESENTER OVER `jobcore` SINCE THE MERGE. This module used to keep its
own registry, its own lock and its own drain thread, and it was the
unfinished one: no budget, `proc.kill()` instead of a group kill, no
reap grace, no `ended` reason. The harness's registry had all four. Two
implementations of one mechanism drift, so the machinery moved to
`jobcore` and what stays here is what the MODEL reads -- the `since`
cursor, the window, and the prose. Behaviour the model can observe is
unchanged by the move itself.
"""

import errors
import jobcore
import limits
import os
import time

# The window the model reads, as text. Small on purpose: this is context,
# and `jobcore` keeps every byte's COUNT so a cursor survives the drop.
#
# CONFIGURABLE BY THE POD, not by the model. The compressed-timings
# profile sets this at dispatch so a 2 KB window can be exercised without
# a job that writes 32 KB. Read with the one-argument form and an
# explicit fallback -- `check_no_environ_default` bans the two-argument
# `os.environ.get`, and a hard `os.environ[...]` would fail a pod that
# simply does not set it.
_CAP_ENV = "AGENTOS_JOB_OUTPUT_MAX"
_SLICE_ENV = "AGENTOS_WAIT_SLICE_S"
DEFAULT_MAX_OUTPUT = 32 * 1024


def _max_output() -> int:
    raw = os.environ.get(_CAP_ENV) if _CAP_ENV in os.environ else None
    try:
        return max(1024, int(raw)) if raw else DEFAULT_MAX_OUTPUT
    except (TypeError, ValueError):
        return DEFAULT_MAX_OUTPUT


# Read once per call rather than at import, so the module docstring's
# claim stays true when the profile changes between dispatches.
MAX_OUTPUT = DEFAULT_MAX_OUTPUT

MAX_JOBS = 8
LANE = "model"


def tool_bash_background(args: dict, workspace: str, safe_env: dict) -> dict:
    command = args.get("command", "")
    if not command:
        return errors.fail("E_BAD_ARGUMENT", "command is required")

    live = jobcore.live_in_lane(LANE)
    if live >= MAX_JOBS:
        return errors.fail(
            "E_TOO_LARGE",
            f"too many background jobs ({live}); kill one or wait "
            f"for it to finish")

    # A BUDGET ONLY IF ONE WAS ASKED FOR. `bash_background` has never had
    # a default budget, and giving it one would kill the forty-minute
    # suites that work today -- a safety feature that breaks working
    # stories is not one. Declared, it is enforced in the pod by the
    # watchdog and reported as a TIMEOUT rather than a failure.
    #
    # REFUSED, NEVER CLAMPED, for the reason `limits` gives: a caller
    # that asked for three hours and silently got two reads the kill as
    # its command failing.
    budget = None
    if args.get("timeout") is not None:
        try:
            budget = limits.job_budget({"budget_s": args.get("timeout")})
        except limits.BadTimeout as bad:
            return errors.fail("E_BAD_ARGUMENT", str(bad))

    job_id = jobcore.new_id("job")
    refusal = jobcore.start(
        job_id, command,
        cwd=workspace if os.path.isdir(workspace) else None,
        env=safe_env, budget_s=budget, cap=_max_output(), lane=LANE,
        spill_in=workspace)
    if refusal:
        return errors.fail("E_TRANSIENT", refusal)

    # SAYS WHETHER IT IS BOUNDED, because "it will be stopped after 600s"
    # and "it will run until it finishes" are different things to plan
    # around, and the model cannot tell which it got from a job id.
    bound = (f" It will be stopped after {budget}s and reported as a "
             f"TIMEOUT." if budget is not None
             else " It has NO time limit — pass `timeout` to bound it.")
    # THE WHOLE LOG IS ON DISK, and saying so is the point. The window a
    # poll returns is small because it is context; the file is where a
    # forty-minute build's real failure lives, thousands of lines before
    # the end. A model that does not know the file exists will poll for
    # bytes that were already dropped.
    snap = jobcore.snapshot(job_id)
    spill = ""
    if snap and snap.get("spill_path"):
        spill = (f" Everything it writes also goes to "
                 f"{os.path.relpath(snap['spill_path'], workspace)}, which "
                 f"you can read and grep like any other file.")
    return errors.ok(
        f"started {job_id}: {command}.{bound}{spill}\n"
        f"Wait for it in one step with bash_wait, look without waiting "
        f"with bash_output, list what is running with bash_jobs, stop it "
        f"with bash_kill.",
        # THE FIRST MIRROR WRITE. Without it a job that the model starts
        # and never polls is invisible to anyone asking how the story is
        # going -- which is the case a long job is most likely to be in.
        job=_mirror_facts(snap) if snap else None)


# ONE SLICE OF A WAIT, and the number is a bridge constraint rather than
# a policy. The sandbox blocks here while the worker holds an HTTP read
# open; block for an hour and the read times out and the model is told
# the bridge is dead. So the sandbox waits a bounded slice and says
# whether it is done, and the WORKER re-issues until it is -- which is
# what keeps a two-hour wait to one model step.
WAIT_SLICE_S = 20.0

# THE SLICE HAS A HARD CEILING AND IT IS NOT A MATTER OF TASTE.
# `bridge_timeout("bash_wait")` returns `limits.WAIT_BRIDGE_S`, and that
# comment says why: it "has to out-wait that slice or every wait comes
# back as a dead bridge". A slice longer than the worker's HTTP read
# does not make the model wait longer -- it makes every single slice
# time out, and the worker's reachability check is what then destroys a
# pod that was only waiting. So a setting is clamped here rather than
# obeyed, and the clamp leaves room for the answer to travel.
_SLICE_MARGIN_S = 5.0


def wait_slice_s() -> float:
    """One slice, resolved AT CALL TIME and bounded by the bridge.

    A FUNCTION AND NOT A CONSTANT, for the reason `jobs_client._knob`
    states: a module constant is bound when the module is imported, and
    rebinding it after reading a setting changes nothing that already
    captured it. This used to be the constant above, so
    `AGENTOS_WAIT_SLICE_S` reached `jobs_client` -- the HARNESS path --
    and never reached the model's `bash_wait` at all.
    """
    ceiling = max(1.0, float(limits.WAIT_BRIDGE_S) - _SLICE_MARGIN_S)
    if _SLICE_ENV not in os.environ:
        return min(WAIT_SLICE_S, ceiling)
    try:
        asked = float(os.environ.get(_SLICE_ENV) or WAIT_SLICE_S)
    except (TypeError, ValueError):
        return min(WAIT_SLICE_S, ceiling)
    return min(max(0.1, asked), ceiling)

# How often this loop looks. Local to the sandbox -- jobcore is in this
# process -- so it is cheap, and the only cost of a short interval is
# how quickly a finished job is noticed.
WAIT_TICK_S = 0.25


def tool_bash_wait(args: dict) -> dict:
    """Wait for a background job. ONE step, however long it takes.

    THE ALTERNATIVE IS WHY THIS EXISTS. Polling a job in a loop costs one
    model step per poll, one journal entry per poll, and a round trip of
    tokens per poll -- and every `bash_output` on one job signs the same
    stall signature unless the cursor moves, so a twelve-minute build
    could be watched seven times before the leg died `stalled` on the
    eighth. A forty-minute suite is not watchable that way at all.

    So the model calls this once. Underneath, the sandbox waits a slice
    and the worker re-issues; neither the slices nor the re-issues are
    steps, and the model sees one call that returns when the job is done.

    RETURNS THE SAME PROSE `bash_output` DOES when the job ends, because
    the thing the model wanted was the result -- making it call
    `bash_output` afterwards to find out what happened would put the
    extra step back.
    """
    job_id = args.get("job_id", "")
    raw_since = args.get("since")
    since = raw_since if isinstance(raw_since, int) and not isinstance(
        raw_since, bool) else 0

    if jobcore.snapshot(job_id) is None:
        known = ", ".join(jobcore.known(LANE)) or "none"
        return errors.fail("E_BAD_ARGUMENT",
                           f"unknown job {job_id!r}; known: {known}")

    deadline = time.monotonic() + wait_slice_s()
    while time.monotonic() < deadline:
        snap = jobcore.snapshot(job_id)
        if snap is None or not snap["running"]:
            break
        time.sleep(WAIT_TICK_S)

    snap = jobcore.snapshot(job_id)
    if snap is not None and snap["running"]:
        # NOT AN ANSWER THE MODEL SEES. The worker reads this marker and
        # calls again; it reaches the model only if the worker's own
        # ceiling ran out first, and then it reads as what it is.
        body = tool_bash_output({"job_id": job_id, "since": since})
        body["output"] = ("[still running after the wait slice]" + chr(10)
                          + body["output"])
        body["wait_incomplete"] = True
        return body

    # Finished: hand back exactly what a poll would say, so the model
    # needs no second call to learn the outcome.
    return tool_bash_output({"job_id": job_id, "since": since})


def _mirror_facts(snap: dict) -> dict:
    """The job's state as DATA, beside the prose the model reads.

    THE WORKER CANNOT PARSE THE PROSE, and should not try: `bash_output`
    renders for a model -- an exit code inside a sentence, beside notes
    about dropped bytes -- and reading that programmatically is the
    fault shape that once reported a 45MB capture as "stored nothing".
    Two consumers, two formats, on one envelope.

    The worker mirrors this to the control panel. It cannot be done in
    here: the mirror speaks HTTP to the panel through `broker`, and this
    module is stdlib-only, loaded by bare name inside the sandbox.
    """
    return {
        "id": snap["id"], "command": snap["command"],
        "produced": snap["produced"], "running": snap["running"],
        "exit_code": snap["exit_code"], "ended": snap["ended"],
    }


def _spill_hint(snap: dict) -> str:
    """Where the bytes that fell out of the window still are.

    SAID AT THE MOMENT THEY ARE LOST, not only at start. A model reading
    "earlier output dropped" three legs after it started the job has no
    reason to remember a path mentioned once, and the natural response
    to dropped bytes -- poll harder -- cannot recover them.
    """
    path = (snap or {}).get("spill_path") or ""
    return f"; the whole log is in {path}" if path else ""


def _budget_note(snap: dict) -> str:
    """What a budget kill has to say to be actionable.

    THREE FACTS, and the old message had none of them: how long it
    actually ran, that the number was the caller's own, and how much
    room there is above it. Without the third a model cannot tell
    whether asking for more is even allowed, and the safe-looking move
    is to ask for less -- which is what run 262 did, twice.
    """
    asked = (snap or {}).get("budget_s")
    started, finished = snap.get("started_at"), snap.get("finished_at")
    ran = (f"{finished - started:.0f}s"
           if started and finished else "its full budget")
    ceiling = limits.job_max_s()
    if not asked:
        return (f"[stopped after {ran} because it reached its budget. This "
                f"is a TIMEOUT, not a failing command]")
    room = ("" if asked >= ceiling else
            f" You may ask for up to {ceiling}s: pass a larger `timeout` "
            f"to bash_background, or declare `timeout_s` on the check.")
    return (f"[stopped after {ran}: it reached the {asked}s budget THIS "
            f"CALL ASKED FOR. This is a TIMEOUT, not a failing command, "
            f"and the work was not finished.{room}]")


def tool_bash_output(args: dict) -> dict:
    """Read a background job's output, optionally only what is new.

    `since` IS WHAT MAKES WATCHING A BUILD AFFORDABLE, and it is also what
    stops the stall clock killing the watcher.

    Without it, every poll re-sent the whole window — the same bytes, into
    the context, once per poll — and every poll of one job was a call with
    identical arguments. The progress clock signs a step by tool and
    operand, so twenty polls of one job were twenty repeats of one
    signature and the leg died `stalled` on the eighth. The tool that
    exists to watch a twelve-minute build could be used seven times.

    With it, a poll asks a different question each time (`since` advances),
    gets only what is new, and repeating one question twenty times is
    genuinely a stall — which is the clock working rather than misfiring.
    """
    job_id = args.get("job_id", "")
    raw_since = args.get("since")
    since = raw_since if isinstance(raw_since, int) and not isinstance(
        raw_since, bool) else 0
    since = max(0, since)

    snap = jobcore.snapshot(job_id, since=since)
    if snap is None:
        known = ", ".join(jobcore.known(LANE)) or "none"
        return errors.fail(
            "E_BAD_ARGUMENT",
            f"unknown job {job_id!r}; known: {known}")

    produced = snap["produced"]
    kept_from = snap["kept_from"]
    # TEXT AT THE EDGE. The buffer is bytes so a cursor is a byte offset;
    # a UTF-8 sequence split by the window's drop decodes to a
    # replacement character rather than raising.
    slice_ = snap["chunk"].decode("utf-8", "replace")
    running = snap["running"]
    code = snap["exit_code"]
    window = _max_output()

    # ONLY A SUPPLIED CURSOR CAN BE OUTRUN. `since = 0` is "give me
    # everything you have", which on an already-truncated job is the
    # ordinary first poll, not a poller who fell behind — and saying it
    # was would be a confusing accusation on the very first call.
    missed = since > 0 and since < kept_from

    status = "running" if running else f"finished (exit {code})"
    head = f"[{job_id}] {status} · bytes {since}-{produced}"
    notes = []
    if missed:
        notes.append(
            f"[{kept_from - since} bytes between your cursor and the buffer "
            f"were dropped — earlier output dropped because the job outran "
            f"the {window // 1024} KB window; poll more often to keep up"
            + _spill_hint(snap) + "]")
    elif snap["dropped"]:
        # Says which END was dropped. "truncated at 32 KB" left the reader
        # to assume the tail was cut — which is the opposite of what
        # happens. A model told its output was cut short reads the last
        # line as the last thing that happened, rather than as the newest
        # of a longer run.
        notes.append(f"[earlier output dropped — showing the last "
                     f"{window // 1024} KB" + _spill_hint(snap) + "]")
    if not slice_:
        notes.append("[no new output since your last poll]"
                     if since else "[no output yet]")
    # WHY IT STOPPED, when that is not simply "it exited". A signal death
    # is indistinguishable between a budget and a cancel, and "your suite
    # ran out of time" is a different sentence from "your suite failed".
    if not running and snap["ended"] == "budget":
        # NAMES THE NUMBER AND THE CEILING, because "it reached its
        # budget" alone left the model no way to revise the one figure it
        # most needs to. Run 262: a 1200s job was killed 42% through a
        # suite, and the very next attempt asked for 300 -- a quarter of
        # what had already been proved insufficient. The start message
        # says "it will be stopped after Ns"; nothing said anything at
        # the other end, so the guess had no feedback to correct it.
        # Two facts turn it into arithmetic.
        notes.append(_budget_note(snap))
    elif not running and snap["ended"] == "cancel":
        notes.append("[stopped because it was cancelled, not because it "
                     "failed]")
    # ALWAYS SAYS THE NEXT CURSOR, so following the protocol needs no
    # arithmetic from the model — the number to pass back is in the text.
    tail = (f"\nNext poll: bash_output(job_id={job_id!r}, since={produced})"
            if running else "")

    body = "\n".join([head] + notes + ([slice_] if slice_ else [])) + tail
    # exit_code is the TOOL's, not the job's: reading the output of a
    # failing job succeeded. The job's own code is in the text, so a model
    # cannot mistake "I polled it" for "it passed".
    #
    # `job` rides alongside for the WORKER, which mirrors it to the panel
    # so a person asking mid-run can be answered. The model reads the
    # prose; nothing parses it.
    return errors.ok(body, job=_mirror_facts(snap))


def tool_bash_jobs(args: dict) -> dict:
    """Every background job this run still has going.

    WHAT IT IS FOR: a leg boundary loses the model's memory of a job id,
    not the job. `kill_all` has no caller and the shim has no shutdown
    hook, so a job started in leg N is still running in leg N+1 -- the
    model simply no longer knows what to poll. Without this the only way
    back to a live job is a `bash_output` on an id the model no longer
    has, which answers with the list of known ids in an error. Asking
    for a list should not require causing an error first.

    LIVE ONLY. A finished job is in the registry for a while (the buffer
    is what a caller reads AFTER it exits) but it is not something to
    resume, and listing it would invite a poll that returns a cursor
    nobody needs.
    """
    # EVERY KNOWN JOB RIDES ALONG AS DATA, live or finished, and that is
    # wider than the prose on purpose.
    #
    # The mirror row in `kernel.execution_jobs` is written when a job
    # starts and again from whatever `bash_wait`/`bash_output` returns.
    # A job the model starts and then never waits on gets the first write
    # and no second one, so the row claims `running = true` for ever.
    # Measured on run 262: job-8e62ccf4 read as running for half an hour
    # after the sandbox had logged `exit_code=4 elapsed_s=0.9`. Harmless
    # to the run -- the sandbox is the authority and the model had its
    # result -- but every operator query reads the mirror, and a
    # diagnosis is only as good as the table it is made from.
    #
    # This is the natural place to reconcile: the worker mirrors what
    # comes back, and `bash_jobs` is already called at leg boundaries,
    # which is exactly when an abandoned job is most likely to exist.
    facts = [_mirror_facts(s) for s in
             (jobcore.snapshot(i) for i in jobcore.known(LANE))
             if s is not None]

    live = jobcore.running_jobs(LANE)
    if not live:
        return errors.ok("No background jobs are running.", jobs=facts)

    now = time.time()
    lines = [f"{len(live)} background job(s) running:"]
    for job in sorted(live, key=lambda j: j["started_at"]):
        age = int(now - job["started_at"])
        lines.append(
            f"  {job['id']} · {age}s · {job['produced']} bytes · "
            f"{job['command'][:100]}")
    lines.append("Poll one with bash_output(job_id=..., since=0).")
    return errors.ok("\n".join(lines), jobs=facts)


def tool_bash_kill(args: dict) -> dict:
    job_id = args.get("job_id", "")
    found, was_running = jobcore.cancel(job_id, "cancel")
    if not found:
        return errors.fail("E_BAD_ARGUMENT", f"unknown job {job_id!r}")
    if not was_running:
        snap = jobcore.snapshot(job_id)
        code = snap["exit_code"] if snap else None
        return {"output": f"{job_id} had already finished (exit {code})",
                "error": None, "exit_code": 0}
    return errors.ok(f"killed {job_id}")


def kill_all() -> int:
    """Stop every live job in the model's lane.

    A job outliving its sandbox would keep writing into a workspace the
    next leg has already started editing. Still has no caller: the shim
    has no shutdown hook, which is WHY a job survives a leg boundary and
    is the property `bash_jobs` exists to make visible rather than fix.
    """
    return jobcore.kill_all(LANE)
