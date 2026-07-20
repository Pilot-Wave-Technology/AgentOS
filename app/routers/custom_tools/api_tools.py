"""Custom tool CRUD handlers — mounted by api_routes.py at /api/custom-tools/."""
import logging

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import require_admin, get_current_user
from app.database import get_pool
from app.models.custom_tool import (
    BulkCredentialRequest,
    CustomToolCreateRequest,
    CustomToolTestRequest,
    CustomToolUpdateRequest,
)
from app.services.custom_tools.crud import (
    bulk_set_credential,
    create_custom_tool,
    get_custom_tool,
    get_tool_credential,
    list_custom_tools,
    set_custom_tool_status,
    update_custom_tool,
)
from app.services.custom_tools.generators import (
    available_tiers as _generator_available_tiers,
    generate as _generate_tool_source,
)

log = logging.getLogger(__name__)


async def api_list_tools(_user=Depends(get_current_user)):
    pool = get_pool()
    tools = await list_custom_tools(pool)
    return JSONResponse([_tool_summary(t) for t in tools])


async def api_create_tool(body: CustomToolCreateRequest, user=Depends(require_admin)):
    """Create a custom tool.

    If `manifest` is already provided by the caller (TypeScript wizard, scripted
    creates), we trust it and store as-is. Otherwise, if the tier has a
    registered generator, we run it against the body to synthesise
    `source_code` + `manifest` from the wizard-shaped config. This is the
    bridge that lets the four wizards produce runtime-shape tools without
    every wizard handler reimplementing source generation.
    """
    pool = get_pool()

    source_code = body.source_code
    manifest = body.manifest
    parameters_schema = body.parameters_schema

    if manifest is None and body.tier in _generator_available_tiers():
        inputs = await _build_generator_inputs(pool, body)
        try:
            gen = _generate_tool_source(body.tier, inputs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"generator: {exc}")
        source_code = source_code or gen["source_code"]
        manifest = gen["manifest"]
        # Wizard may have submitted an empty/default schema; the generator's
        # is more informative — keep whichever the caller explicitly provided.
        if not parameters_schema or parameters_schema == {"type": "object", "properties": {}}:
            parameters_schema = gen["parameters_schema"]

    try:
        tool = await create_custom_tool(
            pool,
            name=body.name,
            label=body.label,
            description=body.description,
            tier=body.tier,
            parameters_schema=parameters_schema,
            config=body.config,
            source_code=source_code,
            created_by=user.get("email"),
            credential_id=body.credential_id,
            manifest=manifest,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _signal_reload()
    return JSONResponse(_tool_summary(tool), status_code=201)


async def _build_generator_inputs(pool, body: CustomToolCreateRequest) -> dict:
    """Map the request body into the dict shape each generator expects.

    Tier-aware reshape: http_action and typescript come through this path;
    openapi and mcp_server have their own dedicated wizard endpoints.
    """
    inputs = dict(body.config or {})

    # Generators want credential as {name, type, header_name?}, not credential_id.
    if body.credential_id:
        cred = await get_tool_credential(pool, body.credential_id)
        if cred:
            inputs["credential"] = {
                "name": cred["name"],
                "type": cred["credential_type"],
                "header_name": cred.get("header_name"),
            }

    if body.tier == "typescript":
        # The admin paste their source on the typescript form.
        inputs["source_code"] = body.source_code or ""
        inputs["manifest"] = body.manifest
        inputs["parameters_schema"] = body.parameters_schema

    if body.parameters_schema:
        inputs["parameters_schema"] = body.parameters_schema

    return inputs


async def api_get_tool(tool_name: str, _user=Depends(get_current_user)):
    pool = get_pool()
    tool = await get_custom_tool(pool, tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")
    return JSONResponse(_tool_detail(tool))


async def api_update_tool(tool_name: str, body: CustomToolUpdateRequest, _user=Depends(require_admin)):
    pool = get_pool()
    try:
        # exclude_unset=True so that credential_id: null (explicit clear) is included,
        # while omitted fields are ignored. exclude_none would silently drop the clear.
        tool = await update_custom_tool(pool, tool_name, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _signal_reload()
    return JSONResponse(_tool_detail(tool))


async def api_disable_tool(tool_name: str, _user=Depends(require_admin)):
    pool = get_pool()
    try:
        tool = await set_custom_tool_status(pool, tool_name, "disabled")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _signal_reload()
    return JSONResponse(_tool_summary(tool))


async def api_enable_tool(tool_name: str, _user=Depends(require_admin)):
    pool = get_pool()
    try:
        tool = await set_custom_tool_status(pool, tool_name, "enabled")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _signal_reload()
    return JSONResponse(_tool_summary(tool))


async def api_test_tool(tool_name: str, body: CustomToolTestRequest, _user=Depends(require_admin)):
    """Run a one-shot admin test of the tool through the OpenClaw capability
    runtime via the tools.test_execute RPC. The plugin owns execution; this
    endpoint is only the admin-side caller.
    """
    pool = get_pool()
    tool = await get_custom_tool(pool, tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")

    from app.openclaw import gateway_admin
    try:
        result = await gateway_admin.call(
            "tools.test_execute",
            {"toolName": tool_name, "args": body.args},
        )
    except Exception as exc:
        log.warning("test_execute RPC failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"gateway test_execute failed: {exc}")
    return JSONResponse(result)


async def api_regenerate_tool(tool_name: str, body: dict, _user=Depends(require_admin)):
    """Re-run the tier's generator against an updated config and persist a fresh
    source_code + manifest.

    Used by the edit flow for aggregated tools (mcp_server, openapi) — admin
    changes the operations list, server URL, or bound credential, and this
    endpoint produces a new TS source + manifest atomically. Issues tools.reload
    so the gateway picks up the new source on its next invocation.

    Body shape (all fields optional, merged on top of the tool's current state):
      {
        config: { ... tier-specific updates ... },
        credential_id: "..." | null
      }
    """
    pool = get_pool()
    tool = await get_custom_tool(pool, tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")
    if tool["tier"] not in _generator_available_tiers():
        raise HTTPException(
            status_code=400,
            detail=f"regenerate not supported for tier {tool['tier']!r}",
        )

    new_config = {**(tool.get("config") or {}), **(body.get("config") or {})}
    credential_id = body.get("credential_id", tool.get("credential_id"))

    credential: dict | None = None
    if credential_id:
        cred = await get_tool_credential(pool, credential_id)
        if cred:
            credential = {
                "name": cred["name"],
                "type": cred["credential_type"],
                "header_name": cred.get("header_name"),
            }

    # Per-tier input shaping — mirrors what the wizard generate endpoints
    # build. Kept local since the tiers only differ in which config keys
    # the generator wants.
    tier = tool["tier"]
    inputs: dict
    if tier == "openapi":
        inputs = {
            "server_url": new_config.get("server_url", ""),
            "operations": new_config.get("operations", []),
            "credential": credential,
            "tool_label": tool["label"],
        }
    elif tier == "mcp_server":
        inputs = {
            "server_url": new_config.get("server_url", ""),
            "transport": new_config.get("transport", "sse"),
            "tools": new_config.get("tools", []),
            "tool_label": tool["label"],
        }
    elif tier == "http_action":
        inputs = {
            **new_config,
            "credential": credential,
            "parameters_schema": tool.get("parameters_schema"),
        }
    else:
        # typescript regenerates from admin-edited source; goes through PATCH instead
        raise HTTPException(
            status_code=400,
            detail=f"regenerate not applicable for tier {tier!r}; use PATCH",
        )

    try:
        gen = _generate_tool_source(tier, inputs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"generator: {exc}")

    try:
        updated = await update_custom_tool(pool, tool_name, {
            "config": new_config,
            "source_code": gen["source_code"],
            "manifest": gen["manifest"],
            "parameters_schema": gen["parameters_schema"],
            "credential_id": credential_id,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await _signal_reload()
    log.info("tool regenerated: name=%s tier=%s", tool_name, tier)
    return JSONResponse(_tool_detail(updated))


async def api_bulk_set_credential(body: BulkCredentialRequest, _user=Depends(require_admin)):
    if not body.tool_names:
        raise HTTPException(status_code=400, detail="tool_names must not be empty")
    log.info(
        "bulk credential assign: tools=%d credential_id=%s",
        len(body.tool_names), body.credential_id,
    )
    pool = get_pool()
    try:
        updated = await bulk_set_credential(pool, body.tool_names, body.credential_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _signal_reload()
    return JSONResponse({"updated": updated, "count": len(updated)})


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _tool_summary(t: dict) -> dict:
    return {
        "name": t["name"],
        "label": t["label"],
        "description": t["description"],
        "tier": t["tier"],
        "status": t["status"],
        "created_at": t["created_at"].isoformat() if t.get("created_at") else None,
    }


def _tool_detail(t: dict) -> dict:
    d = _tool_summary(t)
    d.update({
        "parameters_schema": t.get("parameters_schema") or {},
        "config": t.get("config") or {},
        "source_code": t.get("source_code"),
        "manifest": t.get("manifest"),
        "credential_id": t.get("credential_id"),
        "updated_at": t["updated_at"].isoformat() if t.get("updated_at") else None,
    })
    return d


async def _signal_reload() -> None:
    try:
        from app.openclaw import gateway_admin
        await gateway_admin.tools_reload()
        log.debug("tools.reload signalled")
    except Exception as exc:
        log.warning("tools.reload signal failed (non-fatal): %s", exc)
