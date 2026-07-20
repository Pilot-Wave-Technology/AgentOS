"""
Goal-weightage recompute task — Taximeter v5's holistic weightage recompute.

This is one task handler for the generic task runner (see task_runner/main.py).
It exposes a single `run_once()` entry point that the dispatcher calls:

  run_once() -> bool
    Claim and process EVERY currently-stale goal in this one execution
    (SELECT FOR UPDATE SKIP LOCKED, one at a time, looping until none
    remain or a safety cap is hit) — unlike document_render.py's
    one-item-per-execution pattern, this doesn't trickle through a
    backlog one goal per trigger: the operation is cheap (a few queries +
    one quick Gemini call per goal), so there's no resource-budget reason
    to throttle it the way a heavier per-item handler would need to.
    Returns True if at least one goal was processed, False if none were
    stale (duplicate trigger / race, or the watchdog's own check already
    went stale). A single goal's failure doesn't stop the others — it's
    logged and the loop continues — but the first failure is re-raised
    after every stale goal has been attempted, so the execution still
    shows as failed in Cloud Run history for visibility.

"Stale" mirrors app/services/taximeter/weightage_watchdog.py's own check
exactly: a goal whose weightage has never been computed, or whose story/
resource count has grown since the last computation. See
D:\\Projects\\agent-control-panel\\TAXIMETER_V5_REDESIGN.md for the full
design discussion.

Holistic, not incremental (resolved 2026-07-08): each recompute re-assesses
the goal's current scope fresh, ignoring its own prior weightage value —
avoids drift from compounding small misjudgments across many small
adjustments. Open-ended scale, no fixed ceiling.
"""

import logging
import re

import psycopg2

from task_runner.config import DATABASE_URL, GEMINI_API_KEY

_log = logging.getLogger("goal_weightage_recompute")

_MODEL = "gemini-2.5-flash"

# Defensive cap on how many goals run_once() will process in a single
# execution — not expected to ever be hit at this feature's real scale
# (a handful of active goals, not thousands), but an explicit bound is
# cheaper than trusting an unbounded while-loop, and the Cloud Run job's
# own --task-timeout would otherwise be the only thing stopping it.
_MAX_GOALS_PER_EXECUTION = 100

_WEIGHTAGE_PROMPT = """You are assessing the relative importance/scope of a project goal for a work-tracking system. Goals with more scope, complexity, or ongoing delivered work should have a higher "weightage" number; narrow, simple, or stalled goals should have a lower one.

Goal: {goal_name}
Current weightage: {current_weightage}

Story breakdown:
  Completed: {done_count}
  Cancelled or rejected: {cancelled_count}
  Still active or pending: {active_count}
Number of resources (repos, docs, etc.) attached: {resource_count}

Recent stories (title — status):
{story_lines}

Assess this goal's weightage FRESH, based on its current scope as described above — do not just nudge the current weightage, re-evaluate it holistically as if seeing it for the first time. Completed stories are positive evidence of real, growing scope and delivered impact. Cancelled or rejected stories are NOT positive evidence of growing scope — they may indicate churn or scope that didn't pan out, and should not inflate the weightage the way completed work does. The scale is open-ended (no fixed maximum) — a genuinely large, complex, actively-delivering goal should have a proportionally larger number than a small, narrow, or stalled one.

Respond with ONLY a single positive number (e.g. "12" or "3.5"), nothing else.
"""

# Same staleness definition as app/services/taximeter/weightage_watchdog.py's
# _STALE_GOALS_QUERY — kept in sync manually since these are two separate
# deployables (control-panel/agent-management vs. task-runner) that can't
# share a Python import.
_CLAIM_STALE_GOAL_QUERY = """
    SELECT g.id, g.name, g.weightage
      FROM goals g
     WHERE g.status = 'active'
       AND (
             g.weightage_recomputed_at IS NULL
          OR EXISTS (
               SELECT 1 FROM stories s
                WHERE s.goal_id = g.id AND s.created_at > g.weightage_recomputed_at
             )
          OR EXISTS (
               SELECT 1 FROM goal_resources gr
                WHERE gr.goal_id = g.id AND gr.created_at > g.weightage_recomputed_at
             )
           )
     ORDER BY g.weightage_recomputed_at ASC NULLS FIRST
     FOR UPDATE OF g SKIP LOCKED
     LIMIT 1
"""


