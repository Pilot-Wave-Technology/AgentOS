"""How long a command may run, in one place on both sides of the bridge.

THE DEFECT THIS CLOSES. `_tool_bash` has read `args["timeout"]` since it was
written, capped at 120s — and `timeout` was never in `bash`'s schema, so no
model could set it and every foreground command died at the 30s default. The
120s the description advertised was unreachable. Exactly the shape already
fixed for `read`'s `offset`/`limit`: a parameter that is implemented,
documented and impossible to send.

AND THE HALF THAT WOULD HAVE MADE THE FIX A LIE. `call_bridge` uses a 30s
HTTP read timeout of its own. Putting `timeout` in the schema without
raising that would turn a 90-second command into `bridge unreachable:
timed out` at 30 seconds — the same failure, now with the model's own
argument apparently ignored, which is worse than not offering it.

That second timeout was already biting something. `env_install` shells
`apt-get update && apt-get install`, which routinely exceeds 30 seconds, and
its bridge call took the default. A slow install came back as a dead bridge.

E14 — AND WHY THE HARNESS NO LONGER HAS A CEILING AT ALL. `SETUP_MAX_S`
(600s, and 120s before that) lived here because the harness's own
install is not a model request. It was still a CLIFF: a monorepo whose
install exceeds the number comes back as `TimeoutError`, which reads as
a broken manifest or a dead network and is neither — that is how KER25
lost a story at the previous cliff. Raising the number moves the cliff
and keeps the real cost, which is a bridge call held open for the whole
install: one thread of `kernel/executors.py`'s tool pool, a `_TOOL_LOCK`
nothing else can have, and an install a restarted worker cannot find.

So the harness got the shape the MODEL has had since `bash_background`:
`job_start` → `job_poll(job, since)` → `job_cancel`. The budget is
enforced IN THE POD; no bridge call is ever long; a thread is held for a
poll and never for a run. `SETUP_MAX_S` and `SETUP_FLAG` retired with
the blocking path — the flag was always "not a security boundary… a
nudge towards the right tool", and the right tool now exists.

IMPORTED TWO WAYS, deliberately. The worker says
`from kernel.bridge.limits import ...`; the sandbox shim runs with
`kernel/bridge/` directly on `sys.path` and says `import limits`, the same
as it already does for `errors`. One file, so the sandbox's cap and the
worker's patience cannot drift apart — and if they drift, the shell hangs
up mid-command and the run is told the bridge died.
"""

from __future__ import annotations

import os

# What a command gets if it asks for nothing. Short on purpose: most calls
# are `ls`, `pytest -x` on one file, `git status`.
BASH_DEFAULT_S = 30

# The ceiling, and it is a REFUSAL rather than a clamp. Silently clamping
# 600 to 120 hands back `E_TIMEOUT: no output within 120s`, which reads as
# a slow command rather than an out-of-range request — so the model retries
# the same thing instead of using `bash_background`, which is the answer.
#
# UNCHANGED BY E14, deliberately. The nudge works; raising it would
# remove the nudge without removing the problem.
BASH_MAX_S = 120

# What the HARNESS gives a foreground call now that it has a job API:
# 30 seconds, the same as anything else with no timeout of its own. A
# `stat`, a marker `touch`, a `test -d` — none of these are long, and a
# short ceiling is the 2026-09-17 lesson: a call that can be slow will
# eventually be slow at the worst moment. Anything that can legitimately
# take minutes is a JOB, not a foreground call.
HARNESS_FOREGROUND_S = 30

# ── The harness's job API ────────────────────────────────────────────────
# Three tool names, handled OUTSIDE the shim's tool lock (see shim.py) so
# a poll never queues behind the model's foreground `bash`.
JOB_START = "job_start"
JOB_POLL = "job_poll"
JOB_CANCEL = "job_cancel"
JOB_TOOLS = (JOB_START, JOB_POLL, JOB_CANCEL)

# The budget a job gets when its caller declares none. Thirty minutes:
# a cold `pnpm install` on a monorepo is minutes rather than seconds and
# nobody is waiting on it — the poller returns between polls.
JOB_DEFAULT_S = 1800

# The ceiling on a DECLARED budget, and the number is not arbitrary: it
# is `kernel/verifier/budget.MAX_S`, the longest a criterion may ask for.
# A check against that budget through a 600s channel was twelve times
# over; a channel narrower than the budget it carries is the cliff again.
#
# RESTATED RATHER THAN IMPORTED, for the reason at the top of this file:
# this module is read inside the sandbox, where `kernel.verifier` does
# not exist. `test_long_tools.py` asserts the two are equal, so they
# cannot drift without a test going red.
JOB_MAX_S = 7200

# Below this a budget is a mistake — milliseconds written as seconds.
JOB_MIN_S = 1

# THE POLL ITSELF IS NEVER LONG. It returns what exists and never waits
# for more, so this is a bound on a handler that should answer in
# milliseconds, not a wait. A poll that is lost costs nothing: the job
# outlives its poller, and the next tick asks again by id — which is
# exactly what a blocking call could not do.
POLL_MAX_S = 10

# How much longer the worker waits than the sandbox. The sandbox kills the
# process at its own timeout and still has to serialise the output it
# captured; a margin of zero makes that race, and losing it looks like the
# bridge died rather than the command timing out.
BRIDGE_MARGIN_S = 15

