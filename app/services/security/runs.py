"""Enqueue security review runs + their stages (the queue write side).

A run is BRANCH-scoped: one (clone_url, branch_name). It fans out into one stage
per enabled aspect + a convener stage that depends on them all. Deduped per
branch; global single-run + per-branch dedup are also DB-enforced (partial
unique indices in security_review_tables.sql).
"""
from __future__ import annotations

import logging
import re
from uuid import uuid4

import asyncpg

from app.database import get_pool

log = logging.getLogger(__name__)

# Aspects the council runs (the convener depends on whatever is here). Their
# templates + AGENTS.md are seeded by the security_seed_* migrations and the
# per-aspect tools are installed on the VM (redeploy.sh). A stage whose tool is
# missing just reports failed and the convener notes the coverage gap.
ENABLED_ASPECTS: list[str] = ["secrets", "sast", "sca", "access", "infra"]


async def _wake_dispatcher(pool) -> None:
    """Cross-service wake: NOTIFY security_wake so a dispatcher in any service
    processes the new run immediately instead of waiting for its poll. Best-effort
    — the poll loop is the backstop."""
    try:
        await pool.execute("SELECT pg_notify('security_wake', '')")
    except Exception:
        log.warning("security: pg_notify(security_wake) failed (poll will still pick it up)")


async def resolve_branch_resource(resource_id: str) -> dict | None:
    """Resolve a branch goal_resource into the fields a run needs.

    Accepts either a 'branch' resource (preferred) or a 'repo' resource (falls
    back to its default_branch). Returns clone_url/branch_name/repo_owner/
    repo_name, or None if not a usable repo/branch resource.
    """
    pool = get_pool()
    r = await pool.fetchrow(
        "SELECT id, kind, name, parent_id, clone_url, branch_name, default_branch, "
        "repo_owner, repo_name FROM goal_resources WHERE id = $1",
        resource_id,
    )
    if not r or r["kind"] not in ("branch", "repo"):
        return None

    clone_url = (r["clone_url"] or "").strip()
    repo_owner = (r["repo_owner"] or "").strip()
    repo_name = (r["repo_name"] or "").strip()
    if r["kind"] == "branch":
        branch_name = (r["branch_name"] or "").strip()
        # A branch row inherits clone_url/owner/name from its repo parent; fill any
        # gaps from the parent so older rows still resolve.
        if (not clone_url or not repo_owner or not repo_name) and r["parent_id"]:
            parent = await pool.fetchrow(
                "SELECT clone_url, repo_owner, repo_name FROM goal_resources WHERE id = $1",
                r["parent_id"],
            )
            if parent:
                clone_url = clone_url or (parent["clone_url"] or "").strip()
                repo_owner = repo_owner or (parent["repo_owner"] or "").strip()
                repo_name = repo_name or (parent["repo_name"] or "").strip()
    else:  # repo
        branch_name = (r["default_branch"] or "main").strip()

    if not clone_url or not branch_name:
        return None
    return {
        "resource_id": r["id"],
        "clone_url": clone_url,
        "branch_name": branch_name,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
    }


def _already_running_reason(branch_name: str, status: str | None) -> str:
    verb = "running" if status == "running" else "queued"
    return (f"A security review of branch '{branch_name}' is already {verb} — wait for it "
            f"to finish before starting another.")


def _owner_repo_from(clone_url: str, repo_owner: str, repo_name: str) -> tuple[str, str]:
    if repo_owner and repo_name:
        return repo_owner, repo_name
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", clone_url or "")
    return (m.group(1), m.group(2)) if m else ("", "")


