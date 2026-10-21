"""A git engine failure that needs a person says so in the story thread.

THE SILENT STALL. `record_node_github_failure` classifies a GitHub
failure, writes `git_nodes.last_error`, and — for 401/403/404 — sets the
node ESCALATED and parks its merge-queue slot. All correct, and all
invisible: the only reader was the git-engine admin page. So a story
whose push was refused because the App installation lacked a permission
simply stopped progressing. The thread said nothing. `story_status` said
nothing. The state machine said nothing, because from the kernel's point
of view nothing had gone wrong — the work was done, and the engine's
half was where it died.

ESCALATED ONLY, DELIBERATELY. A transient failure (5xx, 429, network)
keeps its state and retries next tick, and carding those would put a
notification in front of a person for something that fixes itself in
thirty seconds — which is how people learn to ignore cards. ESCALATED is
this module's own word for "does not self-heal without a human", so it
is exactly the set worth interrupting someone for. Transient errors are
still visible: `story_progress` reads `last_error` whatever the state.

RE-CARDS ON A DIFFERENT ERROR, NEVER THE SAME ONE. The claim records the
error text that was carded, so a node that fails the same way twice does
not post twice, and a node that fails a NEW way does.
"""

from __future__ import annotations

import json
import logging
import uuid

from app.database import get_pool
from app.services.git_graph.node_errors import is_human_cancel
from app.services.story_card_thread import newest_human_thread

log = logging.getLogger(__name__)

BATCH = 20

# Claim-before-post, like every other card poster here: the cloud runs
# more than one control panel and two of them polling the same row would
# each post it.
CLAIM_SQL = """
    UPDATE git_nodes SET error_card_error = last_error,
                         error_card_posted_at = now()
     WHERE id = $1 AND last_error IS NOT NULL
       AND error_card_error IS DISTINCT FROM last_error
    RETURNING id
"""

PENDING_SQL = """
    SELECT n.id AS node_id, n.branch_name, n.trunk_branch, n.last_error,
           n.state, r.owner, r.name AS repo_name,
           s.id AS story_id, s.title, n.goal_id,
           -- The run the Retry button acts on. A story may have been
           -- attempted more than once; the live attempt is the one
           -- whose engine error is worth clearing, else the latest.
           (SELECT e.id FROM kernel.executions e
             WHERE e.story_id = s.id
             ORDER BY (e.container_id IS NOT NULL) DESC, e.id DESC
             LIMIT 1) AS run_id,
           COALESCE(
             """ + newest_human_thread("s.id") + """,
             s.active_thread_id, s.thread_id) AS thread_id
      FROM git_nodes n
      JOIN git_repos r ON r.id = n.git_repo_id
      JOIN git_node_stories gns ON gns.git_node_id = n.id
      JOIN stories s ON s.id = gns.story_id
     WHERE n.state = 'ESCALATED'
       AND n.last_error IS NOT NULL
       AND n.error_card_error IS DISTINCT FROM n.last_error
     ORDER BY n.updated_at
     LIMIT $1
"""


def _advice(error: str) -> str:
    """The same reading `kernel.assistant.remedies` gives the assistant.

    Said here too rather than referenced, because a person looking at
    the card should not have to ask the assistant what it means.
    """
    low = (error or "").lower()
    if "403" in low or "permission" in low:
        return ("The GitHub App was refused. Either its installation is "
                "missing a permission on this repo, or its token was "
                "minted before one was granted.")
    if "401" in low:
        return ("The GitHub App's credentials were rejected — the "
                "installation may have been revoked.")
    if "404" in low:
        return ("GitHub could not find the branch or the repo. It may "
                "have been deleted, or the App lost access to it.")
    return "This does not retry on its own."


