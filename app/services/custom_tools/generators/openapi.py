"""Generator for the OpenAPI Import wizard.

Wizard inputs:
    server_url:  str
    operations:  list[{operation_id, method, path, description}]
    credential:  {name, type, header_name?} | None
    tool_label:  str

Produces a single aggregated tool whose `execute(args)` dispatches on
`args.operation` to one of the selected operations via `openapi.call`.
Path templates with `{param}` are substituted by the SDK from
`args.arguments`; remaining args become query string (GET/DELETE) or
JSON body (POST/PUT/PATCH).
"""
import json
import logging
from typing import Optional
from urllib.parse import urlparse

from app.services.custom_tools.generators import GeneratedTool, register

log = logging.getLogger(__name__)


def _generate(inputs: dict) -> GeneratedTool:
    server_url: str = inputs["server_url"]
    operations: list[dict] = inputs["operations"]
    credential: Optional[dict] = inputs.get("credential")
    tool_label: str = inputs.get("tool_label") or "API"

    host = urlparse(server_url).hostname
    if not host:
        raise ValueError(f"openapi: cannot parse hostname from server_url {server_url!r}")

    if not operations:
        raise ValueError("openapi: at least one operation required")

    op_map = {
        op["operation_id"]: {"method": op["method"], "path": op["path"]}
        for op in operations
    }
    op_names = list(op_map.keys())

    # Per-op argument schemas (optional) — embedded so the runtime can
    # validate args.arguments before dispatch via the `validate` global.
    op_schemas = {
        op["operation_id"]: op["parameters_schema"]
        for op in operations
        if op.get("parameters_schema")
    }

    # ─── Source ─────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append(f"const SERVER_URL = {json.dumps(server_url)};")
    lines.append(f"const OPERATIONS = {json.dumps(op_map)};")
    if op_schemas:
        lines.append(f"const SCHEMAS = {json.dumps(op_schemas)};")
    lines.append("")
    lines.append("async function execute(args) {")
    lines.append("  const operation = args && args.operation;")
    lines.append("  const operationArgs = (args && args.arguments) || {};")
    lines.append("  if (!operation || !(operation in OPERATIONS)) {")
    lines.append(
        "    throw new Error(`unknown operation '${operation}' — must be one of: "
        + ", ".join(op_names)
        + "`);"
    )
    lines.append("  }")
    if op_schemas:
        lines.append("  if (operation in SCHEMAS) {")
        lines.append("    validate(operationArgs, SCHEMAS[operation], `${operation}.arguments`);")
        lines.append("  }")
    lines.append("  const headers = {};")
    if credential:
        cname = credential["name"]
        ctype = credential["type"]
        lines.append(f"  const __secret = await secrets.get({json.dumps(cname)});")
        if ctype == "bearer":
            lines.append("  headers['Authorization'] = `Bearer ${__secret}`;")
        elif ctype == "api_key":
            header_name = credential.get("header_name") or "X-API-Key"
            lines.append(f"  headers[{json.dumps(header_name)}] = __secret;")
        elif ctype == "basic":
            lines.append("  headers['Authorization'] = `Basic ${btoa(__secret)}`;")
        else:
            raise ValueError(f"openapi: unsupported credential type {ctype!r}")
    lines.append(
        "  return await openapi.call({ serverUrl: SERVER_URL, operations: OPERATIONS, headers }, operation, operationArgs);"
    )
    lines.append("}")
    source_code = "\n".join(lines)

    # ─── Manifest ───────────────────────────────────────────────────
    manifest: dict = {"capabilities": {"net": {"hosts": [host]}}}
    if credential:
        manifest["capabilities"]["secrets"] = {"names": [credential["name"]]}

    # ─── Parameters schema (operation enum + arguments object) ──────
    op_docs = "; ".join(
        f"{op['operation_id']} ({op['method']} {op['path']})"
        + (f" — {op['description']}" if op.get("description") else "")
        for op in operations
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": op_names,
                "description": f"Operation to perform. Options: {op_docs}",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments for the chosen operation (path params, query params, or request body).",
            },
        },
        "required": ["operation"],
    }

    log.info(
        "openapi generated: host=%s operations=%d schemas_embedded=%d credential=%s source_bytes=%d",
        host, len(operations), len(op_schemas), bool(credential), len(source_code.encode()),
    )

    return {
        "source_code": source_code,
        "manifest": manifest,
        "parameters_schema": parameters_schema,
    }


register("openapi", _generate)
