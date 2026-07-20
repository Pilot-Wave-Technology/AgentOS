"""`git_dispatch` NOTIFY channel + helper (plan §5.0 / §5.5.2).

The web tier does DB-only work (a topology reconcile, an approval, a webhook
row) and fires NOTIFY git_dispatch; the single-leader engine worker LISTENs and
wakes to perform the actual git work. Kept in its own tiny module so the web tier
can NOTIFY WITHOUT importing engine.py (which instantiates the controller
singleton and starts background tasks).
"""
import json

from app.database import get_pool

GIT_DISPATCH_CHANNEL = "git_dispatch"

# Component C: the engine fires this after it cuts + links a story's workstream
# branch, so the WEB tier re-provisions a story that was lazy-waiting for the cut.
STORY_BRANCH_READY_CHANNEL = "story_branch_ready"

# The engine fires this the FIRST time it pins a merge PR on a node, so the WEB tier
# posts a "review + merge this PR" notice into the assignee's assistant chat (the
# engine worker has no gateway/session access). Idempotency is enforced web-side via
# git_nodes.pr_opened_notified_at, so a repeated signal or the poll backstop is safe.
GIT_PR_OPENED_CHANNEL = "git_pr_opened"


async def notify_git_dispatch(payload: str = "") -> None:
    """Fire NOTIFY git_dispatch (optional payload, e.g. a goal id). Best-effort:
    NOTIFY is not durable; the engine's poll backstop covers a missed signal."""
    pool = get_pool()
    await pool.execute("SELECT pg_notify($1, $2)", GIT_DISPATCH_CHANNEL, payload)


async def notify_git_pr_opened(goal_id: str, node_id: str) -> None:
    """Fire NOTIFY git_pr_opened ({goal_id, node_id}) the first time a node's merge PR
    is pinned, so the web tier posts the "review + merge" notice to the assistant chat.
    Best-effort: NOTIFY is not durable; the listener's poll backstop (over nodes with a
    pr_number but no pr_opened_notified_at) covers a missed signal."""
    pool = get_pool()
    await pool.execute(
        "SELECT pg_notify($1, $2)",
        GIT_PR_OPENED_CHANNEL,
        json.dumps({"goal_id": goal_id, "node_id": node_id}),
    )


async def notify_story_branch_ready(goal_id: str, story_id: str) -> None:
    """Fire NOTIFY story_branch_ready ({goal_id, story_id}) after a story's branch
    is cut + linked, so the web-tier listener re-provisions it. Best-effort: NOTIFY
    is not durable; the listener's poll backstop covers a missed signal."""
    pool = get_pool()
    await pool.execute(
        "SELECT pg_notify($1, $2)",
        STORY_BRANCH_READY_CHANNEL,
        json.dumps({"goal_id": goal_id, "story_id": story_id}),
    )
