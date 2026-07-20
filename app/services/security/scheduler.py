"""Security Council scheduler — periodically enqueues a security review for the
trunk of every repo in play plus only the feature branches whose git-engine
workstream is still open (not merged/abandoned), across non-deleted goals.

Complements (does not replace) the manual shield-button trigger. The skip logic
in enqueue_branch_review (trigger != 'manual') means a scheduled batch only spends
runs on branches that actually CHANGED since their last fully-successful review —
unchanged branches are skipped; manual triggers always run.

Design (mirrors the Goal Review Council scheduler):
* The schedule ledger is `security_review_runs` itself: a batch is "due" when no
  run with `trigger='scheduled'` was created within the last `interval_hours`.
* The loop ticks every `tick_seconds` and fires the batch the first time it
  observes the window has elapsed (survives restarts; no cadence drift).
* Per-branch dedup + global single-run are DB-enforced, so the fanned-out batch
  drains one branch at a time.
* Security reviews are heavier than the goal loop, so the default cadence is
  weekly (168h). Gated OFF by default (SECURITY_SCHEDULE_ENABLED).
"""
from __future__ import annotations

import asyncio
import logging
import os

from app.database import get_pool

log = logging.getLogger(__name__)

# git_nodes.state values where the feature branch EXISTS on the remote and the
# workstream is still open. Explicit allowlist (not a denylist) so a future new
# state doesn't silently get scanned. Excludes:
#   PLANNED / BRANCHING  — branch not cut yet (nothing to clone),
#   MERGED / ABANDONED   — workstream closed (landed or torn down).
# ESCALATED / CONFLICTED / PROTECTION_BLOCKED are stalled-but-live: the branch has
# real code worth scanning, and the change-gate skips it once reviewed unchanged.
_OPEN_NODE_STATES: list[str] = [
    "BRANCH_CREATED", "IN_PROGRESS", "READY", "QUEUED", "SYNCING",
    "MERGEABLE", "MERGING", "CONFLICTED", "PROTECTION_BLOCKED", "ESCALATED",
]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class SecurityScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._enabled = _env_bool("SECURITY_SCHEDULE_ENABLED", False)
        self._interval_hours = _env_int("SECURITY_SCHEDULE_HOURS", 168)  # weekly
        self._tick_seconds = _env_int("SECURITY_SCHEDULE_TICK_SECONDS", 1800)

    def start(self) -> None:
        if not self._enabled:
            log.info("security scheduler disabled (SECURITY_SCHEDULE_ENABLED=false)")
            return
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever())
            log.info("security scheduler started — every %dh over active branch resources (tick %ds)",
                     self._interval_hours, self._tick_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run_forever(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self._maybe_run_batch()
            except Exception as exc:  # never let the loop die
                log.exception("security scheduler tick error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _maybe_run_batch(self) -> None:
        pool = get_pool()

        # Due iff no scheduled run was created within the last interval (DB clock).
        due = await pool.fetchval(
            "SELECT NOT EXISTS ("
            "  SELECT 1 FROM security_review_runs "
            "  WHERE trigger = 'scheduled' "
            "    AND created_at > now() - make_interval(hours => $1))",
            self._interval_hours,
        )
        if not due:
            return

        # Review scope: the TRUNK of every repo in play + only FEATURE branches
        # whose git-engine workstream is still OPEN. Driven off git_nodes (whose
        # state retires to MERGED/ABANDONED) rather than the full set of registered
        # branch resources — so merged / torn-down feature branches drop out of the
        # scan set automatically, and we never scan every branch off a trunk.
        # Deduped by (clone_url, branch_name); delivery still fans out to all goals.
        branches = await pool.fetch(
            """
            WITH scope AS (
                -- (a) TRUNK of every repo registered to a non-deleted goal — sourced
                --     from the repo resource (NOT git_nodes), so a repo with no open
                --     workstream (brand-new, or all branches merged) still gets its
                --     trunk scanned. Trunk branch: the engine's git_repos.trunk_branch
                --     if reconciled, else the repo resource's default_branch, else main.
                SELECT DISTINCT
                       lower(gr.clone_url) AS ci_url,
                       lower(COALESCE(NULLIF(grepo.trunk_branch, ''),
                                      NULLIF(gr.default_branch, ''), 'main')) AS ci_branch,
                       gr.clone_url AS clone_url,
                       COALESCE(NULLIF(grepo.trunk_branch, ''),
                                NULLIF(gr.default_branch, ''), 'main') AS branch_name,
                       gr.repo_owner AS repo_owner, gr.repo_name AS repo_name
                FROM goal_resources gr
                JOIN goals g ON g.id = gr.goal_id
                LEFT JOIN git_repos grepo ON lower(grepo.clone_url) = lower(gr.clone_url)
                WHERE g.status = 'active' AND gr.status = 'active' AND gr.kind = 'repo'
                  AND COALESCE(gr.clone_url, '') <> ''
                UNION
                -- (b) FEATURE branches whose git-engine workstream is still OPEN — i.e.
                --     the branch has been cut and the node is neither closed
                --     (MERGED/ABANDONED) nor pre-cut (PLANNED/BRANCHING, no branch yet).
                SELECT DISTINCT
                       lower(r.clone_url), lower(n.branch_name),
                       r.clone_url, n.branch_name, r.owner, r.name
                FROM git_nodes n
                JOIN git_repos r ON r.id = n.git_repo_id
                JOIN goals g ON g.id = n.goal_id
                WHERE g.status = 'active'
                  AND n.state = ANY($1::text[])
                  AND COALESCE(n.branch_name, '') <> '' AND COALESCE(r.clone_url, '') <> ''
            )
            SELECT DISTINCT ON (ci_url, ci_branch)
                   clone_url, branch_name, repo_owner, repo_name
            FROM scope
            ORDER BY ci_url, ci_branch
            """,
            _OPEN_NODE_STATES,
        )
        if not branches:
            log.info("security scheduler: batch due but no trunk / open-workstream branches to review")
            return

        from app.services.security.runs import enqueue_branch_review

        created = skipped_unchanged = skipped_active = 0
        for b in branches:
            branch = {
                "resource_id": None, "clone_url": b["clone_url"],
                "branch_name": b["branch_name"],
                "repo_owner": b["repo_owner"] or "", "repo_name": b["repo_name"] or "",
            }
            try:
                res = await enqueue_branch_review(
                    branch, created_by="security-scheduler", trigger="scheduled",
                )
                if res.get("created"):
                    created += 1
                elif res.get("skipped"):
                    skipped_unchanged += 1   # unchanged since last full review
                else:
                    skipped_active += 1      # already had a pending/running run
            except Exception:
                log.exception("security scheduler: enqueue failed for branch %s", b["branch_name"])

        # Wake the dispatcher so the freshly-enqueued runs process promptly.
        if created:
            try:
                await pool.execute("SELECT pg_notify('security_wake', '')")
            except Exception:
                pass
        log.info("security scheduler: %dh batch — %d branch(es); %d enqueued, %d unchanged-skipped, "
                 "%d already-queued", self._interval_hours, len(branches), created,
                 skipped_unchanged, skipped_active)


security_scheduler = SecurityScheduler()
