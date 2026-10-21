"""Is verification switched on, platform-wide?

ONE SPELLING, and the session that produced this file is the argument
for it. Four separate capabilities in this system were off because
nobody had set an environment variable, and not one of them said so:

    AGENTOS_ASSIST       the assessor, the investigator, the answerer
                         and the scout -- five LLM features, dark since
                         their Deployments existed
    AGENTOS_HUMAN_DONE   nobody was ever asked to accept a story
    BUILD_SHA            every log line in the fleet claimed one build
    driver.name          an attribute no driver defines, behind which
                         three reapers returned [] on every tick

Each was invisible in exactly the same way: the feature's absence looks
identical to the feature having nothing to do. So this switch is a ROW
a person sets, not a variable a deployment forgets, and the product can
read back what it is.

OFF IS THE DEFAULT AND OFF IS THE FALLBACK. An unreadable switch means
verification does not run -- and that direction is deliberate rather
than convenient. Verification CREATES things: a claim, a pod, prepare
Jobs. Defaulting the other way would have an outage in the settings
table spend disk and pods across the fleet, at the moment the database
is least able to say why. A run that skips verification is still
self-checked against its own plan and still parked for a person; a run
that cannot be verified because nobody is watching the switch is not.

NOT CACHED, and deliberately. One indexed read of one row per claim --
`propose_completion` happens once per generation, not once per step --
against an operator lever whose whole value is that it takes effect on
the next tick. A cache here is how Freeze System came to be a button
that did nothing for the kernel (`conductor/pauses.py`).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

KEY = "verification_enabled"


async def enabled(conn) -> bool:
    """True only if a platform admin has switched verification on.

    Never raises. A switch that could fail a leg would be a worse
    problem than the one it solves.
    """
    try:
        got = await conn.fetchval(
            "SELECT value FROM platform_settings WHERE key = $1", KEY)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning(
            "could not read the verification switch (%s); treating it as "
            "OFF -- the run is self-checked and parked for a person "
            "rather than verified", exc)
        return False
    return bool(got)
