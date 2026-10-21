"""Whose problem is a failed push?

TWO KINDS OF `publish failed`, AND ONLY ONE IS THE EXECUTOR'S.

    branch-shaped   non-fast-forward, protected branch, diverged. The
                    executor can see it, can fix it, and a leg spent
                    rebasing is a leg well spent.
    forge-shaped    "Repository not found", 403, authentication
                    refused. None of it is visible from inside the
                    workspace and none of it is the executor's to fix.

Both took the same path: bump `generation`, and tell the executor to
"check the branch state (diverged? protected?), resolve, and propose
completion again". On a forge refusal that instruction sends a model
looking for a cause that is not in the repository. Measured on dev
execution 3 -- generations 5, 6 and 7 spent on `git remote -v`,
`git config --list --show-origin`, `git ls-remote`, `git reflog` and
`git status`, against a 404 the token could not see past.

THE EXISTING BACKSTOP IS THE LEG CEILING, which
`kernel/workspace/podfs_client.py` records from an earlier run: it
"bounced on `publish failed` until it ran out of legs". Twelve legs,
then silence. That bounds the bill and answers nobody.

NOT ON THE FIRST FAILURE. `kernel/mailbox/schema.py` makes the
argument for the escalation floor -- one refusal "is the system
working and is not a question for anybody" -- and the dev failure that
prompted this file proves it: the push failed once, retried itself,
and delivered. Parking the first would have interrupted a run that
recovered on its own. So the first forge refusal is retried like any
other; the second parks.
"""

import logging

logger = logging.getLogger(__name__)

# How many forge refusals in a row before a person is asked. Two,
# matching the `escalation` evidence floor this park is raised under --
# the floor is not worked around, it is met.
PARK_AFTER = 2

# ── the classifier ───────────────────────────────────────────────────
#
# ON GIT'S OWN WORDS, because they are the only signal that survives
# the pod boundary: `publish_execution` catches `WorkspaceOpFailed` and
# returns git's message verbatim, having already decided that "a 403
# from the forge is a permission to grant, not a pod to go looking
# for".
#
# THE EXECUTOR LIST IS THE EXPLICIT ONE and everything else is a
# person's, which is the opposite of the safe-looking default. An
# unrecognised git error handed to the executor costs up to twelve
# legs of a model guessing; handed to a person it costs one card with
# a Retry button on it. The expensive mistake is the one worth
# defaulting away from.
_EXECUTOR_SHAPED = (
    "non-fast-forward",
    "fetch first",
    "protected branch",
    "behind its remote",
    "tip of your current branch is behind",
    "updates were rejected",
    "cannot lock ref",
    "failed to lock",
)


def blocker_for(detail: str) -> str:
    """'executor' when the run can fix it itself, otherwise 'person'."""
    text = (detail or "").lower()
    for marker in _EXECUTOR_SHAPED:
        if marker in text:
            return "executor"
    return "person"


def is_executors(detail: str) -> bool:
    return blocker_for(detail) == "executor"


# ── the park ─────────────────────────────────────────────────────────


async def note_forge_refusal(conn, execution_id: int) -> int:
    """Count this refusal. Returns the new consecutive count.

    `or 0` IS NOT COSMETIC. `RETURNING` yields nothing when the row is
    gone -- a cancelled or reaped run whose last leg is still finishing
    -- and the raw None then met `count >= PARK_AFTER` and raised
    TypeError, which took out the whole publish-failure path rather
    than one park. The existing publish tests caught it, with a fake
    connection that does not implement RETURNING; the production shape
    is rarer and would have been worse.

    Reading 0 degrades to EXACTLY the old behaviour -- tell the
    executor, spend the leg -- which is the right place to fall back
    to when the count cannot be established.
    """
    count = await conn.fetchval(
        "UPDATE executions SET publish_error_count = publish_error_count + 1 "
        "WHERE id = $1 RETURNING publish_error_count", execution_id)
    return int(count or 0)


async def clear_forge_refusals(conn, execution_id: int) -> None:
    """A delivered push ends the streak.

    Unconditional, and cheap: a run that never failed writes 0 over 0.
    Leaving it to only fire `IF publish_error_count > 0` would be one
    more branch for no saving, and the branch is where a reset gets
    forgotten.
    """
    await conn.execute(
        "UPDATE executions SET publish_error_count = 0 WHERE id = $1",
        execution_id)


async def park_on_forge_refusal(conn, execution_id: int, generation: int,
                                detail: str, count: int) -> int | None:
    """Ask a person and stop giving the run legs. Returns the question id.

    THE QUESTION IS NOT OPTIONAL. `claimable()` is what stops the work
    -- it selects `state = 'RUNNING'` and so a PARKED_HOT row is simply
    never offered another leg -- but nothing returns a parked run to
    RUNNING except an ANSWER. `harness/retry.py` clears `stall_reason`
    and the counters and deliberately does not touch `state`; it also
    refuses outright while a question is open, saying "answering it is
    what resumes the run". So parking without asking would stop the run
    for ever, with a Retry button that cannot start it again.

    `escalation` BECAUSE THE FLOOR IS MET, not worked around: it wants
    two refusals and the name of what keeps refusing, and by the time
    this is called there are two and the name is the push.
    """
    from kernel.mailbox.api import ask_question

    question = (
        "The work is committed but the forge refused the push, twice. "
        "This is not the branch and not something the run can fix from "
        "inside its workspace:\n\n" + (detail or "").strip()[:600] +
        "\n\nUsually a credential or a repository permission. Fix it and "
        "press Retry, or say what the run should do instead.")

    result = await ask_question(
        conn, execution_id, "escalation", question,
        {"question": question, "raised_by": "publish",
         "refusals": count, "checks": ["publish"]},
        generation=generation,
        content_hash=f"publish-{execution_id}-{count}")
    if not isinstance(result, int):
        # SAID OUT LOUD AND NOT PARKED. A park whose question was
        # refused is a run stopped with nothing to answer -- the exact
        # dead end this function's docstring describes. Better to leave
        # it RUNNING and let the leg ceiling bound it, as it did before.
        logger.warning("execution %s: publish park could not ask (%s); "
                       "leaving the run RUNNING",
                       execution_id, getattr(result, "code", result))
        return None

    await conn.execute(
        "UPDATE executions SET state = 'PARKED_HOT' WHERE id = $1 "
        "AND state IN ('RUNNING', 'VERIFYING')", execution_id)
    logger.warning("execution %s parked: the forge refused the push %d "
                   "times running", execution_id, count)
    return result