# Anything with no timeout of its own. A read or a glob that takes this
# long has something else wrong with it.
BRIDGE_DEFAULT_S = 30


class BadTimeout(ValueError):
    """The requested timeout is not a number of seconds we will run for."""


def _whole_seconds(raw, what: str) -> int:
    """A whole number of seconds, or raise with something to correct.

    `bool` is an `int` in Python, and `True` would arrive as one second.
    A float is rejected rather than truncated: `1.5` silently becoming
    `1` is the clamp problem again, one order of magnitude down.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BadTimeout(
            f"{what} must be a whole number of seconds, got {raw!r}")
    return raw


def bash_timeout(args: dict) -> int:
    """Seconds for one `bash` call, or raise with something to correct.

    Rejects rather than coerces, in both directions. A string, a zero and a
    ten-minute request are three different mistakes and each gets its own
    sentence — a model that is told what is wrong fixes it on the next step,
    and one that is silently corrected repeats it.
    """
    raw = args.get("timeout")
    if raw is None:
        return BASH_DEFAULT_S
    seconds = _whole_seconds(raw, "timeout")
    if seconds < 1:
        raise BadTimeout(f"timeout must be at least 1 second, got {seconds}")
    if seconds > BASH_MAX_S:
        raise BadTimeout(
            f"timeout {seconds}s exceeds the {BASH_MAX_S}s ceiling for a "
            f"foreground command. Use bash_background and poll bash_output "
            f"— a long job you can watch beats a long call you cannot.")
    return seconds


def _from_env(name: str, fallback: int) -> int:
    """A timings knob the pod was dispatched with, or the standing value.

    THE SANDBOX CANNOT LOOK THESE UP. It is stdlib-only with no database
    and no DSN, so a per-story compressed profile reaches it as a literal
    placed in the container env at dispatch and nowhere else.

    One-argument `.get` behind an `in os.environ` test, because
    `check_no_environ_default` bans the two-argument form across
    `kernel/` -- and a bare `os.environ[name]` would raise on every
    production pod, which sets none of these.
    """
    if name not in os.environ:
        return fallback
    try:
        return max(1, int(os.environ.get(name) or fallback))
    except (TypeError, ValueError):
        return fallback


def job_default_s() -> int:
    return _from_env("AGENTOS_JOB_DEFAULT_S", JOB_DEFAULT_S)


def job_max_s() -> int:
    return _from_env("AGENTOS_JOB_MAX_S", JOB_MAX_S)


def job_budget(args: dict) -> int:
    """Seconds a harness job may run IN THE POD, or raise.

    A REFUSAL and not a clamp, for the same reason `bash_timeout` is one:
    a caller that asked for three hours and silently got two would read
    the kill as its command failing. `budget.timeout_for` clamps a
    CONTRACT's declaration and says so in a log, which is the right
    behaviour one layer up; by the time it reaches here somebody has
    already decided.
    """
    default_s, max_s = job_default_s(), job_max_s()
    raw = args.get("budget_s")
    if raw is None:
        return default_s
    seconds = _whole_seconds(raw, "budget_s")
    if seconds < JOB_MIN_S:
        raise BadTimeout(
            f"budget_s must be at least {JOB_MIN_S} second, got {seconds}")
    if seconds > max_s:
        raise BadTimeout(
            f"budget_s {seconds}s exceeds the {max_s}s ceiling for a "
            f"harness job, which is the longest a criterion may declare "
            f"(kernel/verifier/budget.MAX_S). Work that needs longer than "
            f"two hours needs a different shape, not a bigger number.")
    return seconds


# One `bash_wait` slice, plus room for the answer to come back. The
# sandbox bounds its own wait; this only has to out-wait that bound, and
# the LOOP that makes a two-hour wait one step lives in the worker.
WAIT_BRIDGE_S = 45


def bridge_timeout(tool: str, args: dict) -> int:
    """How long the WORKER waits on the bridge for this call.

    Always longer than whatever the sandbox will spend, so a command that
    times out reports `E_TIMEOUT` with its own duration rather than
    `bridge unreachable`.

    THE JOB OPS ARE ALL SHORT, which is the point of them: `job_start`
    spawns and returns, `job_poll` reads a buffer, `job_cancel` signals.
    None of them waits for the work, so none of them takes the work's
    budget — that number never appears on this side of the bridge.
    """
    if tool == JOB_POLL:
        return POLL_MAX_S
    if tool in JOB_TOOLS:
        return BRIDGE_DEFAULT_S
    # `bash_wait` BLOCKS IN THE SANDBOX for one bounded slice, so this
    # has to out-wait that slice or every wait comes back as a dead
    # bridge. It does NOT have to cover the whole job: the worker
    # re-issues, and that loop is what makes a two-hour wait one step.
    if tool == "bash_wait":
        return WAIT_BRIDGE_S
    if tool != "bash":
        return BRIDGE_DEFAULT_S
    try:
        return bash_timeout(args) + BRIDGE_MARGIN_S
    except BadTimeout:
        # The sandbox refuses it and says why. Waiting the default is
        # enough to carry that refusal back.
        return BRIDGE_DEFAULT_S
