"""Enqueue council runs + their stages (the queue write side)."""
from __future__ import annotations

import logging
from uuid import uuid4

import asyncpg

from app.database import get_pool
from app.models.story import is_engine_git_story

log = logging.getLogger(__name__)


def _already_running_reason(trigger: str | None, status: str | None) -> str:
    """Human-facing explanation when a run is deduped against an in-flight one.

    Names whether the blocking review is scheduled vs manual so the person who
    clicked (or the scheduler log) knows exactly why nothing new was enqueued.
    """
    label = {
        "scheduled": "scheduled (automatic 12-hour)",
        "manual": "manually triggered",
        "council_wake": "manually triggered",
        "story_done": "story",
    }.get(trigger or "manual", trigger or "")
    verb = "running" if status == "running" else "queued"
    who = f"A {label} council review" if label else "A council review"
    return f"{who} is already {verb} for this goal — wait for it to finish before starting another."


async def _wake_dispatcher(pool) -> None:
    """Cross-service wake: NOTIFY council_wake so a dispatcher in ANY service
    (the co-located web one or the standalone council-worker) processes the new
    run immediately instead of waiting for its poll. Best-effort — the poll loop
    is the backstop. (In-process callers may also call council_dispatcher.trigger()
    directly; this is what reaches a separate process.)"""
    try:
        await pool.execute("SELECT pg_notify('council_wake', '')")
    except Exception:
        log.warning("council: pg_notify(council_wake) failed (poll will still pick it up)")


async def enqueue_run(
    goal_id: str, created_by: str | None = None, trigger: str = "manual",
) -> dict:
    """Create a pending council run + its Phase-1 stages (goal_health → convener).

    trigger: how this run was started — 'manual' (default, the run endpoint),
    'scheduled' (the 12-hour batch over active goals), 'council_wake', etc. It is
    recorded on the run row for observability and is what the scheduler reads to
    decide when the next batch is due. The set of stages produced is identical
    regardless of trigger.

    Deduped: if a pending/running run already exists for this goal, returns it
    instead of creating a second (also DB-enforced by the partial unique index).
    """
    # The Platform Improvement goal (__platform__) is never goal/story-reviewed —
    # its stories are small no-code integration ops worked directly by the
    # platform assistant, not goal work. Guard here so ALL paths (scheduler,
    # manual, council_wake) skip it.
    if goal_id == "__platform__":
        return {"created": False, "run_id": None,
                "reason": "platform goal is not council-reviewed"}

    pool = get_pool()

    existing = await pool.fetchrow(
        "SELECT id, status, trigger FROM council_review_runs "
        "WHERE goal_id = $1 AND status IN ('pending', 'running')",
        goal_id,
    )
    if existing:
        return {
            "created": False,
            "run_id": existing["id"],
            "status": existing["status"],
            "trigger": existing["trigger"],
            "reason": _already_running_reason(existing["trigger"], existing["status"]),
        }

    run_id = uuid4().hex
    try:
        await pool.execute(
            "INSERT INTO council_review_runs (id, goal_id, trigger, status, created_by) "
            "VALUES ($1, $2, $3, 'pending', $4)",
            run_id, goal_id, trigger, created_by,
        )
    except asyncpg.exceptions.UniqueViolationError:
        existing = await pool.fetchrow(
            "SELECT id, status, trigger FROM council_review_runs "
            "WHERE goal_id = $1 AND status IN ('pending', 'running')",
            goal_id,
        )
        return {
            "created": False,
            "run_id": existing["id"] if existing else None,
            "trigger": existing["trigger"] if existing else None,
            "reason": _already_running_reason(
                existing["trigger"] if existing else None,
                existing["status"] if existing else "pending",
            ),
        }

    gh_id = uuid4().hex
    pr_id = uuid4().hex
    story_id_stage = uuid4().hex
    conv_id = uuid4().hex
    await pool.execute(
        "INSERT INTO council_review_stages (id, run_id, goal_id, stage, status, depends_on) "
        "VALUES ($1, $2, $3, 'goal_health', 'pending', '{}')",
        gh_id, run_id, goal_id,
    )
    await pool.execute(
        "INSERT INTO council_review_stages (id, run_id, goal_id, stage, status, depends_on) "
        "VALUES ($1, $2, $3, 'progress', 'pending', '{}')",
        pr_id, run_id, goal_id,
    )

    # ONE story stage per run, with story_id NULL — not one per story.
    #
    # The per-story fan-out this replaces created N gateway agents for an
    # N-story goal, and each of them read a gateway workspace through
    # `shca_instance_id` to do it. Pod-executed stories have no such
    # workspace, so those reviewers judged against empty documents. The
    # single stage reads `stories` and `story_reviews` instead, which
    # outlive every substrate change, and costs one agent whatever the
    # goal's size.
    #
    # NULL story_id is safe against `council_stages_unique`: it indexes
    # COALESCE(story_id, ''), so this is a singleton per run exactly the
    # way goal_health and convener are.
    await pool.execute(
        "INSERT INTO council_review_stages (id, run_id, goal_id, stage, status, depends_on) "
        "VALUES ($1, $2, $3, 'story', 'pending', '{}')",
        story_id_stage, run_id, goal_id,
    )

    await pool.execute(
        "INSERT INTO council_review_stages (id, run_id, goal_id, stage, status, depends_on) "
        "VALUES ($1, $2, $3, 'convener', 'blocked_on_deps', ARRAY['goal_health', 'progress', 'story'])",
        conv_id, run_id, goal_id,
    )
    await _wake_dispatcher(pool)
    log.info("council: enqueued run=%s goal=%s (4 stages)", run_id, goal_id)
    return {"created": True, "run_id": run_id, "status": "pending"}


