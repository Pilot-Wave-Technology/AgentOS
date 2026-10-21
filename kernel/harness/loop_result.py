"""The two shapes `run_leg` hands back, and the reason there are two.

EVERY CALLER READS THIS DICT AND NONE OF THEM SHARES A FILE WITH IT --
`kernel/worker/loop.py` advances the generation off `predicate`, the
console renders `final_text`, the card sweeper reads `parked`, and MAX_LEGS
counts the legs that came back `stopped`. So the shape is a contract
between six files, and it deserves to be written down in one.

IT IS NOW A WIRE FORMAT, and `loop_wire` is the envelope it travels in.
D2 moved the leg runner into the worker container, so this dict crosses
a process boundary on every pod-driven leg. Anything added here has to
survive `json.dumps` -- `parked` is flattened to three plain fields for
exactly that reason, rather than handing back the `ParkRequested`
exception it came from -- and anything that changes its SHAPE has to
bump `loop_wire.WIRE_VERSION`, because a pod dispatched before a
conductor rollout is still running after it.

THE REFUSAL IS THE SECOND SHAPE, AND IT IS NOT AN ERROR RETURN. A leg
whose snapshot preflight fails has written nothing, so `steps` is 0 and
`predicate` is None -- but `stopped` is True and there is a
`stop_reason` key the completed shape does not have. A caller that only
looks at `predicate` cannot tell that apart from a leg that ran cleanly
and stopped talking, which is why `stop_reason` exists.

WHY THE REFUSAL LIVES HERE RATHER THAN BESIDE THE PREFLIGHT. The rule it
carries is a property of the RETURN, not of the check: the caller makes
the state transition, and this file must not UPDATE `executions` on the
way out. The preflight failure may have left the connection in a failed
transaction state (ES2 surprise #3: Postgres transaction error state
lingers), so a tidy-looking "mark it FAILED before returning" would
raise a second, more confusing error on a connection that is already
poisoned.

Do not "simplify" the two shapes into one by giving the completed leg a
`stop_reason: None`. A key that is present and null reads as an answer;
its absence reads as a question that was never asked, and the callers
above distinguish them by `in`.
"""

from __future__ import annotations

import logging

from kernel.harness.snapshots import SnapshotPreflightError, snapshot_preflight

logger = logging.getLogger(__name__)


def refusal(stop_reason: str, budget_basis: str = "") -> dict:
    """The second shape, for any reason a leg must not start.

    EXTRACTED AT D2 RATHER THAN COPIED. The preflight was the only
    refusal while the leg ran wherever it was called from; now the POD
    can decline one too -- another process holds the lock, or the kill
    switch went on between the conductor's check and the pod's start
    (§6 D4). Those are refusals in exactly this sense: nothing was
    journalled, no generation was spent, and the caller must be able to
    tell that from a leg that ran and stopped talking.

    Spelling them by hand in three places is how the third one comes to
    omit `stop_reason` -- which is the ONE key that distinguishes the two
    shapes, and whose absence a caller reads as "this leg ran".
    """
    return {
        "steps": 0,
        "predicate": None,
        "budget_basis": budget_basis,
        "proposals": [],
        "stopped": True,
        "stop_reason": stop_reason,
    }


async def preflight_refusal(conn, execution_id: int,
                            budget_basis: str) -> dict | None:
    """The refusal shape if the leg must not start, else None."""
    try:
        await snapshot_preflight(conn, execution_id)
    except SnapshotPreflightError as e:
        logger.error("Snapshot preflight failed, refusing to start: %s", e)
        return refusal("preflight_failed", budget_basis)
    return None


def completed_leg(*, step_records: list[dict], firing_predicate,
                  budget_basis: str, proposals: list[dict],
                  total_spend: float, final_text, parked) -> dict:
    """The shape a leg that actually ran hands back.

    `predicate` is None and `stopped` False when the model simply stopped
    talking -- `loop_endings.MODEL_DONE` exists so that case never
    arrives here wearing a predicate it did not have.
    """
    return {
        "steps": len(step_records),
        "predicate": firing_predicate.predicate if firing_predicate else None,
        "budget_basis": budget_basis,
        "proposals": proposals,
        "stopped": firing_predicate is not None,
        "total_spend": total_spend,
        "final_text": final_text,
        "parked": None if parked is None else {
            "request_id": parked.request_id, "kind": parked.kind,
            "summary": parked.summary,
        },
    }
