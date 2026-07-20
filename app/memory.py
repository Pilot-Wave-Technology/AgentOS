"""Assistant Memory System — CRUD, compaction, settings, MEMORY.md generation.

Memory is a *query layer* over existing tables (ledger_events, tasks, messages)
plus a small agent_memories table for data that has no other home (preferences,
observations, conversation summaries, conventions).

The assistant's memory spans all goals (three-level scoping):
  global → goal → conversation
"""
import logging
from typing import Optional

import asyncpg

from app.agent_types import ASSISTANT, to_db_agent_type
from app.database import get_pool
from app.gemini_client import summarize_conversation

_DB_AGENT_TYPE = to_db_agent_type(ASSISTANT)  # 'SHCA' — DB constant, see agent_types.py

_log = logging.getLogger(__name__)

# ── System defaults (overridden per-goal via goals table columns) ────────────

DEFAULTS = {
    "goal_max_kb": 50,
    "conversation_max_kb": 20,
    "compaction_target_pct": 60,
    "recent_messages": 20,
    "auto_summary_interval": 20,
    "stale_conversation_days": 14,
    "instance_idle_minutes": 30,
}

GLOBAL_MAX_KB = 100  # system-wide, not per-goal configurable


# ── Settings resolution ──────────────────────────────────────────────────────

async def get_memory_settings(pool: asyncpg.Pool, goal_id: str) -> dict:
    """Resolve memory settings: goal override → system default."""
    row = await pool.fetchrow("""
        SELECT memory_goal_max_kb, memory_conversation_max_kb,
               memory_compaction_target_pct, memory_recent_messages,
               memory_auto_summary_interval, memory_stale_conversation_days,
               memory_instance_idle_minutes
        FROM goals WHERE id = $1
    """, goal_id)

    settings = dict(DEFAULTS)
    if row:
        col_map = [
            ("goal_max_kb", "memory_goal_max_kb"),
            ("conversation_max_kb", "memory_conversation_max_kb"),
            ("compaction_target_pct", "memory_compaction_target_pct"),
            ("recent_messages", "memory_recent_messages"),
            ("auto_summary_interval", "memory_auto_summary_interval"),
            ("stale_conversation_days", "memory_stale_conversation_days"),
            ("instance_idle_minutes", "memory_instance_idle_minutes"),
        ]
        for key, col in col_map:
            if row[col] is not None:
                settings[key] = row[col]

    return settings


# ── Actor resolution helpers ─────────────────────────────────────────────────

async def resolve_actor_name(pool: asyncpg.Pool, actor_id) -> str:
    """Resolve a ledger actor_id (UUID) to a human-readable name.

    Checks users table first (human actors), then agents table.
    """
    if not actor_id:
        return "system"

    actor_str = str(actor_id)

    # Try users
    row = await pool.fetchrow(
        "SELECT display_name, email FROM users WHERE actor_id = $1",
        actor_id,
    )
    if row:
        return row["display_name"] or row["email"]

    # Try agents
    row = await pool.fetchrow(
        "SELECT name FROM agents WHERE actor_id = $1",
        actor_id,
    )
    if row:
        return row["name"]

    return "unknown"


async def resolve_agent_name(pool: asyncpg.Pool, agent_id: str) -> str:
    """Resolve an agent ID to its display name."""
    if not agent_id:
        return "unassigned"
    row = await pool.fetchrow("SELECT name FROM agents WHERE id = $1", agent_id)
    return row["name"] if row else agent_id


async def find_task_approver(pool: asyncpg.Pool, task_id: str) -> Optional[str]:
    """Find the human who approved a task (from ledger state transitions)."""
    row = await pool.fetchrow("""
        SELECT actor_id FROM ledger_events
        WHERE context_task_id = $1
          AND event_type = 'TASK_STATUS_CHANGED'
          AND payload->>'to_state' IN ('approved', 'done')
          AND actor_type = 'human'
        ORDER BY timestamp DESC LIMIT 1
    """, task_id)
    if row:
        return await resolve_actor_name(pool, row["actor_id"])
    return None


async def get_goal_name(pool: asyncpg.Pool, goal_id: str) -> str:
    """Get a goal's display name."""
    if not goal_id:
        return "Global"
    row = await pool.fetchrow("SELECT name FROM goals WHERE id = $1", goal_id)
    return row["name"] if row else goal_id


# ── Memory CRUD ──────────────────────────────────────────────────────────────

