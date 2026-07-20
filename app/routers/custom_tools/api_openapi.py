"""OpenAPI spec import endpoints — /api/custom-tools/openapi/."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth import require_admin
from app.database import get_pool
from app.models.custom_tool import OpenAPISpecImportRequest, OpenAPIToolsGenerateRequest
from app.services.custom_tools.crud import create_custom_tool, create_openapi_spec, get_tool_credential
from app.services.custom_tools.generators import generate as generate_tool_source
from app.services.custom_tools.openapi_parser import (
    extract_operations,
    extract_server_url,
    fetch_spec,
    parse_spec_text,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/openapi")


@router.post("/specs", status_code=201)
async def api_create_openapi_spec(body: dict, _user=Depends(require_admin)):
    """Store a parsed spec so the generate step can reference it by ID."""
    pool = get_pool()
    try:
        spec = await create_openapi_spec(
            pool,
            name=body.get("name", ""),
            spec_content=body.get("spec_content") or {},
            base_url=body.get("base_url"),
            credential_id=body.get("credential_id"),
            spec_url=body.get("spec_url"),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"id": str(spec["id"]), "name": spec["name"]}, status_code=201)


@router.post("/parse")
async def api_parse_openapi(body: OpenAPISpecImportRequest, _user=Depends(require_admin)):
    """Parse a spec from URL or pasted content and return the operation list."""
    try:
        if body.spec_url:
            spec = await fetch_spec(body.spec_url)
        elif body.spec_content:
            spec = parse_spec_text(body.spec_content)
        else:
            raise HTTPException(status_code=400, detail="provide spec_url or spec_content")
        server_url, url_inferred = extract_server_url(spec, body.base_url, spec_fetch_url=body.spec_url)
        operations = extract_operations(spec, server_url)
        return JSONResponse({
            "server_url": server_url,
            "server_url_inferred": url_inferred,
            "operations": operations,
            "spec": spec,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/generate", status_code=201)
async def api_generate_openapi_tools(body: OpenAPIToolsGenerateRequest, user=Depends(require_admin)):
    """Generate a single aggregated tool from selected OpenAPI operations."""
    if not body.operations:
        raise HTTPException(status_code=400, detail="no operations selected")

    pool = get_pool()
    spec_row = await pool.fetchrow("SELECT * FROM openapi_specs WHERE id = $1", body.spec_id)
    if not spec_row:
        raise HTTPException(status_code=404, detail="openapi spec not found")

    spec_content = spec_row["spec_content"]
    if isinstance(spec_content, str):
        import json as _json
        spec_content = _json.loads(spec_content)
    derived_server_url, _ = extract_server_url(
        spec_content,
        base_url_override=spec_row.get("base_url") or None,
        spec_fetch_url=spec_row.get("spec_url"),
    )

    spec_credential_id = str(spec_row["credential_id"]) if spec_row["credential_id"] else None
    label = body.tool_label or body.tool_name.replace("_", " ").title()

    # Look up the bound credential (by id) so the generator can reference it
    # by name in the generated source — the runtime's secrets.get() is
    # name-keyed.
    credential_for_generator: dict | None = None
    if spec_credential_id:
        cred_row = await get_tool_credential(pool, spec_credential_id)
        if cred_row:
            credential_for_generator = {
                "name": cred_row["name"],
                "type": cred_row["credential_type"],
                "header_name": cred_row.get("header_name"),
            }

    # Run the generator — it produces source_code (TS), manifest, and
    # parameters_schema in the runtime shape.
    generated = generate_tool_source(
        "openapi",
        {
            "server_url": derived_server_url,
            "operations": [
                {
                    "operation_id": op.operation_id,
                    "method": op.method,
                    "path": op.path,
                    "description": op.description or "",
                }
                for op in body.operations
            ],
            "credential": credential_for_generator,
            "tool_label": label,
        },
    )

    # Keep a config record of the operations + spec for the admin UI (read-only
    # display, re-import flows). Execution itself uses source_code + manifest.
    config = {
        "spec_id": body.spec_id,
        "server_url": derived_server_url,
        "operations": [
            {
                "operation_id": op.operation_id,
                "method": op.method,
                "path": op.path,
                "description": op.description or "",
            }
            for op in body.operations
        ],
    }

    ops_summary = "; ".join(
        f"{op.operation_id} — {op.description}" if op.description else op.operation_id
        for op in body.operations
    )
    description = body.tool_description or f"{label}. Operations: {ops_summary}"

    try:
        tool = await create_custom_tool(
            pool,
            name=body.tool_name,
            label=label,
            description=description,
            tier="openapi",
            parameters_schema=generated["parameters_schema"],
            config=config,
            source_code=generated["source_code"],
            created_by=user.get("email"),
            credential_id=spec_credential_id,
            manifest=generated["manifest"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await _signal_reload()

    log.info("openapi tool generated: name=%s operations=%d", tool["name"], len(body.operations))
    return JSONResponse({"created": tool["name"], "operations": len(body.operations)}, status_code=201)


async def _signal_reload() -> None:
    try:
        from app.openclaw import gateway_admin
        await gateway_admin.tools_reload()
        log.debug("tools.reload signalled")
    except Exception as exc:
        log.warning("tools.reload signal failed (non-fatal): %s", exc)
