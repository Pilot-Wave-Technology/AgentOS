"""GitHub API operations for the git engine — perform-once + verify-by-read (plan §5.2).

Low-level, stateless calls. The engine mints its OWN short-lived installation
token (github_tokens.generate_installation_token) per repo — independent of any
agent session (plan §5.5.9). These are the ONLY GitHub writes the engine does;
write endpoints are NOT idempotent-by-assertion, so callers verify by reading
(e.g. create-ref treats 422 "already exists" as success, not failure).

Surface: read trunk SHA, create a branch ref (idempotent), read-only ahead/behind
status, PR/protection/checks/mergeability reads (used by detection + the grind
loop). NOTE: the write merge ops (`merge_pr` / `open_or_get_pr` / `merge_ref_into`)
are RETIRED from the live path (merge-model rework, spec §10) — the engine no
longer self-merges; a human merges on the PR and `detect.py` reconciles. They
remain defined only for build §5/§7 to lift the parts they reuse.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx

from app.github_tokens import generate_installation_token

from . import gh_client

_log = logging.getLogger(__name__)

_API = "https://api.github.com"
_TIMEOUT = 20

# Step-1 reads (get_pr, get_branch_protection, + the new mergeability/checks/
# verification/rulesets/merge-queue reads) go through the pooled, dual-budget
# `gh_client` (spec §7). The legacy write/compare ops below still open a per-call
# client; they migrate to gh_client when the merge lane is rebuilt (build §3/§5).


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def mint_repo_token(repo_name: str, permissions: Optional[dict] = None) -> str:
    """Mint an installation token scoped to one repo. Default perms (contents:write,
    metadata:read, workflows:write) cover create-ref + compare; PR/merge perms are
    added by callers that need them (later steps)."""
    data = await generate_installation_token([repo_name], permissions)
    return data["token"]


async def get_ref_sha(
    token: str, owner: str, name: str, branch: str
) -> Optional[str]:
    """HEAD SHA of refs/heads/{branch}, or None if the ref does not exist (404)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_API}/repos/{owner}/{name}/git/ref/heads/{branch}",
            headers=_headers(token), timeout=_TIMEOUT,
        )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


async def get_trunk_sha(token: str, owner: str, name: str, trunk_branch: str) -> str:
    """Live HEAD SHA of the repo's trunk. Always read live before a cut (plan §9)."""
    sha = await get_ref_sha(token, owner, name, trunk_branch)
    if sha is None:
        raise RuntimeError(f"trunk branch {trunk_branch!r} not found in {owner}/{name}")
    return sha


async def create_ref(
    token: str, owner: str, name: str, branch: str, sha: str
) -> str:
    """Create refs/heads/{branch} at `sha`. Returns 'created' or 'exists'.

    422 "Reference already exists" is treated as success (verify-by-read, plan §5.2):
    the ref is there; we do NOT re-point it to a different sha (the recorded base_sha
    is authoritative; the live tip may have advanced)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_API}/repos/{owner}/{name}/git/refs",
            headers=_headers(token),
            json={"ref": f"refs/heads/{branch}", "sha": sha},
            timeout=_TIMEOUT,
        )
    if resp.status_code == 201:
        return "created"
    if resp.status_code == 422 and "already exists" in resp.text.lower():
        return "exists"
    _log.error("create_ref %s/%s %s failed (%d): %s",
               owner, name, branch, resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return "created"  # unreachable; raise_for_status throws on non-2xx


async def delete_ref(token: str, owner: str, name: str,
                     branch: str) -> bool:
    """Delete a branch ref. Best-effort by design: 404/422 means already
    gone (a human deleted it, or the repo auto-deletes heads on merge) —
    that is success, not failure. Called only for a branch the engine
    itself cut, and only after its merge landed on trunk, so the commits
    are safe wherever this errs."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(
            f"{_API}/repos/{owner}/{name}/git/refs/heads/{branch}",
            headers=_headers(token),
        )
    return r.status_code in (204, 404, 422)


