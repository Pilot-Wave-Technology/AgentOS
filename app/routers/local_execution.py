"""Local Execution Mode — sign-in, checkout lifecycle, hydration (M0–M2).

  POST /internal/local/auth-exchange|auth-refresh|auth-revoke — extension sign-in (§6)
  POST /internal/local/story-token   — mint the story-scoped human-local token
  POST /internal/local/checkout|heartbeat|complete — the story lock lifecycle
  POST /internal/local/hydrate|github-token        — read-only bundle + clone token
  GET  /internal/local/stories       — the caller's assignable stories

Execution + diff-landing endpoints live in `local_execution_run.py` (same router).
See LOCAL_EXECUTION_MODE_PLAN.md §4.2.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, Response

import logging

from app.internal_auth import verify_internal_bearer
from app.memory_v2.local_snapshot import read_local_memory, write_local_memory
from app.routers.local_common import (
    router, _enforce_local_scope, _field, _resolve_local_human,
    supports_multi_repo, supports_path_contract,
)
from app.services.ext_auth import exchange_code, refresh_session, revoke_handle
from app.services.local_auth import mint_story_token
from app.services.local_checkout import claim_checkout, heartbeat_checkout, release_checkout
from app.services.local_hydrate import (
    build_hydration_bundle, list_local_stories, mint_repo_clone_token,
)

_log = logging.getLogger(__name__)

# reason → HTTP status for the story-token endpoint
_TOKEN_ERR_STATUS = {"story_not_found": 404, "not_assignee": 403, "no_assistant_instance": 409}


@router.post("/auth-exchange")
async def auth_exchange(request: Request) -> JSONResponse:
    """Exchange a one-time sign-in code for a session token + refresh handle.
    Unauthenticated — the code IS the (single-use, short-TTL) credential (§6)."""
    body = await request.json()
    code = _field(body, "code")
    if not code:
        return JSONResponse({"ok": False, "error": "code required"}, status_code=400)
    result = await exchange_code(code)
    return JSONResponse(result, status_code=200 if result.get("ok") else 401)


@router.post("/auth-refresh")
async def auth_refresh(request: Request) -> JSONResponse:
    """Exchange a refresh handle for a fresh session token. The handle IS the credential."""
    body = await request.json()
    handle = _field(body, "refresh_handle")
    if not handle:
        return JSONResponse({"ok": False, "error": "refresh_handle required"}, status_code=400)
    result = await refresh_session(handle)
    return JSONResponse(result, status_code=200 if result.get("ok") else 401)


@router.post("/auth-revoke")
async def auth_revoke(request: Request) -> JSONResponse:
    """Sign-out: revoke a refresh handle server-side so it can mint no further
    tokens. Unauthenticated — presenting the handle IS the authority to kill it.
    Idempotent (already-revoked/unknown handles still return ok)."""
    body = await request.json()
    handle = _field(body, "refresh_handle")
    if handle:
        await revoke_handle(handle)
    return JSONResponse({"ok": True})


@router.post("/story-token")
async def story_token(request: Request) -> JSONResponse:
    """Mint the story-scoped human-local token. HUMAN-authenticated (ext-session
    token or Firebase) — the caller must be the story's assignee (§6/§14.1)."""
    uid = await _resolve_local_human(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    result = await mint_story_token(uid, story_id)
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    return JSONResponse(result, status_code=_TOKEN_ERR_STATUS.get(result.get("reason"), 409))


@router.post("/checkout")
async def checkout(request: Request) -> JSONResponse:
    verify_internal_bearer(request)
    body = await request.json()
    story_id, goal_id, uid = _field(body, "story_id"), _field(body, "goal_id"), _field(body, "uid")
    if not (story_id and goal_id and uid):
        return JSONResponse({"ok": False, "error": "story_id, goal_id, uid required"}, status_code=400)
    _enforce_local_scope(request, story_id, goal_id)
    result = await claim_checkout(
        story_id, goal_id, uid,
        path_contract=supports_path_contract(request), multi_repo=supports_multi_repo(request),
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/heartbeat")
async def heartbeat(request: Request) -> JSONResponse:
    verify_internal_bearer(request)
    body = await request.json()
    story_id, session_id = _field(body, "story_id"), _field(body, "checkout_session_id")
    if not (story_id and session_id):
        return JSONResponse({"ok": False, "error": "story_id, checkout_session_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    result = await heartbeat_checkout(story_id, session_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/complete")
async def complete(request: Request) -> JSONResponse:
    verify_internal_bearer(request)
    body = await request.json()
    story_id, session_id = _field(body, "story_id"), _field(body, "checkout_session_id")
    if not (story_id and session_id):
        return JSONResponse({"ok": False, "error": "story_id, checkout_session_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    result = await release_checkout(story_id, session_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/hydrate")
async def hydrate(request: Request) -> JSONResponse:
    """Read-only hydration bundle: story meta + persona + plan + feature branch."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    result = await build_hydration_bundle(
        story_id, path_contract=supports_path_contract(request),
        multi_repo=supports_multi_repo(request),
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


@router.post("/github-token")
async def github_token(request: Request) -> JSONResponse:
    """Repo-scoped, read-only, short-lived GitHub token for the local clone (§8.2)."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    result = await mint_repo_clone_token(story_id, multi_repo=supports_multi_repo(request))
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/profile")
async def profile(request: Request) -> JSONResponse:
    """The executor profile this story would run under, as data.

    WHY THE CONTROL PLANE ANSWERS THIS. The profile tree ships with the
    control plane. The extension cannot see it, and a copy inside an
    installed client is the mistake `local_common.client_capabilities`
    argues against at length -- an installed extension is a client the
    control plane does not control and cannot upgrade.

    A SEPARATE TREE FROM THE PODS'. This resolves against `profiles-local/`
    (`kernel.profiles.local_registry`), not `profiles/`. A pod profile
    describes a sandbox -- legs, a step budget, `story_commit`, `bash_wait`,
    `propose_completion` -- none of which exists when Claude Code is the
    executor in somebody's editor. What the two lanes share is the KEY: one
    derivation says which profile a story is, so only what that profile SAYS
    differs between them.

    IT CAN REFUSE, and a refusal is the useful answer. A profile whose
    tools have no local equivalent, or whose workspace driver the local
    lane cannot honour, describes a story that would not be the same run
    in an editor. Saying so beats approximating it.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)

    from app.database import get_pool
    from app.services.local_tool_surface import (ProfileNotRunnableLocally,
                                                 bridge_tools_for, refuse_driver)
    from app.services.story_profile import resolve_local_for_story
    from kernel.profiles.record import UnknownProfileError

    try:
        prof = await resolve_local_for_story(get_pool(), story_id)
    except UnknownProfileError as unknown:
        return JSONResponse({"ok": False, "reason": "unknown_profile",
                             "error": str(unknown)}, status_code=200)

    driver_refusal = refuse_driver(prof.workspace_driver)
    if driver_refusal:
        return JSONResponse({"ok": False, "reason": "not_runnable_locally",
                             "error": driver_refusal}, status_code=200)
    try:
        bridge = bridge_tools_for(prof.tools)
    except ProfileNotRunnableLocally as refusal:
        return JSONResponse({"ok": False, "reason": "not_runnable_locally",
                             "error": str(refusal)}, status_code=200)

    return JSONResponse({
        "ok": True,
        "kind": prof.kind,
        "domain": prof.domain,
        "phase": prof.phase,
        "workspace_driver": prof.workspace_driver,
        # The ROLE the agent is playing -- `profiles-local/<p>/prompt.md`,
        # written for a run in an editor. This becomes the body of
        # `.claude/agents/assistant.md`; `build_local_bundle` renders it.
        "prompt_preamble": prof.prompt_preamble,
        "prompt_files": list(prof.prompt_files),
        "tools": list(prof.tools),
        # ONLY the control-plane tools. The agent's own Read/Bash/Grep are
        # its business: it is running in the person's editor on the person's
        # machine, and a narrow `permissions.allow` would be theatre its own
        # Bash walks through. Mapped here rather than in the client because a
        # client cannot be told about a new bridge tool without reinstalling.
        "bridge_tools": bridge,
        "model": prof.model,
        "requires_plan": prof.requires_plan,
        "allows_completion": prof.allows_completion,
    }, status_code=200)


@router.post("/memory-hydrate")
async def memory_hydrate(request: Request) -> JSONResponse:
    """Seed data: the assistant's latest memory files to plant in the checkout so
    memory carries into the local story (§14.3 / M6)."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    return JSONResponse(await read_local_memory(story_id), status_code=200)


@router.post("/memory-writeback")
async def memory_writeback(request: Request) -> JSONResponse:
    """Persist the memory a local session produced.

    `consolidated` says whether the session actually performed its
    story-complete consolidation. The control plane suppresses the gateway
    SHCA's flush for a checked-out local story on the grounds that the
    extension authors its own -- and the extension's is best-effort, so
    without this the two could both decline and the story would finish with
    no assistant checkpoint anywhere. Absent means false, which is the safe
    reading: the fallback runs.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    return JSONResponse(
        await write_local_memory(story_id, body.get("files") or {},
                                 consolidated=bool(body.get("consolidated"))),
        status_code=200)




@router.get("/stories")
async def my_stories(request: Request) -> JSONResponse:
    """List the caller's assigned, non-terminal stories (ext-session or Firebase).

    RESTORED 2026-10-01. This decorator was deleted by accident in dc5d5d94 while the
    `/memory-writeback` body above it was being replaced; `list_local_stories` kept
    being imported and kept working, so nothing in the control plane noticed. The
    extension's story list is its ENTRY POINT -- with this route gone it opened on
    `/internal/local/stories -> 404 {}` and no story could be imported at all.
    `test_the_local_router_keeps_its_routes` now pins the whole set.
    """
    uid = await _resolve_local_human(request)
    return JSONResponse({"ok": True, "stories": await list_local_stories(uid)})


@router.get("/resource-object")
async def local_resource_object(request: Request):
    """The bytes of one document this story was granted.

    THE SAME DOOR THE POD USES, WITH THE LOCAL LANE'S KEY. The pod
    fetches its `docs/` through `/internal/resource-object`, which is
    authorised by `verify_internal_bearer` -- the kernel's own token,
    which the extension does not and should not hold. Rather than
    widen that endpoint's auth, this one applies the local lane's
    scope check and then performs the identical grant-scoped read.

    THE GRANT IS THE AUTHORISATION, not the URI: a story may read what
    it was granted and nothing else. Taking a bare `gs://` would make
    this an open proxy onto the resource bucket for anybody holding
    any story token.
    """
    verify_internal_bearer(request)
    story_id = (request.query_params.get("story_id") or "").strip()
    resource_id = (request.query_params.get("resource_id") or "").strip()
    if not story_id or not resource_id:
        return JSONResponse({"ok": False,
                             "error": "story_id and resource_id are required"},
                            status_code=400)
    _enforce_local_scope(request, story_id)

    from app.database import get_pool
    from app.routers.resource_object import MAX_BYTES
    row = await get_pool().fetchrow(
        """
        SELECT gr.name,
               COALESCE(NULLIF(gr.clone_url, ''), NULLIF(gr.uri, ''),
                        NULLIF(gr.gcs_uri, '')) AS uri
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1 AND sr.resource_id = $2
        """, story_id, resource_id)
    if row is None:
        return JSONResponse(
            {"ok": False,
             "error": f"story {story_id} was not granted that resource"},
            status_code=404)
    if not row["uri"]:
        return JSONResponse(
            {"ok": False, "error": f"{row['name']!r} has no stored object"},
            status_code=404)

    try:
        from app.storage import download_resource_uri
        blob = download_resource_uri(row["uri"])
    except Exception as exc:                                # noqa: BLE001
        # Named, for the reason the pod's copy of this gives: "could
        # not fetch" with no cause sent one person through IAM for an
        # hour.
        _log.exception("local resource-object: %s (%s)",
                       resource_id, row["uri"])
        return JSONResponse(
            {"ok": False, "error": f"fetching {row['name']!r}: {exc}"},
            status_code=502)

    if len(blob) > MAX_BYTES:
        return JSONResponse(
            {"ok": False,
             "error": f"{row['name']!r} is {len(blob):,} bytes; the "
                      f"workspace copies documents up to {MAX_BYTES:,}"},
            status_code=413)
    return Response(content=blob, media_type="application/octet-stream")


@router.post("/cancel")
async def local_cancel_story(request: Request) -> JSONResponse:
    """Stop a story from the editor, and let its checkout go.

    NOT `/internal/goals/{pid}/stories/{sid}/cancel`, which exists and
    would have been one line to call. That route takes an internal
    bearer and checks nothing else, so a story token minted for one
    story could cancel another -- fine for the GO's own tool, which
    holds the platform token, and not fine for a token that lives on
    somebody's laptop. `_enforce_local_scope` is the difference.

    AND THE ASSIGNEE, for the same reason `claim_checkout` checks it:
    the story token is minted only for the assignee, so this is that
    rule applied at the second door that acts on the story.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"},
                            status_code=400)
    _enforce_local_scope(request, story_id)

    from app.database import get_pool
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT goal_id, assigned_to FROM stories WHERE id = $1", story_id)
    if not row:
        return JSONResponse({"ok": False, "error": "story not found"},
                            status_code=404)

    uid = await _resolve_local_human(request)
    if (row["assigned_to"] or "").strip() != uid:
        return JSONResponse(
            {"ok": False,
             "error": "only the person this story is assigned to can "
                      "cancel it"},
            status_code=403)

    # THE ACTOR IS NAMED. The tool route passes `actor_uid=""` because
    # the GO has no human behind it; this one does, and the ledger is
    # where "who stopped this" is answered later.
    from app.routers.stories import _cancel_story
    reason = (body.get("reason") or "").strip() or "Cancelled from VS Code"
    return await _cancel_story(row["goal_id"], story_id, reason,
                               actor_uid=uid)
