"""Global concurrency ceiling for claude-code story DAGs.

A **DAG** here = one claude-code story cycle: a parent task whose children are
`executor='claude-code-phase'` rows (their `parent_task_id` is the DAG root).
This module caps how many DAGs execute concurrently at `TASK_DAG_CEILING` (N),
admitting waiting DAGs FIFO. Full design + rationale: TASK_DAG_CEILING_PLAN.md.

Classification is DERIVED from phase-task states (no stored queue table, so
nothing can drift/leak):

  * TERMINAL phase = done/approved/cancelled/halted/failed/rejected.
  * A DAG is EXECUTING (holds a slot) iff it has >=1 non-terminal phase AND
    >=1 phase that has already started (state not in ready/pending). This
    covers running, `blocked` (holds the slot, by design), and between-phases —
    downstream phases sit `pending` during interleaved gates, so a DAG never
    falls out of the set mid-cycle.
  * A DAG is WAITING iff it has non-terminal phases but NONE has started.
  * A DAG frees its slot automatically when all phases go terminal (parent →
    awaiting_approval) or it fails/cancels — it simply stops matching EXECUTING.

Rollout is shadow-first. With `task_dag_ceiling_enforce=False` (default) this
module ONLY logs what admission WOULD do — dispatch is unchanged. Enforcement
(actually deferring over-ceiling DAGs, under an advisory lock) lands in a later
step once the shadow logs validate on dev.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from app.config import get_settings
from app.database import get_pool

_log = logging.getLogger(__name__)

# Phase states that no longer consume (or will consume) execution.
TERMINAL_PHASE_STATES: tuple[str, ...] = (
    "done", "approved", "cancelled", "halted", "failed", "rejected",
)
# A phase in these states has never been dispatched (DAG hasn't "started").
_NOT_STARTED_STATES: tuple[str, ...] = ("ready", "pending")

# Fixed key for pg_advisory_xact_lock during admission (enforcement step). A
# constant so every replica contends on the same lock. (Unused until enforce.)
ADMISSION_LOCK_KEY: int = 0x0DA6_CE11  # "dag ceil"

# Throttle for the idle heartbeat log (seconds).
_HEARTBEAT_INTERVAL_S = 60.0
_last_heartbeat_at: float = 0.0

# ── classification queries (current-cycle scoped via the HAVING clauses) ──────
# Old / replanned cycles fall out automatically: all their phases are terminal,
# so the "has >=1 non-terminal phase" HAVING fails.

_EXECUTING_SQL = """
    SELECT p.parent_task_id                                    AS dag_id,
           max(p.story_id)                                     AS story_id,
           max(p.goal_id)                                      AS goal_id,
           count(*)                                            AS total_phases,
           count(*) FILTER (WHERE p.state = 'done')            AS done_phases,
           bool_or(p.state = 'blocked')                        AS has_blocked
      FROM tasks p
     WHERE p.executor = 'claude-code-phase'
       AND p.parent_task_id IS NOT NULL
     GROUP BY p.parent_task_id
    HAVING count(*) FILTER (WHERE p.state <> ALL($1::text[])) >= 1   -- work remaining
       AND count(*) FILTER (WHERE p.state <> ALL($2::text[])) >= 1   -- has started
"""

_WAITING_SQL = """
    SELECT p.parent_task_id                                    AS dag_id,
           max(p.story_id)                                     AS story_id,
           max(p.goal_id)                                      AS goal_id,
           count(*)                                            AS total_phases,
           min(p.created_at)                                   AS queued_since
      FROM tasks p
     WHERE p.executor = 'claude-code-phase'
       AND p.parent_task_id IS NOT NULL
     GROUP BY p.parent_task_id
    HAVING count(*) FILTER (WHERE p.state <> ALL($1::text[])) >= 1   -- work remaining
       AND count(*) FILTER (WHERE p.state <> ALL($2::text[])) = 0    -- nothing started
     ORDER BY min(p.created_at) ASC
"""

# Count of EXECUTING DAGs — the authoritative ceiling number, re-read under the
# admission advisory lock so multi-replica admits can't overshoot N.
_COUNT_EXECUTING_SQL = """
    SELECT count(*) FROM (
        SELECT p.parent_task_id
          FROM tasks p
         WHERE p.executor = 'claude-code-phase'
           AND p.parent_task_id IS NOT NULL
         GROUP BY p.parent_task_id
        HAVING count(*) FILTER (WHERE p.state <> ALL($1::text[])) >= 1
           AND count(*) FILTER (WHERE p.state <> ALL($2::text[])) >= 1
    ) x
