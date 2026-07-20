"""Security Council dispatcher — claims one branch run at a time (global), runs
its aspect stages then the convener, and delivers the report to every active goal
that references the branch.

Mirrors app/services/council/dispatcher.py: global single-run is DB-enforced by a
partial unique index; the dispatcher additionally only claims a new run when none
is 'running'. Woken by trigger() (on enqueue) + a `security_wake` NOTIFY +
periodic poll. Aspect stages run sequentially (one heavy scan fleet at a time),
then the convener; isolated so one failing stage never aborts the rest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import asyncpg

from app.database import get_pool
from app.services.security.provisioning import run_stage
from app.services.security.delivery import deliver_report

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Hard wall-clock ceiling for a single run. Aspect stages run SERIALLY, each with
# a ~75-min nudge ceiling (5 aspects ≈ 6.2h legit worst case) + convener, so 8h
# clears a genuinely long scan while guaranteeing a crashed/orphaned run (stuck
# 'running' because the process died mid-run) can never block the single-run
# queue forever — the reaper reclaims it and frees the slot.
_RUN_MAX_HOURS = _env_int("SECURITY_RUN_MAX_HOURS", 8)


async def _security_system_block():
    """Return an ExecutionBlock if a system-wide pause is active, else None.

    Security runs are not goal-scoped so only the system-level pause applies.
    """
    from app.services import execution_control  # noqa: PLC0415
    return await execution_control.is_system_paused()


def _rows_affected(status: str) -> int:
    try:
        return int(status.split()[-1])
    except Exception:
        return 0


class SecurityDispatcher:
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
            log.info("security dispatcher started")

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

    def _on_security_wake(self, _conn, _pid, _channel, _payload) -> None:
        self.trigger()

    async def _listen_notifications(self) -> None:
        while not self._stop.is_set():
            pool = get_pool()
            conn = None
            try:
                conn = await pool.acquire()
                await conn.add_listener("security_wake", self._on_security_wake)
                log.info("security: LISTEN security_wake active")
                while not self._stop.is_set() and not conn.is_closed():
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
            except Exception as exc:
                log.warning("security: notification listener error: %s", exc)
            finally:
                if conn is not None:
                    try:
                        await conn.remove_listener("security_wake", self._on_security_wake)
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
                log.exception("security dispatcher tick error: %s", exc)
                ran = False
            if ran:
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _reap_stale_runs(self, pool) -> None:
        """Never let a run block the queue forever. Reclaim any run stuck
        'running' past the hard ceiling — the only way that happens is the
        dispatcher process died mid-run (Cloud Run restart/crash), leaving the
        row 'running' with nobody advancing it, which otherwise blocks the global
        single-run gate permanently. COALESCE(started_at, created_at) covers
        legacy rows written before the started_at column existed."""
        reaped = await pool.execute(
            "UPDATE security_review_runs SET status='failed', finished_at=NOW() "
            "WHERE status='running' "
            "AND COALESCE(started_at, created_at) < NOW() - make_interval(hours => $1)",
            _RUN_MAX_HOURS,
        )
        n = _rows_affected(reaped)
        if n:
            log.warning("security: reaped %d stale 'running' run(s) past %dh ceiling — slot freed",
                        n, _RUN_MAX_HOURS)

    async def _tick(self) -> bool:
        """Process at most one run. Returns True if a run was processed."""
        pool = get_pool()
        # Anti-block: free the queue of any run orphaned in 'running' before the
        # single-run gate below would otherwise see it and stall forever.
        await self._reap_stale_runs(pool)
        if await pool.fetchval("SELECT 1 FROM security_review_runs WHERE status='running' LIMIT 1"):
            return False  # global single-run
        run = await pool.fetchrow(
            "SELECT id, repo_owner, repo_name, branch_name, clone_url, head_sha "
            "FROM security_review_runs WHERE status='pending' ORDER BY created_at LIMIT 1",
        )
        if not run:
            return False
        # Kill switch — system-pause gate only (security runs are branch-scoped,
        # not goal-scoped, so goal-level pauses don't apply here).
        blk = await _security_system_block()
        if blk:
            log.info("security: dispatch blocked by system pause (reason=%s) — skipping run=%s",
                     blk.reason or "<none>", run["id"])
            return False
        try:
            claimed = await pool.execute(
                "UPDATE security_review_runs SET status='running', started_at=NOW() "
                "WHERE id=$1 AND status='pending'",
                run["id"],
            )
        except asyncpg.exceptions.UniqueViolationError:
            return False  # another instance started a run (global single-running index)
        if _rows_affected(claimed) == 0:
            return False
        await self._process_run(dict(run))
        return True

    async def _safe_run_stage(self, run: dict, stage_id: str, aspect: str) -> bool:
        try:
            return await run_stage(run, stage_id, aspect)
        except Exception as exc:
            log.exception("security: stage %s crashed (run=%s) — isolating, continuing", aspect, run["id"])
            try:
                pool = get_pool()
                await pool.execute(
                    "UPDATE security_review_stages SET status='failed', "
                    "error=COALESCE(NULLIF(error, ''), $1), finished_at=NOW() "
                    "WHERE id=$2 AND status NOT IN ('done','failed')",
                    f"stage crashed: {exc}"[:500], stage_id,
                )
            except Exception:
                pass
            return False

    async def _process_run(self, run: dict) -> None:
        pool = get_pool()
        run_id = run["id"]
        log.info("security: processing run=%s branch=%s", run_id, run["branch_name"])

        # Pre-flight: if the branch no longer exists on the remote (deleted /
        # merged-and-pruned), complete the run cleanly (skipped) — never spawn scan
        # agents against a gone branch and never fail 5 clone attempts. Only a
        # DEFINITIVE 404 skips; on any uncertainty we fall through and review.
        try:
            from app.services.security.runs import branch_remote_state
            if await branch_remote_state(run) == "gone":
                await pool.execute(
                    "UPDATE security_review_runs SET status='done', finished_at=NOW() WHERE id=$1",
                    run_id,
                )
                log.info("security: run=%s branch=%s no longer exists on remote — skipped, no scan",
                         run_id, run["branch_name"])
                return
        except Exception as exc:
            log.info("security: run=%s branch existence pre-check errored (%s) — proceeding to review",
                     run_id, exc)

        try:
            # Aspect stages first (sequential — one scan fleet at a time), each isolated.
            aspect_stages = await pool.fetch(
                "SELECT id, aspect FROM security_review_stages "
                "WHERE run_id=$1 AND aspect <> 'convener' ORDER BY created_at",
                run_id,
            )
            aspect_ok = 0
            for st in aspect_stages:
                if await self._safe_run_stage(run, st["id"], st["aspect"]):
                    aspect_ok += 1

            # If EVERY aspect failed specifically because the branch couldn't be
            # cloned (deleted mid-run, or a transient fetch failure), complete the
            # run QUIETLY — no convener, no "incomplete" delivery. An unclonable
            # branch is skipped, not surfaced as an error (matches the deleted-branch
            # pre-flight). A mixed/scanner failure falls through to normal handling.
            if aspect_stages and aspect_ok == 0:
                errs = await pool.fetch(
                    "SELECT error FROM security_review_stages "
                    "WHERE run_id=$1 AND aspect <> 'convener'", run_id,
                )
                if errs and all((e["error"] or "").startswith("CLONE_UNAVAILABLE") for e in errs):
                    await pool.execute(
                        "UPDATE security_review_runs SET status='done', finished_at=NOW() WHERE id=$1",
                        run_id,
                    )
                    log.info("security: run=%s branch=%s unclonable on all aspects — skipped quietly, no delivery",
                             run_id, run["branch_name"])
                    return

            # Convener — always runs (treats a missing aspect as a coverage gap).
            conv = await pool.fetchrow(
                "SELECT id FROM security_review_stages WHERE run_id=$1 AND aspect='convener'", run_id,
            )
            conv_ok = False
            if conv:
                await pool.execute(
                    "UPDATE security_review_stages SET status='pending' "
                    "WHERE id=$1 AND status='blocked_on_deps'",
                    conv["id"],
                )
                conv_ok = await self._safe_run_stage(run, conv["id"], "convener")

            delivered = 0
            if conv and conv_ok:
                delivered = await self._deliver(run, conv["id"])
            elif conv:
                await self._deliver_failure(run)

            status = "done" if conv_ok else "failed"
            await pool.execute(
                "UPDATE security_review_runs SET status=$1, finished_at=NOW() WHERE id=$2",
                status, run_id,
            )
            log.info("security: run=%s finished status=%s aspect_ok=%d conv_ok=%s delivered=%d",
                     run_id, status, aspect_ok, conv_ok, delivered)
        except Exception as exc:
            log.exception("security: run=%s failed: %s", run_id, exc)
            await pool.execute(
                "UPDATE security_review_runs SET status='failed', finished_at=NOW() WHERE id=$1", run_id,
            )

    async def _deliver(self, run: dict, conv_stage_id: str) -> int:
        pool = get_pool()
        row = await pool.fetchrow("SELECT output FROM security_review_stages WHERE id=$1", conv_stage_id)
        if not row or row["output"] is None:
            return 0
        out = row["output"]
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except Exception:
                pass
        report = out if (isinstance(out, str) and out.strip()) else "(convener produced no report)"
        return await deliver_report(run, report)

    async def _deliver_failure(self, run: dict) -> None:
        """Convener failed → no synthesized report. Post a short coverage-aware note
        to each referencing goal so a failed run isn't silently invisible."""
        pool = get_pool()
        stages = await pool.fetch(
            "SELECT aspect, status FROM security_review_stages WHERE run_id=$1 AND aspect <> 'convener'",
            run["id"],
        )
        cov = ", ".join(f"{s['aspect']}: {'✓' if s['status']=='done' else '✗'}" for s in stages) or "(none)"
        note = (
            "**Security review incomplete** — the council ran but could not synthesize a final "
            f"report (the convener step failed). Aspect coverage this run: {cov}. "
            "No consolidated recommendations this time; re-trigger to retry."
        )
        await deliver_report(run, note)


security_dispatcher = SecurityDispatcher()