# ── Claim pending work from DB ────────────────────────────────────────────────

def claim_stale_goal() -> tuple[str, str, float] | None:
    """Claim the oldest stale goal and return (goal_id, goal_name, current_weightage).

    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent executions don't double-process
    the same goal — same pattern as document_render.py's claim_pending_resource().
    Returns None if no stale goal exists (duplicate trigger / race).
    """
    _log.info("claim_stale_goal: querying for oldest stale goal")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_CLAIM_STALE_GOAL_QUERY)
                row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        _log.info("claim_stale_goal: no stale goals — nothing to do")
        return None

    goal_id, goal_name, current_weightage = row
    _log.info(
        "claim_stale_goal: claimed goal_id=%s name=%s current_weightage=%s",
        goal_id, goal_name, current_weightage,
    )
    return goal_id, goal_name, float(current_weightage)


def gather_goal_context(
    goal_id: str,
) -> tuple[int, int, int, int, list[tuple[str, str]]]:
    """(done_count, cancelled_count, active_count, resource_count,
    recent_stories) — the holistic scope signals fed to Gemini alongside
    its own judgment.

    Story counts are split by outcome, not just totaled: a goal that's
    actually delivering (stories reaching `done`) should read differently
    from one that's churning through `cancelled`/`rejected` work, even if
    both created the same number of stories. `recent_stories` pairs each
    title with its current state so Gemini can see which recent work
    actually landed versus which didn't.
    """
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        count(*) FILTER (WHERE state = 'done') AS done_count,
                        count(*) FILTER (WHERE state IN ('cancelled', 'rejected')) AS cancelled_count,
                        count(*) FILTER (WHERE state NOT IN ('done', 'cancelled', 'rejected')) AS active_count
                      FROM stories
                     WHERE goal_id = %s
                    """,
                    (goal_id,),
                )
                done_count, cancelled_count, active_count = cur.fetchone()
                cur.execute(
                    "SELECT count(*) FROM goal_resources WHERE goal_id = %s AND status != 'deleted'",
                    (goal_id,),
                )
                resource_count = cur.fetchone()[0]
                cur.execute(
                    "SELECT title, state FROM stories WHERE goal_id = %s ORDER BY updated_at DESC LIMIT 20",
                    (goal_id,),
                )
                recent_stories = [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()
    _log.info(
        "gather_goal_context: goal_id=%s done=%d cancelled=%d active=%d resources=%d",
        goal_id, done_count, cancelled_count, active_count, resource_count,
    )
    return done_count, cancelled_count, active_count, resource_count, recent_stories


def update_weightage(goal_id: str, new_weightage: float) -> None:
    """Write the recomputed weightage and stamp weightage_recomputed_at=now()
    so the next staleness check (here and in the watchdog) treats this goal
    as current again until new activity lands."""
    _log.info("update_weightage: goal_id=%s new_weightage=%s", goal_id, new_weightage)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE goals SET weightage = %s, weightage_recomputed_at = now() WHERE id = %s",
                    (new_weightage, goal_id),
                )
    finally:
        conn.close()


def _parse_weightage(text: str) -> float:
    """Extract the first positive number from Gemini's response text."""
    match = re.search(r"[-+]?\d*\.?\d+", text)
    if not match:
        raise ValueError(f"could not parse a number from Gemini response: {text!r}")
    value = float(match.group())
    if value <= 0:
        raise ValueError(f"parsed weightage must be positive, got {value} from response: {text!r}")
    return value


