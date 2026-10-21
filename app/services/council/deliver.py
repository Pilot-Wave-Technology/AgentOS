"""The packet that gets delivered when the convener didn't produce one.

EVERY GOAL REVIEW PRODUCES ITS FINDINGS. That is the requirement this
module exists to satisfy, and it is not the same as "the council usually
works". A cycle that delivers nothing is worse than a plain packet twice
over: the goal goes another twelve hours unreviewed, and the orchestrator
learns that a review is a thing which sometimes simply does not arrive --
after which the ones that do arrive carry less weight.

WHAT IT REPLACES. The old path posted a note saying the council "could
not synthesize a final review", listed which stages passed, and told
nobody to do anything. But the stages that DID run had already found
things; that note threw them away and reported the plumbing instead. The
ledger holds those findings, so a packet can be drawn from them in code
with no model involved -- plainer than a synthesised one, never absent.

AND IT WAKES THE GO TO REPLY, where the old note deliberately did not.
The old reasoning was sound for what it delivered: nothing actionable, so
do not demand an answer. This delivers open findings, which are
actionable, so it asks for one.
"""

from __future__ import annotations

import logging

from app.services.council import findings
from app.services.council.inputs import stage_output

logger = logging.getLogger(__name__)

TITLE = "Goal Review Council"
SENDER = "Goal Review Council"


async def deliver_packet(goal_id: str, packet: str, *,
                         title: str = TITLE, wake: bool = True) -> bool:
    """Orchestrator card for people, `[REVIEW:goal]` wake for the GO.

    TWO SURFACES, ON PURPOSE. The card is the durable human-facing copy
    and lives in the orchestrator channel so it does not clutter the main
    goal thread. The wake carries the full packet into the GO's context,
    where the `[REVIEW:goal]` marker triggers its review skill.

    Returns whether the human-facing card landed. The GO wake is
    best-effort: a missed wake costs one cycle's response, a missed card
    loses the review from the record.
    """
    card_ok = False
    try:
        from app.services.orchestrator_delivery import persist_review_to_orchestrator
        card_ok = await persist_review_to_orchestrator(
            goal_id, "goal", title, packet, SENDER)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("council: orchestrator persist failed for %s: %s",
                       goal_id, exc)

    try:
        from app.services.go_events import _send_system_message_to_go
        # mirror_to_mesh=False: the card above is already the formatted
        # copy. Mirroring would duplicate it, unformatted and
        # marker-prefixed, straight into Agent Mesh.
        await _send_system_message_to_go(
            goal_id, f"[REVIEW:goal] {title}\n\n{packet}",
            wake_up=wake, mirror_to_mesh=False)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("council: GO wake failed for %s: %s", goal_id, exc)

    return card_ok


async def deliver_ledger_packet(goal_id: str, run_id: str) -> bool:
    """Draw a packet from the ledger and deliver it. The floor.

    Called when the convener failed, never ran, or the driver crashed.
    Never raises: this IS the fallback, and a fallback that can fail the
    same way as the thing it backs up is not one.
    """
    try:
        open_items = await findings.store.open_findings(goal_id)
        failed = await stage_output.failed_stages(run_id)
        packet = findings.render.packet(goal_id, open_items, failed)
    except Exception:                                       # noqa: BLE001
        logger.exception("council: could not build ledger packet for %s", goal_id)
        return False

    title = f"{TITLE} — assembled by the harness"
    # WAKES THE GO EVEN THOUGH THE CONVENER FAILED, as long as there is
    # something open to act on. With an empty ledger there is nothing to
    # ask for, and demanding a reply to "nothing was found and the
    # reviewers failed" trains the GO to answer reviews with noise.
    return await deliver_packet(goal_id, packet, title=title,
                                wake=bool(open_items))