async def resolve_remote_head_sha(branch: dict) -> str | None:
    """Current tip commit SHA of the branch, via the GitHub API (no clone). Used
    by the scheduled-skip check to compare against the last reviewed SHA. Returns
    None on any failure — the caller then treats the branch as 'changed' and
    reviews it (safe default: never skip on uncertainty)."""
    owner, repo = _owner_repo_from(branch.get("clone_url", ""),
                                   branch.get("repo_owner", ""), branch.get("repo_name", ""))
    if not owner or not repo:
        return None
    token = ""
    try:
        from app.github_tokens import generate_installation_token
        tok = await generate_installation_token([repo], permissions={"contents": "read"})
        token = (tok or {}).get("token") or ""
    except Exception as exc:
        log.info("security: head-sha token mint failed for %s (trying unauth): %s", repo, exc)
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch['branch_name']}"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return ((resp.json() or {}).get("commit") or {}).get("sha") or None
        log.info("security: head-sha lookup HTTP %s for %s/%s@%s",
                 resp.status_code, owner, repo, branch["branch_name"])
    except Exception as exc:
        log.info("security: head-sha lookup failed for %s/%s: %s", owner, repo, exc)
    return None


async def branch_remote_state(branch: dict) -> str:
    """Does the branch still exist on the remote? Returns 'gone' (definitive 404),
    'exists' (200), or 'unknown' (anything else — no owner/repo, auth/network
    error, rate limit). The dispatcher uses this to SKIP a deleted branch cleanly
    (mark the run done, no scan) instead of failing five clone attempts. On
    'unknown' the caller proceeds and reviews — never skip on uncertainty."""
    owner, repo = _owner_repo_from(branch.get("clone_url", ""),
                                   branch.get("repo_owner", ""), branch.get("repo_name", ""))
    if not owner or not repo:
        return "unknown"
    token = ""
    try:
        from app.github_tokens import generate_installation_token
        tok = await generate_installation_token([repo], permissions={"contents": "read"})
        token = (tok or {}).get("token") or ""
    except Exception as exc:
        log.info("security: branch-state token mint failed for %s (trying unauth): %s", repo, exc)
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch['branch_name']}"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return "exists"
        if resp.status_code == 404:
            return "gone"
        log.info("security: branch-state HTTP %s for %s/%s@%s",
                 resp.status_code, owner, repo, branch["branch_name"])
    except Exception as exc:
        log.info("security: branch-state lookup failed for %s/%s: %s", owner, repo, exc)
    return "unknown"


async def last_full_review_run_id(clone_url: str, branch_name: str, head_sha: str) -> str | None:
    """The id of the most recent run that fully reviewed this exact (clone_url,
    branch, head_sha) — status='done' with NO failed stages — or None. A run with
    any failed stage does NOT count, so a partial/failed review never blocks a
    scheduled retry (graceful stage-failure handling)."""
    if not head_sha:
        return None
    pool = get_pool()
    return await pool.fetchval(
        "SELECT r.id FROM security_review_runs r "
        "WHERE lower(r.clone_url) = lower($1) AND lower(r.branch_name) = lower($2) "
        "AND r.status = 'done' AND r.head_sha = $3 "
        "AND NOT EXISTS (SELECT 1 FROM security_review_stages s "
        "                WHERE s.run_id = r.id AND s.status = 'failed') "
        "ORDER BY r.finished_at DESC LIMIT 1",
        clone_url, branch_name, head_sha,
    )


async def _record_skip(clone_url: str, branch_name: str, head_sha: str,
                       reviewed_run_id: str) -> None:
    """Persist the skip decision (audit) — one upserted row per branch. skip_count
    resets to 1 when the pinned head changes (a new review happened), else increments."""
    pool = get_pool()
    try:
        await pool.execute(
            "INSERT INTO security_review_skips (id, clone_url, branch_name, head_sha, reviewed_run_id) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (lower(clone_url), lower(branch_name)) DO UPDATE SET "
            "  skip_count = CASE WHEN security_review_skips.head_sha = EXCLUDED.head_sha "
            "                    THEN security_review_skips.skip_count + 1 ELSE 1 END, "
            "  head_sha = EXCLUDED.head_sha, "
            "  reviewed_run_id = EXCLUDED.reviewed_run_id, "
            "  last_skipped_at = NOW()",
            uuid4().hex, clone_url, branch_name, head_sha, reviewed_run_id,
        )
    except Exception as exc:
        log.warning("security: failed to record skip for branch %s: %s", branch_name, exc)


