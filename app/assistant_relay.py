"""Relay agent messages to the assistant thread that created the task.

All agent activity (progress, questions, completions, failures) is posted
to the assistant's source thread so the user sees everything in one place.
Messages include structured card_data for rich UI rendering.
"""

import json
import logging
import uuid

from app.database import get_pool
from app.notifications import push_notification
from app.openclaw import session_manager

logger = logging.getLogger(__name__)


async def post_card_to_assistant_thread(
    *,
    task: dict,
    card: dict,
    content_markdown: str,
    sender: str = "system",
    notify_type: str = "agent_update",
    notify_title: str = "",
) -> str | None:
    """Post a structured card message to the assistant thread that created the task.

    Returns the message ID if posted, None if no source thread found.

    Args:
        task: Full task dict (must have source_thread_id, goal_id)
        card: Structured card dict (from app.cards)
        content_markdown: Fallback text content (for non-card-aware clients)
        sender: Sender identifier (agent template_id or "system")
        notify_type: Notification type for push_notification
        notify_title: Notification title (empty = no notification)
    """
    pool = get_pool()

    source_thread_id = task.get("source_thread_id")
    if not source_thread_id:
        # Fall back to the assistant thread for the story this task belongs to
        story_id = task.get("story_id") or ""
        if story_id:
            source_thread_id = await pool.fetchval(
                "SELECT active_thread_id FROM stories WHERE id = $1 AND (active_thread_id IS NOT NULL AND active_thread_id != '')",
                story_id,
            )
    if not source_thread_id:
        logger.warning("Task %s has no assistant thread to post review card to", task["id"])
        return None

    msg_id = str(uuid.uuid4())

    # Insert message with card_data into the assistant thread.
    # card_data is JSONB — pass the JSON string and cast explicitly so
    # PostgreSQL stores it as a JSON object, not a quoted string literal.
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count, card_data)
           VALUES ($1, $2, $3, 'agent', $4, 0, $5::jsonb)""",
        msg_id, source_thread_id, sender, content_markdown, json.dumps(card),
    )

    # Cross-instance browser delivery via the durable fan-out: every web
    # instance LISTENs on chat_msg and re-broadcasts to its own subscribers.
    # Replaces the old local-only in-process broadcast, which silently dropped
    # the card whenever the viewer's WebSocket was on a different Cloud Run
    # instance than the one that created it (→ card only appeared on refresh).
    from app.chat_module.repository import notify_new_message
    await notify_new_message(source_thread_id, msg_id)

    # Forward to the assistant as input so it gets the FULL update
    # (not just the user, via the UI). The assistant can choose to respond or
    # [NO_REPLY]. Goes through the chat_module queue (when CHAT_QUEUE_ENABLED)
    # so it's durable, serialized with any concurrent user messages on the
    # thread, and reuses on_message. persisted_message_id reuses the row we
    # just INSERTed (no double-persist). queue_kind=system_wake triggers a
    # turn; the assistant decides reply vs NO_REPLY.
    try:
        thread_row = await pool.fetchrow(
            "SELECT goal_id, agent_id FROM threads WHERE id = $1", source_thread_id,
        )
        if thread_row:
            from app.services.agent_driver import ensure_shca_session
            session = await ensure_shca_session(
                thread_row["goal_id"], thread_row["agent_id"], source_thread_id,
            )
            if session is not None:
                await session.send_to_gateway(
                    json.dumps({
                        "type":      "message",
                        "content":   content_markdown,
                        "thread_id": source_thread_id,
                    }),
                    persisted_message_id=msg_id,
                    queue_kind="system_wake",
                )
    except Exception as exc:
        logger.warning(
            "Forward task card to assistant failed (task=%s msg=%s): %s",
            task.get("id", "?"), msg_id[:8], exc,
        )

    # Mirror to Slack story channel as Block Kit card (if mapped)
    try:
        from app.channels.slack.outbound import relay_card
        await relay_card(task.get("goal_id", ""), source_thread_id, card, content_markdown)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("Slack card relay failed for task %s: %s", task["id"], exc)

    # Mirror plain text to Notion assistant subpage (if mapped). Slack is excluded
    # here because it already received a Block Kit card above.
    try:
        from app.channels.notion.outbound import relay_agent_message as _notion_relay
        await _notion_relay(task.get("goal_id", ""), source_thread_id, content_markdown, msg_id)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("Notion card relay failed for task %s: %s", task["id"], exc)

    # Push notification
    if notify_title:
        creator_uid = task.get("created_by", "")
        if creator_uid and creator_uid != "system":
            await push_notification(
                creator_uid,
                type=notify_type,
                title=notify_title,
                body=content_markdown[:200],
                goal_id=task.get("goal_id", ""),
                task_id=task["id"],
                link=f"/goals/{task['goal_id']}/chat/{_get_assistant_agent_id(source_thread_id)}?thread_id={source_thread_id}",
            )

    logger.info(
        "Posted %s card to assistant thread %s for task %s",
        card.get("card_type", "unknown"), source_thread_id[:12], task["id"],
    )
    return msg_id


async def post_system_card_to_thread(
    *,
    thread_id: str,
    goal_id: str,
    card: dict,
    content_markdown: str,
    notify_uid: str = "",
    notify_title: str = "",
    notify_type: str = "agent_update",
) -> str | None:
    """Post a SYSTEM card to a specific thread: persist the message, broadcast to the
    live browser WS, and (optionally) fire an in-app bell to `notify_uid`.

    Unlike post_card_to_assistant_thread this does NOT forward the message to the agent —
    it's a human-facing notice (e.g. "your PR is open, please merge"), so it must not
    wake the assistant or burn a turn. Returns the message id."""
    if not thread_id:
        return None
    pool = get_pool()
    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages (id, thread_id, sender, role, content, token_count, card_data)
           VALUES ($1, $2, 'system', 'agent', $3, 0, $4::jsonb)""",
        msg_id, thread_id, content_markdown, json.dumps(card),
    )
    try:
        # Durable cross-instance fan-out (see post_card_to_assistant_thread).
        from app.chat_module.repository import notify_new_message
        await notify_new_message(thread_id, msg_id)
    except Exception as exc:
        logger.warning("post_system_card: fan-out notify failed thread=%s: %s",
                       thread_id[:12], exc)
    if notify_uid and notify_uid != "system" and notify_title:
        try:
            agent_id = await pool.fetchval(
                "SELECT agent_id FROM threads WHERE id = $1", thread_id,
            )
            await push_notification(
                notify_uid,
                type=notify_type,
                title=notify_title,
                body=content_markdown[:200],
                goal_id=goal_id,
                link=f"/goals/{goal_id}/chat/{agent_id or ''}?thread_id={thread_id}",
            )
        except Exception as exc:
            logger.warning("post_system_card: bell notify failed uid=%s: %s",
                           notify_uid, exc)
    return msg_id