def _human_cancel_card(row) -> dict:
    """A PERSON CLOSED THE PR. That is a decision, not a fault.

    The failure card below is wrong for this in three ways, and it is the
    card this case got: it says "the git engine cannot continue", which
    blames the engine for doing exactly what it should; it says "fix the
    cause, then retry the story", when there is no cause and retrying
    re-runs work somebody just rejected; and it carries `retry_run`, so the
    one button on screen is the one action that must not be taken.

    What is true instead: the branch still exists with its commits, nothing
    was merged, dependents are held (the merge story is `rejected`, which
    the dependency rule reads as unmet), and there are exactly two ways on
    — reopen the PR, or plan the work differently. Both are a person's.
    """
    branch = row["branch_name"]
    return {
        "type": "agent_card",
        "card_type": "git_pr_closed_unmerged",
        "goal_id": row["goal_id"], "story_id": row["story_id"],
        "node_id": row["node_id"], "branch": branch,
        "agent": "system", "agent_name": "Git engine",
        "title": f"PR closed without merging on {branch}",
        "body": (
            f"**The pull request for `{branch}` → `{row['trunk_branch']}` "
            f"was closed without merging**, so the engine has stopped "
            f"managing this branch.\n\n"
            f"Nothing was lost: the branch and its commits are still on "
            f"GitHub. Nothing was landed either, and anything waiting on "
            f"this work stays blocked until it is.\n\n"
            f"Two ways forward, both yours:\n"
            f"- **Reopen the PR** on GitHub if it was closed by mistake — "
            f"the engine picks it up again on its next pass.\n"
            f"- **Replan the work** if the approach was wrong. This branch "
            f"will not resume on its own.\n\n"
            f"_Retrying the story will not help: it would re-run work that "
            f"was just rejected._"),
        "options": [], "allow_custom": False,
        # DELIBERATELY ABSENT: `retry_run`. See the docstring.
    }


def _card(row) -> dict:
    branch = row["branch_name"]
    return {
        "type": "agent_card",
        "card_type": "git_node_error",
        "goal_id": row["goal_id"], "story_id": row["story_id"],
        "node_id": row["node_id"], "branch": branch,
        "agent": "system", "agent_name": "Git engine",
        "title": f"Git engine stopped on {branch}",
        "body": (f"⚠️ **The git engine cannot continue on "
                 f"`{branch}`** → `{row['trunk_branch']}`\n\n"
                 f"```\n{(row['last_error'] or '')[:400]}\n```\n\n"
                 f"{_advice(row['last_error'])} The story's work is safe — "
                 f"this is the delivery half. Fix the cause, then retry "
                 f"the story to clear the error and let the engine try "
                 f"again."),
        "options": [], "allow_custom": False,
        # The engine does not retry a node it has escalated, so without
        # this the person fixes the permission and nothing happens.
        "retry_run": row["run_id"],
    }


async def sweep() -> int:
    """Post a card for each newly ESCALATED node. Returns how many."""
    pool = get_pool()
    posted = 0
    async with pool.acquire() as conn:
        for row in await conn.fetch(PENDING_SQL, BATCH):
            if not row["thread_id"]:
                # Claim it anyway or the poller retries this row for
                # ever; the error is still on the node and in
                # story_progress.
                await conn.execute(CLAIM_SQL, row["node_id"])
                log.warning("node error card: node %s (story %s) has no "
                            "thread", row["node_id"], row["story_id"])
                continue
            if not await conn.fetchval(CLAIM_SQL, row["node_id"]):
                continue                       # another panel got there
            card = (_human_cancel_card(row)
                    if is_human_cancel(row["last_error"]) else _card(row))
            msg_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO messages (id, thread_id, role, sender, "
                "content, card_data, created_at) "
                "VALUES ($1,$2,'agent','system',$3,$4::jsonb, NOW())",
                msg_id, row["thread_id"], card["body"], json.dumps(card))
            try:
                from app.chat_module.repository import notify_new_message
                await notify_new_message(row["thread_id"], msg_id)
            except Exception:                               # noqa: BLE001
                log.exception("node error card: fan-out failed (node=%s)",
                              row["node_id"])
            posted += 1
    return posted
