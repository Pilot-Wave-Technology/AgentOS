"""MCP server discovery and tool-generation endpoints — /api/custom-tools/mcp/."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.auth import require_admin
from app.database import get_pool
from app.services.custom_tools.credential import decrypt_value, inject_auth_header
from app.services.custom_tools.crud import create_custom_tool, get_tool_credential
from app.services.custom_tools.generators import generate as generate_tool_source
from app.services.custom_tools.mcp_discovery import discover_mcp_tools

log = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp")


class MCPDiscoverRequest(BaseModel):
    transport: str = "sse"          # 'sse' or 'stdio'
    server_url: str | None = None   # sse
    command: str | None = None      # stdio
    args: list[str] | None = None   # stdio
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    credential_id: str | None = None
    timeout_seconds: int = 30


class MCPGenerateRequest(BaseModel):
    tool_name: str
    tool_label: str | None = None
    tool_description: str | None = None
    transport: str
    server_url: str | None = None
    command: str | None = None
    cmd_args: list[str] | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    credential_id: str | None = None
    tools: list[dict]   # [{name, description, input_schema}] from discover step

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-z][a-z0-9_]{0,63}$", v):
            raise ValueError("tool_name must match ^[a-z][a-z0-9_]{0,63}$")
        return v


@router.post("/discover")
async def api_mcp_discover(body: MCPDiscoverRequest, _user=Depends(require_admin)):
    """Connect to an MCP server and return its tool list (preview only, no DB writes)."""
    log.info(
        "MCP discover: transport=%s server=%s credential=%s",
        body.transport, body.server_url or body.command, bool(body.credential_id),
    )

    # If a stored credential was selected, decrypt it and inject as an auth header
    # on top of any headers the admin entered manually.
    headers = dict(body.headers or {})
    if body.credential_id:
        pool = get_pool()
        cred = await get_tool_credential(pool, body.credential_id)
        if not cred:
            raise HTTPException(status_code=400, detail=f"credential {body.credential_id!r} not found")
        plaintext = decrypt_value(body.credential_id, cred["encrypted_value"])
        inject_auth_header(headers, cred["credential_type"], plaintext, cred.get("header_name"))

    try:
        tools = await discover_mcp_tools(
            body.transport,
            url=body.server_url,
            command=body.command,
            args=body.args,
            env=body.env,
            headers=headers,
            timeout_seconds=body.timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse({"tools": tools, "count": len(tools)})


@router.post("/generate", status_code=201)
async def api_mcp_generate(body: MCPGenerateRequest, user=Depends(require_admin)):
    """Register selected MCP tools as a single aggregated mcp_server tool."""
    if not body.tools:
        raise HTTPException(status_code=400, detail="tools list is empty")

    log.info(
        "MCP generate: transport=%s server=%s tools=%d tool_name=%s",
        body.transport, body.server_url or body.command, len(body.tools), body.tool_name,
    )

    pool = get_pool()
    label = body.tool_label or body.tool_name.replace("_", " ").title()

    # When a credential is bound, hydrate to {name, type, header_name} so the
    # generator can emit the right secrets.get + header-injection JS. The
    # runtime secrets capability is name-keyed; credential_id never reaches
    # generator code.
    credential_for_generator: dict | None = None
    if body.credential_id:
        cred = await get_tool_credential(pool, body.credential_id)
        if cred:
            credential_for_generator = {
                "name": cred["name"],
                "type": cred["credential_type"],
                "header_name": cred.get("header_name"),
            }

    # Generator builds source_code + manifest. SSE only — see mcp_server.py.
    generated = generate_tool_source(
        "mcp_server",
        {
            "server_url": body.server_url or "",
            "transport": body.transport,
            "tools": body.tools,
            "tool_label": label,
            "credential": credential_for_generator,
        },
    )

    # config holds the inputs the admin UI re-uses (re-discover button, edit
    # flows). The runtime uses source_code + manifest for execution.
    config: dict = {"transport": body.transport, "tools": body.tools}
    if body.transport == "sse":
        config["server_url"] = body.server_url or ""
    if body.transport == "stdio":
        config["command"] = body.command or ""
        if body.cmd_args:
            config["args"] = body.cmd_args
    if body.env:
        config["env"] = body.env
    if body.headers:
        config["headers"] = body.headers

    tool_docs = "; ".join(
        f"{t['name']}" + (f" — {t['description']}" if t.get("description") else "")
        for t in body.tools
    )
    description = body.tool_description or f"{label}. Tools: {tool_docs}"

    try:
        tool = await create_custom_tool(
            pool,
            name=body.tool_name,
            label=label,
            description=description,
            tier="mcp_server",
            parameters_schema=generated["parameters_schema"],
            config=config,
            source_code=generated["source_code"],
            created_by=user.get("email"),
            credential_id=body.credential_id,
            manifest=generated["manifest"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        from app.openclaw import gateway_admin
        await gateway_admin.tools_reload()
    except Exception as exc:
        log.warning("tools.reload signal failed (non-fatal): %s", exc)

    log.info("MCP server tool registered: %s (transport=%s, tools=%d)", tool["name"], body.transport, len(body.tools))
    return JSONResponse(
        {"created": tool["name"], "tools": len(body.tools)},
        status_code=201,
    )
