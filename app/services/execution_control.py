"""Kill switch: the single source of truth for "is execution paused?".

This module is the ONLY place that reads or writes the ``execution_pauses``
table. Every dispatch path (autonomous task dispatch) and conversational path
(GO/SHCA replies) depends on the functions here rather than touching the table
directly — that keeps the pause model in one place (Dependency Inversion) and
makes it trivial to reason about precedence.

Scopes and what they block (behaviour is derived from ``scope`` — there is no
separate "mode" column):

  * ``system``         — freezes EVERYTHING, everywhere: no autonomous dispatch
                         and no conversational replies. The single row uses
                         ``scope_id = ''``.
  * ``goal``           — freezes one goal: no autonomous dispatch and no
                         conversational replies for that goal. ``scope_id`` is
                         the ``goals.id``.
  * ``agent_instance`` — graceful stop of one running instance: its next
                         autonomous dispatch is skipped. Does NOT freeze
                         conversation. ``scope_id`` is the ``agent_instances.id``.

A row's PRESENCE means "paused at that scope"; resuming DELETEs the row. The
full pause/resume history lives in the ledger (``execution.*`` events), so this
table only ever holds what is currently paused.

This module deliberately knows NOTHING about ``stories.execution_paused`` — that
is the separate operator-mode replan flag, gated independently in the phase
dispatcher. The two systems never mix.

Fail-direction (intentional):
  * ``dispatch_block`` (autonomous) FAILS CLOSED — on an unexpected error it
    reports a block, so work safely waits a tick rather than dispatching during
    a freeze.
  * The conversational backstop in ``openclaw.send_to_gateway`` FAILS OPEN — a
    bug here must never wedge the whole platform; the per-adapter gates are the
    primary UX path.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Optional

from app.database import get_pool

_log = logging.getLogger(__name__)

Scope = Literal["system", "goal", "agent_instance"]

# Atomicity (operator-facing clear transitions): a pause/resume write is a single
# row flip, so it is inherently all-or-nothing — but a transient DB hiccup must
# not leave the operator guessing whether the freeze took. We retry the write a
# few times with a short backoff; if it still fails the mutator RAISES so the
# router can surface it to the user as a system issue rather than silently
# leaving dangling state.
_MUTATOR_MAX_ATTEMPTS = 3
_MUTATOR_BACKOFF_S = 0.25


@dataclass(frozen=True)
class ExecutionBlock:
    """Why a dispatch / reply is currently forbidden."""
    scope: Scope
    reason: str
    scope_id: str


class ExecutionPausedError(RuntimeError):
    """Raised by operations that would START or advance agent work when the kill
    switch is engaged (e.g. beginning a story). Subclasses ``RuntimeError`` so
    existing "provisioning failed" handlers already treat it as a failure and
    abort cleanly; callers that want a distinct user-facing message (e.g. an
    HTTP 423) can catch it specifically. Carries the offending ``ExecutionBlock``.
    """

    def __init__(self, block: "ExecutionBlock") -> None:
        self.block = block
        super().__init__(
            f"Execution is paused ({block.scope}). Resume in the control panel to continue."
        )


async def is_system_paused() -> Optional[ExecutionBlock]:
    """Return an ExecutionBlock if system-wide execution is frozen, else None.

    Reads the single ``scope='system'`` row directly every call (no cache).
    This is a cheap indexed singleton lookup, and reading fresh every time means
    a pause/resume takes effect identically across ALL control-panel processes —
    Cloud Run can run more than one instance, and a per-process cache would let a
    freeze lag (or a stale dispatch slip through) on instances that didn't serve
    the toggle. Correctness of the freeze beats saving one tiny query.
    """
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT reason FROM execution_pauses WHERE scope = 'system' LIMIT 1"
    )
    block = ExecutionBlock("system", row["reason"] or "", "") if row else None
    if block:
        _log.debug("execution_control: SYSTEM is paused (reason=%s)", block.reason or "<none>")
    return block


async def dispatch_block(
    goal_id: str,
    instance_id: Optional[str] = None,
) -> Optional[ExecutionBlock]:
    """Highest-precedence block forbidding AUTONOMOUS dispatch for this work,
    or None if dispatch is allowed.

    Precedence: system > goal > agent_instance (broadest scope wins). This is the
    authoritative per-task guard; the bulk dispatch queries also filter in SQL so
    paused work is never fetched, making this a defence-in-depth check.

    Fails CLOSED: any error is treated as "blocked" so a bug can't let work slip
    through during a freeze.
    """
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT scope, scope_id, reason
              FROM execution_pauses
             WHERE scope = 'system'
                OR (scope = 'goal'           AND scope_id = $1)
                OR (scope = 'agent_instance' AND scope_id = $2)
             ORDER BY CASE scope
                          WHEN 'system' THEN 0
                          WHEN 'goal'   THEN 1
                          ELSE 2
                      END
             LIMIT 1
            """,
            goal_id or "",
            instance_id or "",
        )
        if row is None:
            return None
        block = ExecutionBlock(row["scope"], row["reason"] or "", row["scope_id"] or "")
        _log.info(
            "execution_control: dispatch blocked by %s scope (id=%s reason=%s) "
            "for goal=%s instance=%s",
            block.scope, block.scope_id or "-", block.reason or "<none>",
            goal_id, instance_id or "-",
        )
        return block
    except Exception:
        # Fail closed: never dispatch when we can't confirm it's allowed.
        _log.exception(
            "execution_control: dispatch_block check errored — failing CLOSED "
            "(blocking) for goal=%s instance=%s", goal_id, instance_id or "-",
        )
        return ExecutionBlock("system", "execution_control check failed (fail-closed)", "")


