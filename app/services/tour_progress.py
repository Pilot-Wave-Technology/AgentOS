"""Server-side tracking of which guided-tour milestones a user has completed.

Backs Settings > Help & Explainability's guided tours. Replaces the original
localStorage-only "seen" flags for cross-device consistency: a milestone
completed on one browser stays completed when the same user logs in from
another. See static/js/tour.js for the client side (autoFireTour(),
restartJourney()).

check_and_mark_milestone() is the only way a milestone gets marked
completed - deciding "has this been seen" and recording "it's now seen"
happen as one atomic DB round-trip, called from the route that renders the
milestone's page, before the response is sent. This replaces an earlier
client-side design (check window.completedMilestones, then fire a
follow-up POST to mark it) that raced page navigation: a user clicking
away before that follow-up request finished meant the write silently
never happened. Deciding and recording server-side, inside the same
request that renders the page, closes that race entirely - there's no
window between "decided" and "recorded" for a navigation to land in.
"""
import logging

from app.database import get_pool

_log = logging.getLogger(__name__)

# Every milestone in the overall guided journey, in order — platform-overview
# through security-intro. Kept in sync with JOURNEY_MILESTONE_IDS in
# static/js/tour.js (that copy drives the client-side "was this tour part of
# the journey" check before firing the abandon-cascade below).
JOURNEY_MILESTONE_IDS = [
    "platform-overview",
    "goal-dashboard-intro",
    "goal-thread-intro",
    "agent-mesh-intro",
    "resources-intro",
    "story-board-intro",
    "assistant-thread-intro",
    "tasks-board-intro",
    "council-intro",
    "security-intro",
]


async def check_and_mark_milestone(user_id: str, milestone_id: str) -> bool:
    """Atomically checks whether this milestone is new, marking it completed
    if so. Returns True the first time only (caller should auto-fire the
    tour); False on every subsequent call (already seen)."""
    pool = get_pool()
    row = await pool.fetchrow(
        """INSERT INTO user_tour_progress (user_id, milestone_id)
           VALUES ($1, $2)
           ON CONFLICT (user_id, milestone_id) DO NOTHING
           RETURNING milestone_id""",
        user_id, milestone_id,
    )
    is_first_visit = row is not None
    if is_first_visit:
        _log.info("tour_progress: user_id=%s completed milestone=%s (first visit)", user_id, milestone_id)
    return is_first_visit


async def check_and_mark_milestone_if_enabled(user: dict, milestone_id: str) -> bool:
    """Same as check_and_mark_milestone(), but skips the DB write entirely -
    leaving the milestone unclaimed - when the user has guided tours turned
    off in Settings > Help & Explainability.

    Without this, a milestone visited while tours are disabled still gets
    silently marked complete server-side (only the client-side overlay was
    suppressed), so re-enabling tours later would never show it - the
    first-visit opportunity was already burned. Routes should call this
    instead of check_and_mark_milestone() directly."""
    if not user.get("preferences", {}).get("startup_wizard_enabled", True):
        return False
    return await check_and_mark_milestone(user["uid"], milestone_id)


async def reset_all_progress(user_id: str) -> None:
    """Wipes every completed milestone for this user — 'restart guided tour'."""
    pool = get_pool()
    result = await pool.execute(
        "DELETE FROM user_tour_progress WHERE user_id = $1", user_id,
    )
    _log.info("tour_progress: user_id=%s reset all progress (%s)", user_id, result)


async def mark_journey_abandoned(user_id: str) -> None:
    """Marks every remaining milestone in the overall journey complete at
    once. Called when a user closes a journey tour early (✕ or Escape)
    instead of finishing it via Next/Continue - treated as "I don't want to
    see the rest of this guided journey," not just "not right now for this
    one page," so nothing in JOURNEY_MILESTONE_IDS can auto-fire later.
    Milestones already marked are a no-op (ON CONFLICT DO NOTHING) — this
    always marks the *entire* list rather than computing "from here on",
    since every milestone is account-wide and idempotent to re-mark."""
    pool = get_pool()
    for milestone_id in JOURNEY_MILESTONE_IDS:
        await pool.execute(
            """INSERT INTO user_tour_progress (user_id, milestone_id)
               VALUES ($1, $2)
               ON CONFLICT (user_id, milestone_id) DO NOTHING""",
            user_id, milestone_id,
        )
    _log.info(
        "tour_progress: user_id=%s abandoned journey - marked all %d milestones complete",
        user_id, len(JOURNEY_MILESTONE_IDS),
    )
