"""Running one of the story's own checks, with the budget it declared.

WHY THIS IS NEW AND NOT AN OBVIOUS GAP. A command criterion has never
been runnable properly from inside a leg, and `selfcheck.py` says why in
its own words: it ran "as a foreground `bash` pinned to a 120s ceiling,
which meant a suite slower than two minutes could never pass; and
running it uncapped would repeat, in the executor's own sandbox, a suite
the executor had just run as its work." So command criteria are reported
`unmeasured` and a person reads the counts.

WHAT CHANGED IS THE FIRST HALF OF THAT SENTENCE. A job carries the
criterion's own `timeout_s`, so the 120s cliff is gone -- a forty-minute
suite can now be run by the thing that declared it needs forty minutes.
This does not turn verification on, and it does not re-run anything: it
gives the executor a way to run ITS OWN declared check once, under the
budget the contract already states, and to have that run recorded.

AND IT RUNS ONCE. The executor usually runs its suite while working and
is then asked for the same suite as a check, in the same cycle, with
nothing changed in between -- forty minutes twice for one measurement.
A7's fingerprint covers uncommitted work, so "nothing changed" is safe
to believe, and a matching earlier run is reported with the job id it
came from rather than re-run. `kernel/harness/claim.py` holds the key.

THE BUDGET IS THE CONTRACT'S, NOT THE MODEL'S. `budget.timeout_for`
resolves it from the criterion's spec and then the contract, with a
default -- the same resolution the Verifier uses. A model that could
choose the budget for the check that judges it would be setting its own
measurement, which is the argument `verifier/budget` makes about
contracts generally.

`checks_for` IS THE READER, AND THIS USED TO HAVE ITS OWN. The first
version of this file queried `contracts.criteria` and
`contracts.verify_timeout_s`, and neither exists -- criteria are ROWS in
`kernel.criteria`, keyed by `contract_id`. Execution 253 called the tool
and got `UndefinedColumnError: column c.criteria does not exist`.
`selfcheck.checks_for` had been reading them correctly all along,
excluding held-out ones and taking the criteria rather than the plan
payload "where a human edited a check at approval". One reader.

A LABEL IS NOT A NAME, which is the other thing 253 showed: both of its
criteria are labelled `goal`. The label says whether a check must fail
now or hold now -- it is the KIND of promise, and the prompt tells the
model to use exactly those two words, so it cannot identify anything. So
this matches on the COMMAND, runs the only check when there is only one,
and lists the choices rather than guessing when there are several.
Running the wrong forty-minute suite costs more than asking.

IT RUNS IN THE SANDBOX, through the ordinary job machinery, because that
is where the tree is and where the setups put the PATH. What is
worker-side is only deciding WHICH command and WHICH budget -- both of
which come from the database, which the sandbox cannot read.
"""

from __future__ import annotations

import logging

from kernel.bridge import jobspill
from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)


def _menu(checks: list) -> str:
    """The command checks, numbered, for a refusal the model can act on."""
    return "\n".join(
        f"  {i}. [{c.get('label') or '?'}] {c['run']}"
        for i, c in enumerate(checks, 1))


def _pick(checks: list, which: str):
    """`(chosen, refusal)` -- one command check, or why it is not one.

    `which` matches a 1-based position or a case-insensitive substring
    of the command.
    """
    if not checks:
        return None, ("this plan declares no command checks. An artifact "
                      "check is measured at the claim, from the harness's "
                      "own record of what you emitted -- there is nothing "
                      "to run.")

    wanted = (which or "").strip()
    if not wanted:
        if len(checks) == 1:
            return checks[0], ""
        return None, ("this plan declares more than one command check, so "
                      "name one -- by its number here, or by any "
                      "distinctive part of its command:\n" + _menu(checks))

    if wanted.isdigit():
        index = int(wanted)
        if 1 <= index <= len(checks):
            return checks[index - 1], ""
        return None, (f"there is no check {index}. This plan has "
                      f"{len(checks)}:\n" + _menu(checks))

    hits = [c for c in checks if wanted.lower() in c["run"].lower()]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, (f"no check's command contains {wanted!r}. This plan "
                      f"has:\n" + _menu(checks))
    return None, (f"{wanted!r} matches {len(hits)} checks. Name one more "
                  f"exactly, or use its number:\n" + _menu(hits))


def _path_note(command: str, declared: str) -> str:
    """Whether this mount's own tools are on PATH, said either way.

    THE EMPTY CASE IS THE ONE WORTH SAYING. A check that quietly got the
    right PATH teaches nothing; a check that quietly got the image's
    PATH is the defect above, and it looked identical to working for two
    runs. So the note is always present and names which happened.
    """
    if command == declared:
        return ("No in-tree bin directory is known for this mount, so the "
                "check runs on the image's PATH. If it fails on a missing "
                "dependency that is why -- run env_setup, or commit a "
                "manifest or an .agentos/env.")
    prefix = command[:len(command) - len(declared)].strip().rstrip(";")
    return f"This mount's own tools go first: {prefix}"


