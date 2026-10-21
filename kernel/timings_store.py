"""Resolving a story's timings profile, and stamping it on a run.

TWO FUNCTIONS AND ONE RULE: resolve once, at dispatch, and never again.
`for_story` is the only thing that reads `story_timings`; everything
downstream reads the stamp on `kernel.executions`. That is what makes a
run carry the profile it started under, and it is why an expiry can be a
plain timestamp rather than a question every reader has to remember to
ask the same way.

The alternative -- each reader consulting the table live -- was tried in
spirit by `verification_switch`, which has two readers with two separate
SELECTs. It works for one boolean. For eleven numbers read from the pod,
the conductor, the harness and the panel it would be eleven chances to
write a slightly different predicate, and an expiry honoured in some
places and not others is worse than no expiry at all.

EXPIRED READS AS PRODUCTION, and so does absent, unreadable, and a
profile name nobody recognises. The direction `verification_switch`
chose: a settings fault must not change fleet behaviour at the moment it
can least explain itself.
"""

from __future__ import annotations

import json
import logging

from kernel import timings

logger = logging.getLogger(__name__)


async def for_story(conn, story_id: str | None) -> tuple[str, dict]:
    """`(profile, overrides)` for this story, right now.

    Never raises. A story with no row, an expired row, or a table this
    connection cannot read all answer `production`.
    """
    if not story_id:
        return timings.PRODUCTION, {}
    try:
        row = await conn.fetchrow(
            "SELECT profile, overrides FROM kernel.story_timings "
            "WHERE story_id = $1 AND expires_at > now()", story_id)
    except Exception:                                       # noqa: BLE001
        logger.exception("timings: could not read the profile for %s; "
                         "dispatching as production", story_id)
        return timings.PRODUCTION, {}
    if row is None:
        return timings.PRODUCTION, {}

    profile = row["profile"] or timings.PRODUCTION
    if profile not in timings.PROFILES:
        logger.warning("timings: story %s names an unknown profile %r; "
                       "dispatching as production", story_id, profile)
        return timings.PRODUCTION, {}

    raw = row["overrides"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return profile, (raw if isinstance(raw, dict) else {})


async def stamp(conn, execution_id: int, profile: str) -> None:
    """Record the profile on the run. Called once, at dispatch.

    Best-effort: a run that fails to record a COMPRESSED stamp reads as
    production afterwards, which is the safe direction -- the run itself
    already has its resolved numbers, and what is lost is the label on a
    card rather than the behaviour.
    """
    if profile == timings.PRODUCTION:
        return                       # NULL already means production
    try:
        await conn.execute(
            "UPDATE kernel.executions SET timings_profile = $2 WHERE id = $1",
            execution_id, profile)
    except Exception:                                       # noqa: BLE001
        logger.exception("timings: could not stamp execution %s", execution_id)


async def of_execution(conn, execution_id: int) -> str:
    """The profile this run was dispatched under. Never raises."""
    try:
        got = await conn.fetchval(
            "SELECT timings_profile FROM kernel.executions WHERE id = $1",
            execution_id)
    except Exception:                                       # noqa: BLE001
        logger.exception("timings: could not read the stamp on %s",
                         execution_id)
        return timings.PRODUCTION
    return got if got in timings.PROFILES else timings.PRODUCTION


async def values_for(conn, execution_id: int) -> dict:
    """Every knob's value for this run, from its stamp.

    The overrides are NOT re-read: they were folded into the sandbox env
    at dispatch, and a reader that re-read them could act on an override
    added after the run began.
    """
    return timings.values(await of_execution(conn, execution_id))
