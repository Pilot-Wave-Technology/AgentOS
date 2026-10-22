"""Model registry HTTP surface.

Phase A2 (story `afbe242c`) shipped this router with a single
handler — `GET /internal/model_catalog` — serving the authoritative
model + synthetic-provider catalog to polling OpenClaw gateways.
Phase A4 (story `2eec79d9`) grew the same router with an admin
UI + CRUD surface under `/settings/model-registry`. Cycle-01 of
story `414f92d6` rewired that admin surface to the new v2 backend
(`model_providers` + extended `models`) while leaving the wire
endpoint byte-compatible by non-modification — the two paths are
provably independent by diff.

Both surfaces live here because they are the *same* registry viewed
through two different auth tiers: the gateway pull is
machine-to-machine (Bearer-authed), the admin UI is browser-based
(Firebase JWT + `require_admin` / `get_current_user`).

Gateway pull contract (unchanged from Phase A2):
    GET /internal/model_catalog
      auth   Bearer <INTERNAL_API_KEY> — same trust boundary as every
             other `/internal/*` route (see `app/internal_auth.py`).
      body   JSON:
               {
                 "models":    { "<id>": {id, name, contextWindow}, ... },
                 "providers": { "<name>": <config JSONB verbatim>,  ... },
                 "providerCredentials": {
                   "<real-upstream-id>": {secretProject, secretId, secretVersion}, ...
                 },
                 "generated_at": "<ISO-8601 UTC timestamp>"
               }

             `providerCredentials` is keyed by real upstream provider id
             (e.g. "anthropic"), a different granularity from `providers`
             (keyed by variant/alias id) — see
             `emitter.build_provider_credentials_payload`'s docstring.
             References only — never a resolved secret value. See
             research/model_registry_secret_management_design.md.

The `models` and `providers` values are built by the same
`emitter.build_*_payload` functions Phase A1 used, so the wire-key
whitelist (only `id / name / contextWindow` per model; provider
`config` forwarded verbatim) is preserved unchanged.

Failure modes:
  * Missing / wrong Bearer → 401 (via `verify_internal_bearer`).
  * DB fetch failure       → 500 { "error": "<str>" }. Gateways are
                             expected to keep their last-known-good
                             catalog on 500 rather than blank it.

Admin surface (cycle-01 v2):
    GET  /settings/model-registry                                                    — any logged-in user
    POST /settings/model-registry/providers                                          — require_admin (create)
    POST /settings/model-registry/providers/{id}/update                              — require_admin
    POST /settings/model-registry/providers/{id}/disable                             — require_admin
    POST /settings/model-registry/providers/{id}/enable                              — require_admin
    POST /settings/model-registry/providers/{id}/refresh-credentials                 — require_admin
    POST /settings/model-registry/providers/{id}/revoke-credentials                  — require_admin
    POST /settings/model-registry/models                                             — require_admin (create)
    POST /settings/model-registry/models/{model_id:path}/update                      — require_admin
    POST /settings/model-registry/models/{model_id:path}/disable                     — require_admin
    POST /settings/model-registry/models/{model_id:path}/enable                      — require_admin
    POST /settings/model-registry/models/{model_id:path}/context-windows             — require_admin (add)
    POST /settings/model-registry/models/{model_id:path}/context-windows/{tokens:int}/remove — require_admin

Every mutating route delegates to the `*_v2` surface in
`app/services/model_registry/management.py` so validation, the
injection-safe UPDATE builder, the application-layer FK check on
`provider_id`, the shorthand-required + duplicate-window guards, and
the provider-in-use guard on disable all stay in one place. The
router only translates the HTML form into a payload/patch dict,
converts empty-string optional fields to "absent" (or drops them
from the patch), and catches `ManagementError` to re-render the page
with an inline error banner. On success handlers return a 303
redirect to the list page so a browser refresh does not re-submit
the form. Both `list_providers_v2` + `list_base_models_v2` reads
run on every render — the tables are small, indexed by `status`,
and paginating the admin view would add complexity we do not need
at this scale.

See `documentation/MODEL_REGISTRY_CONTRACT.md` for the full contract
description.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, require_admin
from app.database import get_pool
from app.internal_auth import verify_internal_bearer
from app.ledger.journal import append_event
from app.ledger.schema import MODEL_PROVIDER_SECRET_REF_CHANGED
from app.roles import SystemRole
from app.services.model_registry.availability import effective_provider_status
from app.services.model_registry.emitter import (
    build_models_payload,
    build_provider_credentials_payload,
    build_providers_payload,
)
from app.services.model_registry.gateway_sync import sync_gateway_model_catalog
from app.services.model_registry.gateway_variant import gateway_model_id
from app.services.model_registry.management import (
    ManagementError,
    add_context_window_v2,
    create_base_model_v2,
    create_provider_v2,
    list_base_models_v2,
    list_providers_v2,
    list_tags_v2,
    remove_context_window_v2,
    revoke_provider_credential_v2,
    set_base_model_status_v2,
    set_provider_status_v2,
    update_base_model_v2,
    update_provider_v2,
)
from app.services.model_registry.queries import (
    fetch_active_base_models,
    fetch_native_provider_configs,
)
from app.services.model_registry.synthesizer import synthesize_all_variants
from app.services.model_pricing import (
    PricingValidationError,
    create_price_version,
    list_pricing_grouped,
)
from app.services.misc_models import (
    MiscModelValidationError,
    list_misc_models,
    list_utility_model_options,
    set_misc_model,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_templates = Jinja2Templates(directory="app/templates")


@router.get("/internal/model_catalog")
async def internal_model_catalog(request: Request):
    """Serve the authoritative model catalog to a polling gateway.

    Fetches active `models` rows (family B) once, runs them through the
    emitter — which both enumerates each row's own entry AND synthesizes
    every context-window variant on the fly — and returns the envelope
    described in the module docstring. No caching layer — Postgres
    handles it cheaply (a small table indexed by `status`), and any
    caching / backoff belongs on the gateway side.
    """
    verify_internal_bearer(request)

    try:
        pool = get_pool()
        base_rows = await fetch_active_base_models(pool)
        native_providers = await fetch_native_provider_configs(pool)
    except Exception as exc:
        logger.error("model_catalog: DB fetch failed — %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    payload = {
        "models": build_models_payload(base_rows),
        "providers": build_providers_payload(base_rows, native_providers),
        "providerCredentials": build_provider_credentials_payload(native_providers),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "model_catalog: served models=%d providers=%d credentials=%d",
        len(payload["models"]), len(payload["providers"]),
        len(payload["providerCredentials"]),
    )
    return JSONResponse(payload)


@router.get("/internal/misc_model_settings")
async def internal_misc_model_settings(request: Request):
    """Serve the utility-model overrides to the OpenClaw gateway's
    model-registry plugin, same trust boundary and polling shape as
    GET /internal/model_catalog above, but for the separate
    misc_model_settings config (see app/services/misc_models.py).

    Only purposes with an explicit override are included — a purpose
    absent from `overrides` means "use the caller's own hardcoded
    default", so the gateway-side consumer needs no knowledge of what
    those defaults are.
    """
    verify_internal_bearer(request)

    try:
        pool = get_pool()
        rows = await list_misc_models(pool)
    except Exception as exc:
        logger.error("misc_model_settings: DB fetch failed — %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    # A purpose marked `serve_default` is ALWAYS served, default included:
    # for it this registry is the source of truth, not the gateway's own
    # provisioned value (see `gateway_compaction` in misc_models.py).
    overrides = {row["purpose_key"]: row["model_id"] for row in rows
                 if row["is_override"] or row["serve_default"]}
    # The gateway looks the compaction summarizer up in its own registry,
    # which holds context-window variants, not base ids: serve the largest
    # variant (gateway_variant.py says why).
    if "gateway_compaction" in overrides:
        try:
            overrides["gateway_compaction"] = await gateway_model_id(
                pool, overrides["gateway_compaction"])
        except Exception as exc:
            logger.error("misc_model_settings: summarizer variant lookup failed — %s", exc)
    payload = {
        "overrides": overrides,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("misc_model_settings: served %d override(s)", len(overrides))
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Admin UI (cycle-01 v2). Every mutating handler goes through
# `require_admin`; the read handler is open to any authenticated user.
# ---------------------------------------------------------------------------


def _group_hierarchy_v2(
    providers: list[dict],
    models: list[dict],
) -> list[dict]:
    """Group `list_base_models_v2` rows under each `list_providers_v2` row.

    Produces the view-model the template consumes:
        [{ id, label, base_url, api_key_source, api_type,
           runtime_status, status,
           secret_project, secret_id, secret_version,
           credential_status, credential_error, credential_checked_at,
           effective_status,
           models: [{ id, label, tier, context_window,
                      provider_shorthand, available_context_windows,
                      tags, status,
                      synthesized_variants: [<from synthesize_all_variants>],
                      gateway_reachable, gateway_checked_at
                    }, ...]
         }, ...]

    Every key here is explicit (never `dict(row)`/`**row`) — a column
    missing from this list resolves to Jinja's `Undefined` in the
    template rather than `None`, and `Undefined is not none` is `True`
    (they're different objects), so an `{% if x is not none %}` guard
    silently fires on a field that was simply never added here. Confirmed
    live: this exact gap made `gateway_reachable`/the provider secret
    fields render as an always-red mark / never-shown health section
    until added to this hardcoded list — add any new column HERE, not
    just to the DB/`list_providers_v2`/`list_base_models_v2` reads.

    Ordered by provider id, then model id. `synthesized_variants` is
    pre-expanded here so the template does not have to import the
    synthesizer helper — each variant carries the base's `tags` too
    (same capabilities, different size; see `synthesizer.synthesize_variant`).
    """
    by_pid: dict[str, list[dict]] = {}
    for m in models:
        row = {
            "id": m["id"],
            "label": m["label"],
            "tier": m["tier"],
            "context_window": m["context_window"],
            "provider_shorthand": m.get("provider_shorthand"),
            "available_context_windows": list(
                m.get("available_context_windows") or []
            ),
            "tags": list(m.get("tags") or []),
            "status": m["status"],
            "synthesized_variants": synthesize_all_variants(m),
            "gateway_reachable": m.get("gateway_reachable"),
            "gateway_checked_at": m.get("gateway_checked_at"),
        }
        by_pid.setdefault(m["provider_id"], []).append(row)
    for lst in by_pid.values():
        lst.sort(key=lambda r: r["id"])

    grouped: list[dict] = []
    for p in sorted(providers, key=lambda r: r["id"]):
        grouped.append({
            "id": p["id"],
            "label": p["label"],
            "base_url": p.get("base_url"),
            "api_key_source": p.get("api_key_source"),
            "api_type": p.get("api_type"),
            "runtime_status": p["runtime_status"],
            "status": p["status"],
            "secret_project": p.get("secret_project"),
            "secret_id": p.get("secret_id"),
            "secret_version": p.get("secret_version"),
            "credential_status": p.get("credential_status"),
            "credential_error": p.get("credential_error"),
            "credential_checked_at": p.get("credential_checked_at"),
            # Single precedence-ordered status for display — see
            # availability.effective_provider_status's docstring. Computed
            # here (not stored) so it can never drift from the three raw
            # fields above; the template shows this instead of deriving
            # its own if/elif chain over them.
            "effective_status": effective_provider_status(p),
            "models": by_pid.get(p["id"], []),
        })
    return grouped


async def _render_page_v2(
    request: Request,
    user: dict,
    error: str | None = None,
    view: str = "registry",
):
    """Fetch both v2 tables, group into the hierarchy, render the admin page.

    Used by the GET handler AND by every POST handler on the
    `ManagementError` branch, so a validation failure re-renders the
    same page with an inline banner and preserves the operator's
    context (they don't get bounced back to a fresh page).

    `view` picks the tab ('registry' = providers/models/variants,
    'pricing' = the flat current-rate table) — same server-side
    `?view=` tab pattern `agent_registry/index.html` already uses for its
    Templates/Instances tabs, not a client-side JS toggle.
    """
    pool = get_pool()
    provider_rows = await list_providers_v2(pool, include_disabled=True)
    model_rows = await list_base_models_v2(pool, include_disabled=True)
    # 'vision' is hidden from the admin UI for now — not consumed
    # meaningfully anywhere yet (unlike 'reasoning', which the wire
    # contract's `_capability_fields` actually uses). The tag definition
    # and `model_tags` machinery both stay intact; only the checkbox is
    # hidden so it can be wired up again later without a schema change.
    # See migrations/model_tags_unset_vision.sql for the matching
    # one-time cleanup that removed existing 'vision' assignments.
    all_tags = [t for t in await list_tags_v2(pool) if t["name"] != "vision"]
    grouped = _group_hierarchy_v2(provider_rows, model_rows)
    pricing = await list_pricing_grouped(pool)
    # Models with zero pricing rows never appear in `pricing_linked` (it's
    # built from existing `model_pricing` rows) — without this, there'd be
    # no way to give a never-priced model its first rate at all, only ever
    # "+ Add rate" a NEW VERSION on top of one that already exists.
    priced_model_ids = {e["model_id"] for e in pricing["linked"]}
    unpriced_models = [
        {"id": m["id"], "label": m["label"], "provider_id": p["id"]}
        for p in grouped for m in p["models"]
        if m["id"] not in priced_model_ids
    ]
    # Utility-model settings — a separate config surface from the
    # registry/pricing tables above (same registry data, different write
    # path); see app/services/misc_models.py.
    misc_models = await list_misc_models(pool)
    utility_model_options = await list_utility_model_options(pool)
    for pm in misc_models:
        pm["options"] = await list_utility_model_options(
            pool, frozenset(pm["providers"]))
    can_edit = user.get("system_role") == SystemRole.PLATFORM_ADMIN
    # Use the current Starlette 1.x signature (`request` first). The
    # deprecated `(name, context)` form still works on the pinned 0.41.x
    # in production but is a hard error on newer Starlette, so match the
    # forward-compatible convention.
    return _templates.TemplateResponse(
        request,
        "admin/model_registry.html",
        {
            "user": user,
            "view": view if view in ("registry", "pricing", "misc") else "registry",
            "providers": grouped,
            "all_tags": all_tags,
            "pricing_linked": pricing["linked"],
            "pricing_unlinked": pricing["unlinked"],
            "unpriced_models": unpriced_models,
            "unpriced_model_ids": frozenset(m["id"] for m in unpriced_models),
            "misc_models": misc_models,
            "utility_model_options": utility_model_options,
            "error": error,
            "can_edit": can_edit,
        },
    )


@router.get("/settings/model-registry", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    view: str = "registry",
    user: dict = Depends(get_current_user),
):
    """Read-only view of the model registry. Any logged-in user."""
    return await _render_page_v2(request, user, view=view)


# ── Provider writes ────────────────────────────────────────────────────────


@router.post(
    "/settings/model-registry/providers",
    response_class=HTMLResponse,
)
async def admin_create_provider(
    request: Request,
    id: str = Form(...),
    label: str = Form(...),
    base_url: str = Form(""),
    api_type: str = Form(""),
    runtime_status: str = Form("provisioned"),
    secret_id: str = Form(""),
    user: dict = Depends(require_admin),
):
    payload: dict = {
        "id": id.strip(),
        "label": label.strip(),
        "runtime_status": runtime_status.strip() or "provisioned",
    }
    # Optional strings: empty means "absent"; the backend leaves the
    # DB column NULL on absence, matching cycle-02's future API-key
    # expectations (a not-yet-set api_key_source doesn't reserve a slot).
    if base_url.strip():
        payload["base_url"] = base_url.strip()
    if api_type.strip():
        payload["api_type"] = api_type.strip()
    # secret_project is deliberately never collected here — the gateway
    # resolves it against its own GCP project (same project the VM is
    # deployed in) rather than trusting an operator-typed value. secret_version
    # is left at the DB default ('latest') for the same reason: the gateway
    # always resolves the newest version, never a pinned one.
    if secret_id.strip():
        payload["secret_id"] = secret_id.strip()

    try:
        await create_provider_v2(get_pool(), payload)
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/providers/{id}/update",
    response_class=HTMLResponse,
)
async def admin_update_provider(
    request: Request,
    id: str,
    label: str = Form(""),
    base_url: str = Form(""),
    api_type: str = Form(""),
    runtime_status: str = Form(""),
    status: str = Form(""),
    secret_id: str = Form(""),
    user: dict = Depends(require_admin),
):
    # Only fields the operator actually filled in end up in `patch` —
    # an empty string means "leave alone". An entirely empty patch
    # falls through to `update_provider_v2` which raises `empty_patch`;
    # the ManagementError handler below re-renders the page with the
    # error.
    patch: dict = {}
    if label.strip():
        patch["label"] = label.strip()
    if base_url.strip():
        patch["base_url"] = base_url.strip()
    if api_type.strip():
        patch["api_type"] = api_type.strip()
    if runtime_status.strip():
        patch["runtime_status"] = runtime_status.strip()
    if status.strip():
        patch["status"] = status.strip()
    # secret_project/secret_version are never operator-editable — see the
    # matching note in admin_create_provider.
    if secret_id.strip():
        patch["secret_id"] = secret_id.strip()

    secret_ref_changed = bool(patch.get("secret_id"))

    try:
        await update_provider_v2(get_pool(), id, patch)
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))

    if secret_ref_changed:
        await append_event(
            get_pool(),
            event_type=MODEL_PROVIDER_SECRET_REF_CHANGED,
            payload={
                "provider_id": id,
                "field": "secret_reference",
                "secret_id": patch.get("secret_id"),
            },
            actor_id=user["actor_id"],
        )
        logger.info(
            "model_providers: secret reference changed for provider=%s by=%s",
            id, user.get("uid", "unknown"),
        )

    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/providers/{id}/disable",
    response_class=HTMLResponse,
)
async def admin_disable_provider(
    request: Request,
    id: str,
    user: dict = Depends(require_admin),
):
    try:
        await set_provider_status_v2(get_pool(), id, "disabled")
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/providers/{id}/enable",
    response_class=HTMLResponse,
)
async def admin_enable_provider(
    request: Request,
    id: str,
    user: dict = Depends(require_admin),
):
    try:
        await set_provider_status_v2(get_pool(), id, "active")
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/providers/{id}/refresh-credentials",
    response_class=HTMLResponse,
)
async def admin_refresh_provider_credentials(
    request: Request,
    id: str,
    user: dict = Depends(require_admin),
):
    """Explicit, deliberate re-resolve of this provider's Secret Manager
    reference on the gateway, right now, instead of waiting for the next
    restart-triggering change. Accepts the same `config.patch`-adjacent
    gateway restart as every other Model Registry admin action — see
    research/model_registry_secret_management_design.md's "roadmap
    sequencing" section for why this is an accepted trade-off, not an
    oversight.
    """
    logger.info(
        "model_providers: refresh-credentials requested for provider=%s by=%s",
        id, user.get("uid", "unknown"),
    )
    await sync_gateway_model_catalog()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/providers/{id}/revoke-credentials",
    response_class=HTMLResponse,
)
async def admin_revoke_provider_credentials(
    request: Request,
    id: str,
    user: dict = Depends(require_admin),
):
    """Mark this provider's credential 'revoked' — an admin assertion
    that the key is known-dead on the provider's own console
    (Anthropic/Google), not a Secret Manager action. Deliberately does
    NOT call Secret Manager's `DisableSecretVersion` — that requires a
    broader IAM grant than this app's service account has
    (`secretAccessor` only, per the design doc's ownership model), and a
    dedicated Secret Manager admin client this app doesn't have yet.

    What this DOES do: sets `credential_status='revoked'` (sticky — the
    gateway's periodic health report can never overwrite it, see
    `set_provider_credential_status_v2`'s docstring), then triggers a
    catalog resync so the next `models.reload` omits this provider from
    `providerCredentials` entirely (`emitter.build_provider_credentials_payload`)
    — the gateway's plugin stops re-injecting the resolved value into
    its own auth env on subsequent refresh ticks. Secret Manager itself
    is untouched; the secret still resolves fine if queried directly.
    Does NOT invalidate the underlying provider API key (only the
    provider's own console can do that) and does NOT stop a request
    already in flight with the old key.

    Reversed by rotating to a new `secret_id` via the edit-provider
    form (which resets `credential_status` back to 'unknown' — see
    `update_provider_v2`'s secret_id-rotation behavior) — there is no
    separate "un-revoke" action, since the point of revoking is that
    the old secret is genuinely dead.
    """
    try:
        await revoke_provider_credential_v2(get_pool(), id)
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))

    await append_event(
        get_pool(),
        event_type=MODEL_PROVIDER_SECRET_REF_CHANGED,
        payload={"provider_id": id, "field": "credential_status", "value": "revoked"},
        actor_id=user["actor_id"],
    )
    logger.warning(
        "model_providers: credential REVOKED for provider=%s by=%s "
        "(AgentOS-side bookkeeping + gateway stops using it going forward — "
        "does not touch Secret Manager or the underlying provider key; see docstring)",
        id, user.get("uid", "unknown"),
    )
    await sync_gateway_model_catalog()
    return RedirectResponse("/settings/model-registry", status_code=303)


async def _notify_gateway_reload() -> None:
    """Tell the gateway to re-pull the model catalog and sync provider
    auth config, so a newly added/edited model is both known AND usable
    immediately rather than waiting on the 5-minute self-heal poll (and,
    for provider auth, waiting forever — see
    `gateway_sync.sync_provider_auth_configs_to_gateway`). Soft-fail
    throughout, mirrored from `model_change.py`'s `agents.update` pattern.
    """
    await sync_gateway_model_catalog()


# ── Base-model writes ──────────────────────────────────────────────────────


@router.post(
    "/settings/model-registry/models",
    response_class=HTMLResponse,
)
async def admin_create_model(
    request: Request,
    provider_id: str = Form(...),
    id_suffix: str = Form(...),
    label: str = Form(...),
    tier: str = Form(...),
    context_window: int = Form(...),
    provider_shorthand: str = Form(""),
    tags: list[str] = Form(default=[]),
    user: dict = Depends(require_admin),
):
    # The Add Model form does NOT collect `available_context_windows`
    # — variants are added via the dedicated `/context-windows` route
    # only, keeping the guided add-flow single-purpose per step.
    #
    # `id` is no longer typed by the admin directly — it's joined
    # server-side from `provider_id` (already a separate field, already
    # validated against `model_providers`) and `id_suffix` (the form
    # field name avoids Pydantic's reserved `model_` attribute prefix —
    # it's still "the model name/id half" semantically). Previously the
    # admin had to re-type the provider prefix inside a combined `id`
    # field on top of the separate `provider_id` selector, which was
    # redundant and forced the model half through the same one-slash
    # regex as `id` itself. It's genuinely free text now — it may
    # contain further slashes (e.g. Vertex's
    # `publishers/google/models/gemini-pro`) — see `_MODEL_ID_RE`'s
    # docstring in management.py for why only the provider half needs an
    # unambiguous shape.
    payload: dict = {
        "id": f"{provider_id.strip()}/{id_suffix.strip()}",
        "provider_id": provider_id.strip(),
        "label": label.strip(),
        "tier": tier.strip(),
        "context_window": context_window,
        "tags": tags,
    }
    if provider_shorthand.strip():
        payload["provider_shorthand"] = provider_shorthand.strip()

    try:
        await create_base_model_v2(get_pool(), payload)
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/models/{model_id:path}/update",
    response_class=HTMLResponse,
)
async def admin_update_model(
    request: Request,
    model_id: str,
    label: str = Form(""),
    tier: str = Form(""),
    context_window: str = Form(""),
    provider_shorthand: str = Form(""),
    status: str = Form(""),
    tags: list[str] = Form(default=[]),
    tags_submitted: str = Form(""),
    user: dict = Depends(require_admin),
):
    patch: dict = {}
    if label.strip():
        patch["label"] = label.strip()
    if tier.strip():
        patch["tier"] = tier.strip()
    if context_window.strip():
        try:
            patch["context_window"] = int(context_window)
        except (TypeError, ValueError):
            return await _render_page_v2(
                request, user,
                error=(
                    f"context_window must be an integer, "
                    f"got {context_window!r}"
                ),
            )
    if provider_shorthand.strip():
        patch["provider_shorthand"] = provider_shorthand.strip()
    if status.strip():
        patch["status"] = status.strip()
    # Checkboxes submit nothing when all are unchecked, so "tags absent"
    # is indistinguishable from "clear all tags" without a sentinel —
    # `tags_submitted` (a hidden always-present field) disambiguates:
    # present means "the checkbox set is the new tag list, even if empty".
    if tags_submitted:
        patch["tags"] = tags

    try:
        await update_base_model_v2(get_pool(), model_id, patch)
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/models/{model_id:path}/disable",
    response_class=HTMLResponse,
)
async def admin_disable_model(
    request: Request,
    model_id: str,
    user: dict = Depends(require_admin),
):
    try:
        await set_base_model_status_v2(get_pool(), model_id, "disabled")
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/models/{model_id:path}/enable",
    response_class=HTMLResponse,
)
async def admin_enable_model(
    request: Request,
    model_id: str,
    user: dict = Depends(require_admin),
):
    try:
        await set_base_model_status_v2(get_pool(), model_id, "active")
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


# ── Context-window writes ──────────────────────────────────────────────────


@router.post(
    "/settings/model-registry/models/{model_id:path}/context-windows",
    response_class=HTMLResponse,
)
async def admin_add_context_window(
    request: Request,
    model_id: str,
    tokens: int = Form(...),
    user: dict = Depends(require_admin),
):
    try:
        await add_context_window_v2(get_pool(), model_id, tokens)
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


@router.post(
    "/settings/model-registry/models/{model_id:path}/context-windows/{tokens:int}/remove",
    response_class=HTMLResponse,
)
async def admin_remove_context_window(
    request: Request,
    model_id: str,
    tokens: int,
    user: dict = Depends(require_admin),
):
    try:
        await remove_context_window_v2(get_pool(), model_id, tokens)
    except ManagementError as exc:
        return await _render_page_v2(request, user, error=str(exc))
    await _notify_gateway_reload()
    return RedirectResponse("/settings/model-registry", status_code=303)


# ── Pricing writes ──────────────────────────────────────────────────────────


@router.post(
    "/settings/model-registry/models/{model_id:path}/pricing",
    response_class=HTMLResponse,
)
async def admin_add_pricing_version(
    request: Request,
    model_id: str,
    input_rate: float = Form(...),
    output_rate: float = Form(...),
    cache_read_rate: str = Form(""),
    cache_write_rate: str = Form(""),
    thinking_rate: str = Form(""),
    provider_label: str = Form(""),
    notes: str = Form(""),
    effective_from: str = Form(""),
    user: dict = Depends(require_admin),
):
    """Insert a new (append-only) pricing version for `model_id`.

    `provider`/`model` are resolved server-side from the `models` row —
    never free-typed — so a new row can never drift into the kind of
    mismatch (`gemini-2.5-flash-exp` vs `-flash`) that made the earlier
    unlinked rows possible in the first place.

    Optional rate fields are `str` (not `float`), parsed manually below —
    the "Add price" modal for a never-priced model leaves them genuinely
    blank (no current value to pre-fill), and FastAPI's automatic `float`
    coercion 422s on an empty-string form submission rather than treating
    it as "absent".
    """
    cache_read_rate_val = float(cache_read_rate) if cache_read_rate.strip() else 0.0
    cache_write_rate_val = float(cache_write_rate) if cache_write_rate.strip() else 0.0
    thinking_rate_val = float(thinking_rate) if thinking_rate.strip() else None
    effective_from_val = (
        datetime.fromisoformat(effective_from).replace(tzinfo=timezone.utc)
        if effective_from.strip() else None
    )
    try:
        await create_price_version(
            get_pool(), model_id,
            input_rate=input_rate, output_rate=output_rate,
            cache_read_rate=cache_read_rate_val, cache_write_rate=cache_write_rate_val,
            thinking_rate=thinking_rate_val,
            provider_label=provider_label.strip() or None,
            notes=notes.strip() or None,
            effective_from=effective_from_val,
        )
    except PricingValidationError as exc:
        return await _render_page_v2(request, user, error=str(exc), view="pricing")
    return RedirectResponse("/settings/model-registry?view=pricing", status_code=303)


# ── Utility-model settings writes ────────────────────────────────────────────
# Separate "completely different thing" from the registry/pricing tabs
# above — see app/services/misc_models.py's module docstring for why.


@router.post(
    "/settings/model-registry/misc-models/{purpose_key}/update",
    response_class=HTMLResponse,
)
async def admin_update_misc_model(
    request: Request,
    purpose_key: str,
    model_id: str = Form(""),
    user: dict = Depends(require_admin),
):
    """Set (or, if `model_id` is blank, clear) the Gemini model override for
    one utility purpose. Clearing reverts the call site to its hardcoded
    default — see MISC_MODEL_PURPOSES in app/services/misc_models.py."""
    try:
        await set_misc_model(get_pool(), purpose_key, model_id.strip())
    except MiscModelValidationError as exc:
        return await _render_page_v2(request, user, error=str(exc), view="misc")
    # TELL THE GATEWAY NOW rather than at its next 5-minute poll. Never
    # fatal: the poll is the self-heal, and the save has already landed.
    try:
        from app.openclaw import gateway_admin
        await gateway_admin.call("misc-models.reload", {}, timeout=10)
    except Exception as exc:                                # noqa: BLE001
        logger.info("misc_models: gateway reload after saving %s failed "
                    "(%s); its next poll picks it up", purpose_key, exc)
    return RedirectResponse("/settings/model-registry?view=misc", status_code=303)