"""

# Atomically select + CAS-claim ONE ready phase belonging to one of the given
# DAGs, honoring the same single-flight / goal-active / pause guards as
# phase_dispatcher.find_next_dispatchable_task. Returns the claimed row (now
# state='dispatching', dispatched_at set — identical to the existing claim at
# phase_dispatcher.py:506, so the stale-dispatch reaper covers a crash the same
# way). $1 = candidate parent_task_ids.
_CLAIM_READY_PHASE_SQL = """
    WITH busy_stories AS (
        SELECT DISTINCT story_id FROM tasks
         WHERE story_id IS NOT NULL
           AND (
               (executor = 'claude-code-phase'
                AND state IN ('in_progress', 'dispatching', 'blocked'))
               OR
               (executor = 'system-gate'
                AND state IN ('in_progress', 'dispatching', 'awaiting_approval'))
           )
    ),
    cand AS (
        SELECT t.id
          FROM tasks t
          JOIN stories s ON s.id = t.story_id
         WHERE t.executor = 'claude-code-phase'
           AND t.state = 'ready'
           AND t.story_id IS NOT NULL
           AND t.parent_task_id = ANY($1::text[])
           AND EXISTS (SELECT 1 FROM goals g
                        WHERE g.id = t.goal_id AND g.status = 'active')
           AND t.story_id NOT IN (SELECT story_id FROM busy_stories)
           AND s.execution_paused = FALSE
           AND NOT EXISTS (SELECT 1 FROM execution_pauses ep
                            WHERE ep.scope = 'goal' AND ep.scope_id = t.goal_id)
         ORDER BY t.created_at ASC
         LIMIT 1
    )
    UPDATE tasks t
       SET state = 'dispatching', dispatched_at = NOW(), updated_at = NOW()
      FROM cand
     WHERE t.id = cand.id AND t.state = 'ready'
    RETURNING t.*
"""


async def get_executing_dags() -> list[dict]:
    """Every claude-code DAG currently holding a slot (see module docstring)."""
    rows = await get_pool().fetch(
        _EXECUTING_SQL, list(TERMINAL_PHASE_STATES), list(_NOT_STARTED_STATES),
    )
    return [dict(r) for r in rows]


async def get_waiting_dags() -> list[dict]:
    """Claude-code DAGs with runnable phases that haven't started yet, FIFO."""
    rows = await get_pool().fetch(
        _WAITING_SQL, list(TERMINAL_PHASE_STATES), list(_NOT_STARTED_STATES),
    )
    return [dict(r) for r in rows]


def _fmt_dag(d: dict) -> str:
    """Compact one-DAG label for logs: dag=<id8> story=<id8> goal=<id>."""
    dag = (d.get("dag_id") or "?")[:8]
    story = (d.get("story_id") or "-")[:8]
    goal = d.get("goal_id") or "-"
    extra = ""
    if "done_phases" in d:
        extra = f" phases={d.get('done_phases')}/{d.get('total_phases')}"
        if d.get("has_blocked"):
            extra += " BLOCKED-holding"
    return f"dag={dag} story={story} goal={goal}{extra}"


async def get_ceiling_config() -> tuple[int, bool]:
    """Live (N, enforce), read from dag_ceiling_config each tick so UI changes
    take effect immediately. Falls back to the env defaults (config.py) if the
    row/table is missing."""
    settings = get_settings()
    try:
        row = await get_pool().fetchrow(
            "SELECT n, enforce FROM dag_ceiling_config WHERE id = 'singleton'")
        if row is not None:
            return int(row["n"]), bool(row["enforce"])
    except Exception:
        _log.warning("dag_scheduler: config read failed; using env defaults", exc_info=True)
    return settings.task_dag_ceiling, settings.task_dag_ceiling_enforce


async def _count_executing(conn) -> int:
    """Authoritative executing-DAG count. Call with the advisory-locked conn."""
    return int(await conn.fetchval(
        _COUNT_EXECUTING_SQL, list(TERMINAL_PHASE_STATES), list(_NOT_STARTED_STATES),
    ) or 0)


async def _claim_ready_phase(conn, dag_ids: list[str]) -> Optional[dict]:
    """CAS-claim one ready phase of the given DAGs (→ 'dispatching'), or None.
    `conn` may be the pool (continue tier) or a locked connection (admit tier)."""
    if not dag_ids:
        return None
    row = await conn.fetchrow(_CLAIM_READY_PHASE_SQL, dag_ids)
    return dict(row) if row else None


async def _maybe_heartbeat(executing: list[dict], n: int) -> None:
    """Throttled queue-state log, shared by the shadow and enforce paths."""
    global _last_heartbeat_at
    now = time.monotonic()
    if now - _last_heartbeat_at < _HEARTBEAT_INTERVAL_S:
        return
    _last_heartbeat_at = now
    try:
        waiting = await get_waiting_dags()
    except Exception:
        waiting = []
    blocked_holders = sum(1 for d in executing if d.get("has_blocked"))
    k = len({d["dag_id"] for d in executing})
    _log.info(
        "dag_scheduler: executing=%d/%d waiting=%d blocked-holding=%d | "
        "EXECUTING[%s] WAITING[%s]",
        k, n, len(waiting), blocked_holders,
        " ; ".join(_fmt_dag(d) for d in executing) or "-",
        " ; ".join(_fmt_dag(d) for d in waiting) or "-",
    )


