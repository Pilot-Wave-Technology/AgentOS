"""DEPRECATED — superseded by platform_channel_bindings in the generic channel system.
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

CRUD for slack_user_goal_channels — one row per (goal, user).

The post-pivot model: each user has a single SHCA conversation (and a
single Slack channel) for an entire goal. Stories rotate through it via
`current_story_id`. Stable values: `goal_id`, `user_uid`,
`slack_channel_id`, `thread_id`, `agent_id`. Rotating value:
`current_story_id`.

Channel naming convention (set by callers, stored verbatim here):
    <goal_id>-<user-name-slug>-<agent-template-id>
    e.g.  bmi12-abhishek-chaudhary-shca
"""
import logging
import re
from typing import Optional

from app.database import get_pool

_log = logging.getLogger(__name__)


def slugify_for_slack(text: str, max_len: int = 21) -> str:
    """Slugify a display name for Slack channel naming.

    Slack constraints: lowercase a-z, 0-9, hyphens, underscores; ≤80
    chars total. We lowercase, replace runs of non-alphanumerics with
    a single hyphen, and trim to `max_len` so the user-name component
    never starves the goal_id and template suffix.
    """
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "user"


def channel_name_for(goal_id: str, user_display_name: str, agent_template_id: str) -> str:
    """Build the canonical channel name: <goal>-<user-slug>-<agent-id>-<env>.

    Truncates the user-slug component if needed to fit Slack's 80-char
    limit. Goal_id, agent template id, and env suffix are short and stable
    so the user-slug is what gets squeezed.
    """
    from app.config import get_settings
    env_suffix = (get_settings().slack_command_suffix or "").strip().lower()
    env_part = f"-{env_suffix}" if env_suffix else ""
    goal_part = (goal_id or "").lower()[:30]
    agent_part = (agent_template_id or "shca").lower()[:20]
    # 80 - len(goal_part) - len(agent_part) - 2 separators - len(env_part)
    user_budget = max(8, 80 - len(goal_part) - len(agent_part) - 2 - len(env_part))
    user_part = slugify_for_slack(user_display_name, max_len=user_budget)
    return f"{goal_part}-{user_part}-{agent_part}{env_part}"


async def get_by_user_goal(goal_id: str, user_uid: str) -> Optional[dict]:
    """Lookup by (goal_id, user_uid). Returns row even if archived."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT * FROM slack_user_goal_channels
         WHERE goal_id = $1 AND user_uid = $2
    """, goal_id, user_uid)
    return dict(row) if row else None


async def get_by_channel_id(slack_channel_id: str) -> Optional[dict]:
    """Lookup by Slack channel ID. Returns row even if archived."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT * FROM slack_user_goal_channels
         WHERE slack_channel_id = $1
    """, slack_channel_id)
    return dict(row) if row else None


async def get_by_thread_id(thread_id: str) -> Optional[dict]:
    """Lookup by control-panel thread_id (for outbound relay)."""
    pool = get_pool()
    row = await pool.fetchrow("""
        SELECT * FROM slack_user_goal_channels
         WHERE thread_id = $1 AND archived_at IS NULL
    """, thread_id)
    return dict(row) if row else None


async def upsert(
    goal_id: str,
    user_uid: str,
    slack_channel_id: str,
    slack_channel_name: str,
    thread_id: str,
    agent_id: str,
) -> dict:
    """Create the row or revive a previously-archived one. The
    (goal_id, user_uid) pair is stable for the life of the goal so
    every other field gets refreshed on conflict to handle channel
    re-creation, agent-id rotation, etc."""
    pool = get_pool()
    row = await pool.fetchrow("""
        INSERT INTO slack_user_goal_channels
            (goal_id, user_uid, slack_channel_id, slack_channel_name,
             thread_id, agent_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (goal_id, user_uid) DO UPDATE SET
            slack_channel_id   = EXCLUDED.slack_channel_id,
            slack_channel_name = EXCLUDED.slack_channel_name,
            thread_id          = EXCLUDED.thread_id,
            agent_id           = EXCLUDED.agent_id,
            archived_at        = NULL,
            updated_at         = now()
        RETURNING *
    """, goal_id, user_uid, slack_channel_id, slack_channel_name,
        thread_id, agent_id)
    _log.info(
        "Upserted user-goal channel: goal=%s uid=%s channel=#%s thread=%s agent=%s",
        goal_id, user_uid, slack_channel_name, thread_id[:12] if thread_id else "-", agent_id,
    )
    return dict(row)


async def set_current_story(goal_id: str, user_uid: str, story_id: Optional[str]) -> None:
    """Rotate the active story marker. `None` clears it (story finished
    or not currently in story-mode). Mirrors what the SHCA's IDENTITY.md
    will display."""
    pool = get_pool()
    await pool.execute("""
        UPDATE slack_user_goal_channels
           SET current_story_id = $3, updated_at = now()
         WHERE goal_id = $1 AND user_uid = $2
    """, goal_id, user_uid, story_id)
    _log.info(
        "Rotated current_story_id: goal=%s uid=%s story=%s",
        goal_id, user_uid, story_id or "(cleared)",
    )


async def mark_archived(goal_id: str, user_uid: str) -> None:
    """Archive a single user-goal row. Called as part of goal teardown."""
    pool = get_pool()
    await pool.execute("""
        UPDATE slack_user_goal_channels
           SET archived_at = now(), updated_at = now()
         WHERE goal_id = $1 AND user_uid = $2
    """, goal_id, user_uid)
    _log.info("Archived user-goal channel: goal=%s uid=%s", goal_id, user_uid)


async def list_active_for_goal(goal_id: str) -> list[dict]:
    """Every active (user, goal) row — used by the goal-delete cascade
    to know which SHCAs to tear down."""
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT * FROM slack_user_goal_channels
         WHERE goal_id = $1 AND archived_at IS NULL
         ORDER BY created_at
    """, goal_id)
    return [dict(r) for r in rows]