async def save_agent_memory(
    pool: asyncpg.Pool,
    *,
    agent_template_id: str,
    agent_template_name: str,
    scope: str,
    memory_type: str,
    title: str,
    content: str,
    goal_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    human_uid: Optional[str] = None,
    human_name: Optional[str] = None,
    source: str = "agent",
    source_task_id: Optional[str] = None,
    source_thread_id: Optional[str] = None,
    related_files: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> str:
    """Insert a new agent memory. Returns the memory ID."""
    row = await pool.fetchrow("""
        INSERT INTO agent_memories
            (agent_template_id, agent_template_name, scope, goal_id, thread_id,
             memory_type, title, content,
             human_uid, human_name,
             source, source_task_id, source_thread_id,
             related_files, tags)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        RETURNING id
    """,
        agent_template_id, agent_template_name,
        scope,
        goal_id if scope != "global" else None,
        thread_id if scope == "conversation" else None,
        memory_type, title, content,
        human_uid, human_name,
        source, source_task_id, source_thread_id,
        related_files or [], tags or [],
    )
    memory_id = row["id"]
    _log.info(
        "Saved %s memory '%s' for assistant %s (scope=%s, goal=%s)",
        memory_type, title[:50], agent_template_id, scope, goal_id,
    )
    return memory_id


async def get_agent_memory(pool: asyncpg.Pool, memory_id: str) -> Optional[dict]:
    """Fetch a single memory by ID."""
    row = await pool.fetchrow("""
        SELECT * FROM agent_memories WHERE id = $1 AND status = 'active'
    """, memory_id)
    return dict(row) if row else None


async def archive_conversation_memories(pool: asyncpg.Pool, thread_id: str) -> int:
    """Archive all conversation-scoped memories for a thread. Returns count."""
    result = await pool.execute("""
        UPDATE agent_memories SET status = 'archived'
        WHERE thread_id = $1 AND scope = 'conversation' AND status = 'active'
    """, thread_id)
    count = int(result.split()[-1])
    if count:
        _log.info("Archived %d conversation memories for thread %s", count, thread_id)
    return count


# ── Compaction ───────────────────────────────────────────────────────────────

_COMPACTION_PROMPT = """Summarize the following memories into a single concise entry.
Preserve key facts, decisions, and preferences. Drop redundancy.
Keep the summary under 800 characters.

MEMORIES:
{memories}

SUMMARY:"""


async def check_and_compact(
    pool: asyncpg.Pool,
    agent_template_id: str,
    goal_id: Optional[str],
    scope: str,
    thread_id: Optional[str] = None,
) -> None:
    """Check if memory exceeds configured limit. If so, compact oldest entries."""
    # Determine limit
    if scope == "global":
        limit_bytes = GLOBAL_MAX_KB * 1024
        compaction_pct = DEFAULTS["compaction_target_pct"] / 100.0
    else:
        settings = await get_memory_settings(pool, goal_id) if goal_id else DEFAULTS
        key = "conversation_max_kb" if scope == "conversation" else "goal_max_kb"
        limit_bytes = settings[key] * 1024
        compaction_pct = settings["compaction_target_pct"] / 100.0

    # Get current total size
    if scope == "conversation":
        total = await pool.fetchval("""
            SELECT COALESCE(SUM(content_size), 0) FROM agent_memories
            WHERE agent_template_id = $1 AND thread_id = $2
              AND scope = 'conversation' AND status = 'active'
        """, agent_template_id, thread_id)
    elif scope == "goal":
        total = await pool.fetchval("""
            SELECT COALESCE(SUM(content_size), 0) FROM agent_memories
            WHERE agent_template_id = $1 AND goal_id = $2
              AND scope = 'goal' AND status = 'active'
        """, agent_template_id, goal_id)
    else:  # global
        total = await pool.fetchval("""
            SELECT COALESCE(SUM(content_size), 0) FROM agent_memories
            WHERE agent_template_id = $1 AND scope = 'global' AND status = 'active'
        """, agent_template_id)

    if total <= limit_bytes:
        return

    target = int(limit_bytes * compaction_pct)
    excess = total - target

    _log.info(
        "Memory compaction needed: %s scope=%s total=%d limit=%d excess=%d",
        agent_template_id, scope, total, limit_bytes, excess,
    )

    # Select oldest compactable memories
    if scope == "conversation":
        candidates = await pool.fetch("""
            SELECT id, title, content, content_size FROM agent_memories
            WHERE agent_template_id = $1 AND thread_id = $2
              AND scope = 'conversation' AND status = 'active'
            ORDER BY created_at ASC LIMIT 20
        """, agent_template_id, thread_id)
    elif scope == "goal":
        candidates = await pool.fetch("""
            SELECT id, title, content, content_size FROM agent_memories
            WHERE agent_template_id = $1 AND goal_id = $2
              AND scope = 'goal' AND status = 'active'
            ORDER BY created_at ASC LIMIT 20
        """, agent_template_id, goal_id)
    else:
        candidates = await pool.fetch("""
            SELECT id, title, content, content_size FROM agent_memories
            WHERE agent_template_id = $1 AND scope = 'global' AND status = 'active'
            ORDER BY created_at ASC LIMIT 20
        """, agent_template_id)

    to_compact = []
    freed = 0
    for mem in candidates:
        to_compact.append(mem)
        freed += mem["content_size"]
        if freed >= excess:
            break

    if not to_compact:
        return

    # Summarize via Gemini
    texts = [f"**{m['title']}**: {m['content']}" for m in to_compact]
    memories_text = "\n\n".join(texts)

    from app.gemini_client import summarize_conversation as _gemini_call
    # Reuse the Gemini client with a compaction-specific prompt
    from google import genai
    from app.config import get_settings
    settings_obj = get_settings()

    summary = ""
    if settings_obj.gemini_api_key:
        try:
            client = genai.Client(api_key=settings_obj.gemini_api_key)
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=_COMPACTION_PROMPT.format(memories=memories_text),
            )
            summary = response.text.strip()
        except Exception as exc:
            _log.warning("Compaction summarization failed: %s", exc)
            # Fallback: concatenate titles
            summary = "; ".join(m["title"] for m in to_compact)

    if not summary:
        summary = "; ".join(m["title"] for m in to_compact)

    # Atomic: mark old as compacted, insert summary
    async with pool.acquire() as conn:
        async with conn.transaction():
            ids = [m["id"] for m in to_compact]
            await conn.execute("""
                UPDATE agent_memories SET status = 'compacted'
                WHERE id = ANY($1::text[])
            """, ids)

            # Get template name from first candidate's parent
            template_name = await conn.fetchval(
                "SELECT agent_template_name FROM agent_memories WHERE id = $1",
                to_compact[0]["id"],
            )

            await conn.execute("""
                INSERT INTO agent_memories
                    (agent_template_id, agent_template_name, scope, goal_id, thread_id,
                     memory_type, title, content, source)
                VALUES ($1, $2, $3, $4, $5, 'context', $6, $7, 'system')
            """,
                agent_template_id, template_name or agent_template_id,
                scope, goal_id, thread_id,
                f"Compacted {len(to_compact)} memories",
                summary,
            )

    _log.info(
        "Compacted %d memories for %s (scope=%s), freed ~%d bytes",
        len(to_compact), agent_template_id, scope, freed,
    )


