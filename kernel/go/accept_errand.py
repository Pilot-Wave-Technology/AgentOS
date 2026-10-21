"""Carry a person's "approved" onto an errand, and nothing else.

WHY A TOOL AND NOT A READING OF THE CHAT. A person types "approved" and
GO should act on it -- that is the whole point of GO driving errands.
But "approved" in prose is ambiguous: approved WHAT, and did they mean
now. An agent that infers approval from a sentence will eventually infer
it from a sentence that was not one, and the result is work nobody
sanctioned, started without them, which is exactly the trust this
feature spends.

So the sentence is evidence and the tool is the act. GO names the story
it believes was approved and passes the person's uid; if it is not sure
which story, it asks rather than picking.

ERRANDS ONLY, AND THAT IS THE POINT OF THE GUARD. GO may start an
autonomous errand because somebody approved the errand knowing it would
run unattended. It may NOT start an ordinary story: those belong to the
person they are assigned to, they are accepted from the board, and an
agent quietly starting one is a different and much larger authority than
the one that was granted. `work_kind` is read from the row -- not taken
from the caller -- so there is nothing here for a prompt to talk its way
past.

STRAIGHT TO IN PROGRESS. An ordinary story is accepted and then started,
two acts by a person who may do the second later. An errand has no such
person waiting: approval IS the start, and stopping at `accepted` would
leave it sitting for a second click nobody is coming to give.
"""

from __future__ import annotations

import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

# The states a person can still approve from. Past these it is running,
# finished, or already stopped, and "approved" means nothing.
ACCEPTABLE = ("proposed", "accepted")


PENDING_SQL = """
    SELECT id, name, task FROM kernel.errand_proposals
     WHERE goal_id = $1 AND state = 'PENDING'
     ORDER BY created_at
"""


async def pending_proposals(conn, goal_id: str) -> list[dict]:
    """The errand proposals in this goal nobody has decided yet."""
    return [dict(r) for r in await conn.fetch(PENDING_SQL, goal_id)]


