"""GitGraphController — the single-leader engine loop (plan §5.0).

Runs ONLY in the standalone git-engine worker (`app/git_engine_worker.py`), as a
single instance. `min=max=1` is not a correctness guarantee (a revision rollout
briefly runs two instances), so a single global **`gitgraph:engine-leader`**
Postgres advisory lock guarantees exactly one ACTIVE engine: only the lock holder
runs lanes; a second instance idles and retries until it becomes leader. The lock
is held on a DEDICATED connection for the holder's lifetime (it is session-scoped;
returning the connection to the pool would drop it) and auto-releases when that
connection dies — so the standby takes over cleanly on crash/rollover.

This is the Phase-0 SCAFFOLD: leader election + the `git_dispatch` LISTEN/poll
wake loop are wired, but `_tick` is an idle no-op gated behind GITGRAPH_ENABLED.
The per-repo lane drain / merge queue / branch ops land in later build steps;
deploying this changes nothing until the engine is given lane work and enabled.

Mirrors the proven structure of `app/services/council/dispatcher.py` (run loop +
NOTIFY listener + poll backstop), adding leader election on top.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.database import get_pool

from .dispatch import GIT_DISPATCH_CHANNEL

log = logging.getLogger(__name__)

# Single global leader lock (whole engine, not per-repo). Per-repo parallelism is
# in-process (asyncio locks) inside the one leader — added with the lane work.
_LEADER_KEY = "gitgraph:engine-leader"
# Web tier fires NOTIFY git_dispatch on enqueue/approval/webhook; we LISTEN here.
_DISPATCH_CHANNEL = GIT_DISPATCH_CHANNEL


class GitGraphController:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._poll_seconds = 20
        # Dedicated connection that holds the leader advisory lock for our lifetime.
        # NEVER used for other DB work (that would risk returning it to the pool and
        # dropping the lock) — lane reads/writes use separate pool connections.
        self._leader_conn = None
        self._is_leader = False
        # Run the zombie-active lease sweep once per leadership term (reset on loss).
        self._swept = False

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever())
            self._listen_task = asyncio.create_task(self._listen_notifications())
            log.info("git-engine controller started (leader election + %s listener)",
                     _DISPATCH_CHANNEL)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        tasks = [t for t in (self._task, self._listen_task) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._release_leader()
        # Close the shared pooled GitHub client (spec §7) on worker shutdown.
        try:
            from . import gh_client
            await gh_client.aclose()
        except Exception:
            pass
        self._task = None
        self._listen_task = None

    def trigger(self) -> None:
        self._wake.set()

    # ── leader election ──────────────────────────────────────────────────────

    async def _acquire_leader(self) -> bool:
        """Become (or remain) the single engine leader. Holds a DEDICATED pinned
        connection for the lock's lifetime. Returns True iff we hold leadership."""
        if self._is_leader and self._leader_conn is not None:
            # Revalidate the pinned lock connection. Between ticks it sits idle (it
            # only ever ran the advisory-lock query), so a Cloud SQL / VPC idle-reap
            # both invalidates the asyncpg proxy AND drops the session-scoped advisory
            # lock. A cheap round-trip keeps it warm and detects loss. GUARDED: calling
            # ANY method on a released proxy raises InterfaceError — without this the
            # exception escapes _acquire_leader and the tick never runs, so the engine
            # silently does no work despite min=1.
            try:
                if not self._leader_conn.is_closed():
                    await self._leader_conn.execute("SELECT 1")
                    return True
            except Exception as exc:
                log.warning("git-engine: leader connection lost (%s); re-acquiring", exc)
        # Lost/never-had the lock (or our conn died): (re)acquire.
        await self._release_leader()
        pool = get_pool()
        conn = None
        try:
            conn = await pool.acquire()
            got = bool(await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", _LEADER_KEY))
            if got:
                self._leader_conn = conn
                self._is_leader = True
                log.info("git-engine: acquired engine-leader lock")
                return True
            # Another instance is leader — return the probe connection to the pool.
            await pool.release(conn)
            return False
        except Exception as exc:
            log.warning("git-engine: leader acquire failed: %s", exc)
            if conn is not None:
                try:
                    await pool.release(conn)
                except Exception:
                    pass
            return False

    async def _release_leader(self) -> None:
        conn = self._leader_conn
        self._leader_conn = None
        self._is_leader = False
        self._swept = False   # re-sweep when leadership is (re)acquired
        if conn is None:
            return
        try:
            if not conn.is_closed():
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext($1)::bigint)", _LEADER_KEY)
        except Exception:
            pass
        try:
            await get_pool().release(conn)
        except Exception:
            pass

    # ── git_dispatch NOTIFY listener ───────────────────────────────────────────

    def _on_dispatch(self, _conn, _pid, _channel, _payload) -> None:
        """asyncpg LISTEN callback — just wake the loop (don't block here)."""
        self.trigger()

    async def _listen_notifications(self) -> None:
        """Hold a pool connection LISTENing on `git_dispatch`; reconnect on drop.

        NOTIFY is not durable; the `_poll_seconds` loop in `_run_forever` is the
        backstop for anything fired while this listener is down.
        """
        while not self._stop.is_set():
            pool = get_pool()
            conn = None
            try:
                conn = await pool.acquire()
                await conn.add_listener(_DISPATCH_CHANNEL, self._on_dispatch)
                log.info("git-engine: LISTEN %s active", _DISPATCH_CHANNEL)
                while not self._stop.is_set() and not conn.is_closed():
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
            except Exception as exc:
                log.warning("git-engine: notification listener error: %s", exc)
            finally:
                if conn is not None:
                    try:
                        await conn.remove_listener(_DISPATCH_CHANNEL, self._on_dispatch)
                    except Exception:
                        pass
                    try:
                        await pool.release(conn)
                    except Exception:
                        pass
            if not self._stop.is_set():
                await asyncio.sleep(2)  # backoff before reconnect

    # ── run loop ───────────────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                # Only the leader does work; non-leaders idle and retry leadership
                # each cycle so they take over promptly when the leader's lock frees.
                if await self._acquire_leader():
                    if get_settings().gitgraph_enabled:
                        await self._tick()
            except Exception as exc:  # never let the loop die
                log.exception("git-engine tick error: %s", exc)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        """Leader tick. Cut branches for cut-ready PLANNED nodes, advance node
        lifecycle, enqueue approved nodes, and run DETECTION (finalize human-merges,
        handle cancels, advance trunk).

        The engine self-merge LAND is RETIRED (merge-model rework, spec §10): we no
        longer call `merge_lane.run_merge_pass` / `merge_pr`. A human merges on the
        PR; `detect.run_detection_pass` reconciles. The flow now is:
        enqueue_ready_merges (READY → QUEUED, no approve gate) → promote_pending
        (→ active lease slot) → run_grind_pass (sync-check; open the PR when clean;
        spawn a diff story when behind) → human merges → detection finalizes."""
        from app.services.git_graph.branches import (
            advance_node_lifecycle, demote_stale_ready, run_create_pass,
        )
        from app.services.git_graph.merge_lane import enqueue_ready_merges
        from app.services.git_graph.detect import run_detection_pass, reap_deleted_goal_nodes
        from app.services.git_graph.lease import promote_pending, sweep_zombie_active
        from app.services.git_graph.grind import run_grind_pass
        from app.services.git_graph.reconcile import reconcile
        from app.services.git_graph.branch_cleanup_state import (
            retry_pending as retry_branch_cleanup)

        # EVERY tick: reclaim 'active' lease slots held by terminal nodes. A crash —
        # or a torn write before _finalize_merged/_cancel_node committed — can strand an
        # 'active' row on a MERGED/ABANDONED/ESCALATED node, which blocks promote_pending
        # for that (repo,trunk) lane. Running it once per leadership term left such a
        # strand stuck until the next leader restart; the sweep is a cheap single UPDATE,
        # so run it each tick to self-heal within ~20s.
        async with get_pool().acquire() as conn:
            swept = await sweep_zombie_active(conn)
        if swept:
            log.info("git-engine: zombie-active sweep released %d lease slot(s)", swept)

        # Reap FIRST: a deleted/cancelled goal's nodes are soft-orphaned (delete_goal
        # never touches git_nodes, and no pass filters goal status). Terminalize them
        # to ABANDONED up front so the create/advance/enqueue/grind passes below in
        # THIS tick already skip them — never cut/merge a dead goal's branch.
        reaped = await reap_deleted_goal_nodes()

        res = await run_create_pass()
        async with get_pool().acquire() as conn:
            adv = await advance_node_lifecycle(conn)
            # A node that just reached READY is about to be enqueued for merge. Collect
            # those goals so we can refresh their plan BEFORE the merge fires.
            ready_goal_rows = await conn.fetch(
                "SELECT DISTINCT goal_id FROM git_nodes WHERE state = 'READY'"
            )
        # Merge-boundary reconcile: re-derive each goal that has a READY node so the
        # merge never fires on a STALE membership snapshot. A chain member created
        # between the sparse reconcile triggers (import/cancel/manual) is otherwise
        # invisible to the READY gate and gets merged past (the cf064e0a bug). Bounded:
        # only goals with a merge imminent — NOT every tick / every goal. reconcile owns
        # its own txn, so call it outside the conn block.
        reconciled = 0
        for _gr in ready_goal_rows:
            try:
                await reconcile(_gr["goal_id"])
                reconciled += 1
            except Exception:
                log.warning("git-engine: merge-boundary reconcile failed for goal %s",
                            _gr["goal_id"], exc_info=True)
        async with get_pool().acquire() as conn:
            # If reconcile added a new unsettled member, the node drops back out of READY
            # so enqueue won't merge the stale workstream; it re-promotes once that member
            # settles. The merge story's depends_on was refreshed by reconcile in the
            # same pass, so it now tracks the new last story.
            demoted = await demote_stale_ready(conn)
            enq = await enqueue_ready_merges(conn)
        det = await run_detection_pass()   # finalize/cancel frees lease slots
        async with get_pool().acquire() as conn:
            promoted = await promote_pending(conn)   # fill freed/empty slots from the FIFO head
        grind = await run_grind_pass()     # drive active holders: sync / open PR / spawn diff
        # F1 — RETRY THE BRANCH DELETIONS THAT FAILED. Cleanup runs once,
        # at the moment a node merges or an errand is accepted, and a
        # transient 502 there used to be final: the branch stayed, the
        # node stayed at READY, and nothing could tell that row from a
        # successful one. Now it can, so this re-asks. Bounded by tries,
        # so a branch that genuinely cannot be deleted stops being swept
        # and starts being a row in `exhausted()`.
        cleanup = await retry_branch_cleanup(get_pool())
        if (reaped or res["created"] or res["resumed"] or adv["to_in_progress"] or adv["to_ready"]
                or reconciled or demoted
                or enq or det["finalized"] or det["cancelled"] or det["trunk_advanced"]
                or promoted or grind["synced"] or grind["diffs_spawned"]
                or grind["fixes_spawned"] or grind["prs_opened"] or grind["noop_merged"]
                or cleanup["attempted"]):
            log.info("git-engine: reaped=%s create=%s lifecycle=%s reconciled=%d demoted=%d "
                     "enqueued=%s detect=%s promoted=%s grind=%s cleanup=%s",
                     reaped, res, adv, reconciled, demoted, enq, det, promoted, grind,
                     cleanup)


git_graph_controller = GitGraphController()
