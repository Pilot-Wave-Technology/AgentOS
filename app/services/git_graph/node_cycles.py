"""A story's branch landing more than once.

WHY THIS EXISTS. A coding story waits at `awaiting_merge` so a person
can ask for one more thing. Before the merge that costs nothing: the
node is live, the branch is open, and a second push updates the same
PR. AFTER the merge it is a different problem — the branch is merged,
the PR is closed, trunk has moved, and the merge story is `done`. The
next round is a new landing and needs those four things reset.

ONE FEATURE BRANCH PER STORY. A new branch means a new story, so the
branch name never changes here; the node is REOPENED rather than
replaced. That is also the only shape the schema allows:
`git_nodes` carries `UNIQUE (git_repo_id, branch_name)`, so a second
node naming the same branch cannot be inserted at all.

WHAT REOPENING WOULD DESTROY, and what is done about it. Resetting the
node clears `pr_number`, `merge_commit_sha` and `base_sha` — the whole
record of what the previous round was. So the round is copied into
`git_node_landings` first. Without that, a story that went round twice
is indistinguishable from one that went round once, and the PR that
actually landed each round is unfindable from our side.

THE MERGE STORY COMES BACK TOO. It is keyed on the merge GROUP, not the
cycle, so the one that landed round one is `done` — and reconcile's
upsert only revives a story that is still `proposed`. Left alone, round
two would reach READY with nothing for a person to approve and would
sit in the queue for ever.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("app.git_graph.node_cycles")

# Anything not in here is a node still in play. A story with one is not
# ready for another round — the one it is in has not finished.
SETTLED_STATES = ("MERGED", "ABANDONED")

# Everything the previous round wrote and the next one must not inherit.
# `base_sha` especially: left behind, the engine compares the new work
# against the trunk of the PREVIOUS cut and reports it as already landed.
_RESET = """
    UPDATE git_nodes
       SET state = 'PLANNED',
           cycle = cycle + 1,
           base_sha = NULL,
           pr_number = NULL,
           merge_commit_sha = NULL,
           merge_seq = NULL,
           last_synced_trunk_sha = NULL,
           sync_target_sha = NULL,
           pre_merge_trunk_sha = NULL,
           conflict_paths = NULL,
           conflict_base_sha = NULL,
           mergeable = 'unknown',
           last_error = NULL,
           repark_count = 0,
           updated_at = now()
     WHERE id = $1 AND state = 'MERGED'
 RETURNING id
"""


async def reopen_for_next_cycle(conn, story_id: str) -> list[str]:
    """Put this story's merged branches back to work. Returns node ids.

    Empty is the normal answer for a story with nothing merged — asking
    for more work BEFORE the merge needs none of this, because the node
    it has is still live and its PR is still open.

    IDEMPOTENT THROUGH THE LANDINGS TABLE. Its primary key is
    `(git_node_id, cycle)`, so recording the same round twice conflicts
    and does nothing; the reset is guarded on `state = 'MERGED'`, which
    is false the second time. Neither needs a flag somebody has to clear.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT gn.id, gn.goal_id, gn.branch_name, gn.base_sha,
               gn.pr_number, gn.merge_commit_sha, gn.cycle
          FROM git_node_stories gns
          JOIN git_nodes gn ON gn.id = gns.git_node_id
         WHERE gns.story_id = $1 AND gn.state = 'MERGED'
        """, story_id)
    if not rows:
        return []

    # NOT WHILE A SIBLING REPO IS STILL OPEN. A multi-repo story merges
    # one repo at a time, and reopening the first while the second is
    # still in flight would put the group back in the queue halfway
    # through the round it is still finishing.
    if await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM git_node_stories gns "
            "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
            " WHERE gns.story_id = $1 AND gn.state <> ALL($2::text[]))",
            story_id, list(SETTLED_STATES)):
        return []

    reopened: list[str] = []
    for row in rows:
        # THE RECORD OF THE ROUND, BEFORE THE COLUMNS THAT HOLD IT ARE
        # CLEARED. Ordering is the whole correctness of this function.
        await conn.execute(
            """
            INSERT INTO git_node_landings
                (git_node_id, cycle, branch_name, base_sha, pr_number,
                 merge_commit_sha)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (git_node_id, cycle) DO NOTHING
            """,
            row["id"], row["cycle"], row["branch_name"], row["base_sha"],
            row["pr_number"], row["merge_commit_sha"])

        if not await conn.fetchval(_RESET, row["id"]):
            continue
        reopened.append(row["id"])
        _log.info("story %s: reopened %s on %s for cycle %d (round %d "
                  "landed as PR %s)", story_id, row["id"], row["branch_name"],
                  row["cycle"] + 1, row["cycle"], row["pr_number"])

    if reopened:
        await _reopen_merge_stories(conn, reopened)
    return reopened


async def _reopen_merge_stories(conn, node_ids: list[str]) -> int:
    """Put the merge stories for these nodes back in front of a person.

    A merge story is the human approval for a landing. Round one's is
    `done`, and reconcile's upsert only touches a `proposed` one — so
    without this, round two reaches READY with nothing to approve and
    waits in the queue for ever.

    `merge_approved_at = NULL` is the half that matters. A story moved
    back to `proposed` while still carrying last round's approval would
    let round two land on an approval nobody gave it.
    """
    from app.services.story_state_ledger import record_states_where

    moved = await record_states_where(
        conn, "proposed",
        where=("story_kind = 'merge' AND state = 'done' "
               "AND merge_group_id IN ("
               "  SELECT merge_group_id FROM git_merge_group_nodes "
               "   WHERE git_node_id = ANY($1::text[]))"),
        params=(node_ids,), action="reopened_for_next_cycle")
    await conn.execute(
        "UPDATE stories SET merge_approved_at = NULL "
        " WHERE story_kind = 'merge' AND state = 'proposed' "
        "   AND merge_group_id IN ("
        "     SELECT merge_group_id FROM git_merge_group_nodes "
        "      WHERE git_node_id = ANY($1::text[]))",
        node_ids)
    return moved or 0