async def accept_errand(conn, goal_id: str, actor_id: str, story_id: str,
                        human_uid: str, proposal_id=None,
                        all_pending: bool = False):
    """Approve an autonomous errand and let it start.

    NO STORY ID IS THE NORMAL CASE, and getting that wrong produced two
    stories for one errand. An errand has no story until somebody
    approves it -- `errand_grants.approve()` creates the story AND the
    grant together, and the grant is what makes it autonomous. So when a
    person says "approved" there is nothing to name yet, and the first
    version of this refused with `E_NO_STORY`. GO, holding a refusal and
    an approval it believed, created a story itself: `91996a9b` with no
    grant beside `2e0a7c42` with one, on ERR12, for one errand.

    So a bare approval resolves to the PENDING PROPOSAL. Exactly one
    outstanding is the answer; more than one is the ambiguity worth
    asking about, and none means there is nothing to approve.
    """
    if not (human_uid or "").strip():
        return Refused(
            "E_NO_HUMAN",
            "approving an errand puts a person's name on the run and the "
            "cost on their goal. Relay their decision with their uid; "
            "you cannot take this one yourself")

    # ── they named one ───────────────────────────────────────────────
    #
    # THE ANSWER TO "WHICH ONE?". Without this the ambiguity refusal was
    # a dead end: GO asks which errand they meant, they say "the first",
    # and GO has no way to say so -- a proposal has no story id to pass,
    # so it could only ask again. A refusal that cannot be satisfied is
    # worse than no refusal, because it looks like the agent is stuck.
    if proposal_id not in (None, ""):
        try:
            pid = int(proposal_id)
        except (TypeError, ValueError):
            return Refused("E_BAD_PROPOSAL",
                           f"{proposal_id!r} is not a proposal id")
        row = await conn.fetchrow(
            "SELECT id, state FROM kernel.errand_proposals "
            " WHERE id = $1 AND goal_id = $2", pid, goal_id)
        if row is None:
            return Refused("E_NO_SUCH_PROPOSAL",
                           f"there is no proposal #{pid} in this goal")
        if row["state"] != "PENDING":
            return Refused(
                "E_ALREADY_DECIDED",
                f"proposal #{pid} is already {row['state'].lower()}; "
                f"there is nothing left to approve")
        return {"proposal": pid, "approved_by": human_uid,
                "state": "in_progress",
                "detail": "approve that proposal -- it creates the "
                          "errand's story WITH its grant and starts it"}

    # ── they said all of them ────────────────────────────────────────
    #
    # ONE DECISION, NOT N. A person who says "approve all three" made one
    # decision about a set, and the thread should read that way rather
    # than as three separate approvals GO happened to make in a row.
    if all_pending:
        waiting = await pending_proposals(conn, goal_id)
        if not waiting:
            return Refused("E_NOTHING_TO_APPROVE",
                           "there are no errand proposals waiting in this "
                           "goal")
        return {"proposals": [p["id"] for p in waiting],
                "approved_by": human_uid, "state": "in_progress",
                "detail": f"approve all {len(waiting)} -- each becomes a "
                          f"story with its own grant and starts as slots "
                          f"free up. Name them in your report; they were "
                          f"approved together and should be read that way"}

    if not (story_id or "").strip():
        waiting = await pending_proposals(conn, goal_id)
        if not waiting:
            return Refused(
                "E_NOTHING_TO_APPROVE",
                "there is no errand proposal waiting in this goal. If "
                "they meant something else -- a story on the board, a "
                "plan on a run -- that is theirs to do or a different "
                "tool")
        if len(waiting) > 1:
            names = ", ".join(f"#{p['id']} ({p['name'] or p['task'][:30]})"
                              for p in waiting)
            return Refused(
                "E_WHICH_ERRAND",
                f"{len(waiting)} proposals are waiting and 'approved' "
                f"does not say which: {names}. Ask them -- starting the "
                f"wrong one spends their name and their budget on work "
                f"they did not choose. When they answer, call this again "
                f"with `proposal_id`; if they meant all of them, call it "
                f"with `all_pending`")
        # EXACTLY ONE. Approving the PROPOSAL is what creates the story
        # and the grant; returning a story id here instead would be the
        # bug this function exists to stop.
        return {"proposal": waiting[0]["id"],
                "approved_by": human_uid, "state": "in_progress",
                "detail": "approve the proposal -- that creates the "
                          "errand's story WITH its grant and starts it. "
                          "Then say on the goal thread that you carried "
                          "their approval, and whose it was"}
    if not (human_uid or "").strip():
        return Refused(
            "E_NO_HUMAN",
            "approving an errand puts a person's name on every run of "
            "it and the cost on their goal. Relay their decision with "
            "their uid; you cannot take this one yourself")

    row = await conn.fetchrow(
        "SELECT id, goal_id, state, work_kind, title "
        "  FROM stories WHERE id = $1 AND goal_id = $2",
        story_id.strip(), goal_id)
    if row is None:
        return Refused("E_NO_SUCH_STORY",
                       f"{story_id!r} is not a story in this goal")

    # READ FROM THE ROW. The caller does not get to say what kind of
    # work this is.
    if (row["work_kind"] or "story") != "errand":
        return Refused(
            "E_NOT_AN_ERRAND",
            f"{story_id} is an ordinary story, not an autonomous errand. "
            f"Ordinary stories are accepted by the person they belong "
            f"to, from the board. This tool exists so you can start an "
            f"errand somebody approved unattended, and nothing else")

    if row["state"] not in ACCEPTABLE:
        return Refused(
            "E_NOT_PENDING",
            f"that errand is {row['state']}, so there is nothing to "
            f"approve. If it is running, watch it; if it finished, read "
            f"what it produced")

    return {"story": story_id, "title": row["title"],
            "approved_by": human_uid, "state": "in_progress",
            "detail": "the errand is approved and starts as soon as a "
                      "slot is free -- there is no second step. Say on "
                      "the goal thread that you carried their approval, "
                      "and who gave it"}
