"""GitHub App token generation for short-lived, repo-scoped access.

Flow:
  1. Sign a JWT with the App's private key (valid 10 min)
  2. Exchange it for an installation access token scoped to specific repos
  3. Token expires in 1 hour — no long-lived secrets in agent workspaces
"""
import logging
import time
from typing import Optional

import httpx
import jwt

from app.config import get_settings

_log = logging.getLogger(__name__)

# Cache the private key in memory after first fetch
_cached_pem: Optional[str] = None


def _get_private_key() -> str:
    """Get the GitHub App private key (.pem).

    Supports three modes (checked in order):
      1. Secret Manager path: "projects/..." → fetches at runtime
      2. File path: "/path/to/key.pem" → reads file
      3. Literal PEM string: "-----BEGIN RSA PRIVATE KEY-----..." → used directly
         (This is what Cloud Run --set-secrets injects)
    """
    global _cached_pem
    if _cached_pem:
        return _cached_pem

    settings = get_settings()
    secret_value = settings.github_app_private_key_secret

    if not secret_value:
        raise RuntimeError("github_app_private_key_secret is not configured")

    if secret_value.startswith("projects/"):
        # Secret Manager path — fetch at runtime
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=secret_value)
        _cached_pem = response.payload.data.decode("utf-8")
    elif secret_value.startswith("-----BEGIN"):
        # Literal PEM content (injected by Cloud Run --set-secrets)
        _cached_pem = secret_value
    else:
        # Try as file path
        try:
            with open(secret_value) as f:
                _cached_pem = f.read()
        except (FileNotFoundError, IsADirectoryError):
            _cached_pem = secret_value

    return _cached_pem


def _make_app_jwt() -> str:
    """Create a signed JWT for the GitHub App (expires in 10 min)."""
    settings = get_settings()
    if not settings.github_app_id:
        raise RuntimeError("github_app_id is not configured")

    pem = _get_private_key()
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 600,
        "iss": settings.github_app_id,
    }
    return jwt.encode(payload, pem, algorithm="RS256")


async def list_installation_repos() -> list[dict]:
    """List all repos accessible to the GitHub App installation.

    Returns a list of {"full_name": "org/repo", "name": "repo", "html_url": "...",
    "default_branch": "main", "private": bool, ...} dicts.
    """
    settings = get_settings()
    if not settings.github_app_installation_id:
        raise RuntimeError("github_app_installation_id is not configured")

    app_jwt = _make_app_jwt()

    # Get a short-lived token with metadata:read to list repos
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"https://api.github.com/app/installations/{settings.github_app_installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"permissions": {"metadata": "read"}},
            timeout=15,
        )
        if token_resp.status_code != 201:
            _log.error("Failed to get token for repo listing: %s", token_resp.text[:500])
            token_resp.raise_for_status()
        token = token_resp.json()["token"]

        # Paginate through all repos
        repos: list[dict] = []
        page = 1
        while True:
            resp = await client.get(
                "https://api.github.com/installation/repositories",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for r in data.get("repositories", []):
                repos.append({
                    "full_name": r["full_name"],
                    "name": r["name"],
                    "html_url": r["html_url"],
                    "default_branch": r.get("default_branch", "main"),
                    "private": r.get("private", False),
                })
            if len(repos) >= data.get("total_count", 0):
                break
            page += 1

        _log.info("Listed %d repos for installation", len(repos))
        return repos


async def list_repo_branches(repo_full_name: str) -> list[dict]:
    """List branches for a specific repo.

    Args:
        repo_full_name: e.g. "org/my-repo"

    Returns list of {"name": "main", "protected": bool} dicts.
    """
    settings = get_settings()
    if not settings.github_app_installation_id:
        raise RuntimeError("github_app_installation_id is not configured")

    app_jwt = _make_app_jwt()
    repo_name = repo_full_name.split("/")[-1]

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"https://api.github.com/app/installations/{settings.github_app_installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "repositories": [repo_name],
                "permissions": {"contents": "read", "metadata": "read"},
            },
            timeout=15,
        )
        if token_resp.status_code != 201:
            _log.error("Failed to get token for branch listing: %s", token_resp.text[:500])
            token_resp.raise_for_status()
        token = token_resp.json()["token"]

        branches: list[dict] = []
        page = 1
        while True:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/branches",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 100, "page": page},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            for b in data:
                branches.append({
                    "name": b["name"],
                    "protected": b.get("protected", False),
                })
            if len(data) < 100:
                break
            page += 1

        _log.info("Listed %d branches for %s", len(branches), repo_full_name)
        return branches


async def generate_installation_token(
    repo_names: list[str],
    permissions: Optional[dict] = None,
) -> dict:
    """
    Generate a short-lived installation access token scoped to specific repos.

    Args:
        repo_names: GitHub repo names (not full URLs), e.g. ["my-app", "shared-lib"]
        permissions: GitHub API permissions dict, defaults to read/write contents

    Returns:
        {"token": "ghs_...", "expires_at": "2026-...", "permissions": {...}, ...}
    """
    settings = get_settings()
    if not settings.github_app_installation_id:
        raise RuntimeError("github_app_installation_id is not configured")

    app_jwt = _make_app_jwt()

    if permissions is None:
        permissions = {"contents": "write", "metadata": "read", "workflows": "write"}

    body: dict = {
        "repositories": repo_names,
        "permissions": permissions,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.github.com/app/installations/{settings.github_app_installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=body,
            timeout=15,
        )
        if resp.status_code != 201:
            _log.error(
                "GitHub token request failed (%d): %s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()

        data = resp.json()
        _log.info(
            "Generated GitHub token for %d repos, expires %s",
            len(repo_names),
            data.get("expires_at", "?"),
        )
        return data