async def enqueue_branch_review(
    branch: dict, created_by: str | None = None, trigger: str = "manual",
) -> dict:
    """Create a pending security run for a branch + its aspect/convener stages.

    `branch` is the dict from resolve_branch_resource. Deduped per
    (clone_url, branch_name): if an active run exists, returns it.

    SCHEDULED runs (trigger != 'manual') SKIP a branch whose current head SHA was
    already fully reviewed (status='done', no failed stages). MANUAL triggers
    never skip — they always (re)review.
    """
    pool = get_pool()
    clone_url = branch["clone_url"]
    branch_name = branch["branch_name"]

    # Scheduled-skip: only for non-manual triggers, and only when the branch is
    # genuinely unchanged since a fully-successful review. On any uncertainty
    # (can't resolve the head sha), fall through and review.
    if trigger != "manual":
        head_sha = await resolve_remote_head_sha(branch)
        reviewed_run_id = await last_full_review_run_id(clone_url, branch_name, head_sha) if head_sha else None
        if reviewed_run_id:
            await _record_skip(clone_url, branch_name, head_sha, reviewed_run_id)
            log.info("security: scheduled review SKIPPED branch=%s — unchanged (head=%s) "
                     "since last full review (run=%s)", branch_name, head_sha[:12], reviewed_run_id)
            return {
                "created": False,
                "skipped": True,
                "head_sha": head_sha,
                "reviewed_run_id": reviewed_run_id,
                "reason": f"branch '{branch_name}' unchanged (head {head_sha[:12]}) since "
                          f"its last successful review",
            }

    existing = await pool.fetchrow(
        "SELECT id, status FROM security_review_runs "
        "WHERE lower(clone_url) = lower($1) AND lower(branch_name) = lower($2) "
        "AND status IN ('pending', 'running')",
        clone_url, branch_name,
    )
    if existing:
        return {
            "created": False,
            "run_id": existing["id"],
            "status": existing["status"],
            "reason": _already_running_reason(branch_name, existing["status"]),
        }

    run_id = uuid4().hex
    try:
        await pool.execute(
            "INSERT INTO security_review_runs "
            "(id, repo_owner, repo_name, branch_name, clone_url, resource_id, trigger, status, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8)",
            run_id, branch["repo_owner"], branch["repo_name"], branch_name, clone_url,
            branch.get("resource_id"), trigger, created_by,
        )
    except asyncpg.exceptions.UniqueViolationError:
        existing = await pool.fetchrow(
            "SELECT id, status FROM security_review_runs "
            "WHERE lower(clone_url) = lower($1) AND lower(branch_name) = lower($2) "
            "AND status IN ('pending', 'running')",
            clone_url, branch_name,
        )
        return {
            "created": False,
            "run_id": existing["id"] if existing else None,
            "reason": _already_running_reason(branch_name, existing["status"] if existing else "pending"),
        }

    for aspect in ENABLED_ASPECTS:
        await pool.execute(
            "INSERT INTO security_review_stages (id, run_id, aspect, status, depends_on) "
            "VALUES ($1, $2, $3, 'pending', '{}')",
            uuid4().hex, run_id, aspect,
        )
    await pool.execute(
        "INSERT INTO security_review_stages (id, run_id, aspect, status, depends_on) "
        "VALUES ($1, $2, 'convener', 'blocked_on_deps', $3)",
        uuid4().hex, run_id, ENABLED_ASPECTS,
    )
    await _wake_dispatcher(pool)
    log.info("security: enqueued run=%s branch=%s (%d aspect stage(s))",
             run_id, branch_name, len(ENABLED_ASPECTS))
    return {"created": True, "run_id": run_id, "status": "pending"}
