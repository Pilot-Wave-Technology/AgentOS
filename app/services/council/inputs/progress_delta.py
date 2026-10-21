"""Movement since the last review, computed rather than estimated.

PRE-COMPUTED ON PURPOSE. Every number here -- hours stuck, what moved in
the window, what is ready and has not started -- is arithmetic, and a
model asked to do arithmetic over a story list will get it subtly wrong
in a way nobody catches. The `progress` reviewer's job is to say which of
these facts matters, not to derive them.

Lifted out of `provisioning.py` unchanged; it is a self-contained block
with one caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.database import get_pool

log = logging.getLogger(__name__)

# LOST IN THE PHASE-I SPLIT AND RESTORED HERE. The slice that moved this
# module out of `provisioning.py` started one line below these four, so
# they were cut while every reference to them came along -- and the whole
# progress stage failed on `name '_STUCK_HOURS' is not defined` for two
# live cycles before anyone saw it.
_STATE_RANK = {"proposed": 0, "accepted": 1, "in_progress": 2,
               "done": 3, "reviewed": 4}
_STUCK_HOURS = 12
_STALLED_DAYS = 3
# A dependency is "complete" (no longer blocks a dependent from starting) in
# any of these states -- matches the start-gate in story_transitions.
_DEP_SATISFIED = {"done", "reviewed", "cancelled", "rejected"}

def _aware(ts):
    """Normalize a possibly-naive timestamp to UTC-aware (asyncpg returns aware,
    but guard so an aware/naive subtraction can never TypeError)."""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _hours_between(later, earlier) -> int:
    a, b = _aware(later), _aware(earlier)
    if a is None or b is None:
        return 0
    return int((a - b).total_seconds() // 3600)


def _delta_label(reviewed_state: str, current_state: str) -> str:
    """Pre-computed reconciliation: did the story move since its review was done.
    moved-forward / unchanged / regressed / sideways."""
    rs, cs = (reviewed_state or "").lower(), (current_state or "").lower()
    if cs in ("blocked", "rejected", "cancelled"):
        return "unchanged" if rs == cs else "regressed"
    if rs in ("blocked", "rejected", "cancelled"):
        return "moved-forward" if cs in _STATE_RANK else "sideways"
    r, c = _STATE_RANK.get(rs), _STATE_RANK.get(cs)
    if r is None or c is None:
        return "sideways"
    return "moved-forward" if c > r else ("unchanged" if c == r else "regressed")


async def build(goal_id: str, story_rows) -> str:
    """Pre-computed movement since the last completed council run. ALL numbers,
    durations, >12h stuck flags, and assignee NAMES are computed HERE — the
    Progress RA interprets, never recounts. Sub-headers are bold LABELS, never
    '##' headings, because Flash is primed to copy '##' lines as its own output
    headings and would fail section validation. `story_rows` is fetched once by
    the caller (with the resolved `assignee` name) and shared with STORIES.md /
    STORY_REVIEWS.md so current state agrees across all three docs."""
    pool = get_pool()
    # Anchor the window to the last time the PROGRESS stage itself completed for
    # THIS goal — NOT the last completed run. Story-review runs (kind='story')
    # have no progress stage, and a goal run can fail at the convener after
    # progress succeeded; keying on the progress STAGE (status='done') avoids
    # both, so the window is exactly "since the last progress review of this goal".
    # The currently-running progress stage is 'running', so it is excluded.
    window_start = await pool.fetchval(
        "SELECT finished_at FROM council_review_stages "
        "WHERE goal_id = $1 AND stage = 'progress' AND status = 'done' AND finished_at IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT 1",
        goal_id,
    )
    first_review = window_start is None
    if first_review:
        window_start = await pool.fetchval("SELECT created_at FROM goals WHERE id = $1", goal_id)

    # When each story ENTERED its current state (the reliable 'stuck since' clock)
    # + which stories changed state in-window — from the ledger, one query.
    current_state_by_id = {s["id"]: s["state"] for s in story_rows}
    levents = await pool.fetch(
        "SELECT payload->>'story_id' AS sid, payload->>'to_state' AS to_state, timestamp AS ts "
        "FROM ledger_events WHERE event_type = 'story.state_changed' AND context_goal_id = $1 "
        "ORDER BY timestamp",
        goal_id,
    )
    entered_current: dict = {}
    moved_in_window: set = set()
    for e in levents:
        sid = e["sid"]
        if not sid:
            continue
        if current_state_by_id.get(sid) == e["to_state"]:
            entered_current[sid] = e["ts"]   # ascending → keeps the latest entry into current state
        if window_start and e["ts"] and _aware(e["ts"]) > _aware(window_start):
            moved_in_window.add(sid)

    now = datetime.now(timezone.utc)
    created, completed, stuck, stalled, ready = [], [], [], [], []
    for s in story_rows:
        # READY TO START: created/accepted, not yet started, every dependency complete.
        if s["state"] in ("proposed", "accepted"):
            deps = list(s["depends_on"] or [])
            if all(current_state_by_id.get(d) in _DEP_SATISFIED for d in deps):
                ready.append((s, deps))
        if window_start and s["created_at"] and _aware(s["created_at"]) > _aware(window_start):
            created.append(s)
        if s["state"] in ("done", "reviewed") and s["completed_at"] and (
            not window_start or _aware(s["completed_at"]) > _aware(window_start)
        ):
            completed.append(s)
        if s["state"] in ("in_progress", "blocked"):
            entry = entered_current.get(s["id"])
            approx = entry is None
            if entry is None:   # pre-ledger fallback: started_at (in_progress) / updated_at (blocked)
                entry = s["started_at"] if s["state"] == "in_progress" else s["updated_at"]
            hrs = _hours_between(now, entry) if entry else 0
            if hrs > _STUCK_HOURS:
                stuck.append((s, hrs, approx))
        if s["state"] == "in_progress" and s["updated_at"]:
            if _hours_between(now, s["updated_at"]) // 24 >= _STALLED_DAYS:
                stalled.append((s, _hours_between(now, s["updated_at"]) // 24))
    stuck.sort(key=lambda x: x[1], reverse=True)

    win = ("goal inception → now (FIRST review — Created/Completed are cumulative, not a delta)"
           if first_review else f"{window_start} → now")
    L = [
        "# Progress delta — PRE-COMPUTED. Trust these numbers; never recount or recompute a duration.",
        "# NOTE: the sub-headers below are LABELS, not '## ' headings — do not copy them as your output sections.",
        f"WINDOW: {win}   (UTC now: {now.strftime('%Y-%m-%d %H:%M')})",
        "",
        f"CREATED IN WINDOW ({len(created)}):",
        *([f"  - {s['id']} — {s['title']} (assignee: {s['assignee']}, created {s['created_at']})"
           for s in created] or ["  - none"]),
        "",
        f"COMPLETED IN WINDOW ({len(completed)}):",
        *([f"  - {s['id']} — {s['title']} (assignee: {s['assignee']}, completed {s['completed_at']})"
           for s in completed] or ["  - none"]),
        "",
        f"CURRENTLY STUCK (>{_STUCK_HOURS}h) ({len(stuck)}):  [in_progress or blocked > {_STUCK_HOURS}h]",
        *([f"  - {hrs}h{' (approx)' if approx else ''} — {s['id']} — {s['title']} — assignee: {s['assignee']} "
           f"— {s['state']} — {(s['blocked_reason'] or 'no reason given') if s['state'] == 'blocked' else 'in_progress, not completed'}"
           for (s, hrs, approx) in stuck] or ["  - none"]),
        "",
        f"STALLED IN_PROGRESS (no row update >= {_STALLED_DAYS} days) ({len(stalled)}):",
        *([f"  - {s['id']} — {s['title']} — assignee: {s['assignee']} — no update for {d} days"
           for (s, d) in stalled] or ["  - none"]),
        "",
        f"READY TO START (created/accepted, not started, all dependencies complete) ({len(ready)}):",
        *([f"  - {s['id']} — {s['title']} — assignee: {s['assignee']} — state {s['state']} — "
           f"deps complete: {', '.join(deps) if deps else 'none'}"
           for (s, deps) in ready] or ["  - none"]),
        "",
        "MOVED THIS WINDOW (story ids with a state change since window start): "
        + (", ".join(sorted(moved_in_window)) or "none"),
    ]
    return "\n".join(L)
