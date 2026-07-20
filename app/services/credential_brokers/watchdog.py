"""Credential cleanup watchdog — backup to the strict-cleanup hook.

The strict-cleanup hook (sentinel_parser + phase_dispatcher) calls
release_credentials() at every terminal task transition. The watchdog
sweeps for cases the hook missed:

  • broker.revoke() raised — workspace ADC file still on disk
  • Task crashed before reaching a terminal state (stuck in_progress)
  • Control panel restarted during the cleanup hook
  • DB write succeeded but the gateway deleteFile RPC hadn't finished

This is a DB-driven sweep — we only know about credentials we have a
cleanup_handle for. The underlying access tokens expire on their own within
1 hour regardless; this sweep ensures workspace ADC files don't persist
longer than needed.

Cadence: every 30 minutes. The hook is the fast path; the watchdog is
just defense in depth.
"""
from __future__ import annotations

import asyncio
import logging

from app.database import get_pool

# Imported lazily inside the loop to avoid circular import with brokers/__init__
# at module load time.


_log = logging.getLogger(__name__)

# How often the watchdog runs. 30 minutes is plenty — the strict hook
# does the real work; this is for stragglers.
_SWEEP_INTERVAL_SECONDS = 30 * 60

# How long after a task reached a terminal state we wait before forcing
# cleanup. Gives the hook a fair chance to retry on transient failures.
_TERMINAL_TASK_GRACE_MINUTES = 10

# Tasks stuck in non-terminal states with credentials this long are
# almost certainly orphaned (typical task runs in minutes, longest legit
# blocked-with-question span we expect is a day or two).
_STUCK_TASK_MAX_HOURS = 6


async def run_loop() -> None:
    """Top-level entry point. Sleeps then sweeps in a loop. Survives all
    sweep errors — never crashes the background task."""
    _log.info(
        "credential cleanup watchdog started (interval=%ds, terminal-grace=%dm, "
        "stuck-max=%dh)",
        _SWEEP_INTERVAL_SECONDS, _TERMINAL_TASK_GRACE_MINUTES, _STUCK_TASK_MAX_HOURS,
    )
    while True:
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            # Kill switch — skip the sweep while the system is frozen so the
            # watchdog doesn't revoke credentials that a paused agent still needs
            # to resume (M3). The underlying tokens expire within 1h regardless.
            from app.services import execution_control as _ec  # noqa: PLC0415
            _blk = await _ec.is_system_paused()
            if _blk:
                _log.info(
                    "credential watchdog: system paused (reason=%s) — skipping sweep",
                    _blk.reason or "<none>",
                )
                continue
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("credential cleanup watchdog sweep raised — continuing")


async def sweep_once() -> dict:
    """Run a single sweep. Returns counts for diagnostics:
       { 'terminal_swept': N, 'stuck_swept': N, 'failed': N }

    Always emits one summary log line so an operator can confirm the
    watchdog is alive even when there's nothing to clean up. ~48 lines/day
    at the 30-min cadence — cheap heartbeat.
    """
    terminal_swept = await _sweep_terminal_with_stale_handle()
    stuck_swept = await _sweep_stuck_in_progress()
    _log.info(
        "credential watchdog sweep: terminal=%d stuck=%d",
        terminal_swept, stuck_swept,
    )
    return {"terminal_swept": terminal_swept, "stuck_swept": stuck_swept}


async def _sweep_terminal_with_stale_handle() -> int:
    """Tasks in a terminal state that still have a cleanup_handle —
    means the strict-cleanup hook failed or was skipped. Force-release
    them now."""
    from app.services.credential_brokers import release_credentials  # noqa: PLC0415

    pool = get_pool()
    rows = await pool.fetch(
        f"""
        SELECT id, state
          FROM tasks
         WHERE credential_cleanup_handle IS NOT NULL
           AND state IN ('done', 'failed', 'cancelled', 'rejected')
           AND updated_at < NOW() - INTERVAL '{_TERMINAL_TASK_GRACE_MINUTES} minutes'
         LIMIT 200
        """,
    )
    n = 0
    for row in rows:
        task_id = row["id"]
        try:
            released = await release_credentials(task_id)
            if released:
                n += 1
                _log.info(
                    "credential watchdog: swept stale handle for terminal task "
                    "%s (state=%s)",
                    task_id, row["state"],
                )
        except Exception:
            _log.exception(
                "credential watchdog: release_credentials raised for task %s — "
                "will retry next sweep",
                task_id,
            )
    return n


async def _sweep_stuck_in_progress() -> int:
    """Tasks that have been live with credentials for longer than the
    max plausible run. Release their credentials so we don't keep
    minted keys around indefinitely, but DO NOT change the task state —
    operators decide what to do with the task itself (resume, cancel,
    fail). See the comment at the end of this function for the
    rationale."""
    from app.services.credential_brokers import release_credentials  # noqa: PLC0415

    pool = get_pool()
    rows = await pool.fetch(
        f"""
        SELECT id, state
          FROM tasks
         WHERE credential_cleanup_handle IS NOT NULL
           AND state IN ('dispatching', 'in_progress', 'blocked')
           AND COALESCE(dispatched_at, updated_at) < NOW() - INTERVAL '{_STUCK_TASK_MAX_HOURS} hours'
         LIMIT 200
        """,
    )
    n = 0
    for row in rows:
        task_id = row["id"]
        _log.warning(
            "credential watchdog: task %s stuck in %s for > %dh with active "
            "credentials — releasing and leaving state as-is for human review",
            task_id, row["state"], _STUCK_TASK_MAX_HOURS,
        )
        try:
            released = await release_credentials(task_id)
            if released:
                n += 1
        except Exception:
            _log.exception(
                "credential watchdog: release_credentials raised for stuck "
                "task %s — will retry next sweep",
                task_id,
            )
        # Note: we deliberately do NOT change task.state here. A 6h
        # blocked task may be legitimate if the human is offline; the
        # operator's job is to decide what to do with it. We just
        # remove the credential so it doesn't sit in IAM forever.
    return n
