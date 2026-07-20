"""Memory writes audit log.

Backs the `memory_writes` PG table (migration 063). Used by three write
sources:

  - The control-panel `event_bridge` (`written_by='bridge'`)
  - Agent writes captured by the operator workspace PostToolUse hook
    (`written_by='agent'`)
  - Admin edits via the future memory-editor UI (`written_by='admin'`)

Read API powers the admin "memory writes" view.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from app.database import get_pool


_log = logging.getLogger(__name__)

# Cap diff_summary length so a giant Write doesn't blow up the row.
_DIFF_SUMMARY_MAX_CHARS = 200


def _summarize(content: str) -> str:
    s = (content or "").strip()
    if not s:
        return ""
    if len(s) <= _DIFF_SUMMARY_MAX_CHARS:
        return s
    return s[: _DIFF_SUMMARY_MAX_CHARS - 1] + "…"


async def record_memory_write(
    *,
    agent_id: str,
    file_path: str,
    written_by: str,                       # 'agent' | 'bridge' | 'admin'
    bytes_added: int,
    content: Optional[str] = None,
    agent_type: Optional[str] = None,
    goal_id: Optional[str] = None,
    story_id: Optional[str] = None,
    actor_id: Optional[uuid.UUID] = None,
    event_type: Optional[str] = None,
) -> Optional[uuid.UUID]:
    """Insert one audit row. Returns row id, or None if write fails.

    Failure is non-fatal — the actual file write already happened
    (or is happening); the audit row is a side log. We never want an
    audit-table issue to break memory writes.
    """
    if written_by not in ("agent", "bridge", "admin"):
        _log.warning("record_memory_write: invalid written_by=%r — skipping", written_by)
        return None

    try:
        pool = get_pool()
        row_id = await pool.fetchval(
            """INSERT INTO memory_writes
                 (agent_id, agent_type, goal_id, story_id, file_path,
                  written_by, actor_id, bytes_added, diff_summary, event_type)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
               RETURNING id""",
            agent_id, agent_type, goal_id, story_id, file_path,
            written_by, actor_id, bytes_added,
            _summarize(content) if content is not None else None,
            event_type,
        )
        return row_id
    except Exception:
        _log.exception(
            "record_memory_write failed (agent=%s file=%s by=%s) — "
            "non-fatal; the actual write may still have succeeded",
            agent_id, file_path, written_by,
        )
        return None


# ── Read API (powers admin views) ────────────────────────────────────────────

async def recent_writes_for_agent(agent_id: str, limit: int = 100) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT id, agent_id, agent_type, goal_id, story_id, file_path,
                  written_by, actor_id, bytes_added, diff_summary,
                  event_type, written_at
             FROM memory_writes
            WHERE agent_id = $1
            ORDER BY written_at DESC
            LIMIT $2""",
        agent_id, limit,
    )
    return [dict(r) for r in rows]


async def recent_writes_for_goal(goal_id: str, limit: int = 100) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT id, agent_id, agent_type, goal_id, story_id, file_path,
                  written_by, actor_id, bytes_added, diff_summary,
                  event_type, written_at
             FROM memory_writes
            WHERE goal_id = $1
            ORDER BY written_at DESC
            LIMIT $2""",
        goal_id, limit,
    )
    return [dict(r) for r in rows]


async def stats_per_agent(since_days: int = 7) -> list[dict]:
    """How many writes per agent in the last N days, grouped by written_by.

    Useful for "is the memory system actually getting used" dashboards.
    """
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT agent_id, agent_type, written_by, COUNT(*) AS n,
                  SUM(bytes_added) AS bytes_total,
                  MAX(written_at) AS last_at
             FROM memory_writes
            WHERE written_at > NOW() - ($1 || ' days')::INTERVAL
            GROUP BY agent_id, agent_type, written_by
            ORDER BY n DESC""",
        str(since_days),
    )
    return [dict(r) for r in rows]
