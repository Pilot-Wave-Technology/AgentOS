"""The chat dispatcher (Phase 3) — CO-LOCATED in the web tier.

Runs as a background task inside the control-panel process (like task_controller).
It provides durability + per-thread serialization + collect-window batching +
crash recovery, but it does NOT execute turns itself: it drives the EXISTING
`_AgentSession.run_turn_direct`, so the gateway reply flows back through the
session's normal `on_message` path — for GO that means `process_go_response`
(story/task execution) + ledger + Slack + notifications + summarization, all
unchanged. The queue is the seam; the reply pipeline is reused, not duplicated.

Every web instance runs this loop; pg advisory locks ensure exactly one
instance processes a given thread at a time (and give HA).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Optional

import asyncpg

from . import collect
from . import repository as repo
from .config import get_chat_config
from .models import MessageKind, ThreadContext
from .repository import CHANNEL_DISPATCH

_log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self) -> None:
        self.cfg = get_chat_config()
        self._stop = asyncio.Event()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._listen_conn: Optional[asyncpg.Connection] = None
        # Co-located: each held advisory lock pins a pool connection for the
        # turn's duration (minutes), so keep worker count well under the pool.
        self._workers = int(os.environ.get("CHAT_DISPATCH_WORKERS", "8"))
        self._turn_timeout_s = float(os.environ.get("CHAT_TURN_TIMEOUT_S", "360"))

    # ── lifecycle ──────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._stop.clear()
        for _ in range(self._workers):
            self._tasks.append(asyncio.create_task(self._worker()))
        self._tasks.append(asyncio.create_task(self._listen_loop()))
        self._tasks.append(asyncio.create_task(self._poll_loop()))
        self._tasks.append(asyncio.create_task(self._reaper_loop()))
        await self._initial_sweep()
        _log.info(
            "chat_module: dispatcher started (%d workers, instance=%s, poll=%ds, "
            "compaction hold soft=%ds hard=%ds)",
            self._workers, self.cfg.instance_id, self.cfg.dispatch_poll_interval_s,
            self.cfg.compaction_soft_hold_s, self.cfg.compaction_max_hold_s,
        )

    async def stop(self) -> None:
        _log.info("chat_module: dispatcher stopping (draining)…")
        self._stop.set()
        if self._listen_conn is not None:
            try:
                await self._listen_conn.close()
            except Exception:
                pass
        for t in self._tasks:
            t.cancel()

    # ── intake ─────────────────────────────────────────────────────────────────
    def _enqueue_thread(self, thread_id: str) -> None:
        if not thread_id:
            _log.debug("chat_module: _enqueue_thread called with empty thread_id — ignoring")
            return
        if thread_id in self._pending:
            _log.debug("chat_module: thread=%s already pending — deduped", thread_id)
            return
        self._pending.add(thread_id)
        self._queue.put_nowait(thread_id)
        _log.info("chat_module: thread=%s enqueued (queue depth now ~%d)",
                  thread_id, self._queue.qsize())

    async def _initial_sweep(self) -> None:
        try:
            threads = await repo.list_pending_threads()
            _log.info("chat_module: initial sweep found %d pending thread(s)%s",
                      len(threads), f": {threads}" if threads else "")
            for t in threads:
                self._enqueue_thread(t)
        except Exception:
            _log.warning("chat_module: initial sweep failed", exc_info=True)

    async def _listen_loop(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                self._listen_conn = await asyncpg.connect(self.cfg.database_url)
                await self._listen_conn.add_listener(CHANNEL_DISPATCH, self._on_notify)
                backoff = 1
                _log.info("chat_module: dispatcher LISTEN %s active", CHANNEL_DISPATCH)
                while not self._stop.is_set():
                    await asyncio.sleep(5)
                    await self._listen_conn.execute("SELECT 1")
            except Exception:
                _log.warning("chat_module: LISTEN connection dropped — reconnecting in %ds",
                             backoff, exc_info=True)
                if self._listen_conn is not None:
                    try:
                        await self._listen_conn.close()
                    except Exception:
                        pass
                    self._listen_conn = None
                if self._stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _on_notify(self, conn, pid, channel, payload: str) -> None:
        try:
            data = json.loads(payload)
            thread_id = data.get("thread_id", "")
            _log.info("chat_module: NOTIFY received channel=%s thread=%s", channel, thread_id)
            self._enqueue_thread(thread_id)
        except Exception:
            _log.warning("chat_module: NOTIFY parse error payload=%r", payload, exc_info=True)

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.cfg.dispatch_poll_interval_s)
                threads = await repo.list_pending_threads()
                if threads:
                    _log.info("chat_module: poll found %d pending thread(s): %s",
                              len(threads), threads)
                else:
                    _log.debug("chat_module: poll — no pending threads")
                for t in threads:
                    self._enqueue_thread(t)
            except asyncio.CancelledError:
                break
            except Exception:
                _log.warning("chat_module: poll loop error", exc_info=True)

    async def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.cfg.reaper_interval_s)
                _log.debug("chat_module: reaper tick (timeout=%ds)", self.cfg.reaper_timeout_s)
                threads = await repo.reap_stale(self.cfg.reaper_timeout_s)
                if threads:
                    _log.info("chat_module: reaped %d stale thread(s): %s", len(threads), threads)
                    for t in threads:
                        self._enqueue_thread(t)
                else:
                    _log.debug("chat_module: reaper — nothing stale")
            except asyncio.CancelledError:
                break
            except Exception:
                _log.warning("chat_module: reaper error", exc_info=True)

    # ── workers ──────────────────────────────────────────────────────────────────
    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                thread_id = await self._queue.get()
            except asyncio.CancelledError:
                break
            _log.info("chat_module: worker picked up thread=%s", thread_id)
            more = False
            try:
                more = await self._process_thread(thread_id)
            except asyncio.CancelledError:
                _log.debug("chat_module: worker cancelled while processing thread=%s", thread_id)
                self._pending.discard(thread_id)
                break
            except Exception:
                _log.warning("chat_module: dispatch error thread=%s", thread_id,
                             exc_info=True)
            finally:
                self._pending.discard(thread_id)
                self._queue.task_done()
            if more and not self._stop.is_set():
                _log.debug("chat_module: thread=%s has more queued — re-enqueuing", thread_id)
                self._enqueue_thread(thread_id)

    @staticmethod
    async def _defer_for_compaction(session, thread_id: str) -> bool:
        """Whether to hold this thread for an in-flight compaction.

        Fails OPEN: no session, an older session object without the bounded
        check, or an error all mean "dispatch". A conversational gate that fails
        closed turns any local fault into an indefinite silence, which is the
        failure mode this whole change exists to remove.
        """
        check = getattr(session, "should_defer_for_compaction", None) if session else None
        if check is None:
            return False
        try:
            return bool(await check(thread_id))
        except Exception:
            _log.warning(
                "chat_module: compaction gate check failed thread=%s — dispatching",
                thread_id, exc_info=True,
            )
            return False

    async def _process_thread(self, thread_id: str) -> bool:
        """Claim the thread (advisory lock), collect a batch, run one turn.
        Returns True if a turn was dispatched (re-enqueue to drain stragglers)."""
        from app.database import get_pool
        pool = get_pool()
        _log.info("chat_module: _process_thread start thread=%s", thread_id)
        conn = await pool.acquire()
        claimed = False
        try:
            claimed = await repo.try_claim_thread(conn, thread_id)
            if not claimed:
                _log.info("chat_module: thread=%s lock not acquired — another worker/instance owns it", thread_id)
                return False
            _log.info("chat_module: thread=%s lock acquired", thread_id)

            ctx = await repo.get_thread_context(thread_id)
            if ctx is None:
                _log.warning("chat_module: no routing for thread %s — left queued", thread_id)
                return False
            _log.info("chat_module: thread=%s context goal=%s agent=%s agent_type=%s gw_agent=%s",
                      thread_id, ctx.goal_id, ctx.agent_id, ctx.agent_type, ctx.gateway_agent_id)

            # Kill switch — conversational freeze gate.
            from app.services import execution_control  # noqa: PLC0415
            block = await execution_control.conversation_block(ctx.goal_id)
            if block is not None:
                _log.info(
                    "chat_module: deferring thread=%s — execution frozen "
                    "(scope=%s reason=%s); rows stay queued, will drain on resume",
                    thread_id, block.scope, block.reason or "<none>",
                )
                return False
            _log.info("chat_module: thread=%s no execution freeze — proceeding", thread_id)

            # Compaction gate. Bounded — see chat_module/compaction_gate.py.
            # Must NOT use `is_compacting()`, which reports the raw latch with no
            # expiry; that is what turned one lost event into a 53h stall.
            session = await self._ensure_session(ctx)
            if await self._defer_for_compaction(session, thread_id):
                _log.info(
                    "chat_module: deferring thread=%s — agent mid-compaction "
                    "(resumes on compaction-end NOTIFY, or on gate expiry)", thread_id,
                )
                return False

            window = collect.collect_window_ms(ctx, self.cfg)
            if window > 0:
                _log.debug("chat_module: thread=%s collect window %dms — sleeping", thread_id, window)
                await asyncio.sleep(window / 1000.0)
            # Re-check after the collect-window: compaction can start mid-wait.
            if await self._defer_for_compaction(session, thread_id):
                _log.info(
                    "chat_module: deferring thread=%s — compaction started "
                    "during collect-window", thread_id,
                )
                return False

            batch = await repo.select_pending_batch(thread_id, self.cfg.collect_max_batch)
            if not batch:
                _log.info("chat_module: thread=%s no pending batch — nothing to dispatch", thread_id)
                return False
            _log.info("chat_module: thread=%s raw batch size=%d kinds=%s",
                      thread_id, len(batch), [m.kind.value for m in batch])

            # Deduplicate system_inject messages by content hash. Earlier
            # injections with identical content are stale (e.g. the same
            # RESOURCES.md re-injected on every WS reconnect). Keep only
            # the most recent occurrence of each unique content; mark the
            # rest done so they never resurface on a future turn.
            injects = [m for m in batch if not m.kind.triggers_turn]
            triggers = [m for m in batch if m.kind.triggers_turn]
            seen_hashes: set[str] = set()
            kept: list = []
            stale_ids: list[str] = []
            for m in reversed(injects):  # newest-first → first-seen wins
                h = hashlib.sha256((m.content or "").encode("utf-8")).hexdigest()
                if h in seen_hashes:
                    stale_ids.append(m.id)
                else:
                    seen_hashes.add(h)
                    kept.append(m)
            kept.reverse()  # restore chronological order
            batch = kept + triggers
            if stale_ids:
                _log.info(
                    "chat_module: thread=%s pruned %d duplicate system_inject(s) — marking done",
                    thread_id, len(stale_ids),
                )
                await repo.mark_batch_done(message_ids=stale_ids)

            # FCFS: take leading context + the first triggering message only.
            # The rest stay queued; returning True re-enqueues the thread so they
            # run as their own turns, in order — no folding multiple messages
            # into one turn.
            batch = collect.first_turn_slice(batch)
            if not collect.has_turn_trigger(batch):
                _log.info("chat_module: thread=%s batch has no turn trigger (kinds=%s) — deferring",
                          thread_id, [m.kind.value for m in batch])
                return False

            # System-initiated turns run ONE AT A TIME PER HOME.
            #
            # A person typing keeps only the per-thread lock — two people, or
            # one person in two threads, must never wait on each other. But a
            # SYSTEM_WAKE is nobody waiting: it is the memory checkpoint, the
            # consolidation, a GO wake. Those are exactly the turns that write
            # the workspace's shared files, and since Stage K an assistant's
            # threads share a workspace. `MEMORY.md` is written by reading it
            # and writing the whole file back, so two checkpoints overlapping
            # lose one of them — silently, in the turn whose entire job is to
            # record what happened.
            #
            # Uncontended for any agent that has a gateway id per thread, which
            # is every agent except a shared home. So it needs no flag saying
            # which kind this is: the key IS the home.
            home_key = (
                ctx.gateway_agent_id
                if any(m.kind is MessageKind.SYSTEM_WAKE for m in batch)
                else ""
            )
            if home_key and not await self._claim_home(conn, home_key, thread_id):
                # Deferred, not dropped: the row stays queued and the next tick
                # or NOTIFY picks it up. Running it unserialized would be the
                # data loss this exists to prevent.
                return False
            try:
                _log.info("chat_module: thread=%s dispatching batch len=%d goal=%s",
                          thread_id, len(batch), ctx.goal_id)
                await self._run_batch(ctx, batch, session=session)
            finally:
                if home_key:
                    try:
                        await repo.release_home(conn, home_key)
                    except Exception:
                        _log.warning("chat_module: home=%s failed to release lock",
                                     home_key, exc_info=True)
            return True
        finally:
            if claimed:
                try:
                    await repo.release_thread(conn, thread_id)
                    _log.debug("chat_module: thread=%s advisory lock released", thread_id)
                except Exception:
                    _log.warning("chat_module: thread=%s failed to release lock", thread_id, exc_info=True)
            await pool.release(conn)

    # How long a system turn waits for another system turn in the same home.
    # Short: a memory checkpoint is one model turn, and if the holder is
    # slower than this it is better to come back on the next tick than to
    # pin a pool connection behind it.
    _HOME_LOCK_WAIT_S = 20.0
    _HOME_LOCK_POLL_S = 0.5

    async def _claim_home(self, conn, home_key: str, thread_id: str) -> bool:
        """Bounded wait for the home lock. False means defer, never proceed.

        A blocking `pg_advisory_lock` would be simpler and is wrong here: it
        waits forever on a connection this function is holding, so one wedged
        turn would take the pool down with it rather than one thread.
        """
        deadline = time.monotonic() + self._HOME_LOCK_WAIT_S
        while True:
            if await repo.try_claim_home(conn, home_key):
                return True
            if time.monotonic() >= deadline:
                _log.info(
                    "chat_module: thread=%s deferring — another system turn holds "
                    "home=%s (they share one workspace and one MEMORY.md)",
                    thread_id, home_key,
                )
                return False
            await asyncio.sleep(self._HOME_LOCK_POLL_S)

    async def _run_batch(self, ctx: ThreadContext, batch, *, session=None) -> None:
        msg_ids = [m.id for m in batch]
        batch_id = str(uuid.uuid4())
        _log.info("chat_module: run_batch start thread=%s goal=%s batch_id=%s msgs=%d",
                  ctx.thread_id, ctx.goal_id, batch_id, len(msg_ids))
        batch_key = collect.batch_idempotency_key(batch)
        await repo.mark_processing(
            message_ids=msg_ids, batch_id=batch_id, run_id=batch_id,
            claimed_by=self.cfg.instance_id,
        )
        _log.debug("chat_module: run_batch marked processing thread=%s batch_id=%s", ctx.thread_id, batch_id)

        # Caller (_process_thread) usually passes the session it already
        # acquired for the compaction-gate check. Re-fetch here as a fallback
        # for any other call path that invokes _run_batch directly.
        if session is None:
            _log.debug("chat_module: run_batch no session passed — calling _ensure_session thread=%s", ctx.thread_id)
            session = await self._ensure_session(ctx)
        if session is None:
            _log.warning("chat_module: run_batch no session thread=%s goal=%s — marking failed",
                         ctx.thread_id, ctx.goal_id)
            await repo.mark_batch_failed(message_ids=msg_ids, error="no session for thread")
            return

        composed = await self._compose_turn(ctx, batch)
        _log.info("chat_module: run_batch calling run_turn_direct thread=%s goal=%s"
                  " composed_len=%d timeout=%ds",
                  ctx.thread_id, ctx.goal_id, len(composed), int(self._turn_timeout_s))

        # Drive the turn through the EXISTING session: the reply flows back via
        # on_message (GO: process_go_response story/task execution + ledger +
        # Slack + notifications + summarization). idempotency_key dedupes a
        # re-send at the gateway if the reaper requeues after a crash.
        ok = await session.run_turn_direct(
            composed, ctx.thread_id, idempotency_key=batch_key,
            timeout_s=self._turn_timeout_s,
        )
        if ok:
            await repo.mark_batch_done(message_ids=msg_ids)
            _log.info("chat_module: run_batch done thread=%s goal=%s batch_id=%s",
                      ctx.thread_id, ctx.goal_id, batch_id)
        else:
            _log.warning("chat_module: run_batch timed-out/not-ready thread=%s goal=%s batch_id=%s"
                         " — rows stay processing, reaper will requeue",
                         ctx.thread_id, ctx.goal_id, batch_id)

    async def _ensure_session(self, ctx: ThreadContext):
        _log.info("chat_module: _ensure_session thread=%s goal=%s is_group=%s",
                  ctx.thread_id, ctx.goal_id, ctx.is_group)
        try:
            if ctx.is_group:
                from app.services.agent_driver import ensure_go_session
                session = await ensure_go_session(ctx.goal_id)
            else:
                from app.services.agent_driver import ensure_shca_session
                session = await ensure_shca_session(ctx.goal_id, ctx.agent_id, ctx.thread_id)
            if session is None:
                _log.warning("chat_module: _ensure_session returned None thread=%s goal=%s is_group=%s",
                             ctx.thread_id, ctx.goal_id, ctx.is_group)
            else:
                _log.info("chat_module: _ensure_session ok thread=%s goal=%s session_key=%s",
                          ctx.thread_id, ctx.goal_id, getattr(session, "_key", "-"))
            return session
        except Exception:
            _log.warning("chat_module: ensure_session failed thread=%s", ctx.thread_id,
                         exc_info=True)
            return None

    async def _compose_turn(self, ctx: ThreadContext, batch) -> str:
        _log.debug("chat_module: _compose_turn thread=%s is_group=%s batch_len=%d",
                   ctx.thread_id, ctx.is_group, len(batch))
        if ctx.is_group:
            return await self._compose_go_turn(ctx, batch)
        # SHCA: the chat router already stored augmented content in the row;
        # just join (single user, usually one message).
        result = collect.compose_turn_message(batch)
        _log.debug("chat_module: _compose_turn SHCA result_len=%d", len(result))
        return result

    async def _compose_go_turn(self, ctx: ThreadContext, batch) -> str:
        """GO turn: attribute each message to its sender and prepend the slim GO
        context (goal identity + team roster) the goal-thread batcher used to add."""
        # System-only turns (e.g. a memory-flush SYSTEM_WAKE) run in isolation —
        # no goal-context prepend, no sender attribution — so they stay
        # equivalent to the direct dispatch they replaced.
        if batch and all((m.sender or "") == "system" for m in batch):
            _log.debug("chat_module: _compose_go_turn system-only batch — no attribution")
            return collect.compose_turn_message(batch)
        from app.database import get_pool
        pool = get_pool()
        uids = list({m.sender for m in batch if m.sender})
        _log.debug("chat_module: _compose_go_turn resolving %d sender uid(s): %s", len(uids), uids)
        names: dict[str, str] = {}
        if uids:
            try:
                rows = await pool.fetch(
                    "SELECT uid, display_name FROM users WHERE uid = ANY($1::text[])", uids,
                )
                names = {r["uid"]: (r["display_name"] or r["uid"]) for r in rows}
            except Exception:
                _log.warning("chat_module: _compose_go_turn failed to resolve sender names", exc_info=True)
        lines = [
            f"[{names.get(m.sender, m.sender)}]: {m.content}"
            for m in batch if m.content and m.content.strip()
        ]
        formatted = "\n\n".join(lines)
        try:
            from app.services.go_agent import build_go_context
            # THE THREAD, because an errand thread must be told which
            # errand it is. Goal threads pass one too and get nothing
            # extra: `errand_block` returns "" for a thread with no
            # story attached.
            ctxt = await build_go_context(ctx.goal_id, ctx.thread_id)
        except Exception:
            _log.warning("chat_module: _compose_go_turn build_go_context failed goal=%s", ctx.goal_id, exc_info=True)
            ctxt = ""
        result = f"{ctxt}\n\n---\n\n{formatted}" if ctxt else formatted
        _log.debug("chat_module: _compose_go_turn done goal=%s result_len=%d", ctx.goal_id, len(result))
        return result
