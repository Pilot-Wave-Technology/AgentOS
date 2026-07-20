"""GitHub installation token auto-refresh for operator stories (Stage 3-m).

GitHub installation tokens have a 1-hour hard cap. Every 50 minutes, this
background task:
  * Finds every active operator-mode story (workspace_state='ready' AND
    state IN ('accepted','in_progress','blocked'))
  * For each: mints a fresh token via the existing GitHub App machinery
  * Calls shca.workspace.refreshToken on the gateway, which updates the
    `credential.helper` in the story repo's .git/config inline

Failures are logged but don't crash the loop.

Started from app/main.py startup.
"""
from __future__ import annotations

import asyncio
import logging

from app.database import get_pool
from app.github_tokens import generate_installation_token
from app.openclaw import gateway_admin, GatewayError


_log = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 50 * 60  # 50 min — within GitHub's 1h cap


async def refresh_story_token(story_id: str) -> tuple[bool, str | None]:
    """On-demand token refresh for a single story. Called inline right
    before a git push so the credential helper holds a fresh token even
    if the 50-min loop hasn't picked this story up yet.

    Returns (ok, error_message). Failures are non-fatal at the caller.
    """
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT s.id, s.goal_id, s.shca_instance_id,
                  gr.repo_name
             FROM stories s
             LEFT JOIN story_resources sr ON sr.story_id = s.id
             LEFT JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE s.id = $1
              AND gr.kind IN ('repo', 'branch')
              AND gr.repo_name IS NOT NULL
              AND gr.repo_name <> ''
            LIMIT 1""",
        story_id,
    )
    if not row:
        return False, "no repo resource attached to this story"
    if not row["shca_instance_id"]:
        return False, "story has no SHCA instance"

    try:
        token_result = await generate_installation_token([row["repo_name"]])
        token = token_result.get("token", "")
        if not token:
            return False, f"mint returned empty token for {row['repo_name']}"
    except Exception as exc:
        return False, f"token mint failed: {exc}"

    try:
        await gateway_admin.call(
            "shca.workspace.refreshToken",
            {
                "agentId": row["shca_instance_id"],
                "storyId": story_id,
                "githubToken": token,
            },
            timeout=30,
        )
    except GatewayError as exc:
        return False, f"refreshToken RPC failed: {exc}"

    _log.info(
        "token_refresh: on-demand refresh ok for story=%s repo=%s",
        story_id, row["repo_name"],
    )
    return True, None


async def _refresh_one_story(row: dict) -> None:
    story_id = row["id"]
    goal_id = row["goal_id"]
    instance_id = row["shca_instance_id"]

    repo_name = row.get("repo_name") or ""
    if not repo_name:
        _log.debug("token_refresh: story %s has no repo_name — skipping", story_id)
        return

    try:
        token_result = await generate_installation_token([repo_name])
        token = token_result.get("token", "")
        if not token:
            _log.warning("token_refresh: empty token for repo=%s story=%s",
                         repo_name, story_id)
            return
    except Exception as exc:
        _log.warning("token_refresh: mint failed for %s: %s", repo_name, exc)
        return

    try:
        await gateway_admin.call(
            "shca.workspace.refreshToken",
            {
                "agentId": instance_id,
                "storyId": story_id,
                "githubToken": token,
            },
            timeout=30,
        )
        _log.info(
            "token_refresh: refreshed token for story=%s instance=%s repo=%s",
            story_id, instance_id[:24], repo_name,
        )
    except GatewayError as exc:
        _log.warning(
            "token_refresh: gateway RPC failed for story=%s: %s",
            story_id, exc,
        )


async def _refresh_all_active() -> int:
    """Refresh tokens for every active operator story. Returns count refreshed."""
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT s.id, s.goal_id, s.shca_instance_id,
                  gr.repo_name
             FROM stories s
             LEFT JOIN story_resources sr ON sr.story_id = s.id
             LEFT JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE s.execution_mode = 'operator'
              AND s.workspace_state = 'ready'
              AND s.state IN ('accepted', 'in_progress', 'blocked')
              -- Deleted-goal guard: don't keep minting GitHub tokens every 50m
              -- for a story whose goal was soft-deleted (teardown normally clears
              -- workspace_state, but this closes the gap if it didn't).
              AND EXISTS (
                  SELECT 1 FROM goals g
                   WHERE g.id = s.goal_id AND g.status = 'active'
              )
              AND s.shca_instance_id IS NOT NULL
              AND s.shca_instance_id <> ''
              AND gr.kind IN ('repo', 'branch')
              AND gr.repo_name IS NOT NULL
              AND gr.repo_name <> ''
            ORDER BY s.updated_at DESC""",
    )
    count = 0
    for row in rows:
        try:
            await _refresh_one_story(dict(row))
            count += 1
        except Exception:
            _log.exception("token_refresh: unexpected per-story error")
    return count


async def run_loop():
    """Top-level loop. Called from app/main.py as a background task."""
    # Stagger initial run by ~1 min so we don't compete with startup work.
    await asyncio.sleep(60)
    while True:
        try:
            n = await _refresh_all_active()
            if n:
                _log.info("token_refresh: pass complete (%d stories refreshed)", n)
            else:
                _log.debug("token_refresh: no active operator stories")
        except Exception:
            _log.exception("token_refresh: loop iteration crashed")
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
