"""Research-queue drainer — deterministic, engine-owned draining of the
platform_discoveries feed.

Runs in the council-worker alongside the goal-review and security dispatchers.
Its OWN queue (platform_discoveries), strictly one-at-a-time END TO END, in
parallel with the other councils' queues. Mirrors app/services/security/
dispatcher.py: DB-enforced single-run + a `discovery_wake` NOTIFY + periodic poll.

Loop control lives HERE, not in an LLM. Each tick:
  1. Reconcile the one 'researching' discovery from its linked sweep:
       sweep done            -> discovery 'reviewed'  (terminal success)
       sweep failed/missing  -> discovery 'failed'    (terminal; NO re-enqueue)
       sweep in flight       -> wait (watchdog force-fails a stuck one)
  2. If nothing is researching and no sweep is in flight, claim the oldest
     highest-priority 'new' discovery (priority DESC, queued_at ASC) and start
     exactly one research sweep for it, linking the sweep back to the discovery.

The full research->review span keeps the discovery 'researching', so the
single-researching partial unique index serialises the whole pipeline.
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.config import get_settings
from app.database import get_pool
from app.services.platform_council import PLATFORM_GOAL_ID
from app.services.platform_council import runs as _runs

log = logging.getLogger(__name__)


async def _system_paused():
    """Return an ExecutionBlock if a system-wide pause is active, else None.
    Research runs are not goal-scoped, so only the system-level pause applies."""
    from app.services import execution_control  # noqa: PLC0415
    return await execution_control.is_system_paused()


def _rows_affected(status: str) -> int:
    try:
        return int(status.split()[-1])
    except Exception:
        return 0


class ResearchDispatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._poll_seconds = 30

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever())
            self._listen_task = asyncio.create_task(self._listen_notifications())
            log.info("research dispatcher started")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        tasks = [t for t in (self._task, self._listen_task) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._listen_task = None

    def trigger(self) -> None:
        self._wake.set()

    def _on_discovery_wake(self, _conn, _pid, _channel, _payload) -> None:
        self.trigger()

    async def _listen_notifications(self) -> None:
        while not self._stop.is_set():
            pool = get_pool()
            conn = None
            try:
                conn = await pool.acquire()
                await conn.add_listener("discovery_wake", self._on_discovery_wake)
                log.info("research: LISTEN discovery_wake active")
                while not self._stop.is_set() and not conn.is_closed():
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
            except Exception as exc:
                log.warning("research: notification listener error: %s", exc)
            finally:
                if conn is not None:
                    try:
                        await conn.remove_listener("discovery_wake", self._on_discovery_wake)
                    except Exception:
                        pass
                    try:
                        await pool.release(conn)
                    except Exception:
                        pass
            if not self._stop.is_set():
                await asyncio.sleep(2)

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                ran = await self._tick()
            except Exception as exc:
                log.exception("research dispatcher tick error: %s", exc)
                ran = False
            if ran:
                continue  # slot freed / one started — immediately look again
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> bool:
        """Advance the queue by at most one step. Returns True if state changed
        (so the loop re-ticks immediately)."""
        outcome = await self._reconcile_inflight()
        if outcome == "advanced":
            return True          # a discovery reached terminal; try to claim next now
        if outcome == "waiting":
            return False         # one still in flight — nothing to do
        # outcome == "none": queue slot is free. Gate new work on the system pause.
        blk = await _system_paused()
        if blk:
            return False
        return await self._claim_and_start()

    async def _reconcile_inflight(self) -> str:
        """Settle the single 'researching' discovery from its sweep. Returns
        'advanced' | 'waiting' | 'none'."""
        pool = get_pool()
        d = await pool.fetchrow(
            "SELECT id, sweep_id FROM platform_discoveries WHERE status='researching' LIMIT 1")
        if not d:
            return "none"
        watchdog_s = max(60, int(get_settings().platform_research_watchdog_minutes) * 60)
        stale = await pool.fetchval(
            "SELECT (NOW() - updated_at) > ($1 || ' seconds')::interval "
            "FROM platform_discoveries WHERE id=$2", str(watchdog_s), d["id"])

        # Claimed but the sweep was never linked (process died between claim and
        # enqueue) — fail it once it's clearly stale.
        if not d["sweep_id"]:
            if stale:
                await self._fail(d["id"], "no sweep linked (stale)")
                return "advanced"
            return "waiting"

        sweep = await pool.fetchrow(
            "SELECT status FROM platform_sweeps WHERE id=$1", d["sweep_id"])
        status = sweep["status"] if sweep else None

        if status == "done":
            fid = await pool.fetchval(
                "SELECT id FROM platform_registry WHERE run_id=$1 ORDER BY created_at LIMIT 1",
                d["sweep_id"])
            await pool.execute(
                "UPDATE platform_discoveries SET status='reviewed', registry_finding_id=$2, "
                "updated_at=NOW() WHERE id=$1", d["id"], fid)
            log.info("research-queue: discovery %s reviewed (sweep %s done)",
                     d["id"], d["sweep_id"][:8])
            return "advanced"

        if status is None or status == "failed":
            await self._fail(d["id"], f"sweep {status or 'missing'}")
            return "advanced"

        # Sweep still researching/reviewing. Watchdog: if the discovery has been in
        # flight beyond the limit, force-fail the (likely dead) sweep + discovery so
        # the queue can't wedge.
        if stale:
            await pool.execute(
                "UPDATE platform_sweeps SET status='failed', finished_at=NOW() "
                "WHERE id=$1 AND status IN ('researching','reviewing')", d["sweep_id"])
            await self._fail(d["id"], "watchdog timeout (sweep stuck)")
            return "advanced"
        return "waiting"

    async def _fail(self, discovery_id: str, reason: str) -> None:
        pool = get_pool()
        await pool.execute(
            "UPDATE platform_discoveries SET status='failed', updated_at=NOW() WHERE id=$1",
            discovery_id)
        log.warning("research-queue: discovery %s failed (%s)", discovery_id, reason)

    async def _claim_and_start(self) -> bool:
        pool = get_pool()
        # One research sweep at a time, globally — defer to any in-flight sweep
        # (button / steward ad-hoc / a previous drainer start). active_sweep() also
        # stale-fails an abandoned sweep so we don't defer forever.
        if await _runs.active_sweep():
            return False

        # Claim the oldest highest-priority 'new'. The single-researching partial
        # unique index is the serialization point: a racing instance hits it and
        # raises, which we swallow.
        try:
            row = await pool.fetchrow(
                """UPDATE platform_discoveries
                      SET status='researching', sweep_id=NULL, updated_at=NOW()
                    WHERE id = (SELECT id FROM platform_discoveries WHERE status='new'
                                 ORDER BY priority DESC, queued_at ASC
                                 FOR UPDATE SKIP LOCKED LIMIT 1)
                  RETURNING id, category, title, summary""")
        except asyncpg.exceptions.UniqueViolationError:
            return False  # another instance already has one researching
        if not row:
            return False

        thread_id = await pool.fetchval(
            "SELECT go_thread_id FROM goals WHERE id=$1", PLATFORM_GOAL_ID)
        topic = (row["title"] or "").strip()
        if row["summary"]:
            topic = f"{topic} — {row['summary']}".strip(" —")

        try:
            res = await _runs.enqueue_sweep(
                kinds=[row["category"]], trigger="queue", created_by="research-drainer",
                thread_id=thread_id, question=topic)
        except Exception as exc:
            res = {"ok": False, "reason": str(exc)}

        if not res.get("ok"):
            # Couldn't start (a sweep raced in). This is a DEFERRAL, not a research
            # failure — release the claim back to 'new' to retry, don't burn it.
            await pool.execute(
                "UPDATE platform_discoveries SET status='new', updated_at=NOW() WHERE id=$1",
                row["id"])
            log.info("research-queue: enqueue deferred for discovery %s (%s) — back to 'new'",
                     row["id"], res.get("reason"))
            return False

        await pool.execute(
            "UPDATE platform_discoveries SET sweep_id=$2 WHERE id=$1", row["id"], res["sweep_id"])
        log.info("research-queue: discovery %s (%s) → sweep %s",
                 row["id"], row["category"], res["sweep_id"][:8])
        return True


research_dispatcher = ResearchDispatcher()
