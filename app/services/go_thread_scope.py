"""Which errand a GO thread is about, said in the turn itself.

WHY IT HAS TO BE IN THE ENVELOPE. `build_go_context` is goal-scoped: it
names the goal, the model and the team, and every thread of that goal
gets the same block. That was complete while the GO had one thread.

An autonomous errand now opens a thread of its own on the GO, and the
gateway gives each thread its own session -- so the GO wakes up in a
conversation with no way to tell which errand it is in. It cannot infer
it from the transcript either: the first turn of a new errand thread has
no transcript.

WHAT IT IS NOT. Not a second context builder, and not a place to grow a
per-thread version of everything `build_go_context` already says. Four
facts, all of them the answer to "which one is this?", and the tools do
the rest -- `story_progress` for what is happening, `read_resource` for
the branch. The GO's own prompt already carries the cost rule that says
it never reads a tree.

GOAL-THREAD TURNS GET NOTHING. A thread with no `story_id` is the main
goal conversation, which is every turn the GO takes today; it must read
exactly as it did before or this is a regression dressed as a feature.
So the empty string is the common return, not the error case.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_SQL = """
    SELECT s.id, s.title, s.state, s.work_kind,
           t.created_by AS approver
      FROM threads t
      JOIN stories s ON s.id = t.story_id
     WHERE t.id = $1 AND t.goal_id = $2
"""


# THE GRANT, AND WHY IT IS IN THE TURN RATHER THAN BEHIND A TOOL.
#
# Autonomy is BOUNDED -- `max_runs`, `expires_at`, `revoked_at`, and a
# pin to the exact plan a person approved. Every one of those bounds is
# something the GO gets asked about ("how many more are there?", "is
# this still running unattended?") and none of it was readable: the GO
# could watch an errand run and not know it was on its last one.
#
# Read here because this block already runs once per turn on an errand
# thread, so it costs one query and no new tool -- and because the
# answer is useless a turn late. Somebody asking "is this still
# autonomous" wants it now, not after a round trip.
#
# THE SAME PREDICATE AS `go_may_drive`, minus the plan pin: a grant that
# exists is one the GO may drive under, and whether its PIN still
# matches is the separate question the last paragraph answers.
_GRANT = """
    SELECT s.approved_by, s.approved_plan_hash, s.revoked_at,
           s.expires_at, s.max_runs, s.run_count, s.cron
      FROM kernel.errand_schedules s
     WHERE s.goal_id = $1
       AND (s.story_id IS NULL OR s.story_id = $2)
     ORDER BY (s.story_id IS NOT NULL) DESC, s.id DESC
     LIMIT 1
"""


def _grant_lines(g) -> str:
    """One or two lines saying what the autonomy actually covers."""
    if g is None or g["approved_by"] is None:
        return ("**Autonomy:** none. Nobody has approved this to run "
                "unattended, so every run waits for a person.\n")
    if g["revoked_at"] is not None:
        return ("**Autonomy:** REVOKED. It runs only when a person says "
                "so, and it is the assistant's again rather than "
                "yours.\n")

    bits = [f"approved by {g['approved_by']}"]
    if g["max_runs"] is not None:
        left = max(int(g["max_runs"]) - int(g["run_count"] or 0), 0)
        # THE REMAINDER, not the total. "3 of 5" is arithmetic somebody
        # has to do while deciding whether to spend one.
        bits.append(f"{left} run(s) left of {g['max_runs']}")
    if g["expires_at"] is not None:
        bits.append(f"expires {g['expires_at']:%Y-%m-%d %H:%M} UTC")
    out = "**Autonomy:** " + ", ".join(bits) + ".\n"

    if not g["approved_plan_hash"]:
        # A grant is minted with a NULL hash and pinned on the first
        # approval, so this is the state before the first plan lands.
        # Normal, and worth naming because it looks like a missing pin.
        out += ("Its first run still parks for plan approval; autonomy "
                "starts from the run after that.\n")
    else:
        out += ("Bound to the plan that person approved. **A steer that "
                "changes what done means makes a NEW plan, and a new "
                "plan is not covered** -- the errand parks for a person, "
                "who may have gone away. Steer anyway when it is wrong; "
                "just say on this thread that you did and what it now "
                "needs.\n")
    return out


async def errand_block(conn, goal_id: str, thread_id: str | None) -> str:
    """The `## This thread` section, or "" for the main goal thread."""
    if not thread_id:
        return ""
    try:
        row = await conn.fetchrow(_SQL, thread_id, goal_id)
    except Exception:                                       # noqa: BLE001
        # NEVER COSTS THE TURN. This is decoration on a context block; a
        # GO that answers without knowing which errand it is in is worse
        # than one that answers, and far better than one that does not.
        log.warning("go thread scope: could not resolve %s in %s",
                    thread_id, goal_id, exc_info=True)
        return ""
    if not row:
        return ""
    # A STORY THREAD THAT IS NOT AN ERRAND IS NOT THE GO'S. Stories stay
    # with the assistant, so naming one here would invite the GO to act
    # on work it is refused by `go_may_drive` anyway -- and a refusal the
    # prompt walked it into reads as a platform bug.
    if (row["work_kind"] or "story") != "errand":
        return ""
    try:
        grant = await conn.fetchrow(_GRANT, goal_id, row["id"])
    except Exception:                                       # noqa: BLE001
        log.warning("go thread scope: could not read the grant for %s",
                    row["id"], exc_info=True)
        grant = None

    return (
        "## This thread\n\n"
        f"**Errand:** {row['id']} — {row['title'] or '(untitled)'}\n"
        f"**State:** {row['state']}\n"
        f"**Approved by:** {row['approver'] or '(unknown)'}\n"
        f"{_grant_lines(grant)}"
        "\nThis conversation is about that errand. Questions here are "
        "about it unless somebody says otherwise, and the person you are "
        "accountable to for it is its approver.\n"
    )
