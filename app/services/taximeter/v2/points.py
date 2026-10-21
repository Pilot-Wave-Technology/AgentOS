"""What each decision is worth, and what each priority is worth.

ONE TABLE, READ BY BOTH THE SCORER AND THE DASHBOARD. The view stores
raw counts per kind, so a weight changes here and nowhere else -- no
migration, no rebuild, and the explainer cannot drift from the maths
because it renders these same numbers rather than restating them.

THE SHAPE OF THE SCALE: a decision that COMMITS the story is worth most
(approving a plan, accepting finished work -- 3), one that unblocks it
is worth less (a merge, an escalation, a clarification -- 2), and one
that merely steers a run in flight is worth least (1). Machine
bookkeeping is worth nothing and is not listed: an unlisted kind scores
zero, so a new proposal kind invented by the kernel does not silently
start paying somebody.
"""

from __future__ import annotations

#: `decision:<kind>` -> points. Kinds absent from this table score 0.
#:
#: `answer` is deliberately absent even though it is a real proposal
#: kind: the answer to a question is already counted through the
#: question itself, and counting both would pay twice for one decision.
DECISION_POINTS: dict[str, int] = {
    # Committing the story.
    "authorization": 3,      # a plan approved, revised or declined
    "acceptance": 3,         # finished work accepted
    "closeout": 3,           # or more work asked for
    "local_plan": 3,         # the same decision in the local lane
    # Unblocking it.
    "merge": 2,              # a merge approved
    "landing": 2,
    "escalation": 2,
    "permission": 2,
    "clarification": 2,
    "decision": 2,
    # Steering a run that is already going.
    "direction": 1,
    "pr_closed": 1,
    "hold": 1,
    "cancel": 1,
    "steer": 1,
    "ask": 1,
}

#: `steward:<event_type>` -> points. Setting the table up for everyone
#: else: a goal created, a member added or removed, a story filed. One
#: point each -- real work, and not the work the story metrics measure.
STEWARD_POINTS: dict[str, int] = {
    "goal.created": 1,
    "goal.member_added": 1,
    "goal.member_removed": 1,
    "goal.member_role_changed": 1,
    "story.created": 1,
}

#: Priority -> weight. The same four values the database now enforces.
PRIORITY_POINTS: dict[str, int] = {
    "low": 1,
    "medium": 3,
    "high": 5,
    "critical": 8,
}


def decision_points(metric_key: str) -> int:
    """Points for a `decision:<kind>` or `steward:<kind>` metric key.

    UNKNOWN IS ZERO, NOT A DEFAULT. A kind nobody has weighed is not
    evidence of effort, and defaulting it to 1 would mean every new
    proposal kind the kernel invents quietly starts paying whoever owns
    the story.
    """
    kind, _, rest = metric_key.partition(":")
    if kind == "decision":
        return DECISION_POINTS.get(rest, 0)
    if kind == "steward":
        return STEWARD_POINTS.get(rest, 0)
    return 0
