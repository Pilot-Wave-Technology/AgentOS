"""Resolve the correct GitProvider from a goal_resources row.

The provider is read from goal_resources.metadata.provider ('github' | 'gitlab').

GitHub: uses the platform GitHub App installation token (short-lived, minted fresh
via generate_installation_token) — NOT a static PAT stored in metadata. This matches
how resource_provisioning.py authenticates git operations in the agent workspace.

GitLab: reads metadata.gitlab_token (a PAT stored in the resource metadata).
"""
from __future__ import annotations

import json
import logging

from .base import GitProvider
from .github import GithubProvider
from .gitlab import GitlabProvider

_log = logging.getLogger(__name__)


async def get_provider(goal_resource: dict) -> GitProvider:
    """Return an authenticated GitProvider for the given goal_resources row.

    For GitHub: mints a fresh GitHub App installation token scoped to the repo.
    For GitLab: reads metadata.gitlab_token.

    goal_resource must be a dict with at minimum:
      - metadata.provider: 'github' | 'gitlab'
      - repo_owner, repo_name  (GitHub — top-level columns on goal_resources)
      - metadata.gitlab_token + metadata.gitlab_project_id  (GitLab)
    """
    metadata = goal_resource.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    provider = metadata.get("provider", "")
    resource_id = goal_resource.get("id")
    _log.debug("git_providers.factory: resolving provider=%r for resource %s", provider, resource_id)

    if provider == "github":
        owner = goal_resource.get("repo_owner", "")
        repo = goal_resource.get("repo_name", "")
        if not owner or not repo:
            raise ValueError(
                f"goal_resource {resource_id}: repo_owner and repo_name are required for GitHub"
            )
        # Mint a fresh GitHub App installation token scoped to this repo.
        # Tokens expire in ~1h; gate handlers are short-lived so no refresh needed.
        from app.github_tokens import generate_installation_token  # noqa: PLC0415
        token_data = await generate_installation_token(repo_names=[repo])
        token = token_data["token"]
        _log.info("git_providers.factory: minted GitHub App token for %s/%s (resource=%s)", owner, repo, resource_id)
        return GithubProvider(token=token, owner=owner, repo=repo)

    if provider == "gitlab":
        token = metadata.get("gitlab_token", "")
        if not token:
            raise ValueError(f"goal_resource {resource_id}: metadata.gitlab_token is missing")
        project_id = str(metadata.get("gitlab_project_id", ""))
        if not project_id:
            raise ValueError(
                f"goal_resource {resource_id}: metadata.gitlab_project_id is required for GitLab"
            )
        base_url = metadata.get("gitlab_base_url", "https://gitlab.com")
        return GitlabProvider(token=token, project_id=project_id, base_url=base_url)

    raise ValueError(
        f"goal_resource {resource_id}: unknown provider={provider!r}. "
        f"Supported values: 'github', 'gitlab'"
    )
