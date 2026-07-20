"""Deliver review reports to a goal's dedicated review channel.

Each producing council gets its own human-observable channel, kept separate
from the Orchestrator Channel (Agent Mesh, GO<->Assistant coordination):

  - "Goal Review Council" reports (story review, goal review, and convener-
    failure packets) land in `goals.council_thread_id`.
  - "Security Council" reports land in `goals.security_thread_id`.
  - Any other sender falls back to `goals.orchestrator_thread_id` (Agent
    Mesh), preserving this function's original behavior.

In every case the GO is woken separately with a compact marker (see
`app/services/go_events._send_system_message_to_go`), while the full report
is persisted here for humans to read. This is the single place a review
report is persisted for humans.
"""
from __future__ import annotations

import json
import logging
import uuid

from app.database import get_pool

log = logging.getLogger(__name__)

# sender -> (goals column holding the destination thread id, broadcaster module, broadcaster fn name)
_DESTINATIONS_BY_SENDER = {
    "Goal Review Council": ("council_thread_id", "app.routers.council_thread", "_broadcast_to_council"),
    "Security Council": ("security_thread_id", "app.routers.security_thread", "_broadcast_to_security"),
}
_DEFAULT_DESTINATION = ("orchestrator_thread_id", "app.routers.orchestrator", "_broadcast_to_orchestrator")


async def persist_review_to_orchestrator(
    goal_id: str,
    subtype: str,
    title: str,
    body_markdown: str,
    sender: str,
) -> bool:
    """Write a review card into the goal's council/security/orchestrator
    channel (chosen by `sender`) and push it to open browser subscribers of
    that channel + external channels (Slack/Notion).

    Args:
        goal_id:       the goal whose channel receives the card.
        subtype:       'story' | 'goal' | 'security' — drives the card badge.
        title:         one-line card header (e.g. "Goal Review Council").
        body_markdown: the full report body (also stored as `content` fallback).
        sender:        attribution label (e.g. "Goal Review Council"); also
                        selects the destination channel via
                        `_DESTINATIONS_BY_SENDER`.

    Returns False if the goal has no destination thread (nothing persisted).
    """
    column, broadcaster_module, broadcaster_fn_name = _DESTINATIONS_BY_SENDER.get(
        sender, _DEFAULT_DESTINATION,
    )

    pool = get_pool()
    goal = await pool.fetchrow(
        f"SELECT {column} FROM goals WHERE id=$1", goal_id,
    )
    if not goal or not goal[column]:
        log.warning("review-delivery: goal %s has no %s; review not persisted", goal_id, column)
        return False
    dest_thread_id = goal[column]

    card = {"type": "review", "subtype": subtype, "title": title,
            "body": body_markdown, "producer": sender}
    msg_id = str(uuid.uuid4())

    # role='human' + a literal sender label matches how orchestrator posts are
    # attributed (the messages.role CHECK allows only 'human'|'agent'). `kind` is
    # left at its default ('user') — identical to the SHCA→orchestrator posts in
    # internal_orchestrator_message — so review rows aren't rejected by the
    # messages_kind_chk CHECK and need no migration. The UI identifies the card
    # by card_data.type=='review', not by kind. card_data is JSONB — pass the
    # JSON string and cast explicitly.
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count, card_data)
           VALUES ($1, $2, $3, 'human', $4, 0, $5::jsonb)""",
        msg_id, dest_thread_id, sender, body_markdown, json.dumps(card),
    )

    # Real-time push to open browser subscribers of the destination channel.
    try:
        import importlib
        broadcaster = getattr(importlib.import_module(broadcaster_module), broadcaster_fn_name)
        await broadcaster(goal_id, json.dumps({
            "type": "message",
            "message": {
                "id": msg_id, "sender": sender, "role": "human",
                "content": body_markdown, "card_data": card,
            },
        }))
    except Exception as exc:
        log.debug("review-delivery: web broadcast failed for %s (%s): %s", goal_id, column, exc)

    # Mirror to the orchestrator Slack channel (if Slack enabled for this goal).
    formatted = f"[{sender}] {title}\n{body_markdown}"
    try:
        from app.channels.slack.outbound import post_to_orchestrator_channel
        await post_to_orchestrator_channel(goal_id, formatted, username=sender)
    except ImportError:
        pass
    except Exception as exc:
        log.debug("review-delivery: Slack orch mirror failed for %s: %s", goal_id, exc)

    # Mirror to the orchestrator Notion feed (if Notion enabled for this goal).
    try:
        from app.channels.notion.outbound import relay_orchestrator_message
        await relay_orchestrator_message(goal_id, formatted)
    except ImportError:
        pass
    except Exception as exc:
        log.debug("review-delivery: Notion orch mirror failed for %s: %s", goal_id, exc)

    return True


async def persist_system_notice_to_orchestrator(goal_id: str, message: str) -> bool:
    """Cross-post a GO system notice (task/story/member state transition,
    from `app.services.go_events._send_system_message_to_go`) into Agent Mesh
    (`orchestrator_thread_id`) so the team sees it without opening the goal
    thread — which only renders the GO's own conversational turns, not the
    system notices that wake or inform it.

    Returns False if the goal has no orchestrator_thread_id, or if persisting
    fails outright (both logged at error level so a lost notice is never
    silent) — never raises, since the caller (go_events) must still be free
    to deliver the message to go_thread_id regardless of this mirror's outcome.
    """
    pool = get_pool()
    try:
        goal = await pool.fetchrow(
            "SELECT orchestrator_thread_id FROM goals WHERE id=$1", goal_id,
        )
        if not goal or not goal["orchestrator_thread_id"]:
            log.warning(
                "system-notice mirror: goal %s has no orchestrator_thread_id; "
                "not persisted to Agent Mesh (still delivered to the GO's own thread)",
                goal_id,
            )
            return False
        dest_thread_id = goal["orchestrator_thread_id"]

        msg_id = str(uuid.uuid4())
        await pool.execute(
            """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
               VALUES ($1, $2, 'system', 'human', $3, 0)""",
            msg_id, dest_thread_id, message,
        )
    except Exception as exc:
        log.error(
            "system-notice mirror: FAILED to persist to Agent Mesh for goal %s — "
            "notice lost from Agent Mesh (still delivered to the GO's own thread): %s | %s",
            goal_id, message[:200], exc,
        )
        return False

    try:
        from app.routers.orchestrator import _broadcast_to_orchestrator
        await _broadcast_to_orchestrator(goal_id, json.dumps({
            "type": "message",
            "message": {"id": msg_id, "sender": "system", "role": "human", "content": message},
        }))
    except Exception as exc:
        log.debug("system-notice mirror: web broadcast failed for %s: %s", goal_id, exc)

    try:
        from app.channels.slack.outbound import post_to_orchestrator_channel
        await post_to_orchestrator_channel(goal_id, message, username="system")
    except ImportError:
        pass
    except Exception as exc:
        log.debug("system-notice mirror: Slack orch mirror failed for %s: %s", goal_id, exc)

    try:
        from app.channels.notion.outbound import relay_orchestrator_message
        await relay_orchestrator_message(goal_id, message)
    except ImportError:
        pass
    except Exception as exc:
        log.debug("system-notice mirror: Notion orch mirror failed for %s: %s", goal_id, exc)

    return True
