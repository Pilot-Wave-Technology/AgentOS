"""Internal endpoints for the Platform Research Council v2 registry tools.

The registry tools are custom http_action tools (executed in the gateway
runtime) that POST/GET here. Bearer-authenticated with the OpenClaw auth token,
same as /internal/go/* (go_internal.py).

  GET  /internal/platform/registry          <- registry_search  (steward + producers + reviewer)
  GET  /internal/platform/registry/item     <- registry_get
  POST /internal/platform/registry/write    <- registry_write    (producers only — by tools_allow)
  POST /internal/platform/registry/verdict  <- registry_verdict  (reviewer only — by tools_allow)

NOTE on access control: all agents authenticate with the SAME shared bearer
token, so these endpoints cannot distinguish callers by identity. The
read-only-steward / producer-write / reviewer-verdict split is enforced by what
each template is granted in `tools_allow` (the steward is never given
registry_write or registry_verdict), NOT by a check here.

The spawn/story tools (research_spawn, review_spawn, story_create) and their
endpoints land with their orchestration logic in a later step.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.services.platform_council import registry as _registry
from app.services.platform_council.registry import RegistryError
from app.services.platform_council import discoveries as _discoveries
from app.services.platform_council.discoveries import DiscoveryError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/platform")


from app.internal_auth import verify_internal_bearer as _verify_auth  # SSOT: app/internal_auth.py


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _qp(request: Request, name: str) -> str:
    """Query param, treating the empty string (an omitted {{param}}) as absent."""
    return (request.query_params.get(name) or "").strip()


def _branch() -> str:
    app_env = (getattr(get_settings(), "app_env", "") or "").lower()
    return "main" if app_env in ("production", "prod") else "dev"


# ── Reads (steward + producers + reviewer) ───────────────────────────────────

@router.get("/registry")
async def registry_search(request: Request):
    """Filtered list. All filters optional; empty = no filter. Lean rows (no
    detail/evidence) — use registry_get for the full finding."""
    _verify_auth(request)
    try:
        limit = int(_qp(request, "limit") or 100)
    except ValueError:
        limit = 100
    items = await _registry.search(
        category=_qp(request, "category"),
        verdict=_qp(request, "verdict"),
        target_role=_qp(request, "target_role"),
        impl_status=_qp(request, "impl_status"),
        fingerprint=_qp(request, "fingerprint"),
        limit=limit,
    )
    return JSONResponse({"ok": True, "count": len(items), "findings": items})


@router.get("/registry/item")
async def registry_get(request: Request):
    """Full finding by id (incl. detail, evidence, impact, verdict, adoption_path)."""
    _verify_auth(request)
    fid = _qp(request, "id")
    if not fid:
        return JSONResponse({"ok": False, "reason": "id required"}, status_code=400)
    row = await _registry.get(fid)
    if not row:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "finding": row})


# ── Writes ───────────────────────────────────────────────────────────────────

@router.post("/registry/write")
async def registry_write(request: Request):
    """Producer finish-tool: batch-upsert this run's top findings.

    Body: {source, entries, run_id?}. `entries` is a JSON array string (the
    http_action tier passes structured args as strings) of:
      {fingerprint, category, title, summary?, detail?, evidence?, impact?,
       provider_family?, target_role?}
    Caps at 5/run (per role for model_research). Upserts on fingerprint.
    """
    _verify_auth(request)
    body = await _body(request)
    source = str(body.get("source") or "").strip()
    entries = body.get("entries")
    if isinstance(entries, str):
        try:
            entries = json.loads(entries) if entries.strip() else []
        except json.JSONDecodeError as exc:
            return JSONResponse(
                {"ok": False, "reason": f"entries must be a valid JSON array: {exc}"},
                status_code=400,
            )
    run_id = (str(body.get("run_id")).strip() or None) if body.get("run_id") else None
    try:
        result = await _registry.upsert_findings(
            source=source, entries=entries or [], run_id=run_id,
            source_agent_id=(str(body.get("agent_id")).strip() or None) if body.get("agent_id") else None,
            branch=_branch(),
        )
    except RegistryError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    # Advance the owning sweep (mark producer done → fan out reviewers when all in).
    try:
        from app.services.platform_council import runs as _runs
        await _runs.on_producer_submit(source, run_id)
    except Exception:
        log.exception("platform: on_producer_submit hook failed (source=%s, run=%s)", source, run_id)
    return JSONResponse(result)


@router.post("/registry/verdict")
async def registry_verdict(request: Request):
    """Integration reviewer: write a verdict into a finding's own row."""
    _verify_auth(request)
    body = await _body(request)
    fid = str(body.get("finding_id") or body.get("id") or "").strip()
    verdict = str(body.get("verdict") or "").strip()
    if not fid or not verdict:
        return JSONResponse({"ok": False, "reason": "finding_id and verdict required"},
                            status_code=400)
    # supported_today arrives as a string ("true"/"false") through http_action.
    st_raw = body.get("supported_today")
    supported_today = None
    if isinstance(st_raw, bool):
        supported_today = st_raw
    elif isinstance(st_raw, str) and st_raw.strip():
        supported_today = st_raw.strip().lower() in ("true", "yes", "1")
    # op_params arrives as a JSON string via http_action (structured args are strings).
    op_params = body.get("op_params")
    if isinstance(op_params, str):
        try:
            op_params = json.loads(op_params) if op_params.strip() else {}
        except json.JSONDecodeError as exc:
            return JSONResponse({"ok": False, "reason": f"op_params must be valid JSON: {exc}"},
                                status_code=400)
    try:
        result = await _registry.set_verdict(
            finding_id=fid, verdict=verdict,
            adoption_path=str(body.get("adoption_path") or ""),
            supported_today=supported_today,
            reviewer_notes=str(body.get("reviewer_notes") or ""),
            op_type=str(body.get("op_type") or "").strip() or None,
            op_params=op_params if isinstance(op_params, dict) else None,
        )
    except RegistryError as exc:
        code = 404 if "not found" in str(exc) else 400
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=code)
    # Advance the owning sweep (finish it when no findings remain pending).
    try:
        from app.services.platform_council import runs as _runs
        await _runs.on_verdict(fid)
    except Exception:
        log.exception("platform: on_verdict hook failed (finding=%s)", fid)
    return JSONResponse(result)


