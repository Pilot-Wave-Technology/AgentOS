"""goal_state — the board, in one call.

GO's prompts used to say "think in people — who is overloaded, who is
idle" and gave GO no data to think WITH. This is the data: every story
with its state and its blockers, load per person, decisions, and asks
waiting. One call, bytes, no tree.

DONE IS NOT LANDED, and this is where the distinction is enforced in
data rather than doctrine. Each story carries `landed` — true only when
a MERGED git node carries it — separately from `state`. Three stories
done on one branch report done=3, landed=0 until the merge story lands,
and a planner reading this cannot honestly announce delivery early.
"""

from __future__ import annotations

# Story states that no longer occupy anyone.
TERMINAL = ("done", "cancelled", "rejected")


async def goal_state(conn, goal_id: str) -> dict:
    stories = await conn.fetch(
        """
        SELECT s.id, s.title, s.state, s.assigned_to, s.story_kind,
               s.work_kind, s.result_summary,
               s.depends_on, s.blocked_reason,
               -- IS IT BEING REVIEWED RIGHT NOW. A coding story stays
               -- `done` while a reviewer reads it in the pod that did
               -- the work, so `done` alone cannot tell GO whether the
               -- story is settled or still has a verdict coming. Told
               -- apart, "waiting on a review" is a status; conflated,
               -- GO plans against a story that may yet come back.
               EXISTS (SELECT 1 FROM kernel.executions e
                        WHERE e.story_id = s.id
                          AND e.phase = 'review'
                          AND e.state = 'RUNNING') AS reviewing,
               EXISTS (SELECT 1 FROM git_node_stories gns
                         JOIN git_nodes gn ON gn.id = gns.git_node_id
                        WHERE gns.story_id = s.id
                          AND gn.state = 'MERGED') AS landed
          FROM stories s WHERE s.goal_id = $1 ORDER BY s.created_at
        """,
        goal_id)
    by_id = {s["id"]: s for s in stories}

    out = []
    for s in stories:
        # I3d — WHICH SHAPE OF WORK THIS IS. `kind` here has always been
        # `story_kind`, the GIT ENGINE's taxonomy (code | merge), and an
        # errand reported as `code` reads to GO as an ordinary coding
        # story. Both are now present under names that say which is which,
        # and `kind` keeps its old meaning so nothing that reads the board
        # today changes.
        errand = (s["work_kind"] or "story") == "errand"
        row = {"story": s["id"], "title": s["title"], "state": s["state"],
               "assigned_to": s["assigned_to"] or None,
               "kind": s["story_kind"],
               "work_kind": s["work_kind"] or "story",
               # state says the work happened; landed says the team has
               # it. Never collapse these — see the module docstring.
               #
               # I3d — EXCEPT FOR AN ERRAND, WHERE THERE IS NOTHING TO
               # LAND. It produces no branch, so `landed` is not false, it
               # is meaningless — and GO is told never to announce
               # delivery until a landing wake arrives. Reported as false
               # it would make every finished errand permanently
               # done-but-not-delivered, and GO would sit on the answer.
               "landed": None if errand else s["landed"]}
        # Only while it is true. A key that is False on every settled
        # story is noise on a board GO reads whole.
        if s["reviewing"]:
            row["reviewing"] = True
        # I3e — what it FOUND, not just that it ended. The wake already
        # tells GO to read this back and no GO tool returned it.
        if s["result_summary"]:
            row["result"] = s["result_summary"][:600]
        waiting = []
        for dep in (s["depends_on"] or []):
            d = by_id.get(dep)
            if d is not None and d["state"] not in TERMINAL:
                waiting.append({"story": dep,
                                "assigned_to": d["assigned_to"] or None,
                                "state": d["state"]})
        if waiting and s["state"] not in TERMINAL:
            # WHO a blocked story waits on, not merely that it waits.
            # "Alice, story-3 is blocked on you" is actionable;
            # "story-3 is blocked" is a status nobody can act on.
            row["waiting_on"] = waiting
        if s["blocked_reason"]:
            row["blocked_reason"] = s["blocked_reason"]
        out.append(row)

    load: dict = {}
    for s in stories:
        if s["state"] not in TERMINAL and s["assigned_to"]:
            load[s["assigned_to"]] = load.get(s["assigned_to"], 0) + 1

    # THE ROSTER, ALWAYS. Humans were re-naming team members in every
    # message because GO had no standing source for who is on the goal.
    # Now the primary sense call carries it: name, role, and load in one
    # row per person, so "assign it to Priya" needs no reminder of who
    # Priya is — and GO addresses people by display name, from data.
    team = await conn.fetch(
        """
        SELECT gm.uid, gm.role, COALESCE(u.display_name, gm.uid) AS name
          FROM goal_members gm
          LEFT JOIN users u ON u.uid = gm.uid
         WHERE gm.goal_id = $1
         ORDER BY gm.added_at
        """,
        goal_id)

    # LIVE only. A retired decision is one somebody deliberately took out
    # of the picture, and letting it keep half of a ten-row window means
    # the board gets WORSE as the record gets richer. The count says how
    # many are hidden so the omission is visible rather than silent.
    decisions = await conn.fetch(
        """
        SELECT id, decision, rationale, decided_by, created_at
          FROM goal_decisions WHERE goal_id = $1 AND retired_at IS NULL
         ORDER BY created_at DESC LIMIT 10
        """,
        goal_id)
    retired = await conn.fetchval(
        "SELECT count(*) FROM goal_decisions "
        " WHERE goal_id = $1 AND retired_at IS NOT NULL", goal_id) or 0
    asks = await conn.fetch(
        """
        SELECT id, actor_id, kind, created_at
          FROM assistant_requests
         WHERE goal_id = $1 AND status = 'PENDING'
         ORDER BY created_at
        """,
        goal_id)

    goal_row = await conn.fetchrow(
        "SELECT name, description, objective FROM goals WHERE id = $1",
        goal_id)

    # Stage L. The BRIEF is the reading of the board, and it goes first
    # because it is the thing a person actually catches up from — the
    # rest of this is the data it was written off. Its AGE travels with
    # it: a brief that has not moved in a week on a goal where four
    # stories landed is a fact anyone can see, which is a better guard
    # than a rule nothing enforces.
    from kernel.go.goal_brief import read_brief
    from kernel.knowledge.alignment import open_flags
    brief = await read_brief(conn, goal_id)
    flags = await open_flags(conn, goal_id)

    return {
        "goal": goal_id,
        "brief": brief,
        # Open alignment questions. They are ON the board because an
        # unanswered one is work nobody is doing while somebody waits.
        "alignment_open": flags,
        "name": goal_row["name"] if goal_row else goal_id,
        "description": (goal_row["description"] or "") if goal_row else "",
        "objective": (goal_row["objective"] or "") if goal_row else "",
        "team": [{"uid": m["uid"], "name": m["name"], "role": m["role"],
                  "open_stories": load.get(m["uid"], 0)}
                 for m in team],
        "stories": out,
        "open": sum(1 for s in stories if s["state"] not in TERMINAL),
        # PUSHED, PR OPEN, WAITING ON A PERSON TO MERGE IT. Its own
        # number rather than folded into `open`, because the action
        # it calls for is nothing like the others: there is no work
        # to assign and nobody to unblock -- somebody has to press
        # merge. A planner that cannot see this separately reads a
        # finished story as still in progress and re-plans around it.
        "awaiting_merge": sum(
            1 for s in stories if s["state"] == "awaiting_merge"),
        # An errand is delivered when it is accepted, so counting one as
        # "done but not landed" would report a backlog that cannot be
        # cleared and that nobody can act on.
        "done_not_landed": sum(
            1 for s in stories
            if s["state"] == "done" and not s["landed"]
            and (s["work_kind"] or "story") != "errand"),
        # Per-person open stories. This is what makes "Alice has three
        # open; this queues behind them" a statement of fact.
        "load": dict(sorted(load.items(), key=lambda kv: -kv[1])),
        "decisions": [{"id": d["id"], "decision": d["decision"],
                       "rationale": d["rationale"],
                       "by": d["decided_by"], "at": d["created_at"]}
                      for d in decisions],
        "decisions_retired": retired,
        "pending_asks": [{"request": a["id"], "from": a["actor_id"],
                          "kind": a["kind"], "asked_at": a["created_at"]}
                         for a in asks],
    }