# ── MEMORY.md generation ────────────────────────────────────────────────────

async def generate_memory_md(
    pool: asyncpg.Pool,
    assistant_template_id: str,
    goal_id: str,
    thread_id: str,
) -> str:
    """Generate MEMORY.md for an assistant conversation instance.

    Combines:
      - Global memories (assistant's brain, cross-goal)
      - Goal data from ledger_events and tasks (no duplication)
      - Goal-scoped agent_memories
      - Conversation-scoped agent_memories
      - Recent messages (verbatim)
      - Older messages (summarized)
    """
    settings = await get_memory_settings(pool, goal_id)
    recent_count = settings["recent_messages"]

    # Fetch assistant template + owner info
    assistant = await pool.fetchrow(
        "SELECT name, owner_actor_id FROM agents WHERE id = $1", assistant_template_id,
    )
    if not assistant:
        return f"# Memory\n\nAssistant template {assistant_template_id} not found.\n"

    owner_name = ""
    if assistant["owner_actor_id"]:
        owner_row = await pool.fetchrow(
            "SELECT display_name FROM users WHERE actor_id = $1", assistant["owner_actor_id"],
        )
        owner_name = owner_row["display_name"] if owner_row else ""

    goal_name = await get_goal_name(pool, goal_id)

    sections: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────
    sections.append(f"# {assistant['name']} — Memory\n")
    if owner_name:
        sections.append(
            f"You are {assistant['name']}, {owner_name}'s personal project agent.\n"
        )

    # ── Global memories ─────────────────────────────────────────────────
    global_mems = await pool.fetch("""
        SELECT title, content, memory_type FROM agent_memories
        WHERE agent_template_id = $1 AND scope = 'global' AND status = 'active'
        ORDER BY memory_type, updated_at DESC LIMIT 20
    """, assistant_template_id)

    if global_mems:
        sections.append("\n## Your Knowledge\n")
        for m in global_mems:
            sections.append(f"- **{m['title']}:** {m['content'][:300]}\n")

    # ── Goal section header + description ─────────────────────────────
    goal_row = await pool.fetchrow(
        "SELECT name, description FROM goals WHERE id = $1", goal_id,
    )
    sections.append(f"\n## Goal: {goal_name}\n")
    if goal_row and goal_row["description"]:
        sections.append(f"{goal_row['description']}\n")

    # ── Resources ───────────────────────────────────────────────────────
    resources = await pool.fetch("""
        SELECT name, type, uri, notes FROM resources
        WHERE goal_id = $1 AND status = 'active'
        ORDER BY type, name
    """, goal_id)

    if resources:
        sections.append("\n### Resources\n")
        for r in resources:
            rtype = r["type"].replace("_", " ").title() if r["type"] else "Other"
            line = f"- **{r['name']}** ({rtype})"
            if r["uri"]:
                line += f" — {r['uri']}"
            if r["notes"]:
                line += f" — {r['notes'][:100]}"
            sections.append(f"{line}\n")

    # ── Agent roster ────────────────────────────────────────────────────
    roster = await pool.fetch("""
        SELECT a.name, a.type, a.scope, a.model
        FROM agent_contexts ac
        JOIN agents a ON a.id = ac.agent_id
        WHERE ac.goal_id = $1 AND a.status = 'active'
    """, goal_id)

    if roster:
        sections.append("\n### Team\n")
        for a in roster:
            role = "coordinator" if a["type"] == _DB_AGENT_TYPE else "worker"
            sections.append(f"- **{a['name']}** ({role}, {a['model']})\n")

    # ── Decisions from ledger ───────────────────────────────────────────
    decisions = await pool.fetch("""
        SELECT payload->>'description' as description, actor_id, timestamp
        FROM ledger_events
        WHERE context_goal_id = $1
          AND event_type IN ('DECISION_RECORDED', 'QUESTION_ANSWERED')
        ORDER BY timestamp DESC LIMIT 15
    """, goal_id)

    if decisions:
        sections.append("\n### Key Decisions\n")
        for d in decisions:
            date = d["timestamp"].strftime("%Y-%m-%d")
            name = await resolve_actor_name(pool, d["actor_id"])
            desc = d["description"] or "(no description)"
            sections.append(f"- **{date} — {name}:** {desc[:200]}\n")

    # ── Tasks: completed ────────────────────────────────────────────────
    completed = await pool.fetch("""
        SELECT id, title, result, assigned_to, created_at FROM tasks
        WHERE goal_id = $1 AND state IN ('done', 'approved') AND status = 'active'
        ORDER BY created_at DESC LIMIT 15
    """, goal_id)

    if completed:
        sections.append("\n### What's Been Done\n")
        for t in completed:
            agent_name = await resolve_agent_name(pool, t["assigned_to"])
            approver = await find_task_approver(pool, t["id"])
            date = t["created_at"].strftime("%Y-%m-%d")
            line = f"- ✅ **{t['title']}** — {agent_name}"
            if approver:
                line += f", approved by {approver}"
            line += f" ({date})\n"
            sections.append(line)

    # ── Tasks: in progress ──────────────────────────────────────────────
    in_progress = await pool.fetch("""
        SELECT title, assigned_to FROM tasks
        WHERE goal_id = $1 AND state = 'in_progress' AND status = 'active'
    """, goal_id)

    if in_progress:
        sections.append("\n### In Progress\n")
        for t in in_progress:
            agent_name = await resolve_agent_name(pool, t["assigned_to"])
            sections.append(f"- 🔄 **{t['title']}** — {agent_name}\n")

    # ── Tasks: blocked ──────────────────────────────────────────────────
    blocked = await pool.fetch("""
        SELECT title, blocked_reason, assigned_to FROM tasks
        WHERE goal_id = $1 AND state = 'blocked' AND status = 'active'
    """, goal_id)

    if blocked:
        sections.append("\n### What's Stuck\n")
        for t in blocked:
            reason = t["blocked_reason"] or "no reason given"
            sections.append(f"- 🚫 **{t['title']}** — {reason}\n")

    # ── Goal-scoped agent_memories ──────────────────────────────────────
    goal_mems = await pool.fetch("""
        SELECT title, content, memory_type FROM agent_memories
        WHERE agent_template_id = $1 AND goal_id = $2
          AND scope = 'goal' AND status = 'active'
        ORDER BY updated_at DESC LIMIT 15
    """, assistant_template_id, goal_id)

    if goal_mems:
        sections.append("\n### Goal Notes\n")
        for m in goal_mems:
            sections.append(f"- **{m['title']}:** {m['content'][:300]}\n")

    # ── Conversation memories ───────────────────────────────────────────
    conv_mems = await pool.fetch("""
        SELECT title, content, source FROM agent_memories
        WHERE agent_template_id = $1 AND thread_id = $2
          AND scope = 'conversation' AND status = 'active'
        ORDER BY created_at DESC LIMIT 15
    """, assistant_template_id, thread_id)

    if conv_mems:
        sections.append("\n## This Conversation\n")
        for m in conv_mems:
            tag = "auto-summary" if m["source"] == "system" else assistant["name"]
            sections.append(f"- [{tag}] {m['title']}: {m['content'][:300]}\n")

    # ── Recent messages ─────────────────────────────────────────────────
    recent = await pool.fetch("""
        SELECT role, content, sender, created_at FROM messages
        WHERE thread_id = $1 ORDER BY created_at DESC LIMIT $2
    """, thread_id, recent_count)

    total_msgs = await pool.fetchval(
        "SELECT count(*) FROM messages WHERE thread_id = $1", thread_id,
    )

    # Older messages summary (if there are more than recent_count)
    if total_msgs and total_msgs > recent_count:
        older = await pool.fetch("""
            SELECT role, content, created_at FROM messages
            WHERE thread_id = $1 ORDER BY created_at ASC LIMIT $2
        """, thread_id, total_msgs - recent_count)

        if older:
            older_dicts = [dict(r) for r in older]
            summary = await summarize_conversation(older_dicts)
            if summary:
                sections.append(
                    f"\n## Earlier in This Conversation\n\n{summary}\n"
                )

    if recent:
        sections.append(f"\n## Recent Messages (last {len(recent)})\n")
        for m in reversed(recent):
            ts = m["created_at"].strftime("%H:%M") if m["created_at"] else ""
            name = m["sender"] if m["role"] == "human" else assistant["name"]
            sections.append(f"**[{name} — {ts}]** {m['content'][:500]}\n")

    # ── Footer: Memory usage instructions ──────────────────────────────
    sections.append("\n---\n")
    sections.append("## How to Use Your Memory\n")
    sections.append(
        "You have three memory tools: **memory_search**, **memory_get**, **memory_save**.\n\n"
        "**Before answering questions about past work, decisions, or status:** "
        "Always call memory_search first. It searches across all your goals.\n\n"
        "**When you learn something important:** Save it with memory_save.\n"
        "- User preferences → type: preference, scope: global\n"
        "- Project decisions → type: convention, scope: goal\n"
        "- Working context → type: context, scope: conversation\n\n"
        "**For full details on a search result:** Use memory_get with the result's ID.\n\n"
        "Your memory persists across conversations and goals. "
        "What you save now, you'll remember in every future conversation.\n"
    )

    return "\n".join(sections)