async def _broadcast_card_to_thread(thread_id: str, card: dict, msg_id: str):
    """Push card to all WebSocket subscribers on the assistant thread.

    Finds the _AgentSession that owns this thread and broadcasts.
    Falls back to a direct approach if session not found.
    """
    payload = json.dumps({
        "type": "agent_card",
        "msg_id": msg_id,
        **card,
    })

    # Find the session that owns this thread
    # The assistant thread is owned by the assistant agent session (pid, agent_id)
    pool = get_pool()
    thread_row = await pool.fetchrow(
        "SELECT goal_id, agent_id FROM threads WHERE id = $1", thread_id,
    )
    if not thread_row:
        return

    pid = thread_row["goal_id"]
    agent_id = thread_row["agent_id"]

    # Get the session and broadcast
    # Try assistant per-conversation key first (3-tuple), then legacy (2-tuple)
    key = (pid, agent_id, thread_id)
    session = session_manager._sessions.get(key)
    if not session:
        # Fall back to legacy 2-tuple key (for non-assistant agents)
        key = (pid, agent_id)
        session = session_manager._sessions.get(key)
    if session:
        await session._broadcast(thread_id, payload)
    else:
        logger.debug("No active session for %s/%s — card saved to DB only", pid, agent_id)


def _get_assistant_agent_id(thread_id: str) -> str:
    """Sync helper to build chat link. Returns empty string if unknown."""
    # This is used for notification links only; non-critical
    return ""


def card_to_markdown(card: dict) -> str:
    """Convert a card to readable markdown for the message content column.

    This is the fallback rendering — used when card_data isn't supported
    (e.g., DB queries, API responses, non-card-aware clients).
    """
    card_type = card.get("card_type", "")
    agent_name = card.get("agent_name", "Agent")
    task_id = card.get("task_id", "")
    title = card.get("title", "")
    body = card.get("body", "")
    context = card.get("context", "")

    icons = {
        "question": "❓",
        "progress": "📋",
        "completion": "✅",
        "failure": "❌",
        "approval": "🎯",
    }
    icon = icons.get(card_type, "📌")

    lines = [f"{icon} **{agent_name}** · `{task_id}`", ""]

    if title:
        lines.append(f"**{title}**")
    if body:
        lines.append(body)

    # Options as a list
    options = card.get("options", [])
    if options:
        lines.append("")
        for opt in options:
            lines.append(f"- {opt.get('label', opt.get('id', ''))}")

    # Artifacts
    artifacts = card.get("artifacts", [])
    if artifacts:
        lines.append("")
        for art in artifacts:
            art_type = art.get("type", "")
            if art_type == "branch":
                lines.append(f"🌿 `{art.get('ref', '')}`")
            elif art_type == "file":
                lines.append(f"📁 `{art.get('path', '')}`")

    if context:
        lines.append("")
        lines.append(f"<details><summary>Details</summary>\n\n{context}\n\n</details>")

    return "\n".join(lines)