async def run_check(ex, conn, tool_args: dict, tool_call_id: str) -> ToolResult:
    """Run one declared check as a job, under its declared budget."""
    execution_id = getattr(ex._envelope, "execution_id", None)
    if execution_id is None:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="run_check: no execution on the envelope.",
                          exit_code=1)

    from kernel.harness.selfcheck import checks_for

    # COMMAND CHECKS ONLY. `checks_for` returns every criterion carrying
    # a `run`, and `kind` travels with the row precisely because dropping
    # it "sent an artifact check to bash" once already.
    declared = [c for c in await checks_for(conn, int(execution_id))
                if (c.get("kind") or "command") == "command"]

    which = str(tool_args.get("which") or tool_args.get("label") or "").strip()
    criterion, refusal = _pick(declared, which)
    if refusal:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error=f"run_check: {refusal}", exit_code=1)

    # `stated` is what the contract says and what a person reads on a
    # card; `command` is what the shell is actually given. They differ by
    # the PATH prefix below, and the messages keep using `stated` so the
    # model is shown its own check rather than our plumbing.
    stated = criterion["run"]

    # THE SAME PATH THE MODEL'S OWN `bash` GETS, which this did not.
    #
    # `_call_inner` wraps every `bash` and its background form in
    # `with_mount_path`, so the mount's in-tree bin directories go on
    # PATH and bare `pytest` resolves to the project's interpreter --
    # `leg_env`'s whole docstring is about why that convention has to be
    # true before a check may rely on it. This function calls
    # `call_bridge` DIRECTLY, a few lines below, and so went around the
    # wrap: the one command in the system that is REQUIRED to use the
    # bare form was the one command that ran without the PATH for it.
    #
    # Measured on runs 256 and 262:
    #
    #   cd repos/<repo> && pytest   -> /usr/local/bin/pytest
    #                               -> ModuleNotFoundError: asyncpg, exit 4
    #   .venv/bin/pytest tests/...  -> 6 passed, same pod, seconds later
    #
    # so `run_check` had never once succeeded, and it reads as a broken
    # repository rather than a missing PATH entry.
    from kernel.worker.leg_env import with_mount_path

    command = with_mount_path(stated, ex._mounts_env)

    from kernel.verifier.budget import timeout_for

    # NO CONTRACT ROW IS READ, and both halves of that are deliberate.
    #
    # IT WOULD CONTRIBUTE NOTHING. `timeout_for` consults, in order, the
    # criterion's `spec.timeout_s` and then the contract's
    # `verify_timeout_s` -- and `kernel.contracts` has no such column.
    # Passing the real row and passing `{}` are the same answer.
    #
    # AND IT COST A PRIVILEGE THE LEG DOES NOT HOLD. Run 255 answered
    # `permission denied for table contracts`: `agentos_leg` is
    # deliberately privilege-free, which
    # `kernel_execution_pod_leg_role.sql` states in capitals, and a
    # useless query is the worst possible reason to widen it. The
    # criteria themselves arrive through `checks_for`, which reads what
    # the leg is already granted.
    budget = int(timeout_for({}, criterion))

    # A7 -- HAS THIS ALREADY RUN, OVER THIS TREE? The executor usually
    # runs its suite while working and is then asked for the same suite
    # as a check, in the same cycle, with nothing changed in between.
    # Forty minutes twice for one measurement is the defect; a
    # fingerprint that includes uncommitted work is what makes "nothing
    # changed" safe to believe.
    from kernel.harness import claim

    fingerprint = await claim.take(ex)
    previous = await claim.prior_run(conn, int(execution_id), command,
                                     fingerprint)
    if previous is not None:
        verdict = ("passed" if previous["exit_code"] == 0
                   else f"failed (exit {previous['exit_code']})")
        return ToolResult(
            tool_call_id=tool_call_id,
            output=(f"That check already ran against this exact tree and "
                    f"{verdict}.\n"
                    f"  {stated}\n"
                    f"Recorded as {previous['job_id']}; its output is in "
                    f"{jobspill.SPILL_DIR}/. Nothing about the workspace "
                    f"has changed since, committed or not, so running it "
                    f"again would measure the same thing. Edit anything "
                    f"and this stops applying."),
            exit_code=0)

    # THE ORDINARY JOB MACHINERY, deliberately. A second way to run a
    # command in the sandbox would be a second watchdog, a second kill
    # path and a second buffer -- the duplication the registry merge
    # existed to remove. This is `bash_background` with a budget the
    # model did not choose.
    from kernel.bridge.client import call_bridge

    extras: dict = {}
    started = await call_bridge(
        "bash_background", {"command": command, "timeout": budget},
        tool_call_id, port=ex._port, host=ex._host, timeout=30,
        extras=extras)
    if started.error:
        return started
    await ex._mirror_job(extras)

    job_id = (extras.get("job") or {}).get("id") or ""
    return ToolResult(
        tool_call_id=tool_call_id,
        output=(f"Running your {criterion.get('label') or 'declared'} check "
                f"as {job_id}, with the {budget}s budget the contract "
                f"declares for it.\n"
                f"  {stated}\n"
                f"{_path_note(command, stated)}\n"
                f"Wait for it with bash_wait(job_id={job_id!r}). The "
                f"result is YOUR measurement of your own check -- it is "
                f"not a verdict, and nothing else re-runs it."),
        exit_code=0)