# ── Conversation lifecycle helpers ───────────────────────────────────────────

async def auto_summarize_if_needed(
    pool: asyncpg.Pool,
    assistant_template_id: str,
    goal_id: str,
    thread_id: str,
) -> None:
    """Check if we've hit the auto-summary interval. If so, summarize."""
    settings = await get_memory_settings(pool, goal_id)
    interval = settings["auto_summary_interval"]

    msg_count = await pool.fetchval(
        "SELECT count(*) FROM messages WHERE thread_id = $1", thread_id,
    )

    if msg_count <= 0 or msg_count % interval != 0:
        return

    _log.info(
        "Auto-summarizing at message %d for thread %s (interval=%d)",
        msg_count, thread_id, interval,
    )

    # Fetch the last N messages for summarization
    recent = await pool.fetch("""
        SELECT role, content, sender, created_at FROM messages
        WHERE thread_id = $1 ORDER BY created_at DESC LIMIT $2
    """, thread_id, interval)

    if not recent:
        return

    summary = await summarize_conversation([dict(r) for r in reversed(recent)])
    if not summary:
        return

    # Get assistant and human info
    assistant = await pool.fetchrow(
        "SELECT name, owner_actor_id FROM agents WHERE id = $1", assistant_template_id,
    )
    thread = await pool.fetchrow(
        "SELECT created_by FROM threads WHERE id = $1", thread_id,
    )
    human_name = ""
    human_uid = ""
    if thread and thread["created_by"]:
        human_uid = thread["created_by"]
        user = await pool.fetchrow(
            "SELECT display_name FROM users WHERE uid = $1", human_uid,
        )
        human_name = user["display_name"] if user else ""

    start = msg_count - interval + 1
    await save_agent_memory(
        pool,
        agent_template_id=assistant_template_id,
        agent_template_name=assistant["name"] if assistant else assistant_template_id,
        scope="conversation",
        memory_type="context",
        title=f"Messages {start}-{msg_count}",
        content=summary,
        goal_id=goal_id,
        thread_id=thread_id,
        human_uid=human_uid,
        human_name=human_name,
        source="system",
        source_thread_id=thread_id,
    )

    # Check compaction after auto-summary
    await check_and_compact(pool, assistant_template_id, goal_id, "conversation", thread_id)



