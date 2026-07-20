"""GitHub REST API implementation of GitProvider.

# GitHub REST API v3 — https://docs.github.com/en/rest
# Written against API version: 2022-11-28
# If this breaks after a GitHub API update, check:
#   - Accept header: 'application/vnd.github+json'
#   - Authentication: Bearer token (not Basic auth, deprecated)
#   - Merge method: 'merge' | 'squash' | 'rebase' — stored in goal_resources.metadata
#   - PR close+comment: PATCH state=closed, then POST /issues/{number}/comments
"""
from __future__ import annotations

import logging

import httpx

from .base import BranchProtection, MergeResult, MRResult, MRStatus

_log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_API_VERSION_HEADER = "2022-11-28"


class GithubProvider:
    """GitProvider implementation for GitHub repositories."""

    def __init__(self, token: str, owner: str, repo: str) -> None:
        self._owner = owner
        self._repo = repo
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION_HEADER,
        }

    def _repo_url(self, path: str) -> str:
        return f"{_GITHUB_API}/repos/{self._owner}/{self._repo}/{path.lstrip('/')}"

    async def create_mr(
        self, source_branch: str, target_branch: str, title: str, description: str
    ) -> MRResult:
        _log.info(
            "github: create PR %s → %s (repo=%s/%s)",
            source_branch, target_branch, self._owner, self._repo,
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._repo_url("pulls"),
                headers=self._headers,
                json={"title": title, "body": description, "head": source_branch, "base": target_branch},
            )
            resp.raise_for_status()
            data = resp.json()
        mr_id = str(data["number"])
        _log.info("github: PR #%s created at %s", mr_id, data["html_url"])
        return MRResult(
            mr_id=mr_id,
            mr_url=data["html_url"],
            source_branch=source_branch,
            target_branch=target_branch,
        )

    async def merge_mr(self, mr_id: str) -> MergeResult:
        _log.info("github: merging PR #%s (repo=%s/%s)", mr_id, self._owner, self._repo)
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                self._repo_url(f"pulls/{mr_id}/merge"),
                headers=self._headers,
                json={"merge_method": "merge"},
            )
            if resp.status_code == 405:
                # Not mergeable (conflicts or already merged)
                _log.warning("github: PR #%s not mergeable: %s", mr_id, resp.text)
                return MergeResult(success=False, conflict_files=[])
            resp.raise_for_status()
            data = resp.json()
        _log.info("github: PR #%s merged, sha=%s", mr_id, data.get("sha", ""))
        return MergeResult(success=True, new_trunk_sha=data.get("sha", ""))

    async def update_mr(self, mr_id: str, description: str) -> None:
        _log.info("github: updating PR #%s description", mr_id)
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                self._repo_url(f"pulls/{mr_id}"),
                headers=self._headers,
                json={"body": description},
            )
            resp.raise_for_status()

    async def get_mr_status(self, mr_id: str) -> MRStatus:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self._repo_url(f"pulls/{mr_id}"),
                headers=self._headers,
            )
            resp.raise_for_status()
            data = resp.json()
        state = data["state"]  # 'open' | 'closed'
        merged = data.get("merged", False)
        if merged:
            state = "merged"
        mergeable = data.get("mergeable") is True
        return MRStatus(
            mr_id=mr_id,
            state=state,
            mergeable=mergeable,
            mr_url=data["html_url"],
        )

    async def close_mr(self, mr_id: str, reason: str) -> None:
        """Close a PR and post a comment explaining why (used on story cancellation)."""
        _log.info("github: closing PR #%s with reason comment", mr_id)
        async with httpx.AsyncClient() as client:
            # Close the PR
            resp = await client.patch(
                self._repo_url(f"pulls/{mr_id}"),
                headers=self._headers,
                json={"state": "closed"},
            )
            resp.raise_for_status()
            # Add reason comment via issues API (PRs share the issues namespace)
            resp = await client.post(
                self._repo_url(f"issues/{mr_id}/comments"),
                headers=self._headers,
                json={"body": reason},
            )
            resp.raise_for_status()
        _log.info("github: PR #%s closed", mr_id)

    async def direct_push_merge(
        self, source_branch: str, target_branch: str
    ) -> MergeResult:
        """Fast-forward merge source into target via the GitHub merge API and push.

        Uses GitHub's branch merge endpoint which handles the ff-merge atomically.
        """
        _log.info(
            "github: direct push merge %s → %s (repo=%s/%s)",
            source_branch, target_branch, self._owner, self._repo,
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._repo_url("merges"),
                headers=self._headers,
                json={"base": target_branch, "head": source_branch},
            )
            if resp.status_code == 204:
                # Already up to date
                _log.info("github: direct push — already up to date")
                return MergeResult(success=True, new_trunk_sha="")
            if resp.status_code == 409:
                _log.warning("github: direct push — merge conflict: %s", resp.text)
                return MergeResult(success=False)
            resp.raise_for_status()
            data = resp.json()
        new_sha = data.get("sha", "")
        _log.info("github: direct push merged, new sha=%s", new_sha)
        return MergeResult(success=True, new_trunk_sha=new_sha)

    async def get_branch_protection(self, branch: str) -> BranchProtection:
        _log.debug("github: querying branch protection for %s", branch)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self._repo_url(f"branches/{branch}/protection"),
                headers=self._headers,
            )
            if resp.status_code == 404:
                # No protection configured
                return BranchProtection(branch=branch, pr_review_required=False, approvals_required=0)
            resp.raise_for_status()
            data = resp.json()
        reviews = data.get("required_pull_request_reviews", {})
        approvals = reviews.get("required_approving_review_count", 0) if reviews else 0
        pr_required = bool(reviews)
        _log.info(
            "github: branch protection on %s — pr_required=%s approvals=%s",
            branch, pr_required, approvals,
        )
        return BranchProtection(
            branch=branch,
            pr_review_required=pr_required,
            approvals_required=approvals,
            raw=data,
        )

    async def get_default_branch(self) -> str:
        _log.debug("github: fetching default branch for %s/%s", self._owner, self._repo)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self._repo_url(""),
                headers=self._headers,
            )
            resp.raise_for_status()
            data = resp.json()
        branch = data.get("default_branch", "main")
        _log.info("github: default branch = %s", branch)
        return branch

    async def register_webhook(
        self, repo_url: str, events: list[str], target_url: str, secret: str
    ) -> str:
        _log.info("github: registering webhook on %s/%s → %s", self._owner, self._repo, target_url)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._repo_url("hooks"),
                headers=self._headers,
                json={
                    "name": "web",
                    "active": True,
                    "events": events,
                    "config": {"url": target_url, "content_type": "json", "secret": secret},
                },
            )
            resp.raise_for_status()
            data = resp.json()
        webhook_id = str(data["id"])
        _log.info("github: webhook registered, id=%s", webhook_id)
        return webhook_id

    async def delete_webhook(self, webhook_id: str) -> None:
        _log.info("github: deleting webhook %s", webhook_id)
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                self._repo_url(f"hooks/{webhook_id}"),
                headers=self._headers,
            )
            resp.raise_for_status()
