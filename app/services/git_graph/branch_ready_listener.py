"""Component C — re-provision a story after the git engine cuts its branch.

When a story's SHCA kickoff runs before the engine has cut the workstream branch,
`provision_story_workspace` returns `{ok:False, waiting:True}` and the kickoff
defers. The engine later cuts + links the branch and fires `NOTIFY
story_branch_ready`. This WEB-TIER listener catches that and re-runs the SHCA
kickoff, which now provisions successfully and starts the assistant on the
engine's branch.

Lives in the web tier ONLY (started from app/main.py lifespan, gated by
`gitgraph_enabled`) — it needs provisioning + gateway access, which the standalone
git-engine worker does not run. Mirrors the council dispatcher's LISTEN+reconnect
loop, plus a poll backstop (NOTIFY is not durable — a signal fired while this
listener is down would otherwise be lost).

Imports of the web-tier kickoff are done lazily inside the handler so importing
this module never pulls provisioning/router code into the engine worker.
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.database import get_pool

from .dispatch import STORY_BRANCH_READY_CHANNEL, GIT_PR_OPENED_CHANNEL
from .story_threads import threads_for_node

log = logging.getLogger(__name__)

# Stall escalation (§0/§10): if a lease holder holds its (repo,trunk) lane this long
# WITH workstreams waiting, notify (NO release — the engine never auto-releases; a
# human decides whether to cancel). Re-notify at most once per stall period.
_STALL_SECONDS = 24 * 3600
_STALL_HOURS = 24

# Re-provision lock retry (2026-08-20 fix): a multi-write story's member repos get
# their branches cut a few seconds apart (routine engine behaviour), each cut firing
# its own NOTIFY for the SAME story. The per-story advisory lock used to be a
# non-blocking try-lock whose failure path silently dropped the NOTIFY — so a 2nd/3rd
# cut landing while the 1st cut's ~5s re-provision RPC was still in flight was lost
# forever, with zero log line. Confirmed live on story e77e0b65: 2 of 3 write repos
# permanently dropped, workspace_state stuck at 'ready'. Retrying a few times with a
# short delay, bounded well past one provisioning attempt's typical duration, turns
# that silent drop into a delayed-but-successful re-provision instead.
_LOCK_RETRY_MAX_ATTEMPTS = 8
_LOCK_RETRY_DELAY_S = 4


class BranchReadyListener:
    def __init__(self) -> None:
        self._listen_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Retain in-flight NOTIFY-triggered re-provision tasks so they aren't GC'd
        # mid-flight and are awaited on stop().
        self._pending: set[asyncio.Task] = set()
        # Backstop sweep cadence. The NOTIFY path handles the common case in
        # ~real time; this only catches signals missed while the listener was down.
        self._poll_seconds = 45
        # Per-node last-stall-notify epoch (monotonic) — dedup the escalation notice.
        self._stall_notified: dict[str, float] = {}

    def start(self) -> None:
        if self._listen_task is None:
            self._stop.clear()
            self._listen_task = asyncio.create_task(self._listen_notifications())
            self._poll_task = asyncio.create_task(self._poll_forever())
            log.info("git branch-ready listener started (LISTEN %s + %ds poll)",
                     STORY_BRANCH_READY_CHANNEL, self._poll_seconds)

    async def stop(self) -> None:
        self._stop.set()
        tasks = [t for t in (self._listen_task, self._poll_task) if t is not None]
        tasks.extend(self._pending)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._listen_task = None
        self._poll_task = None

    # ── NOTIFY path (fast) ──────────────────────────────────────────────────────

    def _on_branch_ready(self, _conn, _pid, _channel, payload) -> None:
        """asyncpg LISTEN callback — don't block here; schedule the re-provision."""
        try:
            data = json.loads(payload) if payload else {}
        except Exception:
            data = {}
        goal_id = data.get("goal_id")
        story_id = data.get("story_id")
        if goal_id and story_id:
            t = asyncio.create_task(self._reprovision_story(goal_id, story_id))
            self._pending.add(t)
            t.add_done_callback(self._pending.discard)

    def _on_pr_opened(self, _conn, _pid, _channel, payload) -> None:
        """asyncpg LISTEN callback for git_pr_opened — schedule the chat notice."""
        try:
            data = json.loads(payload) if payload else {}
        except Exception:
            data = {}
        goal_id = data.get("goal_id")
        node_id = data.get("node_id")
        if goal_id and node_id:
            t = asyncio.create_task(self._post_pr_opened(goal_id, node_id))
            self._pending.add(t)
            t.add_done_callback(self._pending.discard)

    async def _listen_notifications(self) -> None:
        """Hold a pool connection LISTENing on story_branch_ready; reconnect on drop."""
        while not self._stop.is_set():
            pool = get_pool()
            conn = None
            try:
                conn = await pool.acquire()
                await conn.add_listener(STORY_BRANCH_READY_CHANNEL, self._on_branch_ready)
                await conn.add_listener(GIT_PR_OPENED_CHANNEL, self._on_pr_opened)
                log.info("git branch-ready: LISTEN %s + %s active",
                         STORY_BRANCH_READY_CHANNEL, GIT_PR_OPENED_CHANNEL)
                while not self._stop.is_set() and not conn.is_closed():
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
            except Exception as exc:
                log.warning("git branch-ready: listener error: %s", exc)
            finally:
                if conn is not None:
                    try:
                        await conn.remove_listener(STORY_BRANCH_READY_CHANNEL, self._on_branch_ready)
                    except Exception:
                        pass
                    try:
                        await conn.remove_listener(GIT_PR_OPENED_CHANNEL, self._on_pr_opened)
                    except Exception:
                        pass
                    try:
                        await pool.release(conn)
                    except Exception:
                        pass
            if not self._stop.is_set():
                await asyncio.sleep(2)  # backoff before reconnect

    # ── poll backstop (covers a NOTIFY missed while down) ───────────────────────

    async def _poll_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._scan()
            except Exception:
                log.exception("git branch-ready: poll scan error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _scan(self) -> None:
        """Find operator stories that were imported (have an SHCA thread) but are
        still un-provisioned (workspace_state='none' — the lazy-wait leaves it
        unchanged) AND whose workstream branch is now cut (node past PLANNED/
        BRANCHING) → re-provision them."""
        pool = get_pool()
        rows = await pool.fetch(
            "SELECT DISTINCT s.id AS story_id, s.goal_id "
            "FROM stories s "
            "JOIN threads t ON t.id = s.active_thread_id "
            "JOIN git_node_stories gns ON gns.story_id = s.id "
            "JOIN git_nodes n ON n.id = gns.git_node_id "
            "WHERE s.execution_mode = 'operator' "
            "  AND s.active_thread_id <> '' "
            "  AND s.workspace_state = 'none' "
            "  AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected') "
            "  AND n.state NOT IN ('PLANNED', 'BRANCHING') "
            "LIMIT 50",
        )
        for r in rows:
            await self._reprovision_story(r["goal_id"], r["story_id"])

        # Engine-spawned diff stories that still lack a workspace: created_by='engine',
        # operator `code`, in a merge group, assignee set, workstream branch cut (node
        # past PLANNED/BRANCHING), and workspace_state='none'. Gating on workspace_state
        # (NOT active_thread_id) makes this self-healing for BOTH cases funnelling into
        # _reprovision_story: an as-yet-UNBOUND story (no active_thread_id → engine-diff
        # bind+kickoff branch) AND a story that was already bound to an SHCA but whose
        # kickoff never provisioned the workspace (has active_thread_id → standard
        # re-provision branch). The latter is otherwise unreachable — diff stories link
        # via merge_group_id, not git_node_stories, so the first (operator) poll query
        # above never matches them. The NOTIFY path handles the common case in ~realtime;
        # this poll catches a missed/failed signal.
        diffs = await pool.fetch(
            "SELECT DISTINCT s.id AS story_id, s.goal_id "
            "FROM stories s "
            "JOIN git_merge_group_nodes gmn ON gmn.merge_group_id = s.merge_group_id "
            "JOIN git_nodes n ON n.id = gmn.git_node_id "
            "WHERE s.created_by = 'engine' AND s.story_kind = 'code' "
            "  AND s.merge_group_id IS NOT NULL AND s.execution_mode = 'operator' "
            "  AND s.workspace_state = 'none' AND COALESCE(s.assigned_to, '') <> '' "
            "  AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected') "
            "  AND n.state NOT IN ('PLANNED', 'BRANCHING') "
            "LIMIT 50",
        )
        for r in diffs:
            await self._reprovision_story(r["goal_id"], r["story_id"])

        # Multi-repo fix, piece 1c backstop (2026-08-20): 'ready' is a legitimate,
        # common OUTCOME of a partial provision, not proof the job is done — see
        # `is_write_repo_set_incomplete`'s docstring. The NOTIFY path (Component C)
        # is supposed to catch a repo whose branch arrives late, but it's a
        # non-durable signal and its retry (see _LOCK_RETRY_*) is still bounded, so
        # this sweep is the last-resort net: it re-checks EVERY 'ready' operator
        # story, not just ones the NOTIFY path never reached in time. Confirmed live
        # gap (story e77e0b65): the try-lock used to drop this silently forever with
        # no backstop at all, since this poll previously only looked at
        # workspace_state='none'.
        ready = await pool.fetch(
            "SELECT DISTINCT s.id AS story_id, s.goal_id "
            "FROM stories s "
            "JOIN threads t ON t.id = s.active_thread_id "
            "WHERE s.execution_mode = 'operator' "
            "  AND s.active_thread_id <> '' "
            "  AND s.workspace_state = 'ready' "
            "  AND s.state NOT IN ('done', 'reviewed', 'cancelled', 'rejected') "
            "LIMIT 50",
        )
        if ready:
            from app.services.operator_provisioning import is_write_repo_set_incomplete
            for r in ready:
                try:
                    if await is_write_repo_set_incomplete(pool, r["goal_id"], r["story_id"]):
                        log.info(
                            "git branch-ready: poll backstop found story=%s 'ready' "
                            "but still missing repo(s) — retrying", r["story_id"])
                        await self._reprovision_story(r["goal_id"], r["story_id"])
                except Exception:
                    log.exception(
                        "git branch-ready: incomplete-write-repo-set check failed "
                        "for story=%s", r["story_id"])

        await self._scan_pr_opened()
        await self._scan_stalled_lanes()

    # ── PR-opened chat notice ───────────────────────────────────────────────────

    async def _scan_pr_opened(self) -> None:
        """Backstop for the git_pr_opened NOTIFY: post the "review + merge" notice for
        any node that has a pinned PR but no pr_opened_notified_at yet (a signal fired
        while this listener was down). The claim in _post_pr_opened dedups."""
        pool = get_pool()
        rows = await pool.fetch(
            "SELECT id, goal_id FROM git_nodes "
            "WHERE pr_number IS NOT NULL AND pr_opened_notified_at IS NULL "
            "  AND state NOT IN ('MERGED', 'ABANDONED') "
            "LIMIT 50",
        )
        for r in rows:
            await self._post_pr_opened(r["goal_id"], r["id"])

    async def _post_pr_opened(self, goal_id: str, node_id: str) -> None:
        """Post a one-time "PR open — please merge" card to the assistant chat(s) of the
        workstream's member code stories, + an in-app bell to the merge assignee.

        Idempotent across replicas + the NOTIFY/poll paths: atomically CLAIM the node
        (pr_opened_notified_at NULL -> now()) and only post if THIS call won the claim."""
        pool = get_pool()
        node = await pool.fetchrow(
            "UPDATE git_nodes n SET pr_opened_notified_at = now() "
            "FROM git_repos r "
            "WHERE n.id = $1 AND n.goal_id = $2 AND n.git_repo_id = r.id "
            "  AND n.pr_number IS NOT NULL AND n.pr_opened_notified_at IS NULL "
            "  AND n.state NOT IN ('MERGED', 'ABANDONED') "
            "RETURNING n.branch_name, n.trunk_branch, n.pr_number, r.owner, r.name",
            node_id, goal_id,
        )
        if not node:
            return  # no PR pinned / already notified / terminal — nothing to do

        # Owner = the workstream's MERGE-story assignee (the human who should merge);
        # fall back to any member CODE story's assignee. NOTE: we must NOT use the code
        # story's active_thread_id — the PR opens only once the node is READY, which
        # requires every member story SETTLED ('done' clears active_thread_id). So the
        # binding is already gone; we resolve the owner's PERSISTENT SHCA thread instead.
        assignee = await pool.fetchval(
            "SELECT s.assigned_to FROM stories s "
            "JOIN git_merge_group_nodes gmn ON gmn.merge_group_id = s.merge_group_id "
            "WHERE gmn.git_node_id = $1 AND s.story_kind = 'merge' "
            "  AND COALESCE(s.assigned_to, '') <> '' LIMIT 1",
            node_id,
        ) or await pool.fetchval(
            "SELECT s.assigned_to FROM git_node_stories gns JOIN stories s ON s.id = gns.story_id "
            "WHERE gns.git_node_id = $1 AND s.story_kind = 'code' "
            "  AND COALESCE(s.assigned_to, '') <> '' LIMIT 1",
            node_id,
        )

        pr_url = f"https://github.com/{node['owner']}/{node['name']}/pull/{node['pr_number']}"
        trunk = node["trunk_branch"] or "the trunk"
        content = (
            f"🔀 **Merge PR open — `{node['branch_name']}` → `{trunk}`** · "
            f"[#{node['pr_number']}]({pr_url})\n\n"
            f"**Please review and merge this PR on GitHub before continuing.** "
            f"Once you merge it, the engine detects the merge automatically — the branch "
            f"lands, dependent stories unblock, and there's nothing else to do here."
        )
        # `body` carries the markdown so the generic card renderer (title + renderMd(body))
        # shows the link + instruction; `content` is the same text persisted on the row.
        card = {
            "card_type": "git_pr_opened",
            "kind": "git_pr_opened",
            "agent_name": "Git Engine",
            "title": f"PR open — please review & merge: {node['branch_name']} → {trunk}",
            "body": content,
            "pr_url": pr_url,
            "pr_number": node["pr_number"],
            "branch": node["branch_name"],
        }

        # THE THREADS OF THE STORIES THIS BRANCH CARRIES — see
        # git_graph/story_threads.py for why this replaced a per-user lookup.
        #
        # The previous resolution asked for the assignee's SHCA thread in this
        # goal, `ORDER BY created_at LIMIT 1`, and was right while a person had
        # exactly one: "oldest" was "only". Stage K gave every story its own
        # thread and that query silently became "the first story they ever
        # opened here" — so every merge notice in the goal landed in one
        # arbitrary conversation and never in the one being merged.
        #
        # A node can carry several member stories (a shared workstream lands
        # everyone's work at once), so this is a LIST and all of them are told.
        # A story spanning several repos gets one notice per node on its single
        # thread, which is exactly what happened to it.
        targets = await threads_for_node(pool, node_id)

        posted = False
        if targets:
            from app.assistant_relay import post_system_card_to_thread
            for t in targets:
                try:
                    await post_system_card_to_thread(
                        thread_id=t["thread_id"], goal_id=goal_id, card=card,
                        content_markdown=content,
                        notify_uid=t["uid"] or assignee or "",
                        notify_title="PR ready to merge",
                        notify_type="git_pr_opened",
                    )
                    posted = True
                except Exception:
                    log.exception("git pr-opened: post to thread %s failed",
                                  t["thread_id"])

        # The bell is the fallback for "nowhere to post", which is a real state:
        # the story was never imported into an assistant, or its thread has
        # already been closed because the work landed. It also covers every
        # post failing. Losing the notice entirely is the only outcome that
        # would be wrong.
        if not posted and assignee:
            try:
                from app.notifications import push_notification
                await push_notification(
                    assignee, type="git_pr_opened", title="PR ready to merge",
                    body=content[:200], goal_id=goal_id, link=f"/goals/{goal_id}",
                )
            except Exception:
                log.exception("git pr-opened: bell to %s failed", assignee)
        elif not posted:
            log.warning("git pr-opened: node %s has no assignee and no story "
                        "thread — notice not delivered", node_id)

        log.info("git pr-opened: node %s (PR #%s) -> %s thread(s) %s; owner=%s",
                 node_id, node["pr_number"], len(targets),
                 [t["thread_id"][:8] for t in targets] or "(none)",
                 assignee or "(none)")

    async def _scan_stalled_lanes(self) -> None:
        """Stall-escalation NOTIFICATION (§0/§10) — NO release. If a lease holder has
        held its (repo,trunk) lane past the threshold WITH workstreams waiting, notify
        the holder's goal + each waiting goal so a human can decide whether to cancel
        (close) its PR. The engine NEVER auto-releases — this only makes the freeze
        visible/escapable. Deduped per node (re-notify at most once per stall period)."""
        import time as _time
        pool = get_pool()
        holders = await pool.fetch(
            "SELECT q.node_id, q.git_repo_id, q.trunk_branch, q.goal_id, "
            "       n.branch_name, r.owner, r.name "
            "FROM git_merge_queue q JOIN git_nodes n ON n.id = q.node_id "
            "JOIN git_repos r ON r.id = q.git_repo_id "
            "WHERE q.status = 'active' AND q.active_since IS NOT NULL "
            "  AND q.active_since < now() - make_interval(secs => $1)",
            _STALL_SECONDS,
        )
        now = _time.monotonic()
        for h in holders:
            if now - self._stall_notified.get(h["node_id"], 0.0) < _STALL_SECONDS:
                continue
            waiters = await pool.fetch(
                "SELECT DISTINCT goal_id FROM git_merge_queue "
                "WHERE git_repo_id = $1 AND trunk_branch = $2 AND status = 'waiting'",
                h["git_repo_id"], h["trunk_branch"],
            )
            if not waiters:
                continue   # nobody blocked → no cross-goal harm → don't nag
            self._stall_notified[h["node_id"]] = now
            msg = (
                f"⚠️ The merge lane for {h['owner']}/{h['name']} (trunk {h['trunk_branch']}) "
                f"has been held by `{h['branch_name']}` for over {_STALL_HOURS}h with "
                f"{len(waiters)} workstream(s) waiting. If it's stuck, a human can free the "
                f"lane by cancelling (closing) its PR — the engine never auto-releases."
            )
            try:
                from app.services.go_events import _send_system_message_to_go
                for g in {h["goal_id"]} | {w["goal_id"] for w in waiters}:
                    await _send_system_message_to_go(g, msg, wake_up=False)
            except Exception:
                log.warning("git branch-ready: stall escalation notify failed node=%s",
                            h["node_id"])

    # ── shared re-provision (idempotent; the kickoff has its own 2-min guard) ───

    async def _reprovision_story(self, goal_id: str, story_id: str, *, attempt: int = 1) -> None:
        pool = get_pool()
        # Serialize per-story across replicas + the NOTIFY/poll paths. control-panel
        # runs multiple instances, each LISTENing + polling; without this lock,
        # concurrent re-provisions would race the kickoff's marker guard and fire
        # duplicate Story-Mode kickoffs into the same SHCA session. The lock is held
        # on a dedicated connection for the duration of the re-provision only.
        lock_key = f"branch-ready:{story_id}"
        conn = await pool.acquire()
        lock_acquired = False
        try:
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", lock_key)
            lock_acquired = bool(got)
            if lock_acquired:
                # A bare `return` from inside this branch (the common case, e.g.
                # "nothing to do for this story") must NOT trip the retry below — it
                # only exists to unwind out of `_do_reprovision`, so the actual work
                # lives in its own method rather than inline here.
                await self._do_reprovision(conn, goal_id, story_id)
        except Exception:
            log.exception("git branch-ready: re-provision failed for story=%s", story_id)
        finally:
            if lock_acquired:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(hashtext($1)::bigint)", lock_key)
                except Exception:
                    pass
            try:
                await pool.release(conn)
            except Exception:
                pass
        if not lock_acquired:
            if attempt < _LOCK_RETRY_MAX_ATTEMPTS:
                await asyncio.sleep(_LOCK_RETRY_DELAY_S)
                await self._reprovision_story(goal_id, story_id, attempt=attempt + 1)
            else:
                log.warning(
                    "git branch-ready: story=%s still locked after %d attempt(s) "
                    "(~%ds) — giving up this NOTIFY; the poll backstop will retry "
                    "if the write-repo set is still incomplete",
                    story_id, attempt, attempt * _LOCK_RETRY_DELAY_S,
                )

    async def _do_reprovision(self, conn, goal_id: str, story_id: str) -> None:
        """The actual re-provision work, run only once the per-story advisory lock
        is held. Split out from `_reprovision_story` so its many early `return`s
        (all meaning "nothing to do for this story") can't be mistaken by the
        caller for "the lock was contended, retry" (see `_LOCK_RETRY_*`)."""
        pool = get_pool()
        # Deleted-goal guard (covers BOTH the poll scan and the NOTIFY path,
        # since both funnel here): goal delete is soft (goals.status='deleted')
        # and doesn't cascade to stories, so without this we'd re-provision a
        # story assistant — new thread, SHCA bind, kickoff — for a dead goal.
        row = await conn.fetchrow(
            "SELECT s.active_thread_id, s.workspace_state, s.execution_mode, s.state, "
            "       s.created_by, s.merge_group_id, s.assigned_to, s.story_kind, "
            "       t.agent_id "
            "FROM stories s LEFT JOIN threads t ON t.id = s.active_thread_id "
            "WHERE s.id = $1 AND s.goal_id = $2 "
            "  AND EXISTS (SELECT 1 FROM goals g WHERE g.id = s.goal_id AND g.status = 'active')",
            story_id, goal_id,
        )
        if not row:
            return
        thread_id = row["active_thread_id"]
        agent_id = row["agent_id"]

        # Engine-spawned diff/fix story (created_by='engine', in a merge group,
        # operator code) that is NOT yet bound to an SHCA → bind it to its
        # ASSIGNEE's SHCA + kick off, mirroring the UI import sequence
        # (provision_shca_thread_for_story → refresh_story_resources → kickoff).
        # The grind loop spawns a DIFF story whenever the branch is behind trunk
        # (the assistant integrates trunk + reconciles + tests — the engine no longer
        # server-merges) and a FIX story when an open PR's required checks fail.
        is_engine_diff = (
            row["created_by"] == "engine" and row["merge_group_id"]
            and (row["story_kind"] or "") == "code"
            # Local Execution Mode: a local story is driven by the extension, never
            # gateway-dispatched here — this branch runs BEFORE the general
            # execution_mode guard below, so exclude non-operator here too.
            and (row["execution_mode"] or "") == "operator"
        )
        if is_engine_diff and not thread_id:
            assignee = row["assigned_to"] or ""
            if row["state"] in ("done", "reviewed", "cancelled", "rejected"):
                return  # already settled
            if not assignee:
                log.warning(
                    "git branch-ready: engine diff story=%s has no assignee — cannot "
                    "dispatch (needs escalation; check the workstream owner)", story_id)
                return
            from app.routers.assistant import (
                provision_assistant_thread_for_story, dispatch_story_kickoff_to_assistant)
            try:
                t_id, a_id, assistant_busy = await provision_assistant_thread_for_story(
                    assignee, goal_id, story_id, engine_initiated=True)
            except Exception:
                log.exception(
                    "git branch-ready: binding engine diff story=%s failed", story_id)
                return
            if not t_id or not a_id:
                return
            if assistant_busy:
                log.info(
                    "git branch-ready: engine diff story=%s bound but NOT started — "
                    "assignee=%s's assistant is already working on another story",
                    story_id, assignee)
                return
            try:
                from app.resource_provisioning import refresh_story_resources
                await refresh_story_resources(goal_id, story_id)
            except Exception:
                log.warning(
                    "git branch-ready: refresh_story_resources failed for engine "
                    "diff story=%s", story_id)
            ok = await dispatch_story_kickoff_to_assistant(
                goal_id, story_id, t_id, a_id, user_uid=assignee)
            log.info("git branch-ready: dispatched engine diff story=%s ok=%s",
                     story_id, ok)
            return
        # Only re-provision a story that was actually imported to an SHCA
        # (thread + agent), is in operator mode, isn't already provisioned, and
        # is still live. Everything else is a no-op (NOTIFY fires for every
        # member of the cut node, incl. downstream stories not yet imported).
        if not thread_id or not agent_id:
            return
        if (row["execution_mode"] or "") != "operator":
            return
        if row["workspace_state"] == "ready":
            # Multi-repo fix, piece 1c (research/MULTI_REPO_STORIES_PLAN.md §12.5):
            # "ready" alone is NOT "this listener's job is done" for a multi-repo
            # story. `provision_story_workspace` marks the workspace ready as soon
            # as it clones whatever write repos have a live branch RIGHT NOW,
            # skipping any that don't yet — a legitimate, common outcome, not an
            # error. If a repo skipped that way is exactly the one whose branch
            # this NOTIFY is for, returning here means NOTHING ever re-dispatches
            # kickoff for it: dispatch_story_kickoff_to_assistant's own piece-1b
            # guard (assistant.py) correctly defers kickoff for an incomplete set,
            # but that deferral is only useful if something re-checks later — and
            # this listener is that something. Skip the early return when the
            # write-repo set is still incomplete; fall through to re-dispatch.
            from app.services.operator_provisioning import is_write_repo_set_incomplete
            if not await is_write_repo_set_incomplete(pool, goal_id, story_id):
                return  # genuinely complete — nothing left for this listener to do
        if row["state"] in ("done", "cancelled", "rejected"):
            return
        from app.routers.assistant import dispatch_story_kickoff_to_assistant
        ok = await dispatch_story_kickoff_to_assistant(goal_id, story_id, thread_id, agent_id)
        log.info("git branch-ready: re-provisioned story=%s ok=%s", story_id, ok)


branch_ready_listener = BranchReadyListener()