# ── Steward orchestration (on-demand) ────────────────────────────────────────

@router.post("/research/spawn")
async def research_spawn(request: Request):
    """Steward / user on-demand: request research on a specific topic + dimension.
    ENQUEUES a high-priority discovery (front of the research queue); the
    deterministic drainer runs it one-at-a-time end-to-end and the reviewer verdicts
    it. It does NOT spawn a sweep directly — that bypassed the queue + the discovery
    status loop and let two things drive research at once."""
    _verify_auth(request)
    body = await _body(request)
    kind = str(body.get("kind") or "").strip().lower()
    topic = str(body.get("topic") or body.get("question") or "").strip()
    if kind not in ("model", "tool", "skill"):
        return JSONResponse({"ok": False, "reason": "kind must be 'model', 'tool', or 'skill'"},
                            status_code=400)
    try:
        res = await _discoveries.enqueue_topic(kind=kind, topic=topic)
    except _discoveries.DiscoveryError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return JSONResponse(res, status_code=200)


# ── Discovery feed (Independent Platform Research writes; steward reads) ─────────
@router.post("/discovery/write")
async def discovery_write(request: Request):
    """Independent Platform Research finish-tool: batch-insert discoveries (≤15).
    Body: {entries} — a JSON array (string or object) of
      {fingerprint, category(model|tool|skill), title, summary?, url?, released_at?, source?}.
    Dedup on fingerprint (re-discovery refreshes details, preserves processing state)."""
    _verify_auth(request)
    body = await _body(request)
    entries = body.get("entries")
    if isinstance(entries, str):
        try:
            entries = json.loads(entries) if entries.strip() else []
        except json.JSONDecodeError as exc:
            return JSONResponse({"ok": False, "reason": f"entries must be valid JSON: {exc}"},
                                status_code=400)
    try:
        result = await _discoveries.upsert_discoveries(
            entries=entries or [],
            discovered_by=(str(body.get("agent_id")).strip() or None) if body.get("agent_id") else None,
        )
    except DiscoveryError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return JSONResponse(result)


