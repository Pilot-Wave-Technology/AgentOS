"""Deterministic events → memory/<date>.md bridge.

The control panel subscribes to ledger events. On whitelisted events,
this module formats a one-line `EVENT [HH:MM] event_type: details` entry
and calls the openclaw `memory.append` RPC to append it to the target
agent's daily memory file.

The audit row is recorded in `memory_writes` alongside, so even if the
markdown file is later destroyed, we have a forensic trail.

Mid-session caveat: writes here are visible to the live agent only via
`memory_search`/`memory_get` (bootstrap is cached). For events the user
just triggered (e.g. diff approve), the agent typically saw the action
in chat anyway, so this lag is acceptable.

Feature-flagged via settings.memory_v2_bridge_enabled (default False).
Flip to True only after the openclaw memory.append RPC is deployed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import get_settings
from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError
from app.memory_v2.audit import record_memory_write


_log = logging.getLogger(__name__)


# ── Event whitelist + per-agent-type targeting ──────────────────────────────
#
# Per memory_design/deterministic_events.md. For each event we list:
#   - "shca": True if the user's SHCA for the affected story should get the write
#   - "go":   True if the goal's GO should get the write

_EVENT_WHITELIST: dict[str, dict[str, bool]] = {
    # Story lifecycle (terminal transitions only — we skip phase noise)
    "story.created":              {"shca": False, "go": True},
    "story.state_changed":        {"shca": True,  "go": True},   # filtered to terminal in formatter
    "story.phase_changed":        {"shca": True,  "go": False},  # filtered to major phases in formatter

    # Plan + diff outcomes
    "plan.proposed":              {"shca": False, "go": False},  # noisy
    "plan.approved":              {"shca": True,  "go": True},
    "plan.rejected":              {"shca": True,  "go": True},
    "diff.approved_and_pushed":   {"shca": True,  "go": True},
    "diff.approved_but_push_failed": {"shca": True, "go": True},
    "diff.rejected":              {"shca": True,  "go": True},

    # Escalations + approvals
    "escalation.requested":       {"shca": True,  "go": False},
    "escalation.timed_out":       {"shca": True,  "go": False},

    # Goal-level changes
    "goal.member_added":          {"shca": False, "go": True},
    "goal.member_removed":        {"shca": False, "go": True},
    "goal.resource_added":        {"shca": False, "go": True},
}

# Story phases worth surfacing in memory (skip discovery→planning noise).
_INTERESTING_PHASES = frozenset({
    "executing", "awaiting_review", "awaiting_next", "done",
})

# Story states worth surfacing (terminal only — in-flight transitions are noise).
_INTERESTING_TERMINAL_STATES = frozenset({"done", "cancelled", "rejected"})


# ── Public API ──────────────────────────────────────────────────────────────

def is_enabled() -> bool:
    return bool(getattr(get_settings(), "memory_v2_bridge_enabled", False))


async def maybe_handle_ledger_event(event: dict[str, Any]) -> None:
    """Called from ledger.journal.append_event for every event. Filters
    to the whitelist, formats a line, fans out to the relevant agents.

    Non-fatal: if anything fails, log and return — never block ledger writes.
    """
    if not is_enabled():
        return

    event_type = event.get("event_type") or ""
    targets = _EVENT_WHITELIST.get(event_type)
    if not targets:
        return

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return

    # Apply per-event filtering rules
    if event_type == "story.state_changed":
        if (payload.get("to_state") or "") not in _INTERESTING_TERMINAL_STATES:
            return
    elif event_type == "story.phase_changed":
        if (payload.get("to_phase") or "") not in _INTERESTING_PHASES:
            return

    line = _format_event_line(event_type, payload)
    if not line:
        return

    goal_id = (event.get("context_goal_id") or "").strip() or None
    story_id = (payload.get("story_id") or "").strip() or None

    # Initialize BEFORE the try so they're always bound even if the
    # resolver raises — story-complete flush below references them.
    shca_targets: list[str] = []
    go_target: Optional[str] = None

    try:
        # Resolve target agents
        shca_targets, go_target = await _resolve_targets(
            goal_id=goal_id, story_id=story_id, targets=targets,
        )
        for agent_id in shca_targets:
            await _append_to_agent(
                agent_id=agent_id, agent_type="SHCA",
                event_type=event_type, line=line,
                goal_id=goal_id, story_id=story_id,
            )
        if go_target:
            await _append_to_agent(
                agent_id=go_target, agent_type="GO",
                event_type=event_type, line=line,
                goal_id=goal_id, story_id=story_id,
            )
    except Exception:
        _log.exception(
            "event_bridge: failed to fan out event_type=%s payload=%r",
            event_type, payload,
        )

    # Story-complete flush: when a story transitions to terminal state,
    # trigger a story_complete flush turn for the affected SHCA + GO.
    # This is separate from the EVENT-line append above (which is
    # data-only, no agent turn).
    if event_type == "story.state_changed":
        to_state = (payload.get("to_state") or "").lower()
        if to_state in _INTERESTING_TERMINAL_STATES and story_id:
            try:
                await _maybe_trigger_story_complete_flush(
                    goal_id=goal_id, story_id=story_id,
                    final_state=to_state,
                    shca_targets=shca_targets, go_target=go_target,
                )
            except Exception:
                _log.exception(
                    "event_bridge: story-complete flush failed for story=%s",
                    story_id,
                )


async def _maybe_trigger_story_complete_flush(
    *, goal_id: Optional[str], story_id: str, final_state: str,
    shca_targets: list[str], go_target: Optional[str],
) -> None:
    """Fire story_complete flush turns for SHCA + GO. Best-effort; one
    flush per agent. Skips agents with no live session (per flush.py)."""
    from app.memory_v2 import flush as _flush
    if not _flush.is_enabled():
        return

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT title, active_thread_id FROM stories WHERE id = $1",
        story_id,
    )
    if not story:
        return
    story_title = story["title"] or ""

    # Build the list of flushes to fire — SHCA(s) + GO. Each entry is
    # a coroutine awaiting its own gateway admin RPC. Fire them
    # concurrently with asyncio.gather so a slow SHCA (e.g. mid-
    # compaction; admin RPC can wait up to 6 min) does not delay the
    # GO flush, and vice versa. return_exceptions=True so one
    # failure doesn't cancel the others.
    flush_coros = []

    # SHCA targets — resolve template_id + thread_id from gateway id.
    # The CP-side gateway_admin RPC accepts template id; we resolve in
    # send_silent_flush. (Bug observed 2026-05-25 on SUM02 where passing
    # gateway instance id missed the session_manager._sessions key.)
    for gw_agent_id in shca_targets:
        row = await pool.fetchrow(
            """SELECT t.id AS thread_id, ai.template_id
                FROM threads t
                JOIN agent_instances ai ON ai.thread_id = t.id
                                       AND ai.gateway_agent_id = $1
                                       AND ai.status = 'active'
                LIMIT 1""",
            gw_agent_id,
        )
        if not row or not row["thread_id"] or not row["template_id"]:
            continue
        flush_coros.append(_flush.trigger_story_complete_flush(
            agent_id=row["template_id"],
            agent_type="SHCA",
            thread_id=row["thread_id"], goal_id=goal_id or "",
            story_id=story_id, story_title=story_title,
            final_state=final_state,
        ))

    # GO target — use goals.go_thread_id as canonical source of truth
    # (set at goal creation; same column goal_thread.py:210 reads).
    if go_target and goal_id:
        go_thread = await pool.fetchval(
            "SELECT go_thread_id FROM goals WHERE id = $1",
            goal_id,
        )
        if go_thread:
            flush_coros.append(_flush.trigger_story_complete_flush(
                agent_id=go_target, agent_type="GO",
                thread_id=go_thread, goal_id=goal_id,
                story_id=story_id, story_title=story_title,
                final_state=final_state,
            ))
        else:
            _log.warning(
                "event_bridge: goals.go_thread_id is null for goal=%s "
                "(GO target=%s, story=%s); story_complete flush dropped.",
                goal_id, go_target, story_id,
            )

    if flush_coros:
        results = await asyncio.gather(*flush_coros, return_exceptions=True)
        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                _log.exception(
                    "event_bridge: story_complete flush[%d] raised: %s",
                    idx, res,
                )


# ── Internal helpers ────────────────────────────────────────────────────────

def _format_event_line(event_type: str, payload: dict) -> str:
    """One-line formatter. Returns the full text to append (incl. newline)."""
    now = datetime.now(timezone.utc).strftime("%H:%M")
    sid = payload.get("story_id", "?")

    if event_type == "story.created":
        body = f'new story {sid}: "{payload.get("title", "")}" (assigned: {payload.get("assigned_user_id", "?")})'
    elif event_type == "story.state_changed":
        body = f'story {sid} "{payload.get("title", "")}": {payload.get("from_state", "?")} → {payload.get("to_state", "?")}'
    elif event_type == "story.phase_changed":
        body = f'story {sid}: phase {payload.get("from_phase", "?")} → {payload.get("to_phase", "?")}'
    elif event_type == "plan.approved":
        body = f'plan v{payload.get("version", "?")} approved for story {sid} ({payload.get("task_count", "?")} tasks)'
    elif event_type == "plan.rejected":
        body = f'plan v{payload.get("version", "?")} rejected for story {sid}; feedback: {(payload.get("feedback") or "")[:80]}'
    elif event_type == "diff.approved_and_pushed":
        body = f'diff approved for story {sid}: commit {(payload.get("commit_sha") or "")[:12]}'
    elif event_type == "diff.approved_but_push_failed":
        body = f'diff approved for story {sid} but push failed: {(payload.get("push_error") or "")[:120]}'
    elif event_type == "diff.rejected":
        body = f'diff rejected for story {sid}; feedback: {(payload.get("feedback") or "")[:80]}'
    elif event_type == "escalation.requested":
        body = f'escalation requested: {payload.get("kind", "?")} for story {sid}'
    elif event_type == "escalation.timed_out":
        body = f'escalation timed out: {payload.get("kind", "?")} for story {sid}'
    elif event_type == "goal.member_added":
        body = f'{payload.get("uid", "?")} joined this goal as {payload.get("role", "?")}'
    elif event_type == "goal.member_removed":
        body = f'{payload.get("uid", "?")} left this goal'
    elif event_type == "goal.resource_added":
        body = f'resource added: {payload.get("type", "?")} "{payload.get("name", "")}"'
    else:
        body = f'{event_type}: {payload}'

    return f"EVENT [{now}] {event_type}: {body}\n"


async def _resolve_targets(
    *, goal_id: Optional[str], story_id: Optional[str],
    targets: dict[str, bool],
) -> tuple[list[str], Optional[str]]:
    """Resolve which SHCA agent_ids and which GO agent_id should receive
    this event. Returns (list_of_shca_ids, go_id_or_None).
    """
    pool = get_pool()
    shca_ids: list[str] = []
    go_id: Optional[str] = None

    # ── GO target ───────────────────────────────────────────────────
    if targets.get("go") and goal_id:
        try:
            go_id = await pool.fetchval(
                """SELECT id FROM agents
                    WHERE type = 'GO' AND status = 'active'
                      AND id = $1""",
                f"go-{goal_id.lower()}",
            )
        except Exception:
            _log.exception("event_bridge: GO resolution failed (goal=%s)", goal_id)
            go_id = None

    # ── SHCA targets ────────────────────────────────────────────────
    # Schema:
    #   `agents` holds the per-user SHCA TEMPLATE (id like 'abhishek-shca',
    #     owner_actor_id → users.actor_id, type='SHCA')
    #   `agent_instances` holds the per-(goal, thread) INSTANCE (
    #     gateway_agent_id like 'abhishek-shca-tesg6-<uuid>',
    #     template_id → agents.id, goal_id, status='active'|'destroyed')
    #
    # memory.append needs the gateway_agent_id, not the template id, so
    # we join through agent_instances. Multiple instances per (user, goal)
    # may exist (per-thread); fan out to all active ones — each writes to
    # its own workspace memory file.
    #
    # Isolated try/except so SHCA failure doesn't take down GO targeting.
    if targets.get("shca"):
        try:
            if story_id:
                rows = await pool.fetch(
                    """SELECT DISTINCT ai.gateway_agent_id
                         FROM stories s
                         JOIN users u ON u.uid = s.assigned_to
                         JOIN agents a ON a.owner_actor_id = u.actor_id
                                       AND a.type = 'SHCA'
                         JOIN agent_instances ai
                              ON ai.template_id = a.id
                              AND LOWER(ai.goal_id) = LOWER(s.goal_id)
                              AND ai.status = 'active'
                        WHERE s.id = $1""",
                    story_id,
                )
                shca_ids = [r["gateway_agent_id"] for r in rows]
            elif goal_id:
                rows = await pool.fetch(
                    """SELECT DISTINCT ai.gateway_agent_id
                         FROM agent_instances ai
                         JOIN agents a ON a.id = ai.template_id
                                       AND a.type = 'SHCA'
                        WHERE LOWER(ai.goal_id) = LOWER($1)
                          AND ai.status = 'active'""",
                    goal_id,
                )
                shca_ids = [r["gateway_agent_id"] for r in rows]
        except Exception:
            _log.exception(
                "event_bridge: SHCA resolution failed (goal=%s story=%s) — "
                "GO fanout will still proceed",
                goal_id, story_id,
            )
            shca_ids = []

    return shca_ids, go_id


def _today_filename() -> str:
    return f"memory/{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"


async def _append_to_agent(
    *, agent_id: str, agent_type: str, event_type: str,
    line: str, goal_id: Optional[str], story_id: Optional[str],
) -> None:
    """Append `line` to the agent's today daily-memory file + record audit row."""
    filename = _today_filename()
    try:
        result = await gateway_admin.call(
            "memory.append",
            {"agentId": agent_id, "filename": filename, "content": line},
            timeout=15,
        )
        bytes_added = int(result.get("bytes_appended") or 0)
    except GatewayError as exc:
        _log.warning(
            "event_bridge: memory.append failed for agent=%s file=%s: %s",
            agent_id, filename, exc,
        )
        return
    except Exception:
        _log.exception(
            "event_bridge: memory.append raised for agent=%s file=%s",
            agent_id, filename,
        )
        return

    await record_memory_write(
        agent_id=agent_id, agent_type=agent_type,
        file_path=filename, written_by="bridge",
        bytes_added=bytes_added, content=line,
        goal_id=goal_id, story_id=story_id,
        event_type=event_type,
    )