async def enqueue_story_review(
    goal_id: str,
    story_id: str,
    reviewed_state: str,
    trigger: str = "story",
    created_by: str | None = None,
) -> dict:
    """Create a pending single-stage STORY review run for one (story, state).

    Deduped per (goal, story, reviewed_state): an in-flight review of the story
    in a DIFFERENT state (e.g. an interim in_progress review) does NOT block
    enqueuing the final 'done' review — the DB index allows both. The global
    single-run index still serializes execution.
    """
    # Platform Improvement stories are never reviewed (see enqueue_run).
    if goal_id == "__platform__":
        return {"created": False, "run_id": None,
                "reason": "platform goal is not council-reviewed"}

    pool = get_pool()

    # Engine-authored git stories (merge / diff / fix) are mechanical git operations,
    # NOT goal work — never council-review them (review noise, and the done→reviewed
    # transition would make the SHCA workspace eligible for teardown MID-GRIND, pulling
    # the branch workspace out from under the next diff/fix turn). Spec §0.1.
    meta = await pool.fetchrow(
        "SELECT created_by, story_kind, merge_group_id FROM stories WHERE id = $1",
        story_id,
    )
    # THE FINAL REVIEW RUNS IN THE POD NOW, so the council must not also
    # queue one. Two reviewers would produce two rows upserting over each
    # other on (story_id, reviewed_state) -- the last writer winning, at
    # random, between a reviewer that read the workspace and one that
    # read a bundle rebuilt from the database.
    #
    # ONLY THE FINAL REVIEW. Interim reviews of an `in_progress` or
    # `blocked` story are a different thing: the goal council produces
    # them and the convener consumes them, and nothing about this change
    # touches that.
    from kernel.worker.finish import in_pod_review_enabled

    if reviewed_state == "done" and in_pod_review_enabled():
        return {"created": False, "run_id": None,
                "reason": "the final story review runs in the execution "
                          "pod; the council no longer queues one"}

    if meta and is_engine_git_story(meta):
        log.info("council: story=%s is an engine git story — skipping review", story_id)
        return {"created": False, "run_id": None,
                "reason": "engine-authored git story — not council-reviewed"}

    existing = await pool.fetchrow(
        "SELECT id, status FROM council_review_runs "
        "WHERE goal_id = $1 AND kind = 'story' AND story_id = $2 "
        "AND COALESCE(reviewed_state, '') = $3 AND status IN ('pending', 'running')",
        goal_id, story_id, reviewed_state,
    )
    if existing:
        return {
            "created": False,
            "run_id": existing["id"],
            "status": existing["status"],
            "reason": f"A {reviewed_state} review of story {story_id} is already queued or running.",
        }

    run_id = uuid4().hex
    try:
        await pool.execute(
            "INSERT INTO council_review_runs "
            "(id, goal_id, kind, story_id, reviewed_state, trigger, status, created_by) "
            "VALUES ($1, $2, 'story', $3, $4, $5, 'pending', $6)",
            run_id, goal_id, story_id, reviewed_state, trigger, created_by,
        )
    except asyncpg.exceptions.UniqueViolationError:
        existing = await pool.fetchrow(
            "SELECT id, status FROM council_review_runs "
            "WHERE goal_id = $1 AND kind = 'story' AND story_id = $2 "
            "AND COALESCE(reviewed_state, '') = $3 AND status IN ('pending', 'running')",
            goal_id, story_id, reviewed_state,
        )
        return {
            "created": False,
            "run_id": existing["id"] if existing else None,
            "reason": f"A {reviewed_state} review of story {story_id} is already queued or running.",
        }

    stage_id = uuid4().hex
    await pool.execute(
        "INSERT INTO council_review_stages "
        "(id, run_id, goal_id, stage, story_id, reviewed_state, status, depends_on) "
        "VALUES ($1, $2, $3, 'story', $4, $5, 'pending', '{}')",
        stage_id, run_id, goal_id, story_id, reviewed_state,
    )
    await _wake_dispatcher(pool)
    log.info("council: enqueued STORY run=%s goal=%s story=%s state=%s",
             run_id, goal_id, story_id, reviewed_state)
    return {"created": True, "run_id": run_id, "status": "pending"}


