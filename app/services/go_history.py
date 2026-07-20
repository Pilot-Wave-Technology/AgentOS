"""GO goal-summary manager — one living "state of the goal" summary per goal.

The GO agent's OpenClaw session can lose context when it idles out or restarts.
This module maintains a single rolling summary per goal so GO (and humans)
always know the current state of the goal, even across sessions.

How it works:
1. After every GO_SUMMARY_MSG_THRESHOLD GO-sent messages, Gemini Flash
   reconciles the messages since the last checkpoint + the authoritative story
   list into the previous summary (read-modify-write, <=2000 tokens).
2. The single summary is stored (one row per goal) in the goal_summary table.
3. On every GO trigger, inject: the goal summary + raw messages since checkpoint.

The legacy chunked summarizer and the go_conversation_summaries table are
retired; the table is left in place (migration rules) but no longer written.
"""
import asyncio
import logging
from datetime import datetime

from app.database import get_pool
from app.gemini_client import reconcile_goal_summary

_log = logging.getLogger(__name__)

# ── Rolling per-goal summary (migration 076) ────────────────────────────────
# One living summary per goal, refreshed every GO_SUMMARY_MSG_THRESHOLD
# GO-sent messages. Each run folds all thread messages since the stored
# checkpoint (GO + user + system) plus the authoritative story list into the
# previous summary, then advances the checkpoint.
GO_SUMMARY_MSG_THRESHOLD = 10        # refresh after every N GO-sent messages
GO_SUMMARY_CHAR_CAP = 8000           # ~2000 tokens; over this we log a warning
_MSG_TRIM_CHARS = 1500               # per-message trim fed to the model

# Per-goal in-process locks so two rapid GO replies don't read-modify-write
# over each other and lose an update.
_goal_summary_locks: dict[str, asyncio.Lock] = {}


def _goal_summary_lock(goal_id: str) -> asyncio.Lock:
    lock = _goal_summary_locks.get(goal_id)
    if lock is None:
        lock = asyncio.Lock()
        _goal_summary_locks[goal_id] = lock
    return lock


async def build_history_context(goal_id: str) -> str:
    """Build the conversation history block for GO's context.

    Sources the single rolling goal_summary (the state of the goal so far) plus
    any raw messages newer than the summary's checkpoint, so GO sees the
    distilled state + the latest unsummarized turns.
    """
    pool = get_pool()

    go_thread_id = await pool.fetchval(
        "SELECT go_thread_id FROM goals WHERE id = $1", goal_id,
    )
    if not go_thread_id:
        return ""

    # 1. The single rolling goal summary + its checkpoint.
    row = await pool.fetchrow(
        "SELECT summary, checkpoint_at FROM goal_summary WHERE goal_id = $1",
        goal_id,
    )
    summary = (row["summary"].strip() if row and row["summary"] else "")
    since = (row["checkpoint_at"] if row and row["checkpoint_at"] else datetime.min)

    # 2. Raw messages not yet folded into the summary (the live tail).
    recent = await pool.fetch("""
        SELECT m.sender, m.role, m.content, m.created_at,
               COALESCE(u.display_name, m.sender) AS sender_name
        FROM messages m
        LEFT JOIN users u ON u.uid = m.sender
        WHERE m.thread_id = $1 AND m.created_at > $2
        ORDER BY m.created_at DESC LIMIT 30
    """, go_thread_id, since)

    # Reverse to chronological order
    recent = list(reversed(recent))

    # Build the context block
    parts = []

    if summary:
        parts.append("## Goal Summary (state so far)\n")
        parts.append(summary + "\n")

    if recent:
        parts.append("## Recent Messages (since last summary)\n")
        for m in recent:
            ts = m["created_at"].strftime("%H:%M") if m["created_at"] else ""
            name = m["sender_name"] or m["sender"]
            content = m["content"][:500]
            parts.append(f"[{name} at {ts}]: {content}")

    return "\n".join(parts)


# ── Rolling per-goal summary ─────────────────────────────────────────────────

# Fallbacks used only if the DB prompt rows (migration 076) are missing.
_DEFAULT_GOAL_SUMMARY_SYSTEM = (
    "You maintain a single living summary of a project goal for its "
    "orchestrator agent. You are given the existing summary, the new thread "
    "activity since the last checkpoint, and the authoritative current list of "
    "stories (title + state). Return the UPDATED summary. Reflect only what the "
    "inputs state — never invent facts. The story list is authoritative for "
    "which stories exist and their state."
)

_DEFAULT_GOAL_SUMMARY_USER = """You maintain ONE running summary of everything that has happened in this goal so far. It must always reflect the goal's CURRENT state — not a history log.

=== CURRENT SUMMARY (as of the last checkpoint) ===
{current_summary}

=== NEW ACTIVITY since the last checkpoint (chronological — GO, user, and system messages) ===
{new_activity}

=== CURRENT STORIES (authoritative, from the database — each line: <story_id> | <title> [<state>]) ===
{story_snapshot}

Rewrite the summary by INTEGRATING the new activity into the current summary (an UPDATE, not an append). Use the CURRENT STORIES list as the source of truth for which stories exist and their state; drop anything no longer present. ALWAYS start each story line in Completed Stories and Remaining Work with its story id (from the list) for traceability. Carry forward what is still true; correct what changed. Do not write a changelog.

Output under EXACTLY these four headings, in order (keep every heading, write "—" if empty):

## Current Scope
## What the Users Want to Accomplish
## Completed Stories
## Remaining Work

HARD LIMIT: under 2000 tokens (~7500 chars). Output ONLY the four sections."""


