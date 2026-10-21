"""GitHub App token generation for short-lived, repo-scoped access.

Flow:
  1. Sign a JWT with the App's private key (valid 10 min)
  2. Exchange it for an installation access token scoped to specific repos
  3. Token expires in 1 hour — no long-lived secrets in agent workspaces
"""
import asyncio
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
    # 540, NOT 600. GitHub rejects a JWT whose `exp` is more than ten
    # minutes ahead of ITS clock, and 600 leaves exactly zero margin --
    # so the token is valid only while the local clock is not running
    # fast. Four seconds of skew was enough to fail every request with
    # "'Expiration time' claim ('exp') is too far in the future".
    #
    # It never showed on Cloud Run, whose clock is NTP-tight, and that
    # is the point: a boundary value that depends on the caller's clock
    # is a bug that only appears somewhere else.
    #
    # `iat` is backdated for the same reason in the other direction.
    payload = {
        "iat": now - 60,
        "exp": now + 540,
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


async def verify_commit_pushed(
    repo_full_name: str, branch: str, sha: str,
) -> tuple[str, str]:
    """Is `sha` actually reachable from `branch` on the REMOTE?

    Returns ``(status, detail)`` where status is one of:

      * ``"verified"`` — the commit is on the branch.
      * ``"missing"``  — the remote answered, and it is not. A definitive negative.
      * ``"unknown"``  — we could not tell (network, rate limit, auth, unexpected shape).

    The three-way result is deliberate. A caller must be able to distinguish "the push
    did not happen" from "we could not check", because the safe response differs: the
    first is a refusal, the second must be recorded as unverified rather than quietly
    reported as success. Collapsing them into a bool is how a transient outage becomes
    either a blocked user or a false claim.

    Uses `compare/{branch}...{sha}` rather than comparing the branch HEAD to `sha`. The
    question is "is this commit ON the branch", not "is it the tip" — anything else
    pushing to the branch afterwards would make a tip check report a good push as bad.
    GitHub reports `identical` when they are the same commit and `behind` when `sha` is
    an ancestor of the branch; both mean the commit landed.
    """
    settings = get_settings()
    if not settings.github_app_installation_id:
        return "unknown", "github_app_installation_id is not configured"
    if not (repo_full_name and branch and sha):
        return "unknown", "repo, branch and sha are all required"

    try:
        app_jwt = _make_app_jwt()
        repo_name = repo_full_name.split("/")[-1]
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                f"https://api.github.com/app/installations/"
                f"{settings.github_app_installation_id}/access_tokens",
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
                return "unknown", f"token mint failed: HTTP {token_resp.status_code}"
            token = token_resp.json()["token"]

            resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/compare/{branch}...{sha}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=20,
            )
            if resp.status_code == 404:
                # The branch or the commit is not there. Definitive: a push that landed
                # would have created both.
                return "missing", "branch or commit not found on the remote"
            if resp.status_code != 200:
                return "unknown", f"compare failed: HTTP {resp.status_code}"

            status = (resp.json() or {}).get("status") or ""
            if status in ("identical", "behind"):
                return "verified", status
            if status in ("ahead", "diverged"):
                return "missing", f"commit is not on the branch (compare status {status!r})"
            return "unknown", f"unrecognised compare status {status!r}"
    except Exception as exc:  # noqa: BLE001 — any fault is "could not tell", never "did not happen"
        _log.warning(
            "verify_commit_pushed: could not verify %s@%s on %s: %s",
            sha[:12], branch, repo_full_name, exc,
        )
        return "unknown", str(exc)


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
        resp = await _post_access_token(client, settings, app_jwt, body)
        if resp.status_code != 201:
            _log.error(
                "GitHub token request failed (%d): %s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()

        data = resp.json()
        # WHAT GITHUB GRANTED, not what we asked for. The old line said
        # only how many repos and when it expires, so when a token came
        # back WITHOUT the write we requested there was nothing anywhere
        # that said so -- the caller logged "issued write credential"
        # (its own request) and the push failed with a 403 three layers
        # away. Execution 204 cost three rounds of guessing at GitHub App
        # settings that turned out to be fine; the answer was in this
        # response object all along and we threw it away.
        #
        # The repositories come back with `full_name`, which is the other
        # half: `body["repositories"]` is sent as bare NAMES, so an
        # installation spanning two accounts with a same-named repo would
        # scope somewhere nobody asked for, silently. Logging what came
        # back is how that becomes visible rather than inferred.
        granted = data.get("permissions") or {}
        scoped = [r.get("full_name") or r.get("name")
                  for r in (data.get("repositories") or [])]
        _log.info(
            "Generated GitHub token: asked=%s granted=%s repos=%s "
            "expires %s",
            permissions, granted, scoped or repo_names,
            data.get("expires_at", "?"),
        )
        missing = {k: v for k, v in permissions.items()
                   if granted.get(k) != v}
        if missing:
            # LOUD, because every symptom of this is somewhere else. A
            # narrowed token does not fail here; it fails later, as a
            # forge refusal, in a container, on a path that reads like
            # the work being wrong.
            _log.error(
                "GitHub NARROWED the token: asked %s, granted %s. The "
                "installation does not hold %s for these repos. Expect a "
                "403 from git, not an error here.",
                permissions, granted, sorted(missing),
            )
        return data


# F5 — A 5xx HERE IS INVISIBLE THREE LAYERS DOWN. A transient 500 on
# `access_tokens` used to surface as GitHub's `Repository not found` on
# the clone, which reads as a missing repository rather than an auth
# failure; a 502 on the same endpoint stranded a branch. Both were
# transient and neither was retried.
#
# Only 5xx and transport errors are retried. A 401/403/404 is an answer
# — the app is not installed, or not on that repo — and retrying it just
# delays a correct failure by three seconds.
_TOKEN_RETRY_STATUSES = (500, 502, 503, 504)


async def _post_access_token(client, settings, app_jwt: str, body: dict):
    """POST to `access_tokens`, retrying only what is worth retrying."""
    url = (f"https://api.github.com/app/installations/"
           f"{settings.github_app_installation_id}/access_tokens")
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    last_error: Exception | None = None
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2 ** (attempt - 1))
        try:
            resp = await client.post(url, headers=headers, json=body,
                                     timeout=15)
        except httpx.HTTPError as exc:
            last_error = exc
            _log.warning("GitHub token request transport error "
                         "(attempt %d/3): %s", attempt + 1, exc)
            continue
        if resp.status_code not in _TOKEN_RETRY_STATUSES:
            return resp
        last_error = None
        _log.warning("GitHub token request returned %d (attempt %d/3); "
                     "retrying", resp.status_code, attempt + 1)
    if last_error is not None:
        raise last_error
    return resp