async def force_run(goal_id: str, created_by: str | None = None) -> dict:
    """Start a council review of this goal NOW, clearing whatever is in the way.

    FOR TESTING, AND NARROWLY. Two separate things stop an ordinary
    `enqueue_run`, and both are correct in production:

      * `council_runs_active_per_goal` — one active run per goal, so a
        second click does not duplicate work.
      * `council_runs_single_running` — one running run across ALL goals,
        so the council never contends with itself for the gateway.

    The second is what makes "review this goal now" unusable for testing:
    a run wedged on some OTHER goal blocks the one you actually want, and
    from the caller's side it looks like the trigger is broken.

    So this cancels the goal's own active run and any run holding the
    global slot, then enqueues. CANCELS, NOT DELETES: the rows stay, their
    status becomes 'failed' with a reason naming who did it, and the
    findings those runs recorded stay in the ledger where the next run
    will pick them up as already-open. A forced run is a visible event in
    the history, not a gap in it.
    """
    pool = get_pool()

    cancelled = await pool.execute(
        "UPDATE council_review_runs SET status = 'failed', finished_at = NOW() "
        "WHERE status IN ('pending', 'running') AND (goal_id = $1 OR status = 'running')",
        goal_id,
    )
    n = int((cancelled or "UPDATE 0").rsplit(" ", 1)[-1] or 0)

    # Stages of a cancelled run would otherwise sit 'running' for ever and
    # keep their gateway reviewers alive; the deadline sweep would collect
    # them eventually, but a forced run should not leave that behind.
    await pool.execute(
        "UPDATE council_review_stages SET status = 'failed', "
        "error = COALESCE(NULLIF(error, ''), 'superseded by a forced run'), "
        "finished_at = NOW() "
        "WHERE status NOT IN ('done', 'failed') "
        "AND run_id IN (SELECT id FROM council_review_runs "
        "                WHERE status = 'failed' AND finished_at > NOW() - interval '5 seconds')",
    )

    if n:
        log.warning("council: force_run on %s cancelled %d in-flight run(s) "
                    "(requested by %s)", goal_id, n, created_by or "unknown")

    result = await enqueue_run(goal_id, created_by=created_by, trigger="manual")
    result["cancelled_runs"] = n
    return result
