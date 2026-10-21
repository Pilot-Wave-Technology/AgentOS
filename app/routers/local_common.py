"""Local Execution Mode — shared router + auth helpers.

One `APIRouter(prefix="/internal/local")` is defined here and shared by the two
endpoint modules so the surface stays modular without growing a single file past
the size limit: `local_execution.py` (sign-in + checkout + hydrate) and
`local_execution_run.py` (execution + diff landing). Importing either module
registers its routes on THIS router; `main.py` includes it once.
See LOCAL_EXECUTION_MODE_PLAN.md §4.2.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.auth import get_current_user
from app.internal_auth import decode_ext_session_uid, get_agent_principal

router = APIRouter(prefix="/internal/local", tags=["local-execution"])


def _field(body: dict, name: str) -> str:
    return (body.get(name) or "").strip()


# Capability the extension declares once it reads each repo's directory from the
# hydration bundle instead of hardcoding one. Gates the `repos/<owner>__<name>` layout:
# recording that layout for a client that still joins a literal "repo" would describe a
# directory the client never creates, and every consumer would then resolve to a path
# that does not exist.
CAP_PATH_CONTRACT = "path-contract"

# Capability the extension declares once it can actually work a multi-repo story
# locally end to end: clone every repo the token set carries (materializeAll), scope
# the write boundary to the checkout root with a granted-repo allow-list (boundary.js),
# and submit a per-repo diff/commit/push set as one payload (research/
# NREPO_LOCAL_EXECUTION_PLAN.md). Declaring `path-contract` alone is NOT enough — an
# extension build can resolve per-repo directories without yet being able to clone,
# diff, or push more than one. Gates: mint_repo_clone_token / mint_push_token serving
# more than the primary repo (C2/C3), submit_local_diff accepting a `repos[]` payload
# (C4), and both multi-repo × local-execution interlocks (C9) — a story is refused
# multi-repo local work until the calling client declares this.
CAP_MULTI_REPO = "multi-repo"


def client_capabilities(request: Request) -> set[str]:
    """What the calling client says it can handle.

    Read from `X-AgentOS-Client-Capabilities` (comma-separated), which the extension
    sends on every control-plane call from a single choke point so it cannot drift
    per-endpoint.

    An EMPTY set is the correct reading for two different clients, and they must be
    treated identically: one that declares nothing, and one old enough to send no header
    at all. Both can only be assumed to do what the client has always done. This is
    also why the control plane must never infer capability from a version string — a
    version tells you which build is calling, not what it supports, and mapping between
    them is a table that goes stale silently.
    """
    raw = request.headers.get("X-AgentOS-Client-Capabilities", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def supports_path_contract(request: Request) -> bool:
    """Does the caller resolve its clone directory from the bundle rather than hardcode it?"""
    return CAP_PATH_CONTRACT in client_capabilities(request)


def supports_multi_repo(request: Request) -> bool:
    """Can the caller clone, diff, commit, and push more than one repo for a single
    local checkout? See `CAP_MULTI_REPO`'s doc comment for exactly what this gates."""
    return CAP_MULTI_REPO in client_capabilities(request)


async def _resolve_local_human(request: Request) -> str:
    """uid of the human behind a local human-facing call. Accepts the CP-issued
    ext-session token (the extension's credential) or a Firebase login (browser)."""
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    uid = decode_ext_session_uid(token) if token else None
    if uid:
        return uid
    user = await get_current_user(request)
    return user["uid"]


def _enforce_local_scope(request: Request, story_id: str, goal_id: str = "") -> None:
    """A human-local token is story-scoped — it must match the target story/goal.
    These endpoints take story/goal from the request BODY, which ownership_guard
    never inspects, so enforce it here (closes the cross-story lock gap M1 opened
    by making verify_internal_bearer accept the human-local token). No-op for the
    internal-bearer / per-agent callers (no human-local principal)."""
    p = get_agent_principal(request)
    if p is None or p.token_type != "human_local":
        return
    if p.story_id and story_id and p.story_id != story_id:
        raise HTTPException(status_code=403, detail="token not scoped to this story")
    if goal_id and p.goal_id and p.goal_id != goal_id:
        raise HTTPException(status_code=403, detail="token not scoped to this goal")