async def compare(
    token: str, owner: str, name: str, base: str, head: str
) -> dict:
    """GET /compare/{base}...{head} → {ahead_by, behind_by, status}. Read-only.

    With base=trunk, head=branch: ahead_by = commits the branch is ahead of trunk,
    behind_by = commits the branch is behind trunk.

    base/head are URL-encoded per side (the literal `...` separator is preserved):
    branch names contain `/` (feat/<goal>-<story>), and an unencoded slash in the
    single {basehead} path segment is parsed as extra path → 404."""
    basehead = f"{quote(base, safe='')}...{quote(head, safe='')}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_API}/repos/{owner}/{name}/compare/{basehead}",
            headers=_headers(token), timeout=_TIMEOUT,
        )
    resp.raise_for_status()
    data = resp.json()
    return {
        "ahead_by": data.get("ahead_by", 0),
        "behind_by": data.get("behind_by", 0),
        "status": data.get("status", "unknown"),
    }


# ── merge / PR / restack ops (Step 6; PR ops need a token minted with
#    pull_requests:write — the one-time GitHub App re-consent prerequisite) ──

async def open_or_get_pr(
    token: str, owner: str, name: str,
    head_branch: str, base_branch: str, title: str, body: str = "",
) -> dict:
    """Open a PR head_branch→base_branch, or return the existing OPEN one
    (perform-once + verify-by-read). Returns {number, head_sha}.

    Call only when the branch is ahead of trunk (the lane checks `compare` first),
    so the 422 here means "a PR already exists", not "no commits between"."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_API}/repos/{owner}/{name}/pulls",
            headers=_headers(token),
            json={"title": title, "head": head_branch, "base": base_branch, "body": body},
            timeout=_TIMEOUT,
        )
    if resp.status_code == 201:
        d = resp.json()
        return {"number": d["number"], "head_sha": d["head"]["sha"]}
    if resp.status_code == 422:
        # Already exists → find the open PR for this head/base.
        async with httpx.AsyncClient() as client:
            lst = await client.get(
                f"{_API}/repos/{owner}/{name}/pulls",
                headers=_headers(token),
                params={"head": f"{owner}:{head_branch}", "base": base_branch, "state": "open"},
                timeout=_TIMEOUT,
            )
        lst.raise_for_status()
        arr = lst.json()
        if arr:
            return {"number": arr[0]["number"], "head_sha": arr[0]["head"]["sha"]}
    _log.error("open_or_get_pr %s/%s %s→%s failed (%d): %s",
               owner, name, head_branch, base_branch, resp.status_code, resp.text[:300])
    resp.raise_for_status()
    raise RuntimeError("open_or_get_pr: unreachable")


async def close_pr(token: str, owner: str, name: str, number: int) -> bool:
    """Close a PR WITHOUT merging (PATCH /pulls/{n} state=closed) — the explicit
    cancel action (§12). Needs pull_requests:write. Idempotent (closing a closed PR
    is a no-op 200). Returns True on success / already-closed."""
    resp = await gh_client.rest(
        "PATCH", f"/repos/{owner}/{name}/pulls/{number}", token, json={"state": "closed"})
    if resp.status_code in (200, 422):   # 422 if already closed/invalid transition
        return True
    _log.warning("close_pr %s/%s #%s failed (%d): %s",
                 owner, name, number, resp.status_code, resp.text[:200])
    return False


async def get_pr(token: str, owner: str, name: str, number: int) -> dict:
    """GET /pulls/{n} — authoritative state (verify-by-read / recovery).

    `merged` is the completion truth (never SHA reachability). `draft` + `base_ref`
    added for draft-block detection and base-retarget detection (spec §6/§9)."""
    resp = await gh_client.rest("GET", f"/repos/{owner}/{name}/pulls/{number}", token)
    resp.raise_for_status()
    d = resp.json()
    return {
        "number": number,
        "merged": bool(d.get("merged")),
        "merge_commit_sha": d.get("merge_commit_sha"),
        "mergeable": d.get("mergeable"),          # true | false | null (GitHub still computing)
        "mergeable_state": d.get("mergeable_state"),  # clean|behind|dirty|blocked|unstable|draft|unknown
        "head_sha": d["head"]["sha"],
        "base_ref": (d.get("base") or {}).get("ref"),  # retarget detection
        "draft": bool(d.get("draft")),
        "state": d.get("state"),
        "closed_at": d.get("closed_at"),   # ISO ts when closed (null if open) — poll cancel debounce
    }


async def merge_pr(
    token: str, owner: str, name: str, number: int,
    expected_head_sha: str, merge_method: str = "merge",
) -> dict:
    """PUT /pulls/{n}/merge with the expected head `sha` + repo merge_method.

    Status codes are NOT idempotent-by-assertion (plan §5.2, corrected codes):
      200 → {status:'merged', sha}
      409 → head moved (sha mismatch) → caller re-syncs           ('head_moved')
      405 → ambiguous (already-merged OR not-mergeable/protection) → caller
            resolves via get_pr().merged                          ('blocked')
      422 → genuine validation failure → escalate, do NOT retry   ('validation')
    """
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{_API}/repos/{owner}/{name}/pulls/{number}/merge",
            headers=_headers(token),
            json={"sha": expected_head_sha, "merge_method": merge_method},
            timeout=_TIMEOUT,
        )
    if resp.status_code == 200:
        return {"status": "merged", "sha": resp.json().get("sha")}
    if resp.status_code == 409:
        return {"status": "head_moved", "detail": resp.text[:300]}
    if resp.status_code == 405:
        return {"status": "blocked", "detail": resp.text[:300]}
    if resp.status_code == 422:
        return {"status": "validation", "detail": resp.text[:300]}
    resp.raise_for_status()
    return {"status": "merged"}


async def get_repo_settings(token: str, owner: str, name: str) -> dict:
    """GET /repos/{owner}/{name} → default_branch + allowed merge methods (needs
    only metadata:read)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_API}/repos/{owner}/{name}", headers=_headers(token), timeout=_TIMEOUT,
        )
    resp.raise_for_status()
    d = resp.json()
    return {
        "default_branch": d.get("default_branch", "main"),
        "allow_merge": d.get("allow_merge_commit", True),
        "allow_squash": d.get("allow_squash_merge", True),
        "allow_rebase": d.get("allow_rebase_merge", True),
    }