@router.get("/discovery")
async def discovery_search(request: Request):
    """List discoveries (lean). Optional filters: category, status."""
    _verify_auth(request)
    q = request.query_params
    items = await _discoveries.search(
        category=(q.get("category") or "").strip() or None,
        status=(q.get("status") or "").strip() or None,
        limit=int(q.get("limit") or 50),
    )
    return JSONResponse({"ok": True, "count": len(items), "discoveries": items})


@router.get("/discovery/item")
async def discovery_get(request: Request):
    _verify_auth(request)
    did = (request.query_params.get("id") or "").strip()
    if not did:
        return JSONResponse({"ok": False, "reason": "id required"}, status_code=400)
    row = await _discoveries.get(did)
    if not row:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "discovery": row})


@router.post("/review/spawn")
async def review_spawn(request: Request):
    """Steward on-demand: kick off integration review. With `sweep_id`, reviews
    that sweep's pending findings (the interactive flow after research). With
    `finding_id`, reviews one finding. With neither, reviews ALL pending."""
    _verify_auth(request)
    body = await _body(request)
    from app.services.platform_council import runs as _runs
    sweep_id = str(body.get("sweep_id") or "").strip()
    finding_id = str(body.get("finding_id") or "").strip()

    if sweep_id:
        await _runs.begin_review_phase(sweep_id)
        return JSONResponse({"ok": True, "scope": "sweep", "sweep_id": sweep_id})
    if finding_id:
        finding = await _registry.get(finding_id)
        if not finding:
            return JSONResponse({"ok": False, "reason": "finding not found"}, status_code=404)
        from app.services.platform_council import worker as _worker
        label = f"{(finding.get('run_id') or finding_id)[:8]}-adhoc"
        res = await _worker.spawn_reviewer([finding], label)
        return JSONResponse(res, status_code=200 if res.get("ok") else 409)
    # No scope → review everything pending now (manual backstop trigger).
    n = await _runs.review_orphan_pending(min_age_seconds=0)
    return JSONResponse({"ok": True, "scope": "all_pending", "reviewer_groups_spawned": n})


@router.get("/research/status")
async def research_status_tool(request: Request):
    """Steward read-only: current queue/sweep status. Lets the steward see that a
    research/review is already in flight (button, scheduler, or another request) so
    it can tell the user new requests are blocked instead of trying to dispatch."""
    _verify_auth(request)
    from app.services.platform_council import runs as _runs
    status = await _runs.queue_status()
    return JSONResponse(status)


_ADOPTABLE = {"adopt_now"}   # only in-scope (no-code, op-mapped) findings become stories


