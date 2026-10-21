"""What happens when somebody presses a GO card's button in Slack.

THESE BUTTONS DID NOT EXIST, so neither did their handlers. The GO
thread's work -- approving a parked plan, deciding an errand proposal
-- was reachable from the panel and from nowhere else. An assistant
parked on a permission question at 11pm waited until somebody opened
a browser.

EVERY HANDLER DOES THE SAME FOUR THINGS, in this order, and the order
is the point:

  1. Resolve the Slack user to a control-panel uid. No uid, no act:
     an approval is a human act and has to name the human.
  2. Check that human is in the goal. The action_id carries the id it
     acts on, so without this a mapped Slack user could approve a plan
     in a goal they have no role on -- the same hole phase 1 closed on
     the other button families.
  3. Call the SAME service the web route calls. Not a reimplementation:
     `kernel_decide.decide` and the errand approve/decline carry the
     kill-switch checks, the ledger writes and the state machine, and a
     second copy of any of those is a second thing to get wrong.
  4. Tell the person what happened, ephemerally.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


async def handle_park(pool, *, question_id: int, value: str, user_uid: str,
                      text: str = "") -> str:
    """Approve / revise / decline a parked kernel question. Returns a reply."""
    goal_id = await pool.fetchval(
        "SELECT e.goal_id FROM kernel.questions q "
        "  JOIN kernel.executions e ON e.id = q.execution_id "
        " WHERE q.id = $1", question_id)
    if not goal_id:
        return "That question no longer exists."

    from app.auth import get_goal_role
    if await get_goal_role(user_uid, goal_id) is None:
        _log.warning("Slack park decide REFUSED: uid=%s goal=%s q=%s",
                     user_uid, goal_id, question_id)
        return "You don't have access to this goal."

    # The card's option values are the kernel's verdicts, with one
    # exception: a revise or a decline needs a sentence saying what
    # should change, and a button cannot carry one. Asking for it in the
    # channel beats refusing with a validation error the person cannot
    # act on from a button.
    verdict = (value or "").strip()
    if verdict in ("revise", "declined", "decline") and not text.strip():
        return ("Reply in this channel with what should change, and the "
                "assistant will pick it up — a revise needs a reason or it "
                "restarts the same plan.")

    display = await pool.fetchval(
        "SELECT display_name FROM users WHERE uid = $1", user_uid) or ""
    try:
        from app.services.kernel_decide import decide
        result = await decide(question_id=question_id, verdict=verdict,
                              text=text, human_uid=user_uid,
                              display_name=display)
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack park decide failed q=%s: %s", question_id, exc)
        return f"Couldn't record that: {exc}"

    # A durable trace. The reply below is ephemeral -- visible to the
    # presser, in Slack, never persisted -- so without this, somebody
    # reading the thread in the panel sees the card change with no record
    # of who decided it or where.
    from app.channels.slack.audit import record_decision, thread_for_question
    thread_id = await thread_for_question(pool, question_id)
    await record_decision(pool, thread_id=thread_id, user_uid=user_uid,
                          action=verdict.capitalize(), detail=text)
    return f"Recorded: {result.get('verdict', verdict)}."


async def handle_errand(pool, *, proposal_id: int, value: str,
                        user_uid: str, reason: str = "") -> str:
    """Approve or decline an errand proposal. Returns a reply."""
    goal_id = await pool.fetchval(
        "SELECT goal_id FROM kernel.errand_proposals WHERE id = $1",
        proposal_id)
    if not goal_id:
        return "That proposal no longer exists."

    from app.auth import get_goal_role
    if await get_goal_role(user_uid, goal_id) is None:
        _log.warning("Slack errand decide REFUSED: uid=%s goal=%s p=%s",
                     user_uid, goal_id, proposal_id)
        return "You don't have access to this goal."

    verdict = (value or "").strip().lower()
    if verdict not in ("approve", "decline"):
        return "That button is not one I understand."

    from app.services.errand_grants import approve, decline

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if verdict == "approve":
                    await approve(conn, goal_id, proposal_id, user_uid)
                else:
                    await decline(conn, goal_id, proposal_id, user_uid, reason)
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("Slack errand decide failed p=%s: %s", proposal_id, exc)
        return f"Couldn't record that: {exc}"

    from app.channels.slack.audit import record_decision
    go_thread = await pool.fetchval(
        "SELECT go_thread_id FROM goals WHERE id = $1", goal_id)
    await record_decision(
        pool, thread_id=go_thread or "", user_uid=user_uid,
        action=("Errand approved" if verdict == "approve"
                else "Errand declined"),
        detail=reason)
    return ("Approved — the errand will run and you own it."
            if verdict == "approve" else "Declined.")
