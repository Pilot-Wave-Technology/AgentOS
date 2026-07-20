"""Generator for the MCP Server Import wizard.

Wizard inputs:
    server_url:  str — the MCP server's SSE endpoint
    tools:       list[{name, description, input_schema}]
    credential:  {name, type, header_name?} | None — optional bearer / api_key /
                 basic credential. When set, the generated source calls
                 secrets.get() and passes the resulting value as an HTTP
                 header to mcp.call (sent on both SSE handshake and POSTs).
    tool_label:  str

Produces a single aggregated tool whose `execute(args)` calls
`mcp.call(SERVER, args.operation, args.arguments, { headers })`. The mcp
capability enforces server allowlisting; this generator declares the single
server. When a credential is bound, the manifest also declares
secrets.names so the runtime grants secrets.get(<that one name>).

Only SSE transport is supported in the capability runtime. Stdio MCP
servers would need a separate capability (process spawn) which is out
of scope for now.
"""
import json
import logging
from typing import Optional

from app.services.custom_tools.generators import GeneratedTool, register

log = logging.getLogger(__name__)


def _generate(inputs: dict) -> GeneratedTool:
    server_url: str = inputs.get("server_url") or ""
    transport: str = inputs.get("transport") or "sse"
    tools: list[dict] = inputs.get("tools") or []
    tool_label: str = inputs.get("tool_label") or "MCP server"
    credential: Optional[dict] = inputs.get("credential")

    if transport != "sse":
        raise ValueError(
            f"mcp_server: transport {transport!r} not supported by the capability runtime"
            " (only 'sse' for now)"
        )
    if not server_url:
        raise ValueError("mcp_server: server_url required for SSE transport")
    if not tools:
        raise ValueError("mcp_server: at least one tool must be selected")

    tool_names = [t["name"] for t in tools]

    # Per-tool input schemas from MCP discovery — embedded for runtime
    # validation via the `validate` global before dispatch.
    tool_schemas = {
        t["name"]: t["input_schema"]
        for t in tools
        if t.get("input_schema")
    }

    # ─── Source ─────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append(f"const SERVER = {json.dumps(server_url)};")
    lines.append(f"const ALLOWED = new Set({json.dumps(tool_names)});")
    if tool_schemas:
        lines.append(f"const SCHEMAS = {json.dumps(tool_schemas)};")
    lines.append("")
    lines.append("async function execute(args) {")
    lines.append("  const operation = args && args.operation;")
    lines.append("  const operationArgs = (args && args.arguments) || {};")
    lines.append("  if (!operation || !ALLOWED.has(operation)) {")
    lines.append(
        "    throw new Error(`unknown operation '${operation}' — must be one of: "
        + ", ".join(tool_names)
        + "`);"
    )
    lines.append("  }")
    if tool_schemas:
        lines.append("  if (operation in SCHEMAS) {")
        lines.append("    validate(operationArgs, SCHEMAS[operation], `${operation}.arguments`);")
        lines.append("  }")

    # Build per-call options { headers } when a credential is bound. The
    # secret value lives only inside this closure for the duration of one
    # call; mcp.call forwards the headers to both SSE handshake and POSTs.
    if credential:
        cname = credential["name"]
        ctype = credential["type"]
        lines.append(f"  const __secret = await secrets.get({json.dumps(cname)});")
        lines.append("  const headers = {};")
        if ctype == "bearer":
            lines.append("  headers['Authorization'] = `Bearer ${__secret}`;")
        elif ctype == "api_key":
            header_name = credential.get("header_name") or "X-API-Key"
            lines.append(f"  headers[{json.dumps(header_name)}] = __secret;")
        elif ctype == "basic":
            lines.append("  headers['Authorization'] = `Basic ${btoa(__secret)}`;")
        else:
            raise ValueError(f"mcp_server: unsupported credential type {ctype!r}")
        lines.append("  return await mcp.call(SERVER, operation, operationArgs, { headers });")
    else:
        lines.append("  return await mcp.call(SERVER, operation, operationArgs);")
    lines.append("}")
    source_code = "\n".join(lines)

    # ─── Manifest ───────────────────────────────────────────────────
    manifest: dict = {"capabilities": {"mcp": {"servers": [server_url]}}}
    if credential:
        manifest["capabilities"]["secrets"] = {"names": [credential["name"]]}

    # ─── Parameters schema ──────────────────────────────────────────
    tool_docs = "; ".join(
        f"{t['name']}" + (f" — {t['description']}" if t.get("description") else "")
        for t in tools
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": tool_names,
                "description": f"MCP tool to call. Options: {tool_docs}",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments to pass to the MCP tool.",
            },
        },
        "required": ["operation"],
    }

    log.info(
        "mcp_server generated: server=%s transport=%s tools=%d schemas_embedded=%d source_bytes=%d",
        server_url, transport, len(tools), len(tool_schemas), len(source_code.encode()),
    )

    return {
        "source_code": source_code,
        "manifest": manifest,
        "parameters_schema": parameters_schema,
    }


register("mcp_server", _generate)