def _empty_protection() -> dict:
    return {
        "protected": False, "required_checks": [], "required_reviews": 0,
        "linear_history": False, "strict": False, "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False, "required_signatures": False,
        "admin_readable": False,
    }


async def get_branch_protection(
    token: str, owner: str, name: str, branch: str,
) -> dict:
    """GET /repos/.../branches/{branch}/protection. Reading protection needs
    `Administration: read`; if the App lacks it (403) or the branch is unprotected
    (404), degrade to 'not protected' with `admin_readable=False` so callers can
    tell "no protection" from "couldn't read it" (the §3.1 refuse-to-manage gate
    must NOT treat an unreadable trunk as safe). NOTE: classic protection only —
    repos protected solely by RULESETS read empty here; use `get_effective_protection`.

      strict                     = require-up-to-date (the no-stale-merge backbone)
      dismiss_stale_reviews      = a new push dismisses prior approvals
      require_code_owner_reviews = CODEOWNERS approval required
      required_signatures        = signed commits required (the §3.2 pre-flight gate)
    """
    resp = await gh_client.rest(
        "GET", f"/repos/{owner}/{name}/branches/{quote(branch, safe='')}/protection",
        token,
    )
    if resp.status_code in (403, 404):
        out = _empty_protection()
        # 404 = unprotected (we read it, it's just not protected); 403 = can't read.
        out["admin_readable"] = (resp.status_code == 404)
        return out
    resp.raise_for_status()
    d = resp.json()
    rsc = d.get("required_status_checks") or {}
    rpr = d.get("required_pull_request_reviews") or {}
    rlh = d.get("required_linear_history") or {}
    rsig = d.get("required_signatures") or {}
    return {
        "protected": True,
        "admin_readable": True,
        "required_checks": rsc.get("contexts", []) or [],
        "strict": bool(rsc.get("strict", False)),
        "required_reviews": rpr.get("required_approving_review_count", 0) or 0,
        "dismiss_stale_reviews": bool(rpr.get("dismiss_stale_reviews", False)),
        "require_code_owner_reviews": bool(rpr.get("require_code_owner_reviews", False)),
        "linear_history": bool(rlh.get("enabled", False)),
        "required_signatures": bool(rsig.get("enabled", False)),
    }


