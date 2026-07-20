"""Enqueue council runs + their stages (the queue write side)."""
from __future__ import annotations

import logging
from uuid import uuid4

import asyncpg

from app.database import get_pool

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

    # Interim story reviews are part of the goal run (NOT separate runs), so the
    # convener can synthesize them. Snapshot the eligible in_progress/blocked
    # stories at trigger time — one 'story' stage each. The dispatcher runs these
    # before the convener; the convener depends on them.
    eligible = await pool.fetch(
        "SELECT id, state FROM stories "
        "WHERE goal_id = $1 AND state IN ('in_progress', 'blocked') "
        "AND last_reviewed_state IS DISTINCT FROM state",
        goal_id,
    )
    for s in eligible:
        await pool.execute(
            "INSERT INTO council_review_stages "
            "(id, run_id, goal_id, stage, story_id, reviewed_state, status, depends_on) "
            "VALUES ($1, $2, $3, 'story', $4, $5, 'pending', '{}')",
            uuid4().hex, run_id, goal_id, s["id"], s["state"],
        )

    await pool.execute(
        "INSERT INTO council_review_stages (id, run_id, goal_id, stage, status, depends_on) "
        "VALUES ($1, $2, $3, 'convener', 'blocked_on_deps', ARRAY['goal_health', 'progress', 'story'])",
        conv_id, run_id, goal_id,
    )
    await _wake_dispatcher(pool)
    log.info("council: enqueued run=%s goal=%s (%d interim story stage(s))",
             run_id, goal_id, len(eligible))
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
    if meta and (meta["created_by"] == "engine" or meta["story_kind"] == "merge"
                 or meta["merge_group_id"] is not None):
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


async def enqueue_eligible_story_reviews(goal_id: str) -> int:
    """Interim story reviews via the goal review: enqueue a review for every
    in_progress/blocked story whose CURRENT state hasn't been reviewed yet.

    Eligibility = state IN (in_progress, blocked) AND last_reviewed_state IS
    DISTINCT FROM state. The (story,state) dedup in enqueue_story_review prevents
    duplicate in-flight runs, and last_reviewed_state stops re-reviewing a story
    that hasn't moved since its last interim review. Returns the count enqueued.
    """
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, state FROM stories "
        "WHERE goal_id = $1 AND state IN ('in_progress', 'blocked') "
        "AND last_reviewed_state IS DISTINCT FROM state",
        goal_id,
    )
    n = 0
    for r in rows:
        res = await enqueue_story_review(goal_id, r["id"], r["state"], trigger="goal_review")
        if res.get("created"):
            n += 1
    if n:
        log.info("council: enqueued %d interim story review(s) for goal=%s", n, goal_id)
    return n
