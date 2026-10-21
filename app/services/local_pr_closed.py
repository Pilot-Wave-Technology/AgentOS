"""Somebody closed the pull request without merging it.

THE CLOUD ASKS; LOCAL WAS NOT EVEN TOLD. Detection is lane-agnostic --
it watches GitHub and sets the node `ESCALATED` with `last_error = "PR
closed unmerged…"` for any story. What follows is not:
`story_landing.PR_CLOSED_SQL` joins `kernel.executions`, and a local
story has none, so it matches nothing.

The result was silence. The node sat `ESCALATED`, the story sat at
`awaiting_merge`, `has_unlanded_work` saw a lane that had not landed so
Mark Done refused for ever, and the panel went on saying "merge it on
GitHub" about a pull request that was closed.

NOT BLOCKED -- IN PROGRESS. Abhishek, 2026-10-14. A closed pull request
is not a failure and nothing is stuck: a person looked at the work and
decided it was not ready. The story simply has more to do, which is
`in_progress`, and the way to do it is to ask for more work. `blocked`
would be a recovery surface for something that needs no recovering.

AND THE LANE IS LEFT ALONE HERE. Reviving it now would have the engine
open a fresh pull request immediately -- reopening, within a minute,
the one a person had just deliberately closed. It is revived when
somebody asks for more work, which is them saying to carry on; see
`local_more_work._revive_closed_lanes`.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("app.local_pr_closed")

# The error the detection pass writes. Matched as a prefix because it
# carries the PR number after it.
PR_CLOSED_PREFIX = "PR closed unmerged"

# A story whose pull request was closed and which nothing else is still
# landing for. The sibling test is the cloud's own, and for its reason:
# a multi-repo story whose second repo is still open has not stopped,
# and telling somebody it needs attention would be wrong.
CLOSED_SQL = """
    SELECT s.id AS story_id, s.goal_id
      FROM stories s
     WHERE COALESCE(s.execution_mode, '') = 'local'
       AND s.state = 'awaiting_merge'
       AND EXISTS (
               SELECT 1 FROM git_node_stories gns
                 JOIN git_nodes gn ON gn.id = gns.git_node_id
                WHERE gns.story_id = s.id
                  AND gn.state = 'ESCALATED'
                  AND gn.last_error LIKE 'PR closed unmerged%')
       AND NOT EXISTS (
               SELECT 1 FROM git_node_stories gns
                 JOIN git_nodes gn ON gn.id = gns.git_node_id
                WHERE gns.story_id = s.id
                  AND gn.state NOT IN ('MERGED', 'ABANDONED', 'ESCALATED'))
     ORDER BY s.updated_at
     LIMIT 25
"""


async def sweep(pool) -> int:
    """Tell every local story whose PR was closed. Returns how many."""
    told = 0
    for row in await pool.fetch(CLOSED_SQL):
        try:
            told += await _tell(pool, row["story_id"])
        except Exception:                                   # noqa: BLE001
            _log.exception("could not report the closed PR on story %s",
                           row["story_id"])
    return told


async def _tell(pool, story_id: str) -> int:
    """Move the story back into progress and say why. Idempotent.

    GUARDED ON `awaiting_merge`, so the second sweep tick over the same
    story does nothing and a story somebody has already picked back up
    is not dragged backwards.
    """
    moved = await pool.fetchval(
        "UPDATE stories SET state = 'in_progress', current_phase = 'planning', "
        "       updated_at = now() "
        " WHERE id = $1 AND state = 'awaiting_merge' RETURNING id", story_id)
    if not moved:
        return 0

    closed = await pool.fetch(
        "SELECT gn.branch_name, gn.pr_number FROM git_node_stories gns "
        "  JOIN git_nodes gn ON gn.id = gns.git_node_id "
        " WHERE gns.story_id = $1 AND gn.state = 'ESCALATED' "
        "   AND gn.last_error LIKE 'PR closed unmerged%'", story_id)
    named = ", ".join(
        f"{r['branch_name']}" + (f" (PR #{r['pr_number']})" if r["pr_number"] else "")
        for r in closed) or "its branch"
    _log.info("local story %s: PR closed unmerged on %s — back in progress",
              story_id, named)

    from app.services.local_report import report_message  # noqa: PLC0415
    await report_message(
        story_id, "agent",
        f"The pull request was closed without being merged ({named}). "
        "Nothing is wrong and nothing is stuck — somebody looked at this "
        "and decided it was not ready.\n\nThe story is back in progress. "
        "Ask for more work to carry on: the same branch is used and the "
        "next submit opens a fresh pull request on it.",
        card_data={"kind": "pr_closed",
                   "branches": [r["branch_name"] for r in closed]})
    return 1
