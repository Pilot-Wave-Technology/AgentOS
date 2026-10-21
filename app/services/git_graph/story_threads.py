"""Which conversation does a git-engine notice belong in, and when is it over?

TWO QUESTIONS, ONE PLACE, because they are the same question asked at the two
ends of a workstream's life: whose thread does the merge/diff notice go to, and
whose thread is finished once the branch lands. Answering them apart is how they
drifted apart.

WHAT WAS WRONG. `_notify_pr_opened` resolved the assignee's SHCA thread as

    SELECT t.id FROM threads t JOIN agents a ON a.id = t.agent_id
     WHERE t.goal_id = $1 AND t.created_by = $2 AND a.type = 'SHCA'
     ORDER BY t.created_at LIMIT 1

and its comment says why: pre-Stage-K a person had exactly ONE assistant thread
per goal, so "the oldest" was "the only" and the query was unambiguous. Stage K
gave every story its own thread. `ORDER BY created_at LIMIT 1` now names the
FIRST story the person ever opened in that goal — so every merge notice in the
goal, for every branch, lands in one arbitrary story's conversation and none of
them in the story that was actually merged.

THE BINDING THAT ACTUALLY EXISTS. `threads.story_id` (Stage K) says which story
a conversation is about, and `git_node_stories` says which stories a branch
carries. Joining those two is the whole fix: no heuristic, no ordering, no
guess about which thread the person meant.

WHY A LIST AND NOT A THREAD. A node can carry SEVERAL member stories — a branch
named after a dependency is a shared workstream, and its merge lands everyone's
work at once. Returning one thread would mean picking a winner and silently not
telling the others their work just landed. Conversely one story can hold
SEVERAL nodes (a multi-repo change): each node's notice lands on that story's
one thread, so the story sees one notice per repo, which is exactly what
happened.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# A node still in play. Everything else is settled: MERGED landed, ABANDONED was
# torn down and is never coming, ESCALATED is waiting on a human but may still
# merge — so it holds the conversation open.
LIVE_NODE_STATES = ("MERGED", "ABANDONED")


async def threads_for_node(pool, node_id: str) -> list[dict]:
    """Live assistant threads of the stories this branch carries.

    Engine-authored stories are excluded: a diff/fix story the engine spawned is
    machinery for landing somebody's work, not a person's conversation, and its
    own notices belong on the thread of the work it is landing.

    Empty is a real answer — nobody imported the story into an assistant, or the
    thread has already been closed because the work landed. The caller falls
    back to the bell rather than inventing a thread.
    """
    rows = await pool.fetch(
        """SELECT DISTINCT t.id AS thread_id, t.story_id, t.created_by AS uid,
                  s.title
             FROM git_node_stories gns
             JOIN stories s  ON s.id = gns.story_id
             JOIN threads t  ON t.story_id = s.id AND t.goal_id = s.goal_id
            WHERE gns.git_node_id = $1
              AND s.created_by <> 'engine'
              AND COALESCE(s.story_kind, 'code') = 'code'
              AND t.left_goal_at IS NULL
              AND (t.status IS NULL OR t.status IN ('', 'active', 'open'))
            ORDER BY t.story_id""",
        node_id,
    )
    return [dict(r) for r in rows]


async def landed_stories_for_node(pool, node_id: str) -> list[dict]:
    """Member stories whose git work is now completely finished.

    "Finished" is: this story has at least one node that MERGED, and it has no
    node left in a live state. The second clause is what makes multi-repo work —
    a story writing three repos is not done when the first lands, and closing
    its thread then would take the conversation away mid-change.

    ABANDONED counts as settled rather than blocking. A torn-down workstream is
    never going to land, and treating it as live would hold the conversation
    open for the rest of the goal's life waiting for a branch that no longer
    exists. At least one MERGED is still required, so a story whose every node
    was abandoned does NOT read as landed — nothing of it reached trunk.
    """
    rows = await pool.fetch(
        f"""SELECT s.id, s.title, s.goal_id
              FROM git_node_stories gns
              JOIN stories s ON s.id = gns.story_id
             WHERE gns.git_node_id = $1
               AND s.created_by <> 'engine'
               AND COALESCE(s.story_kind, 'code') = 'code'
               -- NOT WHILE A PERSON IS STILL BEING ASKED. A coding story
               -- stays at `awaiting_merge` through the merge and past
               -- it: the run parks again on a `closeout` and the card
               -- asking "close the story, or more work?" is posted into
               -- THIS thread. Closing it here would retire the
               -- conversation the question was asked in, seconds before
               -- asking it -- and "more work" would have nowhere to be
               -- said. The thread closes when the story actually
               -- finishes, which is `on_story_done`'s job.
               AND s.state <> 'awaiting_merge'
               -- nothing of this story's still in play, anywhere
               AND NOT EXISTS (
                     SELECT 1 FROM git_node_stories g2
                       JOIN git_nodes n2 ON n2.id = g2.git_node_id
                      WHERE g2.story_id = s.id
                        AND n2.state NOT IN {LIVE_NODE_STATES})
               -- and something of it actually reached trunk
               AND EXISTS (
                     SELECT 1 FROM git_node_stories g3
                       JOIN git_nodes n3 ON n3.id = g3.git_node_id
                      WHERE g3.story_id = s.id AND n3.state = 'MERGED')""",
        node_id,
    )
    return [dict(r) for r in rows]


async def close_threads_for_landed(pool, node_id: str) -> list[str]:
    """Retire the conversations whose work has fully landed. Returns thread ids.

    `left_goal_at` is the schema's own liveness flag — the sidebar filters on
    it, `_ensure_story_thread`'s find-or-create filters on it, and the ceiling's
    `active_threads` filters on it. Setting it is therefore the whole of
    "disappears from the UI and nothing new attaches to it"; the send guard in
    `app/routers/chat.py` refuses posts to it separately, because a row that is
    merely hidden is still reachable by anyone holding its URL.

    Nothing is deleted. The conversation is the record of how the work was done
    and it stays readable — this closes it, it does not erase it.
    """
    landed = await landed_stories_for_node(pool, node_id)
    if not landed:
        return []

    closed: list[str] = []
    for story in landed:
        rows = await pool.fetch(
            """UPDATE threads
                  SET left_goal_at = now(), status = 'archived'
                WHERE story_id = $1 AND goal_id = $2
                  AND left_goal_at IS NULL
              RETURNING id""",
            story["id"], story["goal_id"],
        )
        for r in rows:
            closed.append(r["id"])
            log.info(
                "git: story %s has landed every branch it holds — closing "
                "thread %s (%s)", story["id"], r["id"], story["title"] or "")
    return closed


async def close_thread_for_finished_story(pool, story_id: str) -> list[str]:
    """A coding story has actually finished. Retire its conversation.

    CLOSURE MOVED HERE FROM THE MERGE PATH, and the move is the point.
    `close_threads_for_landed` fired when the last node reached MERGED,
    which used to be the end of the story and no longer is: the run
    parks again on a `closeout`, the card asking "close the story, or
    more work?" is posted into this very thread, and "more work" has to
    be sayable in it. So the merge path now skips a story still at
    `awaiting_merge`, and this runs when it reaches `done` instead.

    STILL ONLY FOR WORK THAT LANDED. A story with no merged node has
    nothing to show for itself and is not what this is for -- the
    errand path and the cancel path own their own closures, and
    widening this to "any finished story" would quietly take all three
    over.

    Nothing is deleted. The conversation is the record of how the work
    was done and it stays readable.
    """
    landed = await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM git_node_stories gns "
        "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
        " WHERE gns.story_id = $1 AND gn.state = 'MERGED')", story_id)
    if not landed:
        return []
    rows = await pool.fetch(
        """UPDATE threads
              SET left_goal_at = now(), status = 'archived'
            WHERE story_id = $1 AND left_goal_at IS NULL
          RETURNING id""", story_id)
    closed = [r["id"] for r in rows]
    for tid in closed:
        log.info("git: story %s has finished — closing thread %s",
                 story_id, tid)
    return closed


async def close_thread_for_errand(pool, story_id: str) -> list[str]:
    """F2 — an errand's thread closes when the errand is accepted.

    CLOSURE WAS WIRED TO THE MERGE PATH, WHICH AN ERRAND CANNOT REACH.
    `close_threads_for_landed` requires a MERGED node, and D1 guarantees
    an errand never lands: its work is scaffolding and its artifacts are
    the deliverable. So every accepted errand left a live thread behind,
    for ever — visible in the sidebar, counting against the active-thread
    ceiling, and looking like a conversation somebody abandoned.

    The same act as the landed case, reached by the other route. Nothing
    is deleted: the conversation is the record of how the answer was
    produced, and it stays readable.
    """
    rows = await pool.fetch(
        """UPDATE threads
              SET left_goal_at = now(), status = 'archived'
            WHERE story_id = $1
              AND left_goal_at IS NULL
          RETURNING id, goal_id""",
        story_id,
    )
    closed = [r["id"] for r in rows]
    for r in rows:
        log.info("errand %s accepted — closing thread %s", story_id, r["id"])
    # THE STORY'S POINTER GOES TOO. `stories.active_thread_id` is what
    # the assistant resumes from, and a pointer at an archived thread is
    # the stale-binding bug that resumed an un-imported story.
    if closed:
        await pool.execute(
            "UPDATE stories SET active_thread_id = NULL, "
            "is_active_focus = false, updated_at = now() WHERE id = $1",
            story_id)
    return closed
