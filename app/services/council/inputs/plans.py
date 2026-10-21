"""What was decided, read from approved plans.

NOT FROM `goal_decisions`, WHICH IS EMPTY. Measured on CAL05, a real
goal: `goal_decisions` held zero live rows while two of its three stories
carried approved plans. Every earlier version of this stage read the
empty table, so "the work contradicts a decision" was a check that could
never fire -- the same shape of bug as the workspace documents that came
back blank.

A DECISION IS AN APPROVED PLAN, and that is not a workaround. A plan park
carries who approved it and when, so a decision cited here has provenance
a person can open. `goal_decisions` was meant to hold the same thing
stated directly; nothing writes to it.

SUPERSEDED CYCLES ARE KEPT. "Why did we stop doing X" is the question
people actually ask, and it is unanswerable from the live set alone. A
step that appeared in cycle 1, vanished from cycle 2 and was never done
is the commonest way scope quietly shrinks, and it is invisible to anyone
reading only the latest plan.
"""

from __future__ import annotations

import json
import logging

from app.database import get_pool

logger = logging.getLogger(__name__)

# A plan park is stored in `questions` with kind 'authorization'. NOT
# 'plan_approval', which is the caller-facing word `park.py` maps from,
# and NOT `human_requests`, which the E6h migration named and the
# database does not have.
_PLAN_KIND = "authorization"

_MAX_PLANS = 60


def _payload(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return raw or {}


def _one(row, cycle: int) -> list[str]:
    body = _payload(row["payload"])
    steps = body.get("steps") or []
    approved = (row["status"] == "ANSWERED" and not row["declined"])
    verdict = ("approved" if approved
               else "DECLINED" if row["declined"] else row["status"].lower())

    out = [f"#### {row['story_id']} cycle {cycle} — {verdict}"
           + (f", by {row['answered_by']}" if row["answered_by"] else "")]
    if body.get("rationale"):
        out += ["", str(body["rationale"]).strip()]
    if steps:
        out += [""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    out.append("")
    return out


async def plans(goal_id: str) -> str:
    """`PLANS.md` — every plan park in the goal, grouped by story."""
    pool = get_pool()
    try:
        rows = await pool.fetch(
            """
            SELECT e.story_id, q.payload, q.status, q.declined,
                   q.answered_by, q.created_at
              FROM kernel.questions q
              JOIN kernel.executions e ON e.id = q.execution_id
             WHERE e.goal_id = $1 AND q.kind = $2 AND e.story_id IS NOT NULL
             ORDER BY e.story_id, q.id
             LIMIT $3
            """, goal_id, _PLAN_KIND, _MAX_PLANS)
    except Exception as exc:                                # noqa: BLE001
        logger.exception("council: plans for %s", goal_id)
        return ("# Decisions\n\n_(the plan record could not be read: "
                f"{exc}. Treat this as missing information, not as "
                "an absence of decisions.)_\n")

    if not rows:
        return ("# Decisions\n\n_(no plans have been approved in this goal "
                "yet. That is a fact about the goal, not a gap in this "
                "document — nothing has been formally decided.)_\n")

    out = ["# Decisions", "",
           "Each block is a plan a person approved or declined. **An "
           "approved plan is a decision**, with the approver's name on it. "
           "Work that contradicts one is a finding; work that follows one "
           "is correct even if you would have chosen differently.", "",
           "Superseded cycles are kept deliberately. A step that appeared "
           "in an early cycle, vanished from a later one and was never done "
           "is the commonest way scope quietly shrinks, and you cannot see "
           "it from the latest plan alone.", ""]

    seen: dict[str, int] = {}
    for row in rows:
        sid = row["story_id"]
        seen[sid] = seen.get(sid, 0) + 1
        out += _one(row, seen[sid])
    return "\n".join(out)