def recompute_weightage(
    goal_name: str, current_weightage: float, done_count: int, cancelled_count: int,
    active_count: int, resource_count: int, recent_stories: list[tuple[str, str]],
) -> float:
    """One Gemini Flash call: holistic weightage re-assessment given the
    goal's current scope. Raises on any failure — a failed recompute leaves
    weightage_recomputed_at untouched, so the goal stays "stale" and the
    next watchdog tick will simply try again."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured — cannot recompute weightage")

    from google import genai  # noqa: PLC0415

    story_lines = "\n".join(f"- {title} — {state}" for title, state in recent_stories) or "(none yet)"
    prompt = _WEIGHTAGE_PROMPT.format(
        goal_name=goal_name,
        current_weightage=current_weightage,
        done_count=done_count,
        cancelled_count=cancelled_count,
        active_count=active_count,
        resource_count=resource_count,
        story_lines=story_lines,
    )
    _log.info("recompute_weightage: calling Gemini for goal_name=%s", goal_name)
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(model=_MODEL, contents=prompt)
    text = (response.text or "").strip()
    _log.info("recompute_weightage: Gemini responded %r", text)
    return _parse_weightage(text)


# ── Task entry point ───────────────────────────────────────────────────────────

def run_once() -> bool:
    """Recompute weightage for every currently-stale goal, one at a time,
    all within this single execution — see the module docstring for why
    this handler doesn't throttle to one goal per trigger the way
    document_render.py does.

    Returns True if at least one goal was processed, False if there was no
    stale goal. The dispatcher uses the return value to decide whether this
    task had anything to do this execution.
    """
    processed_any = False
    first_failure: Exception | None = None
    processed_count = 0

    while processed_count < _MAX_GOALS_PER_EXECUTION:
        claimed = claim_stale_goal()
        if claimed is None:
            break

        processed_any = True
        processed_count += 1
        goal_id, goal_name, current_weightage = claimed
        _log.info(
            "goal_weightage_recompute: processing goal_id=%s name=%s current_weightage=%s (%d so far this execution)",
            goal_id, goal_name, current_weightage, processed_count,
        )

        try:
            done_count, cancelled_count, active_count, resource_count, recent_stories = (
                gather_goal_context(goal_id)
            )
            new_weightage = recompute_weightage(
                goal_name, current_weightage, done_count, cancelled_count,
                active_count, resource_count, recent_stories,
            )
            update_weightage(goal_id, new_weightage)
            _log.info(
                "goal_weightage_recompute: complete goal_id=%s weightage %.2f -> %.2f",
                goal_id, current_weightage, new_weightage,
            )
        except Exception as exc:
            _log.exception(
                "goal_weightage_recompute: failed goal_id=%s error=%s — "
                "continuing to the next stale goal", goal_id, exc,
            )
            # A failed recompute leaves weightage_recomputed_at untouched (we
            # never reached update_weightage), so this goal stays "stale" and
            # the next execution will simply retry it — no separate retry
            # bookkeeping needed. One bad goal must not block the others, so
            # keep going and surface the first failure only after the loop ends.
            if first_failure is None:
                first_failure = exc
    else:
        _log.warning(
            "goal_weightage_recompute: hit the %d-goal safety cap this execution — "
            "more stale goals likely remain for the next trigger",
            _MAX_GOALS_PER_EXECUTION,
        )

    if not processed_any:
        _log.info("goal_weightage_recompute: no stale goals — nothing to do")
        return False

    _log.info(
        "goal_weightage_recompute: execution complete, %d goal(s) processed", processed_count,
    )
    if first_failure is not None:
        # Re-raised (not swallowed) so the overall job execution still shows
        # as failed in Cloud Run history, per every other handler's
        # convention in this runner — but only after every stale goal found
        # this execution was actually attempted.
        raise first_failure
    return True
