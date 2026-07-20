"""GitLab REST API implementation of GitProvider.

# GitLab REST API v4 — https://docs.gitlab.com/ee/api/rest/
# Written against GitLab 17.x
# If this breaks after a GitLab update, check:
#   - Authentication: Private-Token or Bearer (OAuth2)
#   - Project ID vs namespace path: we use numeric project ID stored in
#     goal_resources.metadata.gitlab_project_id
#   - MR iid vs id: always use iid for project-scoped operations; iid is stored
#     as mr_id in integration_attempts
#   - Close MR + comment: PUT merge_requests/{iid} state_event=close, then POST notes
"""
from __future__ import annotations

import logging

import httpx

from .base import BranchProtection, MergeResult, MRResult, MRStatus

_log = logging.getLogger(__name__)


class GitlabProvider:
    """GitProvider implementation for GitLab repositories."""

    def __init__(self, token: str, project_id: str, base_url: str = "https://gitlab.com") -> None:
        self._project_id = project_id
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Private-Token": token,
            "Content-Type": "application/json",
        }

    def _api_url(self, path: str) -> str:
        return f"{self._base_url}/api/v4/projects/{self._project_id}/{path.lstrip('/')}"

    async def create_mr(
        self, source_branch: str, target_branch: str, title: str, description: str
    ) -> MRResult:
        _log.info(
            "gitlab: create MR %s → %s (project=%s)",
            source_branch, target_branch, self._project_id,
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._api_url("merge_requests"),
                headers=self._headers,
                json={
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "title": title,
                    "description": description,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        # Use iid (project-scoped) not id (global) for all subsequent calls
        mr_id = str(data["iid"])
        _log.info("gitlab: MR !%s created at %s", mr_id, data["web_url"])
        return MRResult(
            mr_id=mr_id,
            mr_url=data["web_url"],
            source_branch=source_branch,
            target_branch=target_branch,
        )

    async def merge_mr(self, mr_id: str) -> MergeResult:
        _log.info("gitlab: merging MR !%s (project=%s)", mr_id, self._project_id)
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                self._api_url(f"merge_requests/{mr_id}/merge"),
                headers=self._headers,
                json={"should_remove_source_branch": False},
            )
            if resp.status_code in (405, 406):
                _log.warning("gitlab: MR !%s not mergeable: %s", mr_id, resp.text)
                return MergeResult(success=False)
            resp.raise_for_status()
            data = resp.json()
        new_sha = data.get("sha", "")
        _log.info("gitlab: MR !%s merged, sha=%s", mr_id, new_sha)
        return MergeResult(success=True, new_trunk_sha=new_sha)

    async def update_mr(self, mr_id: str, description: str) -> None:
        _log.info("gitlab: updating MR !%s description", mr_id)
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                self._api_url(f"merge_requests/{mr_id}"),
                headers=self._headers,
                json={"description": description},
            )
            resp.raise_for_status()

    async def get_mr_status(self, mr_id: str) -> MRStatus:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self._api_url(f"merge_requests/{mr_id}"),
                headers=self._headers,
            )
            resp.raise_for_status()
            data = resp.json()
        state = data["state"]  # 'opened' | 'merged' | 'closed'
        if state == "opened":
            state = "open"
        mergeable = data.get("merge_status") == "can_be_merged"
        return MRStatus(
            mr_id=mr_id,
            state=state,
            mergeable=mergeable,
            mr_url=data["web_url"],
        )

    async def close_mr(self, mr_id: str, reason: str) -> None:
        """Close an MR and add a note (comment) explaining why."""
        _log.info("gitlab: closing MR !%s with reason note", mr_id)
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                self._api_url(f"merge_requests/{mr_id}"),
                headers=self._headers,
                json={"state_event": "close"},
            )
            resp.raise_for_status()
            resp = await client.post(
                self._api_url(f"merge_requests/{mr_id}/notes"),
                headers=self._headers,
                json={"body": reason},
            )
            resp.raise_for_status()
        _log.info("gitlab: MR !%s closed", mr_id)

    async def direct_push_merge(
        self, source_branch: str, target_branch: str
    ) -> MergeResult:
        """Fast-forward merge source into target via the GitLab repository merge API."""
        _log.info(
            "gitlab: direct push merge %s → %s (project=%s)",
            source_branch, target_branch, self._project_id,
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._api_url("repository/merges"),
                headers=self._headers,
                json={"source_branch": source_branch, "target_branch": target_branch},
            )
            if resp.status_code == 406:
                _log.warning("gitlab: direct push — merge conflict: %s", resp.text)
                return MergeResult(success=False)
            resp.raise_for_status()
            data = resp.json()
        new_sha = data.get("id", "")
        _log.info("gitlab: direct push merged, commit=%s", new_sha)
        return MergeResult(success=True, new_trunk_sha=new_sha)

    async def get_branch_protection(self, branch: str) -> BranchProtection:
        _log.debug("gitlab: querying protected branches for %s", branch)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self._api_url(f"protected_branches/{branch}"),
                headers=self._headers,
            )
            if resp.status_code == 404:
                return BranchProtection(branch=branch, pr_review_required=False, approvals_required=0)
            resp.raise_for_status()
            data = resp.json()
        # GitLab protection: push_access_levels controls who can push directly.
        # If merge access is restricted and direct push is blocked, a MR is required.
        push_levels = data.get("push_access_levels", [])
        # access_level 0 = No access; protected = no direct push → MR required.
        direct_push_blocked = all(lv.get("access_level", 0) == 0 for lv in push_levels) if push_levels else False
        _log.info("gitlab: branch protection on %s — direct_push_blocked=%s", branch, direct_push_blocked)
        return BranchProtection(
            branch=branch,
            pr_review_required=direct_push_blocked,
            approvals_required=data.get("approvals_before_merge", 0),
            raw=data,
        )

    async def get_default_branch(self) -> str:
        _log.debug("gitlab: fetching default branch for project %s", self._project_id)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/api/v4/projects/{self._project_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            data = resp.json()
        branch = data.get("default_branch", "main")
        _log.info("gitlab: default branch = %s", branch)
        return branch

    async def register_webhook(
        self, repo_url: str, events: list[str], target_url: str, secret: str
    ) -> str:
        _log.info("gitlab: registering webhook on project %s → %s", self._project_id, target_url)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._api_url("hooks"),
                headers=self._headers,
                json={
                    "url": target_url,
                    "token": secret,
                    "merge_requests_events": True,
                    "push_events": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        webhook_id = str(data["id"])
        _log.info("gitlab: webhook registered, id=%s", webhook_id)
        return webhook_id

    async def delete_webhook(self, webhook_id: str) -> None:
        _log.info("gitlab: deleting webhook %s", webhook_id)
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                self._api_url(f"hooks/{webhook_id}"),
                headers=self._headers,
            )
            resp.raise_for_status()
