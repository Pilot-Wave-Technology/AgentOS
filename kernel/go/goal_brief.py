"""The living brief — where the goal stands, in one paragraph, now.

`goal_state` is the board and `goal_knowledge` is the record; neither is
something a person reads to catch up. This is: the one paragraph that
says what is happening and what it means, current at all times.

REWRITTEN, NEVER APPENDED. A brief that grows is a log, and a log is the
thing nobody reads, which is what this exists to replace. Each rewrite
replaces the last; the history that matters is in the decisions and the
knowledge, which are append-only precisely so this does not have to be.

GO OWNS IT, and writes it on the three events it is already woken for —
a completion report, a goal decision, a resolved alignment flag. No new
trigger and no schedule.

STALENESS IS MADE VISIBLE, NOT PREVENTED. `goal_state` returns the brief
with its age. A brief that has not moved in a week on a goal where four
stories landed is a fact anyone can read, and that is a better guard
than a rule nothing enforces. The goal review council is where it gets
checked for being WRONG, which is the failure a timestamp cannot catch.
"""
from __future__ import annotations

import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

MAX_BRIEF = 4000


async def update_brief(conn, goal_id: str, actor_id: str, text: str):
    """Replace the goal's brief."""
    text = (text or "").strip()
    if not text:
        return Refused(
            "E_EMPTY_BRIEF",
            "the brief is what somebody reads to know where this goal "
            "stands — clearing it leaves them with the board and no "
            "reading of it. Rewrite it instead")
    if len(text) > MAX_BRIEF:
        return Refused(
            "E_BRIEF_TOO_LONG",
            f"{len(text)} characters. This is the paragraph somebody reads "
            f"FIRST; past about {MAX_BRIEF} it is a document and they will "
            f"skip it. The detail belongs in decisions and knowledge, which "
            f"are queryable")

    row = await conn.fetchrow(
        "UPDATE goals SET brief = $2, brief_updated = now() "
        " WHERE id = $1 RETURNING brief_updated", goal_id, text)
    if row is None:
        return Refused("E_NO_GOAL", f"no goal {goal_id}")
    logger.info("goal %s: brief rewritten by %s (%d chars)",
                goal_id, actor_id, len(text))
    return {"goal": goal_id, "updated": row["brief_updated"],
            "chars": len(text),
            "detail": "this is what goal_state shows first, with its age"}


async def read_brief(conn, goal_id: str) -> dict:
    """The brief and how old it is. Empty is reported as empty, not hidden."""
    row = await conn.fetchrow(
        "SELECT brief, brief_updated FROM goals WHERE id = $1", goal_id)
    if row is None:
        return {"text": "", "updated": None, "stale": None}
    return {
        "text": row["brief"] or "",
        "updated": row["brief_updated"],
        # Said in words rather than left as a timestamp for a reader to
        # subtract. "Never written" is a different problem from "written
        # three weeks ago" and they want different responses.
        "state": ("never written" if not (row["brief"] or "").strip()
                  else "current" if row["brief_updated"] else "undated"),
    }


async def retire_decision(conn, goal_id: str, actor_id: str,
                          decision_id: str, reason: str = ""):
    """Take a superseded decision out of the picture, keeping the row.

    "Removed" for every reader — gone from `goal_state` and from every
    default read. The row survives because *why did we stop doing X* is
    the question people actually ask, and a deleted row is the one answer
    nobody can reconstruct.
    """
    if not (decision_id or "").strip():
        return Refused("E_NO_DECISION", "which decision? Pass its id")
    row = await conn.fetchrow(
        """UPDATE goal_decisions
              SET retired_at = now(),
                  rationale = CASE WHEN $3 = '' THEN rationale
                                   ELSE rationale || E'\\n\\nRetired: ' || $3 END
            WHERE id = $1 AND goal_id = $2 AND retired_at IS NULL
        RETURNING id, decision""",
        decision_id.strip(), goal_id, (reason or "").strip()[:500])
    if row is None:
        return Refused(
            "E_NO_SUCH_DECISION",
            f"no live decision {decision_id!r} in this goal — it may "
            f"already be retired")
    logger.info("goal %s: decision %s retired by %s", goal_id, decision_id, actor_id)
    return {"decision": row["id"], "retired": True,
            "was": (row["decision"] or "")[:120],
            "detail": "out of every default read and out of goal_state. "
                      "Still readable when somebody asks why it changed"}