async def close_conversation(
    pool: asyncpg.Pool,
    thread_id: str,
    assistant_template_id: str,
    goal_id: str,
) -> None:
    """Close a conversation: summarize → promote to goal memory → archive."""
    assistant = await pool.fetchrow(
        "SELECT name FROM agents WHERE id = $1", assistant_template_id,
    )
    thread = await pool.fetchrow(
        "SELECT created_by, title FROM threads WHERE id = $1", thread_id,
    )

    if not thread:
        _log.warning("Thread %s not found for close_conversation", thread_id)
        return

    human_name = ""
    human_uid = thread["created_by"] or ""
    if human_uid:
        user = await pool.fetchrow(
            "SELECT display_name FROM users WHERE uid = $1", human_uid,
        )
        human_name = user["display_name"] if user else ""

    # Summarize full conversation and promote to goal-scoped memory
    messages = await pool.fetch("""
        SELECT role, content, created_at FROM messages
        WHERE thread_id = $1 ORDER BY created_at
    """, thread_id)

    if len(messages) > 3:
        summary = await summarize_conversation([dict(r) for r in messages])
        if summary:
            await save_agent_memory(
                pool,
                agent_template_id=assistant_template_id,
                agent_template_name=assistant["name"] if assistant else assistant_template_id,
                scope="goal",
                memory_type="context",
                title=f"Conversation: {thread['title'][:60]}",
                content=summary,
                goal_id=goal_id,
                human_uid=human_uid,
                human_name=human_name,
                source="system",
                source_thread_id=thread_id,
            )

    # Archive conversation-scoped memories
    await archive_conversation_memories(pool, thread_id)

    # Mark thread as closed
    await pool.execute(
        "UPDATE threads SET status = 'closed' WHERE id = $1", thread_id,
    )

    _log.info("Closed conversation %s (thread=%s)", thread["title"], thread_id)


async def cleanup_stale_conversations(pool: asyncpg.Pool) -> int:
    """Close conversations with no messages for N days. Returns count closed."""
    stale = await pool.fetch("""
        SELECT t.id, t.agent_id, t.goal_id
        FROM threads t
        JOIN goals g ON g.id = t.goal_id
        WHERE t.status = 'open'
          AND NOT EXISTS (
              SELECT 1 FROM messages m WHERE m.thread_id = t.id
              AND m.created_at > now() - make_interval(
                  days => COALESCE(g.memory_stale_conversation_days, 14))
          )
    """)

    count = 0
    for t in stale:
        try:
            await close_conversation(pool, t["id"], t["agent_id"], t["goal_id"])
            count += 1
        except Exception as exc:
            _log.warning("Failed to close stale thread %s: %s", t["id"], exc)

    if count:
        _log.info("Cleaned up %d stale conversations", count)
    return count


# ── Search (unified across tables) ──────────────────────────────────────────

async def _resolve_search_goals(
    pool: asyncpg.Pool, agent_template_id: str, goal_id: str, scope: str,
) -> list[str]:
    """Resolve which goal IDs to search based on scope."""
    if scope == "conversation":
        return []  # only search agent_memories for this thread
    elif scope == "goal":
        return [goal_id] if goal_id else []
    else:
        # "all" and "cross_goal" — search ALL goals this assistant is added to
        rows = await pool.fetch(
            "SELECT goal_id FROM agent_contexts WHERE agent_id = $1",
            agent_template_id,
        )
        goal_ids = [r["goal_id"] for r in rows]
        # Ensure current goal is included even if not in agent_contexts
        if goal_id and goal_id not in goal_ids:
            goal_ids.append(goal_id)
        return goal_ids