async def frozen_response(goal_id: str) -> Optional[dict]:
    """Convenience wrapper around ``dispatch_block`` for story-control endpoints/
    services that return a plain ``{ok, error, ...}`` dict (plan approve, diff
    decide, DAG recovery, pause/resume/replan) rather than handling an
    ``ExecutionBlock`` themselves. Returns a ready-to-return response dict if the
    goal or system is frozen, else None — callers just do
    ``if (blk := await frozen_response(goal_id)) is not None: return blk``.
    """
    block = await dispatch_block(goal_id)
    if block is None:
        return None
    return {
        "ok": False,
        "error": "execution_paused",
        "user_message": (
            f"⏸ Execution is paused ({block.scope}). "
            "Resume in the control panel, then try again."
        ),
    }


async def conversation_block(goal_id: str) -> Optional[ExecutionBlock]:
    """Block forbidding a CONVERSATIONAL reply (GO/SHCA) for this goal, or None.

    Only system and goal scopes freeze conversation — an agent_instance pause is
    autonomous-only. Uses the cached system flag for the hot path, then a single
    goal lookup. Callers decide their own fail-direction (the openclaw backstop
    fails open; the per-adapter gates surface a notice on error).
    """
    sys_block = await is_system_paused()
    if sys_block is not None:
        return sys_block

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT reason FROM execution_pauses WHERE scope = 'goal' AND scope_id = $1 LIMIT 1",
        goal_id or "",
    )
    if row is None:
        return None
    block = ExecutionBlock("goal", row["reason"] or "", goal_id)
    _log.info("execution_control: conversation blocked by goal pause (goal=%s)", goal_id)
    return block


# ── mutators (callers are admin-gated in the router) ──────────────────────────

async def _execute_with_retry(label: str, sql: str, *params) -> str:
    """Run a pause/resume write, retrying transient DB failures so the operator's
    action lands atomically. Raises the last error if every attempt fails — the
    router turns that into a visible "system issue" rather than a dangling state.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MUTATOR_MAX_ATTEMPTS + 1):
        try:
            return await get_pool().execute(sql, *params)
        except Exception as exc:  # asyncpg/connection errors are transient-ish
            last_exc = exc
            _log.warning(
                "execution_control: %s write failed (attempt %d/%d): %s",
                label, attempt, _MUTATOR_MAX_ATTEMPTS, exc,
            )
            if attempt < _MUTATOR_MAX_ATTEMPTS:
                await asyncio.sleep(_MUTATOR_BACKOFF_S * attempt)
    _log.error("execution_control: %s write FAILED after %d attempts — surfacing to caller",
               label, _MUTATOR_MAX_ATTEMPTS)
    raise last_exc  # type: ignore[misc]


async def pause(scope: Scope, scope_id: str, reason: str, paused_by: str) -> None:
    """Record a pause at ``scope``/``scope_id`` (idempotent — re-pausing updates
    the reason/actor/timestamp). Retries transient DB errors; raises if the write
    can't be made durable so the operator is told instead of left guessing."""
    await _execute_with_retry(
        f"PAUSE scope={scope} id={scope_id or '-'}",
        """
        INSERT INTO execution_pauses (scope, scope_id, reason, paused_by, paused_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (scope, scope_id)
        DO UPDATE SET reason = EXCLUDED.reason,
                      paused_by = EXCLUDED.paused_by,
                      paused_at = now()
        """,
        scope, scope_id or "", reason or "", paused_by or "",
    )
    _log.info("execution_control: PAUSED scope=%s id=%s by=%s reason=%s",
              scope, scope_id or "-", paused_by or "-", reason or "<none>")


async def resume(scope: Scope, scope_id: str) -> bool:
    """Clear a pause. Returns True if a row was actually removed (False if it was
    not paused). Retries transient DB errors; raises if the write can't be made
    durable so a half-applied resume never leaves work wedged."""
    result = await _execute_with_retry(
        f"RESUME scope={scope} id={scope_id or '-'}",
        "DELETE FROM execution_pauses WHERE scope = $1 AND scope_id = $2",
        scope, scope_id or "",
    )
    removed = result != "DELETE 0"
    _log.info("execution_control: RESUMED scope=%s id=%s (was_paused=%s)",
              scope, scope_id or "-", removed)
    return removed


async def get_pause(scope: Scope, scope_id: str) -> Optional[dict]:
    """Return the pause row for a scope/id, or None if not paused."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT scope, scope_id, reason, paused_by, paused_at "
        "FROM execution_pauses WHERE scope = $1 AND scope_id = $2",
        scope, scope_id or "",
    )
    return dict(row) if row else None


async def list_pauses() -> list[dict]:
    """All currently-active pauses, for the admin control panel / status fragment."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT scope, scope_id, reason, paused_by, paused_at "
        "FROM execution_pauses ORDER BY paused_at DESC"
    )
    return [dict(r) for r in rows]
