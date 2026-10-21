"""GitHub installation token auto-refresh for operator stories (Stage 3-m).

GitHub installation tokens have a 1-hour hard cap. Every 50 minutes, this
background task:
  * Finds every active operator-mode story (workspace_state='ready' AND
    state IN ('accepted','in_progress','blocked','awaiting_merge'))
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


def _brokering_active() -> bool:
    """True when git credentials are brokered per-operation (plan M5).

    Under brokering there is NO token stored in any clone's .git/config, so every
    refresh path here is not merely unnecessary but actively harmful: rewriting the
    credential helper with an inline token would REPLACE the working callback helper
    and pin the story to a token that expires in an hour.
    """
    from app.config import get_settings
    settings = get_settings()
    return bool(
        settings.git_credential_broker_enabled
        and (settings.control_panel_public_url or "").strip()
    )


async def refresh_story_token(story_id: str) -> tuple[bool, str | None]:
    """On-demand token refresh for a single story. Called inline right
    before a git push so the credential helper holds a fresh token even
    if the 50-min loop hasn't picked this story up yet.

    Multi-repo (research/MULTI_REPO_STORIES_PLAN.md §12.6): the token must cover
    EVERY repo the story holds, not just one — the workspace has a single shared
    credential helper, so a token scoped to one repo silently loses write access to
    every other repo the moment it's installed. Resolves the repo set the same way
    `operator_provisioning.provision_story_workspace` does, so both are one source
    of truth for "which repos does this story's workspace need a token for".

    Returns (ok, error_message). Failures are non-fatal at the caller.
    """
    if _brokering_active():
        _log.debug(
            "refresh_story_token: story=%s skipped — credentials are brokered "
            "per-operation, nothing is stored to refresh", story_id,
        )
        return True, None
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT id, goal_id, shca_instance_id FROM stories WHERE id = $1", story_id,
    )
    if not story:
        return False, "story not found"
    if not story["shca_instance_id"]:
        return False, "story has no SHCA instance"

    from app.services.operator_provisioning import (  # noqa: PLC0415
        resolve_repos_for_story,
        token_repo_names_for,
    )
    repos = await resolve_repos_for_story(pool, story["goal_id"], story_id)
    if not repos:
        return False, "no repo resource attached to this story"
    repo_names = token_repo_names_for(repos[0], repos[1:])

    try:
        token_result = await generate_installation_token(repo_names)
        token = token_result.get("token", "")
        if not token:
            return False, f"mint returned empty token for {repo_names}"
    except Exception as exc:
        return False, f"token mint failed: {exc}"

    try:
        await gateway_admin.call(
            "shca.workspace.refreshToken",
            {
                "agentId": story["shca_instance_id"],
                "storyId": story_id,
                "githubToken": token,
            },
            timeout=30,
        )
    except GatewayError as exc:
        return False, f"refreshToken RPC failed: {exc}"

    _log.info(
        "token_refresh: on-demand refresh ok for story=%s repos=%s",
        story_id, repo_names,
    )
    return True, None


async def _refresh_one_story(row: dict) -> None:
    """Mint one token covering every repo `row`'s story holds and push it to the
    gateway. See `refresh_story_token`'s docstring for why the token must be
    multi-repo scoped rather than pinned to a single arbitrary repo."""
    story_id = row["id"]
    goal_id = row["goal_id"]
    instance_id = row["shca_instance_id"]

    from app.services.operator_provisioning import (  # noqa: PLC0415
        resolve_repos_for_story,
        token_repo_names_for,
    )
    pool = get_pool()
    repos = await resolve_repos_for_story(pool, goal_id, story_id)
    if not repos:
        _log.debug("token_refresh: story %s has no repo resources — skipping", story_id)
        return
    repo_names = token_repo_names_for(repos[0], repos[1:])

    try:
        token_result = await generate_installation_token(repo_names)
        token = token_result.get("token", "")
        if not token:
            _log.warning("token_refresh: empty token for repos=%s story=%s",
                         repo_names, story_id)
            return
    except Exception as exc:
        _log.warning("token_refresh: mint failed for %s: %s", repo_names, exc)
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
            "token_refresh: refreshed token for story=%s instance=%s repos=%s",
            story_id, instance_id[:24], repo_names,
        )
    except GatewayError as exc:
        _log.warning(
            "token_refresh: gateway RPC failed for story=%s: %s",
            story_id, exc,
        )


async def _refresh_all_active() -> int:
    """Refresh tokens for every active operator story. Returns count refreshed.

    Selects DISTINCT stories rather than joining to `goal_resources` — a join would
    fan out one row per granted repo, and a multi-repo story would then be refreshed
    once per repo with each call overwriting the previous one's token, leaving only
    the last-processed repo's token installed. Repo resolution is delegated entirely
    to `resolve_repos_for_story` inside `_refresh_one_story`.
    """
    if _brokering_active():
        _log.debug(
            "token_refresh: sweep skipped — credentials are brokered per-operation",
        )
        return 0
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT s.id, s.goal_id, s.shca_instance_id
             FROM stories s
            WHERE s.execution_mode = 'operator'
              AND s.workspace_state = 'ready'
              -- Stage K: a docs-only workspace has no clone, so there is no
              -- credential helper to rotate. Refreshing one would mint a token
              -- every 50 minutes, push it into a workspace that cannot use it,
              -- and fail the refreshToken RPC on a repo that isn't there.
              AND s.workspace_kind = 'clone'
              AND s.state IN ('accepted', 'in_progress', 'blocked',
                              'awaiting_merge')
              -- Deleted-goal guard: don't keep minting GitHub tokens every 50m
              -- for a story whose goal was soft-deleted (teardown normally clears
              -- workspace_state, but this closes the gap if it didn't).
              AND EXISTS (
                  SELECT 1 FROM goals g
                   WHERE g.id = s.goal_id AND g.status = 'active'
              )
              AND s.shca_instance_id IS NOT NULL
              AND s.shca_instance_id <> ''
              AND EXISTS (
                  SELECT 1 FROM story_resources sr
                    JOIN goal_resources gr ON gr.id = sr.resource_id
                   WHERE sr.story_id = s.id
                     AND gr.kind IN ('repo', 'branch')
                     AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
              )
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