async def admit_and_claim_next_phase() -> Optional[dict]:
    """ENFORCE path. Returns a phase task ALREADY claimed to 'dispatching' (the
    caller spawns it via dispatch_phase_task(..., already_claimed=True)), or None
    to dispatch nothing this tick.

    Continue tier (lock-free): keep already-executing DAGs progressing first —
    the CAS in the claim query prevents double-dispatch, so no lock is needed.
    Admit tier (advisory-locked): admit the oldest WAITING DAG only if the
    executing count is under N. Count + claim happen under ONE xact lock so
    concurrent replicas cannot exceed N.
    """
    pool = get_pool()
    n, _enforce = await get_ceiling_config()
    executing = await get_executing_dags()
    exec_ids = [d["dag_id"] for d in executing]

    # 1. Continue tier.
    task = await _claim_ready_phase(pool, exec_ids)
    if task:
        _log.info("dag_scheduler: CONTINUE phase T%s dag=%s (%d/%d)",
                  str(task["id"])[:8], str(task.get("parent_task_id"))[:8], len(exec_ids), n)
        await _maybe_heartbeat(executing, n)
        return task

    await _maybe_heartbeat(executing, n)

    # 2. Admit tier.
    if len(exec_ids) >= n:
        return None
    waiting = await get_waiting_dags()
    if not waiting:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADMISSION_LOCK_KEY)
            cnt = await _count_executing(conn)   # authoritative under the lock
            if cnt >= n:
                _log.info("dag_scheduler: DEFER — ceiling %d reached (under lock)", n)
                return None
            for w in waiting:                    # oldest first (FIFO)
                task = await _claim_ready_phase(conn, [w["dag_id"]])
                if task:
                    _log.info("dag_scheduler: ADMIT dag=%s story=%s phase T%s (now %d/%d)",
                              str(w["dag_id"])[:8], str(w.get("story_id"))[:8],
                              str(task["id"])[:8], cnt + 1, n)
                    return task
    return None


async def shadow_review(next_phase_task: Optional[dict]) -> None:
    """SHADOW-mode observability. Called once per tick with whatever
    `find_next_dispatchable_task()` selected (or None). Logs what the ceiling
    WOULD do — never changes dispatch. Safe no-op semantics.

    Two log streams:
      * per-dispatch decision (when a phase is being dispatched this tick):
        CONTINUE (DAG already executing) / ADMIT (new DAG, slot free) /
        WOULD-DEFER (new DAG, ceiling reached — under enforcement this dispatch
        would be held).
      * throttled heartbeat of the executing (k/N) + waiting queue.
    """
    # Skip all DB work on idle ticks (no dispatch this tick AND heartbeat not due),
    # so shadow observability adds ~nothing when the system is quiet.
    now = time.monotonic()
    heartbeat_due = (now - _last_heartbeat_at) >= _HEARTBEAT_INTERVAL_S
    if next_phase_task is None and not heartbeat_due:
        return

    n, _enforce = await get_ceiling_config()
    try:
        executing = await get_executing_dags()
    except Exception:
        # Never let observability break the dispatch tick.
        _log.warning("dag_scheduler[shadow]: executing query failed", exc_info=True)
        return

    executing_ids = {d["dag_id"] for d in executing}
    k = len(executing_ids)

    # Per-dispatch decision.
    if next_phase_task is not None:
        dag_id = next_phase_task.get("parent_task_id")
        label = (
            f"phase T{str(next_phase_task.get('id'))[:8]} "
            f"dag={str(dag_id)[:8]} story={str(next_phase_task.get('story_id'))[:8]} "
            f"phase={next_phase_task.get('phase')}"
        )
        if dag_id in executing_ids:
            _log.info("dag_scheduler[shadow]: %s -> CONTINUE (DAG already executing; %d/%d slots)",
                      label, k, n)
        elif k < n:
            _log.info("dag_scheduler[shadow]: %s -> ADMIT (new DAG; slot free %d/%d)",
                      label, k, n)
        else:
            _log.warning(
                "dag_scheduler[shadow]: %s -> WOULD-DEFER (ceiling %d reached). "
                "Under enforcement this dispatch would be held. executing=[%s]",
                label, n, ", ".join(_fmt_dag(d) for d in executing),
            )

    # Throttled queue heartbeat (shared with the enforce path).
    await _maybe_heartbeat(executing, n)