async def get_branch_rules(token: str, owner: str, name: str, branch: str) -> list[dict]:
    """GET /repos/.../rules/branches/{branch} — the EFFECTIVE rules from repository
    RULESETS that apply to `branch` (a different surface than classic protection;
    needs only metadata:read, not Administration). Returns the raw rule list
    (each {type, parameters?}); [] if none / on error."""
    resp = await gh_client.rest(
        "GET", f"/repos/{owner}/{name}/rules/branches/{quote(branch, safe='')}", token,
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


async def get_effective_protection(
    token: str, owner: str, name: str, branch: str,
) -> dict:
    """Union of classic branch protection AND repository rulesets (spec §0.1 fix):
    a trunk protected ONLY by a ruleset reads empty through classic protection, so
    the §3.1 refuse-gate would wrongly accept/reject it. Merges both surfaces into
    the canonical protection dict (logical-OR of each guarantee). `manageable` is
    True only when require-up-to-date AND ≥1 required check are present (§3.1)."""
    base = await get_branch_protection(token, owner, name, branch)
    rules = await get_branch_rules(token, owner, name, branch)
    rule_types = {r.get("type") for r in rules}
    # Ruleset equivalents (rule `type` values) of the classic guarantees.
    if "required_status_checks" in rule_types:
        for r in rules:
            if r.get("type") == "required_status_checks":
                p = r.get("parameters") or {}
                checks = [c.get("context") for c in (p.get("required_status_checks") or []) if c.get("context")]
                base["required_checks"] = list({*base["required_checks"], *checks})
                base["strict"] = base["strict"] or bool(p.get("strict_required_status_checks_policy", False))
    if "pull_request" in rule_types:
        for r in rules:
            if r.get("type") == "pull_request":
                p = r.get("parameters") or {}
                base["required_reviews"] = max(
                    base["required_reviews"], int(p.get("required_approving_review_count", 0) or 0))
                base["dismiss_stale_reviews"] = base["dismiss_stale_reviews"] or bool(
                    p.get("dismiss_stale_reviews_on_push", False))
                base["require_code_owner_reviews"] = base["require_code_owner_reviews"] or bool(
                    p.get("require_code_owner_review", False))
    if "required_linear_history" in rule_types:
        base["linear_history"] = True
    if "required_signatures" in rule_types:
        base["required_signatures"] = True
    if "merge_queue" in rule_types:
        base["merge_queue_required"] = True
    base.setdefault("merge_queue_required", False)
    base["protected"] = base["protected"] or bool(rule_types)
    base["manageable"] = bool(base["strict"] and base["required_checks"])
    return base


async def get_combined_status(token: str, owner: str, name: str, ref: str) -> dict:
    """GET /commits/{ref}/status — the legacy combined commit-status (Travis-style
    `status` API). Top-level `state` ∈ success|pending|failure (rolls up errors);
    each per-context `state` ∈ success|pending|failure|**error** — `error` is a
    distinct fourth value and counts as failed. Returns
    {state, contexts:[{context,state}], failed:[contexts that are failure|error]}."""
    resp = await gh_client.rest(
        "GET", f"/repos/{owner}/{name}/commits/{quote(ref, safe='')}/status", token,
    )
    resp.raise_for_status()
    d = resp.json()
    contexts = [{"context": s.get("context"), "state": s.get("state")}
                for s in (d.get("statuses") or [])]
    return {
        "state": d.get("state", "pending"),
        "contexts": contexts,
        "failed": [c["context"] for c in contexts if c["state"] in ("failure", "error")],
    }


async def get_check_runs(token: str, owner: str, name: str, ref: str) -> dict:
    """GET /commits/{ref}/check-runs — the Checks API (GitHub Actions / App checks).
    Returns {failed:[names], pending:[names], all_complete:bool}. Distinguishes a
    red required check (→ fix story) from "not reported yet" (poll / escalate)."""
    resp = await gh_client.rest(
        "GET", f"/repos/{owner}/{name}/commits/{quote(ref, safe='')}/check-runs", token,
        accept="application/vnd.github+json",
    )
    resp.raise_for_status()
    runs = resp.json().get("check_runs") or []
    # Conclusions that are NOT failures (everything else completed — incl. a null/
    # unknown conclusion — is treated as failed, so a stuck "completed but no verdict"
    # run can't be silently counted as green).
    _ok = {"success", "neutral", "skipped", "stale"}
    failed, pending = [], []
    for r in runs:
        nm = r.get("name")
        if r.get("status") != "completed":
            pending.append(nm)          # queued|in_progress|waiting|requested|pending
        elif r.get("conclusion") not in _ok:
            failed.append(nm)           # failure|timed_out|cancelled|action_required|null|unknown
    return {"failed": failed, "pending": pending, "all_complete": not pending,
            "total": len(runs)}


async def get_commit_verification(token: str, owner: str, name: str, ref: str) -> dict:
    """GET /commits/{ref} → verification block. Pre-warn unsigned commits on a
    required-signing trunk (the §3.2 pre-flight gate). {verified, reason}."""
    resp = await gh_client.rest(
        "GET", f"/repos/{owner}/{name}/commits/{quote(ref, safe='')}", token,
    )
    resp.raise_for_status()
    v = (resp.json().get("commit") or {}).get("verification") or {}
    return {"verified": bool(v.get("verified")), "reason": v.get("reason")}


# ── GraphQL reads (canonical mergeability + native merge-queue state, §9) ────────

_MERGE_STATE_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      mergeable
      mergeStateStatus
      reviewDecision
      isInMergeQueue
      mergeQueueEntry { state position estimatedTimeToMerge }
    }
  }
}
"""


async def get_merge_state_status(token: str, owner: str, name: str, number: int) -> dict:
    """GraphQL `mergeStateStatus` — the CANONICAL mergeability (REST `mergeable_state`
    is undocumented, spec §3.1/§9). Also returns `reviewDecision` (dismissal/awaiting
    detection) and merge-queue entry state for queue trunks.

      mergeStateStatus ∈ CLEAN|BEHIND|DIRTY|BLOCKED|UNSTABLE|HAS_HOOKS|DRAFT|UNKNOWN
      reviewDecision   ∈ APPROVED|CHANGES_REQUESTED|REVIEW_REQUIRED|null
    """
    data = await gh_client.graphql(
        _MERGE_STATE_QUERY, {"owner": owner, "name": name, "number": number}, token,
        # mergeStateStatus is gated behind the merge-info schema preview; without
        # this Accept the field errors out (and graphql() would have no data to return).
        accept="application/vnd.github.merge-info-preview+json",
    )
    pr = ((data.get("repository") or {}).get("pullRequest")) or {}
    entry = pr.get("mergeQueueEntry") or {}
    return {
        "mergeable": pr.get("mergeable"),               # MERGEABLE|CONFLICTING|UNKNOWN
        "merge_state_status": pr.get("mergeStateStatus"),
        "review_decision": pr.get("reviewDecision"),
        "in_merge_queue": bool(pr.get("isInMergeQueue")),
        "queue_state": entry.get("state"),              # QUEUED|AWAITING_CHECKS|MERGEABLE|UNMERGEABLE|LOCKED
        "queue_position": entry.get("position"),
    }


async def merge_ref_into(
    token: str, owner: str, name: str, base_branch: str, head_ref: str,
) -> dict:
    """POST /repos/.../merges — server-side merge of `head_ref` INTO `base_branch`
    (a merge commit on base_branch). Used to restack trunk into a feature branch.
    Returns {status: 'merged'|'up_to_date'|'conflict'|'missing'}."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{_API}/repos/{owner}/{name}/merges",
            headers=_headers(token),
            json={"base": base_branch, "head": head_ref},
            timeout=_TIMEOUT,
        )
    if resp.status_code == 201:
        return {"status": "merged", "sha": resp.json().get("sha")}
    if resp.status_code == 204:
        return {"status": "up_to_date"}
    if resp.status_code == 409:
        return {"status": "conflict"}
    if resp.status_code == 404:
        return {"status": "missing"}
    resp.raise_for_status()
    return {"status": "merged"}