@router.post("/story/create")
async def story_create(request: Request):
    """Steward: create an implementation story on the __platform__ goal. For a
    registry finding, pass `finding_id` (allowed only if its verdict is adoptable);
    for a free-form request, pass `title` + `description`. Reuses the GO's story
    creation so the story enters the normal board → planning → execution pipeline."""
    _verify_auth(request)
    body = await _body(request)
    from app.services.platform_council import PLATFORM_GOAL_ID

    title = str(body.get("title") or "").strip()
    description = str(body.get("description") or "").strip()
    finding_id = str(body.get("finding_id") or "").strip()

    finding = None
    if finding_id:
        finding = await _registry.get(finding_id)
        if not finding:
            return JSONResponse({"ok": False, "reason": "finding not found"}, status_code=404)
        verdict = finding.get("verdict")
        if verdict not in _ADOPTABLE:
            return JSONResponse(
                {"ok": False, "reason": f"finding is not adoptable (verdict={verdict!r}); "
                 f"only {sorted(_ADOPTABLE)} findings can become stories"}, status_code=400)
        # Fall back to the finding's own content when the steward didn't override.
        title = title or f"Adopt: {finding.get('title')}"
        if not description:
            description = (
                f"Implement registry finding `{finding_id}` "
                f"({finding.get('category')} / {finding.get('target_role')}).\n\n"
                f"{finding.get('detail') or finding.get('summary') or ''}\n\n"
                f"Adoption path (per integration review): {finding.get('adoption_path') or 'n/a'}"
            )

    # Embed the executable op spec (op_type + op_params) so the platform SHCA can
    # act on the story deterministically. registry.get returns op_params as JSON text.
    if finding and finding.get("op_type"):
        op_params = finding.get("op_params")
        if isinstance(op_params, str):
            try:
                op_params = json.loads(op_params) if op_params.strip() else {}
            except Exception:
                op_params = {}
        description = (description or "").rstrip() + (
            f"\n\n---\nOperation: {finding['op_type']}\n"
            f"Parameters: {json.dumps(op_params or {})}\n"
        )

    if not title:
        return JSONResponse({"ok": False, "reason": "title (or an adoptable finding_id) required"},
                            status_code=400)

    from app.services.go_response import _action_create_story
    params = {"title": title, "description": description,
              "priority": str(body.get("priority") or "medium"),
              "acceptance_criteria": str(body.get("acceptance_criteria") or "")}
    res = await _action_create_story(PLATFORM_GOAL_ID, params)
    if res.get("error"):
        return JSONResponse({"ok": False, "reason": res["error"]}, status_code=400)

    story_id = res.get("story_id")
    if story_id:
        # Platform stories are NON-code: worked directly by the platform-shca via
        # the apply_* tools — never operator/repo mode. Mark them 'ia_dispatch' so
        # the board's operator repo-accept gate doesn't hard-disable Accept (no
        # repo is ever linked here). Execution is human-in-the-loop through the
        # assistant, not IA-task dispatch.
        from app.database import get_pool
        try:
            await get_pool().execute(
                "UPDATE stories SET execution_mode='ia_dispatch' WHERE id=$1 AND goal_id=$2",
                story_id, PLATFORM_GOAL_ID)
        except Exception:
            log.exception("platform: could not set execution_mode for story %s", story_id)
    if finding_id and story_id:
        try:
            await _registry.mark_story_created(finding_id, story_id)
        except Exception:
            log.exception("platform: mark_story_created failed (finding=%s, story=%s)",
                          finding_id, story_id)
    return JSONResponse({"ok": True, "story_id": story_id, "title": title})


# ── Execution: the 4 no-code operations (called by the platform SHCA) ─────────

def _json_arg(body: dict, key: str):
    """Parse a structured arg that http_action passes as a JSON string."""
    v = body.get(key)
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        return json.loads(v)
    return v


@router.post("/apply/model_change")
async def apply_model_change(request: Request):
    _verify_auth(request)
    body = await _body(request)
    from app.services.platform_council import apply as _apply
    res = await _apply.apply_model_change(
        template_id=str(body.get("template_id") or "").strip(),
        model=str(body.get("model") or "").strip())
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/apply/add_skill")
async def apply_add_skill(request: Request):
    _verify_auth(request)
    body = await _body(request)
    try:
        applicable_to = _json_arg(body, "applicable_to")
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "reason": f"applicable_to must be a JSON array: {exc}"}, status_code=400)
    if isinstance(applicable_to, str):
        applicable_to = [applicable_to]
    from app.services.platform_council import apply as _apply
    res = await _apply.apply_add_skill(
        name=str(body.get("name") or "").strip(), title=str(body.get("title") or "").strip(),
        description=str(body.get("description") or "").strip(),
        content=str(body.get("content") or ""), applicable_to=applicable_to or [])
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/apply/create_credential")
async def apply_create_credential(request: Request):
    _verify_auth(request)
    body = await _body(request)
    from app.services.platform_council import apply as _apply
    res = await _apply.apply_create_credential(
        name=str(body.get("name") or "").strip(),
        credential_type=str(body.get("type") or body.get("credential_type") or "").strip(),
        header_name=(str(body.get("header_name")).strip() or None) if body.get("header_name") else None)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/apply/add_tool")
async def apply_add_tool(request: Request):
    _verify_auth(request)
    body = await _body(request)
    try:
        spec = _json_arg(body, "spec") or {}
        credential = _json_arg(body, "credential")
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "reason": f"spec/credential must be valid JSON: {exc}"}, status_code=400)
    from app.services.platform_council import apply as _apply
    res = await _apply.apply_add_tool(
        tier=str(body.get("tier") or "").strip(), name=str(body.get("name") or "").strip(),
        label=str(body.get("label") or "").strip(), description=str(body.get("description") or "").strip(),
        spec=spec if isinstance(spec, dict) else {},
        credential=credential if isinstance(credential, dict) else None)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)