async def _goal_summary_prompt(kind: str, default: str) -> str:
    """Load an active GO goal-summary prompt from memory_prompts, else fallback."""
    try:
        from app.memory_v2.prompts import get_active_prompt
        content = await get_active_prompt("GO", kind)
        return content or default
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("goal_summary prompt load failed (%s): %s", kind, exc)
        return default


async def maybe_update_goal_summary(goal_id: str):
    """Refresh the single rolling summary for a goal if enough GO messages.

    Fired (non-blocking) after each GO reply. Gates on GO_SUMMARY_MSG_THRESHOLD
    GO-sent messages since the stored checkpoint; when reached, folds every
    thread message since the checkpoint plus the live story list into the
    previous summary and advances the checkpoint. One row per goal.
    """
    pool = get_pool()

    go_thread_id = await pool.fetchval(
        "SELECT go_thread_id FROM goals WHERE id = $1", goal_id,
    )
    if not go_thread_id:
        return

    row = await pool.fetchrow(
        "SELECT summary, checkpoint_at, version FROM goal_summary WHERE goal_id = $1",
        goal_id,
    )
    checkpoint = (row["checkpoint_at"] if row and row["checkpoint_at"] else datetime.min)

    # Gate: count GO-sent (role='agent') messages on the goal thread since the
    # checkpoint. Only GO's own messages advance the 10-clock.
    go_msgs = await pool.fetchval(
        """SELECT COUNT(*) FROM messages
            WHERE thread_id = $1 AND role = 'agent' AND created_at > $2""",
        go_thread_id, checkpoint,
    )
    if go_msgs < GO_SUMMARY_MSG_THRESHOLD:
        return

    lock = _goal_summary_lock(goal_id)
    if lock.locked():
        return  # an update is already running for this goal
    async with lock:
        # Re-read inside the lock — another run may have advanced the checkpoint.
        row = await pool.fetchrow(
            "SELECT summary, checkpoint_at, version FROM goal_summary WHERE goal_id = $1",
            goal_id,
        )
        checkpoint = (row["checkpoint_at"] if row and row["checkpoint_at"] else datetime.min)
        prev_summary = (row["summary"].strip() if row and row["summary"] else "")
        version = row["version"] if row else 0

        # All thread messages since checkpoint: GO + user + system.
        msgs = await pool.fetch(
            """SELECT m.role, m.kind, m.content, m.created_at,
                      COALESCE(u.display_name, m.sender) AS sender_name
                 FROM messages m
                 LEFT JOIN users u ON u.uid = m.sender
                WHERE m.thread_id = $1 AND m.created_at > $2
                ORDER BY m.created_at""",
            go_thread_id, checkpoint,
        )
        if not msgs:
            return

        # Authoritative story snapshot (id + title + state). The id is fed in
        # so the model can tag each story in the summary for traceability.
        stories = await pool.fetch(
            "SELECT id, title, state FROM stories WHERE goal_id = $1 ORDER BY created_at",
            goal_id,
        )

        activity_lines = []
        for m in msgs:
            ts = m["created_at"].strftime("%Y-%m-%d %H:%M") if m["created_at"] else ""
            tag = m["role"] if m["kind"] == "user" else f"{m['role']}/{m['kind']}"
            activity_lines.append(
                f"[{ts} {m['sender_name']} ({tag})] {(m['content'] or '')[:_MSG_TRIM_CHARS]}"
            )
        new_activity = "\n".join(activity_lines)

        story_snapshot = (
            "\n".join(f"- {s['id']} | {s['title']} [{s['state']}]" for s in stories)
            if stories else "(no stories created yet)"
        )

        current_summary = prev_summary or "(none yet — this is the first summary for this goal.)"

        system_prompt = await _goal_summary_prompt(
            "goal_summary_system_prompt", _DEFAULT_GOAL_SUMMARY_SYSTEM,
        )
        user_tmpl = await _goal_summary_prompt(
            "goal_summary_user_prompt", _DEFAULT_GOAL_SUMMARY_USER,
        )
        user_prompt = (
            user_tmpl
            .replace("{current_summary}", current_summary)
            .replace("{new_activity}", new_activity)
            .replace("{story_snapshot}", story_snapshot)
        )

        new_summary = await reconcile_goal_summary(system_prompt, user_prompt)
        if not new_summary:
            _log.warning("goal_summary: empty reconciliation result for goal %s", goal_id)
            return  # leave prior summary + checkpoint intact

        if len(new_summary) > GO_SUMMARY_CHAR_CAP:
            _log.warning(
                "goal_summary: %s exceeded cap (%d chars > %d)",
                goal_id, len(new_summary), GO_SUMMARY_CHAR_CAP,
            )

        new_checkpoint = msgs[-1]["created_at"]
        await pool.execute(
            """INSERT INTO goal_summary (goal_id, summary, checkpoint_at, version, updated_at)
               VALUES ($1, $2, $3, $4, NOW())
               ON CONFLICT (goal_id) DO UPDATE
                 SET summary = EXCLUDED.summary,
                     checkpoint_at = EXCLUDED.checkpoint_at,
                     version = goal_summary.version + 1,
                     updated_at = NOW()""",
            goal_id, new_summary, new_checkpoint, version + 1,
        )
        _log.info(
            "goal_summary updated for %s: folded %d msgs + %d stories -> %d chars (v%d)",
            goal_id, len(msgs), len(stories), len(new_summary), version + 1,
        )
