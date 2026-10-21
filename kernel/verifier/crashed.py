"""The adjudicator itself failing — recorded, then bounded.

Separated from `adjudicator.py` because it is not part of judging a
claim: it is what happens when judging one raises, which is a different
subject with a different bound. `adjudicate_pass` calls it from both its
paths and from nowhere else.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Consecutive adjudicator CRASHES before the run parks for a
# person. Three: enough that a genuine blip (a restart, a
# momentary database hiccup) clears itself, few enough that a
# real defect stops costing a verification every tick.
ADJUDICATOR_STRIKES = 3


async def _adjudicator_failed(conn, row: dict, exc: Exception) -> dict:
    """Record a crash, and stop looping once it is clearly not transient.

    The reason lands on stall_reason immediately -- the Executions board
    and the story's stall card both read it, so a stuck run explains
    itself on the first failure rather than the tenth. After three in a
    row the run PARKS on an escalation: a person decides, and a parked
    run is no longer re-adjudicated, which is what actually ends the
    loop. The count resets whenever a claim decides.
    """
    eid = row["id"]
    detail = f"{type(exc).__name__}: {exc}"
    strikes = await conn.fetchval(
        "UPDATE executions SET verify_error_count = verify_error_count + 1, "
        "stall_reason = $2, updated_at = now() "
        "WHERE id = $1 RETURNING verify_error_count",
        eid, f"verification crashed: {detail}"[:500]) or 1
    if strikes < ADJUDICATOR_STRIKES:
        return {"execution_id": eid, "action": "error",
                "detail": f"{detail} (attempt {strikes}/"
                          f"{ADJUDICATOR_STRIKES})"}

    # Three in a row is a defect, not a hiccup. Park it so it stops
    # burning a verification per tick and somebody sees it.
    from kernel.harness.park import open_request

    claim = row.get("claim_seq") or 0
    got = await open_request(
        conn, eid, row.get("generation") or 0, "escalation",
        {"question": "verification cannot complete -- the adjudicator "
                     "keeps failing on this claim. This is a platform "
                     "fault, not a judgement on the work.",
         "refusals": strikes,
         "checks": [detail],
         "attempts": [{"tool": "adjudicate", "detail": detail},
                      {"tool": "adjudicate", "detail": f"retry {strikes}"},
                      {"tool": "verify_claim", "detail": "crashed"}]})
    if isinstance(got, int):
        await conn.execute(
            "UPDATE executions SET verify_error_count = 0 WHERE id = $1",
            eid)
        logger.error("execution %s: adjudicator failed %s times -- parked "
                     "on escalation %s", eid, strikes, got)
        return {"execution_id": eid, "action": "adjudicator_escalated",
                "detail": f"{detail} -- parked for a person after "
                          f"{strikes} attempts"}
    # Could not park (a question is already open, say). The stall_reason
    # above still tells the story; do not spin the counter for ever.
    logger.error("execution %s: adjudicator failing (%s) and could not "
                 "park: %s", eid, detail, got)
    _ = claim
    return {"execution_id": eid, "action": "error",
            "detail": f"{detail} (unparked; see stall_reason)"}
