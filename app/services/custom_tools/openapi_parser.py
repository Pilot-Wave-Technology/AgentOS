"""Fetch and parse OpenAPI specs, extract operations as tool definitions."""
import json
import logging
import re
from typing import Any, Optional

import httpx
import yaml

from app.services.custom_tools.ssrf_guard import assert_safe_url

log = logging.getLogger(__name__)

_OPENAPI_FETCH_TIMEOUT = 15.0  # seconds


async def fetch_spec(url: str) -> dict[str, Any]:
    """Fetch an OpenAPI spec from a URL. Raises ValueError on SSRF or HTTP errors."""
    assert_safe_url(url)
    async with httpx.AsyncClient(timeout=_OPENAPI_FETCH_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    raw = resp.text
    return _parse_spec_text(raw, content_type)


def parse_spec_text(text: str) -> dict[str, Any]:
    """Parse a raw JSON or YAML OpenAPI spec string."""
    return _parse_spec_text(text, "")


def _parse_spec_text(text: str, content_type: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("{") or "application/json" in content_type:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    try:
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict):
            return parsed
    except yaml.YAMLError:
        pass
    raise ValueError("spec content is not valid JSON or YAML")


def extract_server_url(
    spec: dict[str, Any],
    base_url_override: Optional[str] = None,
    spec_fetch_url: Optional[str] = None,
) -> tuple[str, bool]:
    """Return (server_url, url_is_inferred).

    url_is_inferred=True means the URL was resolved from a relative path using
    the spec fetch URL as the origin — the admin should verify it is correct.
    """
    if base_url_override:
        return base_url_override.rstrip("/"), False
    servers = spec.get("servers", [])
    if servers and isinstance(servers, list):
        url = servers[0].get("url", "")
        if url:
            if url.startswith("/"):
                if spec_fetch_url:
                    from urllib.parse import urlparse
                    parsed = urlparse(spec_fetch_url)
                    return f"{parsed.scheme}://{parsed.netloc}{url}".rstrip("/"), True
                # Relative URL with no fetch URL to resolve against — unusable.
                return "", False
            return url.rstrip("/"), False
    return "", False


def extract_operations(spec: dict[str, Any], server_url: str) -> list[dict[str, Any]]:
    """Return a list of candidate operations from the spec paths."""
    ops = []
    paths = spec.get("paths", {})
    for path, path_item in paths.items():
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            op = path_item.get(method)
            if not op:
                continue
            operation_id = op.get("operationId", "")
            summary = op.get("summary", "") or op.get("description", "")
            suggested_name = _operation_id_to_tool_name(operation_id) if operation_id else _path_to_tool_name(method, path)
            schema = _build_parameters_schema(op, spec)
            ops.append({
                "operation_id": operation_id or f"{method}_{path}",
                "method": method.upper(),
                "path": path,
                "server_url": server_url,
                "suggested_name": suggested_name,
                "description": summary[:500] if summary else f"{method.upper()} {path}",
                "parameters_schema": schema,
            })
    log.debug("extracted %d operations from spec", len(ops))
    return ops


def _operation_id_to_tool_name(operation_id: str) -> str:
    # Convert camelCase / PascalCase / kebab-case → snake_case, truncate to 64 chars
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", operation_id)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[-\s]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if s and s[0].isdigit():
        s = "op_" + s
    return s[:64] or "unnamed_op"


def _path_to_tool_name(method: str, path: str) -> str:
    parts = [p for p in path.split("/") if p and not p.startswith("{")]
    slug = "_".join(parts).lower()
    slug = re.sub(r"[^a-z0-9_]", "_", slug)
    return f"{method}_{slug}"[:64] or f"{method}_op"


def _build_parameters_schema(op: dict, spec: dict) -> dict[str, Any]:
    """Build a JSON Schema object from the operation's parameters + requestBody."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in op.get("parameters", []):
        param = _resolve_ref(param, spec)
        pname = param.get("name", "")
        if not pname:
            continue
        schema = param.get("schema", {"type": "string"})
        schema = _resolve_ref(schema, spec)
        properties[pname] = {
            "type": schema.get("type", "string"),
            "description": param.get("description", ""),
        }
        if param.get("required"):
            required.append(pname)

    body = op.get("requestBody", {})
    if body:
        body = _resolve_ref(body, spec)
        content = body.get("content", {})
        json_schema = (
            content.get("application/json", {}).get("schema")
            or next(iter(content.values()), {}).get("schema")
        )
        if json_schema:
            json_schema = _resolve_ref(json_schema, spec)
            for prop_name, prop_schema in json_schema.get("properties", {}).items():
                prop_schema = _resolve_ref(prop_schema, spec)
                properties[prop_name] = {
                    "type": prop_schema.get("type", "string"),
                    "description": prop_schema.get("description", ""),
                }
            req = json_schema.get("required", [])
            required.extend(r for r in req if r not in required)
            if body.get("required"):
                required.extend(p for p in properties if p not in required)

    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _resolve_ref(obj: Any, spec: dict) -> Any:
    if not isinstance(obj, dict):
        return obj
    ref = obj.get("$ref")
    if not ref or not ref.startswith("#/"):
        return obj
    parts = ref.lstrip("#/").split("/")
    node: Any = spec
    for part in parts:
        if not isinstance(node, dict):
            return obj
        node = node.get(part)
    return node if node is not None else obj
