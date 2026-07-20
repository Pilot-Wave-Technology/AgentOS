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

import asyncpg

from app.database import get_pool
from app.services.council.provisioning import run_stage

log = logging.getLogger(__name__)

# Hard ceiling for a 'running' council run. A run only stays 'running' across
# ticks if the dispatcher process died mid-run (the tick processes a run
# synchronously), which otherwise wedges the global single-run gate forever.
# Council reviews finish in minutes, so 4h is a safe "definitely dead" bound.
COUNCIL_RUN_MAX_HOURS = 4


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

    async def _tick(self) -> bool:
        """Process at most one run. Returns True if a run was processed."""
        pool = get_pool()
        # Anti-block: free the queue of any run orphaned in 'running' before the
        # single-run gate below would otherwise see it and stall forever.
        await self._reap_stale_runs(pool)
        if await pool.fetchval("SELECT 1 FROM council_review_runs WHERE status='running' LIMIT 1"):
            return False  # global single-run
        # Deleted-goal guard: goal delete is soft (goals.status='deleted') and
        # does not cancel pending council runs, so without this a story_done
        # NOTIFY (or a stale pending run) would provision RA agents + clone repos
        # for a goal that no longer exists. Skip runs whose goal is not active.
        run = await pool.fetchrow(
            "SELECT crr.id, crr.goal_id, crr.kind, crr.story_id, crr.reviewed_state "
            "FROM council_review_runs crr "
            "WHERE crr.status='pending' "
            "  AND EXISTS (SELECT 1 FROM goals g WHERE g.id = crr.goal_id AND g.status = 'active') "
            "ORDER BY crr.created_at LIMIT 1",
        )
        if not run:
            return False
        # Kill switch — check before claiming so a paused goal doesn't start a new
        # council run. If blocked, leave the run in 'pending' for the next tick.
        blk = await _council_dispatch_block(run["goal_id"])
        if blk:
            log.info("council: dispatch blocked (scope=%s reason=%s) — skipping run=%s goal=%s",
                     blk.scope, blk.reason or "<none>", run["id"], run["goal_id"])
            return False
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

    async def _process_run(self, run_id: str, goal_id: str) -> None:
        pool = get_pool()
        log.info("council: processing run=%s goal=%s", run_id, goal_id)
        try:
            # Execution order: story reviews FIRST, then progress, then
            # goal_health, then convener. Story reviews land in story_reviews
            # before progress + goal_health run, so both can read them (progress
            # reconciles each review's state-at-review-time against the now-
            # current state; goal_health/convener fold them in). Interim story
            # reviews don't transition state or post to GO individually here.
            story_stages = await pool.fetch(
                "SELECT id, story_id, reviewed_state FROM council_review_stages "
                "WHERE run_id=$1 AND stage='story' ORDER BY created_at", run_id,
            )
            # Each story stage is isolated — one failing (or crashing) never stops
            # the others, progress, goal_health, or the convener.
            story_ok = 0
            for ss in story_stages:
                if await self._safe_run_stage(run_id, ss["id"], "story", goal_id):
                    try:
                        await self._record_interim_story_review(
                            ss["id"], goal_id, ss["story_id"], ss["reviewed_state"] or "", run_id,
                        )
                        story_ok += 1
                    except Exception:
                        log.exception("council: record interim story review failed (run=%s story=%s)",
                                      run_id, ss["story_id"])

            pr = await pool.fetchrow(
                "SELECT id FROM council_review_stages WHERE run_id=$1 AND stage='progress'", run_id,
            )
            pr_ok = await self._safe_run_stage(run_id, pr["id"], "progress", goal_id) if pr else False

            gh = await pool.fetchrow(
                "SELECT id FROM council_review_stages WHERE run_id=$1 AND stage='goal_health'", run_id,
            )
            gh_ok = await self._safe_run_stage(run_id, gh["id"], "goal_health", goal_id) if gh else False

            conv = await pool.fetchrow(
                "SELECT id FROM council_review_stages WHERE run_id=$1 AND stage='convener'", run_id,
            )
            conv_ok = False
            if conv:
                # The convener ALWAYS runs even if every upstream reviewer failed —
                # it is built to treat a missing/failed finding as a coverage gap
                # and still produce a packet that reports what failed.
                await pool.execute(
                    "UPDATE council_review_stages SET status='pending' "
                    "WHERE id=$1 AND status='blocked_on_deps'",
                    conv["id"],
                )
                conv_ok = await self._safe_run_stage(run_id, conv["id"], "convener", goal_id)

            if conv and conv_ok:
                delivered = await self._deliver(goal_id, conv["id"])
            elif conv:
                # Convener failed → no synthesized packet. Still tell humans + the
                # GO the council ran but couldn't synthesize, with the coverage,
                # so the run isn't silently invisible.
                await self._deliver_convener_failure(goal_id, run_id)
                delivered = False
            else:
                delivered = False

            status = "done" if conv_ok else "failed"
            await pool.execute(
                "UPDATE council_review_runs SET status=$1, finished_at=NOW() WHERE id=$2",
                status, run_id,
            )
            log.info("council: run=%s finished status=%s gh_ok=%s pr_ok=%s story_ok=%d conv_ok=%s delivered=%s",
                     run_id, status, gh_ok, pr_ok, story_ok, conv_ok, delivered)
        except Exception as exc:
            log.exception("council: run=%s failed: %s", run_id, exc)
            await pool.execute(
                "UPDATE council_review_runs SET status='failed', finished_at=NOW() WHERE id=$1", run_id,
            )

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

    async def _record_interim_story_review(self, stage_id: str, goal_id: str, story_id: str,
                                           reviewed_state: str, run_id: str) -> None:
        """Persist an in-goal-run story stage's review (interim): store to
        story_reviews + bump last_reviewed_state. No state transition (interim)
        and no individual GO delivery — the convener folds it into the packet."""
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT output, instance_id FROM council_review_stages WHERE id=$1", stage_id,
        )
        text = ""
        if row and row["output"] is not None:
            out = row["output"]
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except Exception:
                    pass
            if isinstance(out, str) and out.strip():
                text = out
        if not text:
            return
        await self._store_story_review(
            story_id, goal_id, reviewed_state, text, run_id,
            row["instance_id"] if row else None,
        )
        await pool.execute(
            "UPDATE stories SET last_reviewed_state=$1, updated_at=NOW() WHERE id=$2",
            reviewed_state, story_id,
        )

    async def _store_story_review(self, story_id: str, goal_id: str, reviewed_state: str,
                                  text: str, run_id: str, instance_id: str | None) -> None:
        """Upsert the per-(story, state) review row."""
        pool = get_pool()
        import uuid as _uuid
        await pool.execute(
            """INSERT INTO story_reviews
                   (id, story_id, goal_id, reviewed_state, review_text, reviewer_agent_id, run_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (story_id, reviewed_state) DO UPDATE
                   SET review_text = EXCLUDED.review_text,
                       reviewer_agent_id = EXCLUDED.reviewer_agent_id,
                       run_id = EXCLUDED.run_id,
                       reviewed_at = NOW()""",
            _uuid.uuid4().hex, story_id, goal_id, reviewed_state, text, instance_id, run_id,
        )

    async def _deliver_story(self, goal_id: str, story_id: str, reviewed_state: str,
                             text: str) -> bool:
        """Show the story review in the goal thread (attributed to the council)
        and deliver it to the GO as an inbound message that the GO REPLIES to."""
        pool = get_pool()
        goal = await pool.fetchrow(
            "SELECT go_agent_id, go_thread_id FROM goals WHERE id=$1", goal_id,
        )
        if not goal or not goal["go_agent_id"] or not goal["go_thread_id"]:
            log.warning("council: cannot deliver story review — goal %s has no GO agent/thread", goal_id)
            return False
        kind = "final" if reviewed_state == "done" else f"interim ({reviewed_state})"
        title = f"Story Review — {story_id} ({kind})"

        # Surface 2: a visible review card in the goal's ORCHESTRATOR channel
        # (humans see it there — kept out of the GO's main goal thread). This is
        # the durable human-facing copy; the GO gets the full review in its wake.
        thread_ok = True
        try:
            from app.services.orchestrator_delivery import persist_review_to_orchestrator
            thread_ok = await persist_review_to_orchestrator(
                goal_id, "story", title, text, self.COUNCIL_SENDER,
            )
        except Exception as exc:
            thread_ok = False
            log.warning("council: story-review orchestrator persist failed for %s/%s: %s",
                        goal_id, story_id, exc)

        # Surface 1: wake the GO with the FULL review (marked) so the report is in
        # the GO's context to act on. The [REVIEW:*] marker triggers the
        # review_action_planning skill; the GO replies on its main goal thread.
        # The wake is NOT rendered in the goal-thread UI, so the main thread stays
        # decluttered — the human-facing copy is the orchestrator card above.
        wake = f"[REVIEW:story] {title}\n\n{text}"
        await self._wake_go_to_reply(goal_id, wake)
        return thread_ok

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
        _deliver, _deliver_convener_failure) already calls
        persist_review_to_orchestrator itself first — a formatted review card
        in the council's own thread. Mirroring here too would duplicate that
        card, unformatted and marker-prefixed, straight into Agent Mesh.
        """
        try:
            from app.services.go_events import _send_system_message_to_go
            await _send_system_message_to_go(goal_id, message, wake_up=True, mirror_to_mesh=False)
        except Exception as exc:
            log.warning("council: GO wake-up delivery failed for %s: %s", goal_id, exc)

    async def _deliver_convener_failure(self, goal_id: str, run_id: str) -> None:
        """The convener stage failed, so there is no synthesized packet to deliver.
        Post a short, coverage-aware note to the orchestrator channel (attributed
        to the council) and inform the GO — WITHOUT forcing a reply (nothing
        actionable) — so a failed run is never silently invisible to humans or the
        GO. The orchestrator persist and GO wake each guard their own targets, so
        there is no goal-level early-return here."""
        pool = get_pool()

        rows = await pool.fetch(
            "SELECT stage, status FROM council_review_stages WHERE run_id=$1", run_id,
        )

        def _has(stage: str, status: str) -> bool:
            return any(r["stage"] == stage and r["status"] == status for r in rows)

        def _count(stage: str, status: str) -> int:
            return sum(1 for r in rows if r["stage"] == stage and r["status"] == status)

        gh = "✓ completed" if _has("goal_health", "done") else "✗ failed"
        pr = "✓ completed" if _has("progress", "done") else "✗ failed"
        st_ok, st_fail = _count("story", "done"), _count("story", "failed")

        message = "\n".join([
            "## 🏛️ Goal Review Council — run incomplete",
            "",
            "The council ran but **could not synthesize a final review** — the convener step "
            "failed. The individual reviews that completed are recorded but were not combined "
            "into a packet, so there are no consolidated recommendations this time.",
            "",
            "**Coverage this run:**",
            f"- Goal-health review: {gh}",
            f"- Progress review: {pr}",
            f"- Story reviews: {st_ok} completed, {st_fail} failed",
            "",
            "No action needed — the next scheduled run (every 12h) or a manual trigger will retry.",
        ])

        # Humans: a visible "Goal Review Council" card in the orchestrator channel.
        try:
            from app.services.orchestrator_delivery import persist_review_to_orchestrator
            await persist_review_to_orchestrator(
                goal_id, "goal", "Goal Review Council — run incomplete", message,
                self.COUNCIL_SENDER,
            )
        except Exception as exc:
            log.warning("council: convener-failure note persist failed for %s: %s", goal_id, exc)

        # GO: aware in-context, but no reply forced (nothing to act on). Already
        # persisted as a card above — mirror_to_mesh=False avoids duplicating it.
        try:
            from app.services.go_events import _send_system_message_to_go
            await _send_system_message_to_go(goal_id, message, wake_up=False, mirror_to_mesh=False)
        except Exception as exc:
            log.debug("council: convener-failure note to GO failed for %s: %s", goal_id, exc)


council_dispatcher = CouncilDispatcher()