def _score_result(result: dict, current_goal_id: str) -> float:
    """Score a result based on goal proximity and recency."""
    import datetime
    # Goal proximity boost
    goal_boost = 1.5 if result.get("_goal_id") == current_goal_id else 1.0

    # Recency score (1.0 for today, decays over 30 days to 0.1)
    try:
        date = datetime.datetime.fromisoformat(result["date"].replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        days_old = max(0, (now - date).days)
        recency = max(0.1, 1.0 - (days_old / 30.0) * 0.9)
    except Exception:
        recency = 0.5

    return goal_boost * recency


async def search_memory(
    pool: asyncpg.Pool,
    agent_template_id: str,
    goal_id: str,
    thread_id: Optional[str] = None,
    q: str = "",
    scope: str = "all",
    memory_type: Optional[str] = None,
    max_results: int = 10,
) -> list[dict]:
    """Unified search across ledger_events, tasks, task_activity, messages,
    and agent_memories.

    Scope:
      - "all" (default): ALL goals the assistant is in, current goal ranked higher
      - "goal": current goal only
      - "conversation": current conversation's agent_memories only
      - "cross_goal": same as "all" (backward compat)
    """
    results: list[dict] = []

    if not q:
        return results

    goal_ids = await _resolve_search_goals(pool, agent_template_id, goal_id, scope)
    like_q = f"%{q}%"

    # ── Search ledger events (decisions, answers, approvals) ─────────
    if goal_ids and memory_type in (None, "decision"):
        decisions = await pool.fetch("""
            SELECT event_id::text as id,
                   event_type,
                   payload->>'description' as description,
                   payload->>'outcome_summary' as outcome,
                   actor_id, timestamp, context_goal_id, context_task_id
            FROM ledger_events
            WHERE context_goal_id = ANY($1::text[])
              AND event_type IN (
                  'decision.recorded', 'comms.question.answered',
                  'approval.granted', 'approval.denied'
              )
              AND (payload->>'description' ILIKE $2
                   OR payload->>'outcome_summary' ILIKE $2)
            ORDER BY timestamp DESC LIMIT $3
        """, goal_ids, like_q, max_results)

        for d in decisions:
            human = await resolve_actor_name(pool, d["actor_id"])
            gname = await get_goal_name(pool, d["context_goal_id"])
            desc = d["description"] or d["outcome"] or "(no description)"
            results.append({
                "id": d["id"], "type": "decision",
                "title": desc[:200],
                "content": desc,
                "human": human, "agent": None,
                "goal": gname, "_goal_id": d["context_goal_id"],
                "date": d["timestamp"].isoformat(),
                "source": "ledger",
            })

    # ── Search tasks: outcomes (FTS) ────────────────────────────────
    if goal_ids and memory_type in (None, "task_outcome"):
        tasks = await pool.fetch("""
            SELECT id, title, result, state, assigned_to, created_at, goal_id
            FROM tasks
            WHERE goal_id = ANY($1::text[])
              AND state IN ('done', 'approved', 'failed')
              AND status = 'active'
              AND (search_vector @@ plainto_tsquery('english', $2)
                   OR title ILIKE $3)
            ORDER BY created_at DESC LIMIT $4
        """, goal_ids, q, like_q, max_results)

        for t in tasks:
            agent_name = await resolve_agent_name(pool, t["assigned_to"])
            approver = await find_task_approver(pool, t["id"])
            gname = await get_goal_name(pool, t["goal_id"])
            state_label = "Completed" if t["state"] != "failed" else "Failed"
            results.append({
                "id": t["id"], "type": "task_outcome",
                "title": f"{state_label}: {t['title']}",
                "content": (t["result"] or "")[:500],
                "human": approver, "agent": agent_name,
                "goal": gname, "_goal_id": t["goal_id"],
                "date": t["created_at"].isoformat(),
                "source": "tasks",
            })

    # ── Search tasks: in progress (FTS) ─────────────────────────────
    if goal_ids and memory_type in (None, "task_outcome", "progress"):
        in_progress = await pool.fetch("""
            SELECT id, title, assigned_to, created_at, goal_id, description
            FROM tasks
            WHERE goal_id = ANY($1::text[])
              AND state = 'in_progress' AND status = 'active'
              AND (search_vector @@ plainto_tsquery('english', $2)
                   OR title ILIKE $3)
            ORDER BY created_at DESC LIMIT $4
        """, goal_ids, q, like_q, max_results)

        for t in in_progress:
            agent_name = await resolve_agent_name(pool, t["assigned_to"])
            gname = await get_goal_name(pool, t["goal_id"])
            results.append({
                "id": t["id"], "type": "progress",
                "title": f"In progress: {t['title']}",
                "content": (t["description"] or "")[:500],
                "human": None, "agent": agent_name,
                "goal": gname, "_goal_id": t["goal_id"],
                "date": t["created_at"].isoformat(),
                "source": "tasks",
            })

    # ── Search tasks: blockers (FTS) ────────────────────────────────
    if goal_ids and memory_type in (None, "blocker"):
        blocked = await pool.fetch("""
            SELECT id, title, blocked_reason, assigned_to, created_at, goal_id
            FROM tasks
            WHERE goal_id = ANY($1::text[])
              AND state = 'blocked' AND status = 'active'
              AND (search_vector @@ plainto_tsquery('english', $2)
                   OR title ILIKE $3)
            ORDER BY created_at DESC LIMIT $4
        """, goal_ids, q, like_q, max_results)

        for b in blocked:
            agent_name = await resolve_agent_name(pool, b["assigned_to"])
            gname = await get_goal_name(pool, b["goal_id"])
            results.append({
                "id": b["id"], "type": "blocker",
                "title": f"Blocked: {b['title']}",
                "content": (b["blocked_reason"] or "")[:500],
                "human": None, "agent": agent_name,
                "goal": gname, "_goal_id": b["goal_id"],
                "date": b["created_at"].isoformat(),
                "source": "tasks",
            })

    # ── Search task_activity (FTS — progress updates, completion reports)
    if goal_ids and memory_type in (None, "task_outcome", "progress"):
        activity = await pool.fetch("""
            SELECT ta.id, ta.task_id, ta.type, ta.summary, ta.content,
                   ta.agent_id, ta.created_at, t.title as task_title, t.goal_id
            FROM task_activity ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE t.goal_id = ANY($1::text[])
              AND (ta.search_vector @@ plainto_tsquery('english', $2)
                   OR ta.summary ILIKE $3)
            ORDER BY ta.created_at DESC LIMIT $4
        """, goal_ids, q, like_q, max_results)

        for a in activity:
            agent_name = await resolve_agent_name(pool, a["agent_id"])
            gname = await get_goal_name(pool, a["goal_id"])
            results.append({
                "id": a["id"], "type": "progress",
                "title": f"{a['task_title']}: {a['summary'][:100]}",
                "content": (a["content"] or a["summary"] or "")[:500],
                "human": None, "agent": agent_name,
                "goal": gname, "_goal_id": a["goal_id"],
                "date": a["created_at"].isoformat(),
                "source": "activity",
            })

    # ── Search messages (FTS — conversation history) ────────────────
    if goal_ids and memory_type in (None, "message"):
        msgs = await pool.fetch("""
            SELECT m.id, m.thread_id, m.role, m.content, m.sender,
                   m.created_at, t.title as thread_title, t.goal_id
            FROM messages m
            JOIN threads t ON t.id = m.thread_id
            WHERE t.goal_id = ANY($1::text[])
              AND m.search_vector @@ plainto_tsquery('english', $2)
            ORDER BY m.created_at DESC LIMIT $3
        """, goal_ids, q, max_results)

        for m in msgs:
            gname = await get_goal_name(pool, m["goal_id"])
            results.append({
                "id": m["id"], "type": "message",
                "title": f"[{m['role']}] in '{m['thread_title'][:50]}'",
                "content": m["content"][:500],
                "human": m["sender"] if m["role"] == "human" else None,
                "agent": m["sender"] if m["role"] == "agent" else None,
                "goal": gname, "_goal_id": m["goal_id"],
                "date": m["created_at"].isoformat(),
                "source": "messages",
            })

    # ── Search agent_memories ────────────────────────────────────────
    if memory_type in (None, "preference", "observation", "context", "convention"):
        mem_where = "agent_template_id = $1 AND status = 'active'"
        mem_params: list = [agent_template_id, q, max_results]

        if scope == "conversation" and thread_id:
            mem_where += " AND thread_id = $4"
            mem_params.append(thread_id)
        elif scope == "goal":
            if goal_id:
                mem_where += " AND (goal_id = $4 OR scope = 'global')"
                mem_params.append(goal_id)
        else:
            # "all" / "cross_goal" — search all memories for this assistant
            pass

        mems = await pool.fetch(f"""
            SELECT id, memory_type, title, content, human_name,
                   agent_template_name, related_files, created_at,
                   goal_id, scope
            FROM agent_memories
            WHERE {mem_where}
              AND search_vector @@ plainto_tsquery('english', $2)
            ORDER BY ts_rank(search_vector, plainto_tsquery('english', $2)) DESC
            LIMIT $3
        """, *mem_params)

        for m in mems:
            gname = await get_goal_name(pool, m["goal_id"]) if m["goal_id"] else "Global"
            results.append({
                "id": m["id"], "type": m["memory_type"],
                "title": m["title"],
                "content": m["content"][:500],
                "human": m["human_name"],
                "agent": m["agent_template_name"],
                "goal": gname, "_goal_id": m["goal_id"],
                "scope": m["scope"],
                "files": m["related_files"] or [],
                "date": m["created_at"].isoformat(),
                "source": "memory",
            })

    # ── Rank: goal proximity + recency, tag cross-goal results ───────
    for r in results:
        r["_score"] = _score_result(r, goal_id)
        # Tag cross-goal results with goal name
        if r.get("_goal_id") and r["_goal_id"] != goal_id:
            r["title"] = f"{r['title']} (Goal: {r.get('goal', '')})"

    results.sort(key=lambda r: r["_score"], reverse=True)

    # Clean up internal fields before returning
    for r in results:
        r.pop("_goal_id", None)
        r.pop("_score", None)

    return results[:max_results]


# ── Auto-save helpers (system-generated memories on task events) ─────────────

async def get_goal_assistant(pool: asyncpg.Pool, goal_id: str) -> Optional[dict]:
    """Find the assistant template assigned to a goal."""
    row = await pool.fetchrow(f"""
        SELECT a.id, a.name, a.owner_actor_id
        FROM agent_contexts ac
        JOIN agents a ON a.id = ac.agent_id
        WHERE ac.goal_id = $1 AND a.type = '{_DB_AGENT_TYPE}'
        LIMIT 1
    """, goal_id)
    return dict(row) if row else None


async def auto_save_task_completed(
    pool: asyncpg.Pool, task: dict,
) -> None:
    """Auto-save a goal memory when a task completes."""
    assistant = await get_goal_assistant(pool, task["goal_id"])
    if not assistant:
        return

    agent_name = await resolve_agent_name(pool, task.get("assigned_to", ""))
    approver = await find_task_approver(pool, task["id"])
    result_preview = (task.get("result") or "")[:500]

    title = f"Task completed: {task['title'][:80]}"
    content = f"Completed by {agent_name}."
    if approver:
        content += f" Approved by {approver}."
    if result_preview:
        content += f"\n\nResult: {result_preview}"

    await save_agent_memory(
        pool,
        agent_template_id=assistant["id"],
        agent_template_name=assistant["name"],
        scope="goal",
        memory_type="context",
        title=title,
        content=content,
        goal_id=task["goal_id"],
        human_name=approver,
        source="system",
        source_task_id=task["id"],
    )
    _log.info("Auto-saved task completion memory: %s", title[:60])


async def auto_save_plan_approved(
    pool: asyncpg.Pool, task_id: str, goal_id: str,
    user_name: str, user_uid: str, subtask_count: int,
) -> None:
    """Auto-save a goal memory when a plan is approved."""
    assistant = await get_goal_assistant(pool, goal_id)
    if not assistant:
        return

    task = await pool.fetchrow("SELECT title FROM tasks WHERE id = $1", task_id)
    title_text = task["title"] if task else task_id

    await save_agent_memory(
        pool,
        agent_template_id=assistant["id"],
        agent_template_name=assistant["name"],
        scope="goal",
        memory_type="context",
        title=f"Plan approved: {title_text[:80]}",
        content=f"Approved by {user_name}. {subtask_count} subtasks created.",
        goal_id=goal_id,
        human_name=user_name,
        human_uid=user_uid,
        source="system",
        source_task_id=task_id,
    )
    _log.info("Auto-saved plan approval memory for task %s", task_id)


async def auto_save_task_blocked(
    pool: asyncpg.Pool, task: dict,
) -> None:
    """Auto-save a goal memory when a task gets blocked."""
    assistant = await get_goal_assistant(pool, task["goal_id"])
    if not assistant:
        return

    agent_name = await resolve_agent_name(pool, task.get("assigned_to", ""))
    reason = task.get("blocked_reason", "Unknown reason")

    await save_agent_memory(
        pool,
        agent_template_id=assistant["id"],
        agent_template_name=assistant["name"],
        scope="goal",
        memory_type="context",
        title=f"Blocked: {task['title'][:80]}",
        content=f"Agent {agent_name} is blocked: {reason[:500]}",
        goal_id=task["goal_id"],
        source="system",
        source_task_id=task["id"],
    )
    _log.info("Auto-saved blocker memory for task %s", task["id"])


async def auto_save_question_answered(
    pool: asyncpg.Pool, task_id: str, goal_id: str,
    question: str, answer: str, user_name: str, user_uid: str,
) -> None:
    """Auto-save a goal memory when a human answers an agent question."""
    assistant = await get_goal_assistant(pool, goal_id)
    if not assistant:
        return

    await save_agent_memory(
        pool,
        agent_template_id=assistant["id"],
        agent_template_name=assistant["name"],
        scope="goal",
        memory_type="context",
        title=f"Decided: {question[:80]}",
        content=f"{user_name} answered: {answer[:500]}",
        goal_id=goal_id,
        human_name=user_name,
        human_uid=user_uid,
        source="system",
        source_task_id=task_id,
    )

    # Archive the blocker memory if one exists
    await pool.execute("""
        UPDATE agent_memories SET status = 'archived'
        WHERE source_task_id = $1 AND memory_type = 'context'
          AND title LIKE 'Blocked:%' AND status = 'active'
    """, task_id)
    _log.info("Auto-saved answer memory for task %s", task_id)


# ── Detail (drill-down) ─────────────────────────────────────────────────────

async def get_memory_detail(
    pool: asyncpg.Pool,
    item_id: str,
    source: Optional[str] = None,
) -> Optional[dict]:
    """Get full detail for a memory search result. Auto-detects source if not given."""

    # Auto-detect source by trying each table
    if not source:
        # Try agent_memories first (most common drill-down target)
        row = await pool.fetchrow(
            "SELECT id FROM agent_memories WHERE id = $1", item_id,
        )
        if row:
            source = "memory"
        else:
            # Try tasks
            row = await pool.fetchrow(
                "SELECT id FROM tasks WHERE id = $1", item_id,
            )
            if row:
                source = "tasks"
            else:
                # Try ledger
                try:
                    row = await pool.fetchrow(
                        "SELECT event_id FROM ledger_events WHERE event_id = $1::uuid",
                        item_id,
                    )
                    if row:
                        source = "ledger"
                except Exception:
                    pass

    if not source:
        return None

    if source == "memory":
        mem = await pool.fetchrow(
            "SELECT * FROM agent_memories WHERE id = $1", item_id,
        )
        if not mem:
            return None

        detail: dict = {"source": "memory", "memory": dict(mem)}

        if mem["source_task_id"]:
            task = await pool.fetchrow(
                "SELECT * FROM tasks WHERE id = $1", mem["source_task_id"],
            )
            if task:
                detail["task"] = dict(task)

        return detail

    elif source == "tasks":
        task = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", item_id)
        if not task:
            return None

        detail = {"source": "tasks", "task": dict(task)}

        # Task activity
        activity = await pool.fetch("""
            SELECT type, summary, content, created_at FROM task_activity
            WHERE task_id = $1 ORDER BY created_at
        """, item_id)
        detail["activity"] = [dict(a) for a in activity]

        # Approver
        detail["approver"] = await find_task_approver(pool, item_id)

        # Agent name
        detail["agent"] = await resolve_agent_name(pool, task["assigned_to"])

        # Thread messages (if task has a thread)
        if task["thread_id"]:
            msgs = await pool.fetch("""
                SELECT role, content, sender, created_at FROM messages
                WHERE thread_id = $1 ORDER BY created_at DESC LIMIT 10
            """, task["thread_id"])
            detail["messages"] = [dict(m) for m in msgs]

        return detail

    elif source == "ledger":
        event = await pool.fetchrow(
            "SELECT * FROM ledger_events WHERE event_id = $1::uuid", item_id,
        )
        if not event:
            return None

        detail = {"source": "ledger", "event": dict(event)}
        detail["actor"] = await resolve_actor_name(pool, event["actor_id"])

        if event["context_task_id"]:
            task = await pool.fetchrow(
                "SELECT * FROM tasks WHERE id = $1", event["context_task_id"],
            )
            if task:
                detail["task"] = dict(task)

        return detail

    return None
