"""Who is on this goal, and what they may do.

WHY THIS IS A TOOL AND NOT A PARAGRAPH. The roster used to ride along on
every single message to GO -- a markdown table rebuilt per turn and
prepended to the user's text. On a one-person goal that is ~200
characters and nobody notices. On a real one it is not:

    20 members x (name, uid, role, joined, SHCA)   ~1,050 tokens
    + a 3,000-character description                  ~750 tokens
                                                   ─────────────
                                                   ~1,840 per message

and because it is prepended to the USER MESSAGE it lands in the
transcript, once per turn, permanently. Forty turns is ~74,000 tokens of
the same table -- more than the whole window on a 100K model, and it
pulls compaction forward for no information gained after the first
reading.

NO ACTIVE TASKS HERE, and that is the one deliberate omission. The
roster it replaces carried a per-member subquery of open tasks, which
made the whole block change every time any task changed state -- so it
could never be cached or sent-only-on-change. Tasks were already ruled
out of GO's per-turn context ("use story_status / story_progress") and
came back in through the roster's side door. `goal_state` returns
stories with `assigned_to`; this returns people.

ROLES ARE THE POINT, not a decoration. `decide_plan` requires a goal
ADMIN, `cancel_story` ends somebody's work, and an errand proposal is
approved by whoever holds the goal. GO needs to know who can be asked
for what, and inventing a name is the failure this exists to prevent.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SQL = """
    SELECT gm.uid, gm.role, gm.added_at,
           u.display_name, u.email,
           (SELECT a.id FROM agents a
              JOIN users usr ON usr.actor_id = a.owner_actor_id
             WHERE usr.uid = gm.uid AND a.type = 'SHCA'
               AND a.status = 'active'
             ORDER BY a.created_at LIMIT 1) AS assistant_id
      FROM goal_members gm
      JOIN users u ON u.uid = gm.uid
     WHERE gm.goal_id = $1
     ORDER BY gm.added_at
"""


async def goal_members(conn, goal_id: str) -> dict:
    """Everyone on this goal, with their role and their assistant."""
    rows = await conn.fetch(_SQL, goal_id)
    people = [{
        "uid": r["uid"],
        "name": (r["display_name"] or r["email"] or r["uid"]),
        "email": r["email"] or "",
        "role": r["role"],
        "joined": r["added_at"].isoformat() if r["added_at"] else "",
        # The person's own assistant, when they have one. GO hands work
        # to people THROUGH their assistant, so a roster without this is
        # a list of names it cannot act on.
        "assistant": r["assistant_id"] or "",
    } for r in rows]

    admins = [p["uid"] for p in people if p["role"] == "admin"]
    return {
        "goal": goal_id,
        "count": len(people),
        "members": people,
        # Lifted out because it answers the question GO actually has.
        # `decide_plan` refuses a uid that is not an admin, and hunting
        # for the role field is how it ends up guessing one.
        "admins": admins,
        "detail": (
            "These are the only people on this goal. Do not name anyone "
            "who is not listed here -- if somebody is missing, they have "
            "not been added, and saying otherwise invents a colleague. "
            "Approvals that need a human uid (decide_plan, cancel_story) "
            "need one of `admins`."
            if people else
            "This goal has no members recorded, which is unusual. Say so "
            "rather than assuming an owner: anything needing a human uid "
            "cannot be relayed until somebody is added."),
    }
