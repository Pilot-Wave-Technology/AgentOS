"""Which rung of the ladder a run gets, and who said so.

THE SHAPE IS `kernel/harness/models/choice.py`, deliberately and almost
line for line. That module solves the identical problem for the
executor's model -- a per-story choice that inherits from the goal and
is frozen onto the run at dispatch -- and improvising a second shape
beside it would mean two answers to "how does a person configure one
story" that drift the first time either grows a case.

THE PRECEDENCE, widest to narrowest:

    contract -> story -> goal -> profile -> small

A CONTRACT OUTRANKS EVERYTHING because it knows about the particular
repository: a monorepo whose suite needs 8 GiB is a fact about that
repo, not about stories. It is resolved separately, in
`contract_config`, and this module is what fills the gap below it --
including for a story's FIRST dispatch, which has no contract at all
because its plan is not approved yet. That gap is why a goal-level
setting appeared to do nothing.

None MEANS "NOBODY CHOSE", and that is a different fact from `small`.
A profile naming its own size must still beat a default nobody picked
and must still lose to a story somebody did pick; a function returning
`"small"` for both collapses that and makes it undecidable at the call
site. The `executor_model` version of this function had to be split in
two for exactly this reason.

VALIDATED HERE, AT THE EDGE. An unknown word is refused with the ladder
named rather than silently rounded down -- a story that quietly ran
`small` when somebody asked for `large` is a story that looks merely
slow. `kernel/sizes.py` owns the rung names, so this asks it rather than
carrying a second list.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Spelled out per table rather than interpolated into one f-string, so
# `grep "FROM stories"` finds this reader. The two differ only in the
# table; keeping them apart costs a line and keeps the module greppable.
# Written out whole, not concatenated: a split string is just as
# invisible to grep as an f-string was.
_STORY_SIZE_SQL = (
    "SELECT NULLIF(btrim(coalesce(sandbox_size, '')), '') "
    "FROM stories WHERE id = $1")
_GOAL_SIZE_SQL = (
    "SELECT NULLIF(btrim(coalesce(sandbox_size, '')), '') "
    "FROM goals WHERE id = $1")


def valid(size: str | None) -> str | None:
    """The rung name, normalised, or None. Raises on an unknown word."""
    name = (size or "").strip().lower()
    if not name:
        return None
    from kernel.sizes import SIZES

    if name not in SIZES:
        raise ValueError(
            f"{name!r} is not a machine size. The ladder is: "
            f"{', '.join(sorted(SIZES))}.")
    return name


async def chosen_for_story(conn, story_id, goal_id) -> str | None:
    """A person's choice for this story, or None. NO default.

    `story_id` may be None -- an errand has a goal and no story, and
    inherits the goal's choice, which is what a person would expect from
    a setting described as "this goal's work".

    NEVER RAISES ON A BAD STORED VALUE. Validation belongs at the write,
    and a column that somehow holds a retired rung -- `xlarge` was a real
    rung once and was removed -- must not stop the run dispatching. It
    logs and falls through to the profile, which is where it would have
    been without the setting.
    """
    for scope, sql, key in (("story", _STORY_SIZE_SQL, story_id),
                            ("goal", _GOAL_SIZE_SQL, goal_id)):
        if not key:
            continue
        got = await conn.fetchval(sql, key)
        if not got:
            continue
        try:
            return valid(str(got))
        except ValueError:
            logger.warning(
                "%s %s asks for machine size %r, which is not on the "
                "ladder; the profile's size applies", scope, key, got)
            return None
    return None


async def freeze(conn, execution_id: int, size: str | None) -> None:
    """Record the rung this run was dispatched at, once, at dispatch.

    `WHERE sandbox_size IS NULL` so a second dispatch pass cannot rewrite
    a running story's machine. Matching nothing is not an error -- it
    means somebody got here first, which is the outcome the guard exists
    to produce.

    A None WRITES NOTHING, for the same reason `choice.freeze` does not:
    the column means "this run was pinned to a rung", and freezing a
    resolved default would make every run look deliberately configured
    and would override a profile's own size with a default nobody asked
    for.

    ON THE STORY, NOT THE EXECUTION. `executions` has no size column and
    does not need one: the story is what a person edits and what the
    next run of it inherits, and pinning it there is what makes "once
    dispatched, it cannot be changed" true of the thing they are
    looking at. A run with no story -- an errand -- pins nothing, and
    re-resolves from the goal next time, which is correct because the
    goal is the only thing it could have inherited from.
    """
    if not size:
        return
    story_id = await conn.fetchval(
        "SELECT story_id FROM executions WHERE id = $1", execution_id)
    if not story_id:
        return
    await conn.execute(
        "UPDATE stories SET sandbox_size = $2 "
        " WHERE id = $1 AND sandbox_size IS NULL", story_id, size)