# ── Resource reader helpers ───────────────────────────────────────────────────

async def get_repo_overview(token: str, owner: str, name: str) -> dict:
    """Fetch live repo metadata + open PR count for the resource reader UI.

    Returns a flat dict with display-ready fields. Open PR count is capped at
    100 (first page); repos with >100 open PRs will show 100."""
    async with httpx.AsyncClient() as client:
        repo_resp = await client.get(
            f"{_API}/repos/{owner}/{name}",
            headers=_headers(token), timeout=_TIMEOUT,
        )
    repo_resp.raise_for_status()
    d = repo_resp.json()

    async with httpx.AsyncClient() as client:
        prs_resp = await client.get(
            f"{_API}/repos/{owner}/{name}/pulls",
            headers=_headers(token),
            params={"state": "open", "per_page": 100},
            timeout=_TIMEOUT,
        )
    open_pr_count = len(prs_resp.json()) if prs_resp.status_code == 200 else None

    _log.info("get_repo_overview: %s/%s — prs=%s language=%s",
              owner, name, open_pr_count, d.get("language"))
    return {
        "description": d.get("description") or "",
        "language": d.get("language") or "",
        "open_issues": d.get("open_issues_count", 0),
        "open_prs": open_pr_count,
        "pushed_at": d.get("pushed_at") or "",
        "stars": d.get("stargazers_count", 0),
        "forks": d.get("forks_count", 0),
        "html_url": d.get("html_url", ""),
        "topics": d.get("topics") or [],
        "default_branch": d.get("default_branch", "main"),
        "private": d.get("private", False),
    }


async def get_branch_pr(
    token: str, owner: str, name: str, head_owner: str, branch: str,
) -> Optional[dict]:
    """Find the open PR for head_owner:branch in repo owner/name.

    Returns a compact dict or None if no open PR exists for the branch."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_API}/repos/{owner}/{name}/pulls",
            headers=_headers(token),
            params={"state": "open", "head": f"{head_owner}:{branch}", "per_page": 1},
            timeout=_TIMEOUT,
        )
    if resp.status_code != 200:
        _log.warning("get_branch_pr: %s/%s %s → HTTP %d", owner, name, branch, resp.status_code)
        return None
    arr = resp.json()
    if not arr:
        return None
    pr = arr[0]
    _log.info("get_branch_pr: found PR #%d for %s/%s %s", pr["number"], owner, name, branch)
    return {
        "number": pr["number"],
        "title": pr.get("title", ""),
        "html_url": pr.get("html_url", ""),
        "draft": bool(pr.get("draft")),
        "base_ref": (pr.get("base") or {}).get("ref", ""),
    }
