"""Thread memory — summarizes conversation + goal context for agent continuity.

Called on session creation to generate a MEMORY.md summary from PostgreSQL,
combining conversation history, task state, decisions, and agent roster.
"""
import logging
from typing import Optional

from app.database import get_pool
from app.gemini_client import summarize_conversation

_log = logging.getLogger(__name__)

_MAX_MESSAGES = 50
_RECENT_COUNT = 5


async def generate_thread_memory(pool, thread_id: str, goal_id: str) -> Optional[str]:
    """Fetch messages + goal context from PostgreSQL, summarize via Gemini.

    Returns None if thread has no meaningful history (< 2 messages).
    """
    # ── 1. Conversation messages ─────────────────────────────────────────
    msg_rows = await pool.fetch(
        """SELECT role, content, created_at
           FROM messages
           WHERE thread_id = $1
           ORDER BY created_at DESC
           LIMIT $2""",
        thread_id, _MAX_MESSAGES,
    )

    if len(msg_rows) < 2:
        return None

    messages = [dict(r) for r in reversed(msg_rows)]

    # ── 2. Tasks in this goal ────────────────────────────────────────────
    task_rows = await pool.fetch(
        """SELECT id, title, state, assigned_to, priority, depends_on, description
           FROM tasks
           WHERE goal_id = $1 AND status = 'active'
           ORDER BY created_at DESC
           LIMIT 20""",
        goal_id,
    )
    tasks = [dict(r) for r in task_rows]

    # ── 3. Key decisions from the ledger ─────────────────────────────────
    decision_rows = await pool.fetch(
        """SELECT payload, timestamp
           FROM ledger_events
           WHERE context_goal_id = $1
             AND event_type IN ('decision.recorded', 'comms.thread.refreshed')
           ORDER BY timestamp DESC
           LIMIT 10""",
        goal_id,
    )
    decisions = [dict(r) for r in decision_rows]

    # ── 4. Agent roster for this goal ────────────────────────────────────
    agent_rows = await pool.fetch(
        """SELECT a.id, a.name, a.type, a.scope, a.model
           FROM agent_contexts ac
           JOIN agents a ON a.id = ac.agent_id
           WHERE ac.goal_id = $1 AND a.status = 'active'""",
        goal_id,
    )
    agents = [dict(r) for r in agent_rows]

    # ── 5. Split messages: recent (verbatim) + older (summarized) ────────
    if len(messages) <= _RECENT_COUNT:
        summary = None
        recent = messages
    else:
        older = messages[:-_RECENT_COUNT]
        recent = messages[-_RECENT_COUNT:]
        summary = await summarize_conversation(older)

    return _format_memory_md(
        summary=summary,
        recent_messages=recent,
        tasks=tasks,
        decisions=decisions,
        agents=agents,
    )


def _format_memory_md(
    summary: Optional[str],
    recent_messages: list[dict],
    tasks: list[dict],
    decisions: list[dict],
    agents: list[dict],
) -> str:
    """Format the enriched MEMORY.md content."""
    lines = [
        "# Thread Memory",
        "",
        "_Auto-generated context. Updated on session start._",
        "",
    ]

    # ── Conversation summary ─────────────────────────────────────────────
    if summary:
        lines.append("## Conversation Summary")
        lines.append(summary)
        lines.append("")

    # ── Task state ───────────────────────────────────────────────────────
    if tasks:
        lines.append("## Current Tasks")
        for t in tasks:
            state_icon = {
                "pending": "⬚", "in_progress": "🔄", "awaiting_approval": "⏳",
                "approved": "✅", "done": "✅", "failed": "❌", "rejected": "🚫",
            }.get(t["state"], "?")
            assigned = t["assigned_to"] or "unassigned"
            lines.append(f"- {state_icon} **{t['title']}** [{t['state']}] → {assigned} (priority: {t['priority']})")
            if t.get("depends_on"):
                lines.append(f"  - depends on: {', '.join(t['depends_on'])}")
        lines.append("")

    # ── Key decisions ────────────────────────────────────────────────────
    decision_entries = []
    for d in decisions:
        payload = d["payload"] if isinstance(d["payload"], dict) else {}
        ts = d["timestamp"]
        ts_str = ts.strftime("%m/%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]

        if payload.get("reason"):
            # Thread refresh event
            decision_entries.append(f"- [{ts_str}] Session refreshed ({payload.get('reason', '')})")
        elif payload.get("decision"):
            decision_entries.append(f"- [{ts_str}] {payload['decision']}")
        else:
            # Generic ledger event payload
            desc = payload.get("description", payload.get("title", str(payload)[:100]))
            decision_entries.append(f"- [{ts_str}] {desc}")

    if decision_entries:
        lines.append("## Key Decisions & Events")
        lines.extend(decision_entries)
        lines.append("")

    # ── Agent roster ─────────────────────────────────────────────────────
    if agents:
        lines.append("## Available Agents")
        for a in agents:
            agent_type = {"SHCA": "Assistant (coordinator)", "RA": "RA (reviewer)"}.get(a["type"], "IA (worker)")
            lines.append(f"- **{a['name']}** ({a['id']}) — {agent_type}, scope: {a['scope']}, model: {a['model']}")
        lines.append("")

    # ── Recent messages (verbatim) ───────────────────────────────────────
    lines.append("## Recent Messages")
    for msg in recent_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        ts = msg.get("created_at", "")
        if ts:
            ts_str = ts.strftime("%m/%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
            lines.append(f"**[{role} {ts_str}]** {content}")
        else:
            lines.append(f"**[{role}]** {content}")

    lines.append("")
    return "\n".join(lines)


async def write_thread_memory(
    gateway_admin,
    gateway_agent_id: str,
    thread_id: str,
    goal_id: str = "",
) -> None:
    """Generate and write MEMORY.md for the active thread.

    Called during session provisioning, before the first message is sent.
    Skips silently if there's no meaningful history or if writing fails.
    """
    pool = get_pool()

    # If goal_id not provided, look it up from the thread
    if not goal_id:
        goal_id = await pool.fetchval(
            "SELECT goal_id FROM threads WHERE id = $1", thread_id,
        )
        if not goal_id:
            _log.debug("Thread %s not found — skipping memory generation", thread_id)
            return

    content = await generate_thread_memory(pool, thread_id, goal_id)
    if not content:
        _log.debug("No thread memory to write for thread %s", thread_id)
        return

    try:
        await gateway_admin.set_agent_file(gateway_agent_id, "MEMORY.md", content)
        _log.info("Wrote thread memory (MEMORY.md) for thread %s", thread_id)
    except Exception as exc:
        _log.warning("Failed to write thread memory for %s: %s", thread_id, exc)
