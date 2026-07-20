"""Fetch file content from GitHub repos via the GitHub API.

Uses the GitHub App installation token for authentication.
"""
import logging
from typing import Optional

import httpx

from app.github_tokens import generate_installation_token

_log = logging.getLogger(__name__)


async def fetch_file_from_repo(
    repo_name: str,
    file_path: str,
    branch: str,
) -> Optional[str]:
    """Fetch a file's content from a GitHub repo.

    Args:
        repo_name: GitHub repo name (e.g. "multi_agent_dummy_repo")
        file_path: Path within the repo (e.g. "CLAUDE.md")
        branch: Branch name (e.g. "feature/task-123-something")

    Returns:
        File content as string, or None if not found / error.
    """
    try:
        token_data = await generate_installation_token(
            [repo_name],
            {"contents": "read", "metadata": "read"},
        )
        token = token_data["token"]
    except Exception as exc:
        _log.warning("Failed to get GitHub token for %s: %s", repo_name, exc)
        return None

    # GitHub API: GET /repos/{owner}/{repo}/contents/{path}?ref={branch}
    # Returns base64-encoded content for files < 1MB, or a download URL for larger
    try:
        # Use the raw content endpoint for simplicity. Org/owner is deployment
        # config (GITHUB_ORG) — never hardcoded.
        from app.config import get_settings as _get_settings
        _org = _get_settings().github_org
        url = f"https://raw.githubusercontent.com/{_org}/{repo_name}/{branch}/{file_path}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.raw+json",
            })
            if resp.status_code == 200:
                _log.info("Fetched %s from %s/%s (%d chars)", file_path, repo_name, branch[:20], len(resp.text))
                return resp.text
            elif resp.status_code == 404:
                _log.info("File %s not found in %s/%s", file_path, repo_name, branch[:20])
                return None
            else:
                _log.warning("GitHub API returned %d for %s/%s/%s", resp.status_code, repo_name, branch[:20], file_path)
                return None
    except Exception as exc:
        _log.warning("Failed to fetch %s from %s: %s", file_path, repo_name, exc)
        return None
