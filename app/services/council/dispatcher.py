"""Council dispatcher — claims one run at a time (global), runs its stages, delivers to GO.

Phase 1: co-located in the web tier behind COUNCIL_DISPATCHER_ENABLED. Woken by
trigger() (on enqueue) and a periodic poll. Global single-run is DB-enforced by
the partial unique index; the dispatcher additionally only claims a new run when
none is 'running'. Stages run sequentially (goal_health → convener).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import asyncpg

from app.database import get_pool
from app.services.council import deadline, findings
from app.services.council.deliver import deliver_ledger_packet
from app.services.council.inputs import stage_output
from app.services.council.provisioning import run_stage

log = logging.getLogger(__name__)

# Hard ceiling for a 'running' council run. A run only stays 'running' across
# ticks if the dispatcher process died mid-run (the tick processes a run
# synchronously), which otherwise wedges the global single-run gate forever.
# Council reviews finish in minutes, so 4h is a safe "definitely dead" bound.
COUNCIL_RUN_MAX_HOURS = 4

# How long the queue may sit with pending work it cannot dispatch before we stop
# logging it as routine and escalate to a WARNING. A wedged queue previously
# logged only at INFO, once per 20s tick — indistinguishable from healthy
# activity, which is how a three-day platform-wide review outage went unnoticed
# until someone asked why six stories hadn't been reviewed.
COUNCIL_STALL_WARN_MINUTES = 10


async def _council_dispatch_block(goal_id: str):
    """Return an ExecutionBlock if this goal (or the system) is paused, else None."""
    from app.services import execution_control  # noqa: PLC0415
    return await execution_control.dispatch_block(goal_id)


def _rows_affected(status: str) -> int:
    # asyncpg execute() returns e.g. "UPDATE 1"
    try:
        return int(status.split()[-1])
    except Exception:
        return 0


class CouncilDispatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._poll_seconds = 20
        # Stall tracking (monotonic seconds, None = not stalled). Monotonic so a
        # clock adjustment can't make a wedged queue look healthy.
        self._stalled_since: float | None = None
        self._stall_warned_at: float | None = None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever())
            # Event-driven: LISTEN for story_done (DB trigger on any transition
            # into 'done') + council_wake (a run was enqueued). No polling needed
            # for the common case; the poll loop is a backstop.
            self._listen_task = asyncio.create_task(self._listen_notifications())
            log.info("council dispatcher started")

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

    # ── story_done NOTIFY listener ───────────────────────────────────────────

    def _on_story_done(self, _conn, _pid, _channel, payload) -> None:
        """asyncpg LISTEN callback — schedule the enqueue (don't block here)."""
        try:
            data = json.loads(payload) if payload else {}
        except Exception:
            data = {}
        goal_id = data.get("goal_id")
        story_id = data.get("story_id")
        if goal_id and story_id:
            asyncio.create_task(self._handle_story_done(goal_id, story_id))

    async def _handle_story_done(self, goal_id: str, story_id: str) -> None:
        try:
            from app.services.council.runs import enqueue_story_review
            res = await enqueue_story_review(goal_id, story_id, "done", trigger="story_done")
            self.trigger()
            log.info("council: story_done NOTIFY → enqueue story=%s (%s)",
                     story_id, "created" if res.get("created") else res.get("reason"))
        except Exception:
            log.exception("council: handling story_done NOTIFY failed story=%s", story_id)

    def _on_council_wake(self, _conn, _pid, _channel, _payload) -> None:
        """A run was enqueued (manual goal review / reimport, possibly from
        another service) — wake the loop so it claims it now instead of waiting
        for the next poll. This replaces the in-process trigger() across the
        process boundary once the dispatcher runs in its own worker."""
        self.trigger()

    async def _listen_notifications(self) -> None:
        """Hold a pool connection with LISTEN on `story_done` + `council_wake`;
        reconnect on drop.

        NOTIFY is not durable — anything fired while this listener is down is
        missed. The `_poll_seconds` loop is the backstop for `council_wake`, and
        the import-time re-enqueue of unreviewed 'done' stories covers a missed
        `story_done`.
        """
        while not self._stop.is_set():
            pool = get_pool()
            conn = None
            try:
                conn = await pool.acquire()
                await conn.add_listener("story_done", self._on_story_done)
                await conn.add_listener("council_wake", self._on_council_wake)
                log.info("council: LISTEN story_done + council_wake active")
                # Hold the connection open; re-check liveness periodically and
                # exit promptly on stop.
                while not self._stop.is_set() and not conn.is_closed():
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
            except Exception as exc:
                log.warning("council: notification listener error: %s", exc)
            finally:
                if conn is not None:
                    for ch, cb in (("story_done", self._on_story_done),
                                   ("council_wake", self._on_council_wake)):
                        try:
                            await conn.remove_listener(ch, cb)
                        except Exception:
                            pass
                    try:
                        await pool.release(conn)
                    except Exception:
                        pass
            if not self._stop.is_set():
                await asyncio.sleep(2)  # backoff before reconnect

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                ran = await self._tick()
            except Exception as exc:  # never let the loop die
                log.exception("council dispatcher tick error: %s", exc)
                ran = False
            if ran:
                continue  # immediately look for the next pending run
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _reap_stale_runs(self, pool) -> None:
        """Reclaim any run stuck 'running' past the hard ceiling. The tick
        processes a run synchronously, so a row only stays 'running' across ticks
        if the dispatcher process died mid-run (Cloud Run restart/crash) — which
        otherwise blocks the global single-run gate PERMANENTLY (the whole review
        queue wedges). Mirrors the security council's reaper; the goal council was
        missing it. No started_at column here, so clock on created_at (a pending
        run is claimed within one ~20s tick, so created_at ≈ run start)."""
        reaped = await pool.execute(
            "UPDATE council_review_runs SET status='failed', finished_at=NOW() "
            "WHERE status='running' "
            "AND created_at < NOW() - make_interval(hours => $1)",
            COUNCIL_RUN_MAX_HOURS,
        )
        n = _rows_affected(reaped)
        if n:
            log.warning("council: reaped %d stale 'running' run(s) past %dh ceiling — slot freed",
                        n, COUNCIL_RUN_MAX_HOURS)

    async def _note_stall(self, pool, why: str) -> None:
        """Record that this tick found nothing dispatchable, and escalate to
        WARNING once the queue has held undispatchable work past the threshold.

        An empty queue is not a stall — it clears the tracker. A queue with
        pending runs that never drains is an operational problem that must be
        visible without someone first noticing a downstream symptom, so it graduates
        from INFO-per-tick (which reads as normal activity) to a periodic WARNING
        naming the backlog and how long it has been stuck.
        """
        # Count only runs on ACTIVE goals. A pending run whose goal was soft-deleted
        # can never be dispatched by design, so counting it would warn every
        # threshold forever about a queue that is actually healthy — alert fatigue
        # on permanent debris. Paused goals ARE still active, so a pause-induced
        # stall (the case this exists for) is counted.
        pending = await pool.fetchval(
            "SELECT count(*) FROM council_review_runs crr WHERE crr.status='pending' "
            "AND EXISTS (SELECT 1 FROM goals g WHERE g.id = crr.goal_id AND g.status = 'active')"
        ) or 0
        if not pending:
            self._stalled_since = None
            self._stall_warned_at = None
            return

        now = time.monotonic()
        if self._stalled_since is None:
            self._stalled_since = now
            log.info("council: queue idle with %d pending run(s) — %s", pending, why)
            return

        stalled_for = now - self._stalled_since
        threshold = COUNCIL_STALL_WARN_MINUTES * 60
        if stalled_for < threshold:
            return
        # Warn on entering the stalled state, then at most once per threshold, so
        # a long outage stays visible without reproducing the per-tick log flood.
        if self._stall_warned_at is not None and (now - self._stall_warned_at) < threshold:
            return
        self._stall_warned_at = now
        log.warning(
            "council: queue STALLED for %d min with %d pending run(s) — %s. "
            "No review can complete until this clears; check execution_pauses "
            "and goals.status for the goals owning those runs.",
            int(stalled_for // 60), pending, why,
        )

    async def _tick(self) -> bool:
        """Process at most one run. Returns True if a run was processed."""
        pool = get_pool()
        # THE 40-MINUTE SWEEP FIRST, and the 8-hour reaper below it as the
        # backstop. The difference is not just the clock: the sweep DELIVERS
        # what the run's stages already found before closing it, where the
        # reaper only frees the slot. A wedged run that produces nothing
        # still fails the requirement that every goal review produce its
        # findings, and with the global single-run gate it also stops every
        # OTHER goal being reviewed until it clears.
        try:
            async with pool.acquire() as conn:
                await deadline.sweep(conn)
        except Exception:
            log.exception("council: deadline sweep failed (continuing)")

        # Anti-block: free the queue of any run orphaned in 'running' before the
        # single-run gate below would otherwise see it and stall forever.
        await self._reap_stale_runs(pool)
        if await pool.fetchval("SELECT 1 FROM council_review_runs WHERE status='running' LIMIT 1"):
            return False  # global single-run
        # Deleted-goal guard: goal delete is soft (goals.status='deleted') and
        # does not cancel pending council runs, so without this a story_done
        # NOTIFY (or a stale pending run) would provision RA agents + clone repos
        # for a goal that no longer exists. Skip runs whose goal is not active.
        #
        # Kill switch — paused work is excluded HERE, in candidate selection,
        # rather than checked after picking the oldest run. Picking first and
        # bailing on a block is head-of-line blocking: one paused goal stalls
        # every other goal's reviews indefinitely (observed in prod — a single
        # paused goal froze all council review for three days). Filtering instead
        # means the queue skips past paused goals and keeps draining, while their
        # runs stay 'pending' until they resume. Same in-SQL gate the other
        # dispatch queries use (phase_dispatcher, dag_scheduler, task_controller),
        # widened to 'system' scope, which correctly matches every row and so
        # freezes the queue outright.
        run = await pool.fetchrow(
            "SELECT crr.id, crr.goal_id, crr.kind, crr.story_id, crr.reviewed_state "
            "FROM council_review_runs crr "
            "WHERE crr.status='pending' "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM execution_pauses ep "
            "       WHERE ep.scope = 'system' "
            "          OR (ep.scope = 'goal' AND ep.scope_id = crr.goal_id)"
            "  ) "
            "  AND EXISTS (SELECT 1 FROM goals g WHERE g.id = crr.goal_id AND g.status = 'active') "
            "ORDER BY crr.created_at LIMIT 1",
        )
        if not run:
            # Nothing dispatchable. That is normal when the queue is empty, and a
            # stall worth escalating when pending work exists but is all filtered.
            await self._note_stall(pool, "all pending runs are paused or on inactive goals")
            return False
        # Kill switch, defence in depth. The SQL above already excluded paused
        # goals; this catches a pause created between that read and the claim, and
        # covers agent_instance scope, which goal-level SQL filtering can't express.
        blk = await _council_dispatch_block(run["goal_id"])
        if blk:
            log.info("council: dispatch blocked (scope=%s reason=%s) — skipping run=%s goal=%s",
                     blk.scope, blk.reason or "<none>", run["id"], run["goal_id"])
            return False
        # Dispatching again — clear BOTH stall trackers, so a later stall is timed
        # from scratch and its first WARNING isn't suppressed by the previous one.
        self._stalled_since = None
        self._stall_warned_at = None
        try:
            claimed = await pool.execute(
                "UPDATE council_review_runs SET status='running' WHERE id=$1 AND status='pending'",
                run["id"],
            )
        except asyncpg.exceptions.UniqueViolationError:
            # Another instance started a run between our check and claim — the
            # global single-running index rejected this one. Try again next tick.
            return False
        if _rows_affected(claimed) == 0:
            return False  # lost the race (already claimed)
        if (run["kind"] or "goal") == "story":
            await self._process_story_run(
                run["id"], run["goal_id"], run["story_id"], run["reviewed_state"] or "",
            )
        else:
            await self._process_run(run["id"], run["goal_id"])
        return True

    async def _safe_run_stage(self, run_id: str, stage_id: str, stage: str, goal_id: str) -> bool:
        """run_stage, isolated. A failure (or even an unexpected exception) in one
        stage must NEVER abort the remaining stages of the run, the convener, or
        the dispatcher's processing of other goals. On a crash, mark the stage
        failed and return False so the run continues with partial coverage."""
        try:
            return await run_stage(run_id, stage_id, stage, goal_id)
        except Exception as exc:
            log.exception("council: stage %s crashed (run=%s) — isolating, continuing", stage, run_id)
            try:
                pool = get_pool()
                await pool.execute(
                    "UPDATE council_review_stages SET status='failed', "
                    "error=COALESCE(NULLIF(error, ''), $1), finished_at=NOW() "
                    "WHERE id=$2 AND status NOT IN ('done', 'failed')",
                    f"stage crashed: {exc}"[:500], stage_id,
                )
            except Exception:
                pass
            return False

    async def _ingest(self, goal_id: str, run_id: str, stage: str,
                      stage_id: str) -> None:
        """Fold a finished stage's findings into the ledger, immediately.

        NOT AT THE END OF THE RUN. If the convener then crashes, or the
        deadline sweeps the run, `render.packet` can still draw a real
        packet from what the earlier stages recorded. That is what makes
        "every goal review produces its findings" a property rather than
        a hope.
        """
        try:
            pool = get_pool()
            row = await pool.fetchrow(
                "SELECT output FROM council_review_stages WHERE id=$1", stage_id)
            text = stage_output.as_text(row["output"]) if row else ""
            if not text.strip():
                return
            got = await findings.ingest_stage(goal_id, run_id, stage, text)
            log.info("council: %s findings run=%s recorded=%d closed=%d",
                     stage, run_id, got["recorded"], got["closed"])
        except Exception:
            # The stage did its work and is already recorded as done.
            # Losing the ledger write must not retroactively fail it.
            log.exception("council: ingesting %s findings (run=%s)", stage, run_id)

    async def _stage(self, run_id: str, goal_id: str, stage: str) -> bool:
        """Run one stage by name, then ingest whatever it found."""
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT id FROM council_review_stages WHERE run_id=$1 AND stage=$2",
            run_id, stage)
        if not row:
            return False
        ok = await self._safe_run_stage(run_id, row["id"], stage, goal_id)
        if ok:
            await self._ingest(goal_id, run_id, stage, row["id"])
        return ok

    async def _process_run(self, run_id: str, goal_id: str) -> None:
        log.info("council: processing run=%s goal=%s", run_id, goal_id)
        pool = get_pool()
        try:
            # ORDER MATTERS IN ONE PLACE ONLY: story before goal_health.
            # Health judges whether the work was the right work, and the
            # facts it judges are what the story stage reconciles. Every
            # other ordering here is arbitrary, and NOTHING BLOCKS -- each
            # stage runs whether or not the one before it succeeded, and a
            # failed upstream reaches the next as a stated coverage gap
            # (inputs.stage_output) rather than as silence.
            story_ok = await self._stage(run_id, goal_id, "story")
            gh_ok = await self._stage(run_id, goal_id, "goal_health")
            pr_ok = await self._stage(run_id, goal_id, "progress")

            conv = await pool.fetchrow(
                "SELECT id FROM council_review_stages WHERE run_id=$1 AND stage='convener'",
                run_id)
            conv_ok = False
            if conv:
                # The convener ALWAYS runs, even if every upstream reviewer
                # failed -- it is built to treat a missing finding as a
                # coverage gap and still produce a packet naming what failed.
                await pool.execute(
                    "UPDATE council_review_stages SET status='pending' "
                    "WHERE id=$1 AND status='blocked_on_deps'", conv["id"])
                conv_ok = await self._safe_run_stage(
                    run_id, conv["id"], "convener", goal_id)

            if conv and conv_ok:
                delivered = await self._deliver(goal_id, conv["id"])
            else:
                # NO CONVENER, STILL A REVIEW. The ledger already holds
                # whatever the stages that ran found, so the packet is drawn
                # from it in code. A cycle that delivers nothing teaches the
                # orchestrator that reviews can be ignored, which is a more
                # expensive failure than a plainer packet.
                delivered = await deliver_ledger_packet(goal_id, run_id)

            # DONE IF A PACKET REACHED THE GO, by whichever path. The run
            # status answers "did this review produce its findings", not
            # "did every agent behave".
            status = "done" if delivered else "failed"
            await pool.execute(
                "UPDATE council_review_runs SET status=$1, finished_at=NOW() WHERE id=$2",
                status, run_id)
            log.info("council: run=%s finished status=%s story=%s gh=%s pr=%s "
                     "conv=%s delivered=%s", run_id, status, story_ok, gh_ok,
                     pr_ok, conv_ok, delivered)
        except Exception as exc:
            log.exception("council: run=%s failed: %s", run_id, exc)
            # EVEN HERE. An unexpected crash in the driver is exactly when a
            # goal is most likely to need its review, so try the ledger
            # packet before giving up on the cycle.
            try:
                await deliver_ledger_packet(goal_id, run_id)
            except Exception:
                log.exception("council: ledger packet also failed (run=%s)", run_id)
            await pool.execute(
                "UPDATE council_review_runs SET status='failed', finished_at=NOW() WHERE id=$1",
                run_id)

    async def _process_story_run(self, run_id: str, goal_id: str, story_id: str,
                                 reviewed_state: str) -> None:
        """Run a single-stage story review, persist it, and (for a 'done' review)
        finalize the story to 'reviewed' so its workspace becomes reapable."""
        pool = get_pool()
        log.info("council: processing STORY run=%s goal=%s story=%s state=%s",
                 run_id, goal_id, story_id, reviewed_state)
        try:
            st = await pool.fetchrow(
                "SELECT id FROM council_review_stages WHERE run_id=$1 AND stage='story'", run_id,
            )
            if not st:
                await pool.execute(
                    "UPDATE council_review_runs SET status='failed', finished_at=NOW() WHERE id=$1",
                    run_id,
                )
                return
            ok = await run_stage(run_id, st["id"], "story", goal_id)

            text = ""
            row = await pool.fetchrow(
                "SELECT output, instance_id FROM council_review_stages WHERE id=$1", st["id"],
            )
            if row and row["output"] is not None:
                out = row["output"]
                if isinstance(out, str):
                    try:
                        out = json.loads(out)
                    except Exception:
                        pass
                if isinstance(out, str) and out.strip():
                    text = out

            if ok and text:
                await self._store_story_review(
                    story_id, goal_id, reviewed_state, text, run_id,
                    row["instance_id"] if row else None,
                )
                await pool.execute(
                    "UPDATE stories SET last_reviewed_state=$1, updated_at=NOW() WHERE id=$2",
                    reviewed_state, story_id,
                )
                if reviewed_state == "done":
                    try:
                        from app.services.story_transitions import mark_story_reviewed
                        await mark_story_reviewed(pool, goal_id, story_id)
                    except Exception as exc:
                        log.warning("council: mark_story_reviewed failed story=%s: %s", story_id, exc)
                await self._deliver_story(goal_id, story_id, reviewed_state, text)

            status = "done" if ok else "failed"
            await pool.execute(
                "UPDATE council_review_runs SET status=$1, finished_at=NOW() WHERE id=$2",
                status, run_id,
            )
            log.info("council: STORY run=%s finished status=%s story=%s state=%s",
                     run_id, status, story_id, reviewed_state)
        except Exception as exc:
            log.exception("council: STORY run=%s failed: %s", run_id, exc)
            await pool.execute(
                "UPDATE council_review_runs SET status='failed', finished_at=NOW() WHERE id=$1", run_id,
            )

    async def _deliver_story(self, goal_id: str, story_id: str,
                             reviewed_state: str, text: str) -> bool:
        """Show the story review in the goal thread and hand it to the GO.

        MOVED TO `app/services/story_review_delivery.py`, which this now
        calls. The review has a second producer -- a reviewer running
        inside the execution pod -- and it has to reach the GO by
        exactly the same two surfaces. One function is how that stays
        true; two that agreed when they were written is how it stops
        being true six months later.
        """
        from app.services.story_review_delivery import deliver

        return await deliver(goal_id, story_id, text,
                             reviewed_state=reviewed_state)

    # Attribution label for council reviews in the orchestrator channel. It
    # matches no users.uid and no agents.id, so the orchestrator loader falls
    # back to this literal — the card renders as "Goal Review Council".
    COUNCIL_SENDER = "Goal Review Council"

    async def _deliver(self, goal_id: str, conv_stage_id: str) -> bool:
        pool = get_pool()
        row = await pool.fetchrow("SELECT output FROM council_review_stages WHERE id=$1", conv_stage_id)
        if not row or row["output"] is None:
            return False
        out = row["output"]
        # output holds the convener's review text (stored as a JSON string scalar).
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except Exception:
                pass
        packet = out if (isinstance(out, str) and out.strip()) else "(council produced no packet)"

        goal = await pool.fetchrow(
            "SELECT go_agent_id, go_thread_id FROM goals WHERE id=$1", goal_id,
        )
        if not goal or not goal["go_agent_id"] or not goal["go_thread_id"]:
            log.warning("council: cannot deliver — goal %s has no GO agent/thread", goal_id)
            return False
        title = "Goal Review Council"

        # 1. Persist a review card to the goal's ORCHESTRATOR channel so HUMANS
        #    see the review (attributed to "Goal Review Council"), keeping it out
        #    of the GO's main goal thread. This is the durable human-facing copy;
        #    the GO gets the full packet in its wake.
        thread_ok = True
        try:
            from app.services.orchestrator_delivery import persist_review_to_orchestrator
            thread_ok = await persist_review_to_orchestrator(
                goal_id, "goal", title, packet, self.COUNCIL_SENDER,
            )
        except Exception as exc:
            thread_ok = False
            log.warning("council: goal-review orchestrator persist failed for %s: %s", goal_id, exc)

        # 2. Wake the GO with the FULL review (marked) so the packet is in the
        #    GO's context to act on. The [REVIEW:*] marker triggers the
        #    review_action_planning skill; the GO replies on its main goal
        #    thread. The wake is NOT rendered in the goal-thread UI, so the main
        #    thread stays decluttered — the human-facing copy is the orchestrator
        #    card above.
        wake = f"[REVIEW:goal] {title}\n\n{packet}"
        await self._wake_go_to_reply(goal_id, wake)
        return thread_ok

    async def _wake_go_to_reply(self, goal_id: str, message: str) -> None:
        """Deliver a review to the GO as an inbound user turn so the GO REPLIES.

        Reuses the canonical wake-up path (ensure_go_session + chat.send, which
        wires the GO's on_message so its reply is persisted + broadcast; falls
        back to a user-role chat.inject when no session is warm). The review is
        a message TO the GO, never the GO's own words.

        mirror_to_mesh=False: every caller of this helper (_deliver_story,
        _deliver, deliver.deliver_packet) already calls
        persist_review_to_orchestrator itself first — a formatted review card
        in the council's own thread. Mirroring here too would duplicate that
        card, unformatted and marker-prefixed, straight into Agent Mesh.
        """
        try:
            from app.services.go_events import _send_system_message_to_go
            await _send_system_message_to_go(goal_id, message, wake_up=True, mirror_to_mesh=False)
        except Exception as exc:
            log.warning("council: GO wake-up delivery failed for %s: %s", goal_id, exc)

council_dispatcher = CouncilDispatcher()
