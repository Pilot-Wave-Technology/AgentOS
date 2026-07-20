"""Generator for the HTTP Action wizard.

Wizard inputs:
    url:                str — may contain {{param}} placeholders
    method:             "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
    headers:            dict[str, str] — static headers, no templating
    body_template:      dict | None — keys + values; string values may contain {{param}}
    credential:         {name, type, header_name?} | None
    parameters_schema:  dict — JSON Schema for what the agent passes

Returns capability-runtime source that uses `http.call` plus a manifest
declaring exactly:
  - net.hosts = [host parsed from url]
  - secrets.names = [credential.name] when a credential is bound

No other capabilities are granted.
"""
import json
import logging
from typing import Optional
from urllib.parse import urlparse

from app.services.custom_tools.generators import GeneratedTool, register

log = logging.getLogger(__name__)


_TEMPLATE_HELPER = r"""
function __renderTemplate(tmpl, args) {
  return String(tmpl).replace(/\{\{(\w+)\}\}/g, (_, k) => String(args[k] ?? ''));
}
function __renderTemplateObject(obj, args) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    out[k] = typeof v === 'string' ? __renderTemplate(v, args) : v;
  }
  return out;
}
""".strip()


def _generate(inputs: dict) -> GeneratedTool:
    url: str = inputs["url"]
    method: str = (inputs.get("method") or "GET").upper()
    headers: dict = dict(inputs.get("headers") or {})
    body_template: Optional[dict] = inputs.get("body_template")
    credential: Optional[dict] = inputs.get("credential")
    parameters_schema = inputs.get("parameters_schema") or {"type": "object", "properties": {}}

    host = urlparse(url).hostname
    if not host:
        raise ValueError(f"http_action: cannot parse hostname from URL {url!r}")

    # ─── Source code ────────────────────────────────────────────────
    lines: list[str] = [_TEMPLATE_HELPER, ""]
    lines.append("async function execute(args) {")
    lines.append(f"  const url = __renderTemplate({json.dumps(url)}, args);")
    lines.append(f"  const headers = {json.dumps(headers)};")

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
            # btoa is part of the standard library in modern Node and available in vm contexts.
            lines.append("  headers['Authorization'] = `Basic ${btoa(__secret)}`;")
        else:
            raise ValueError(f"http_action: unsupported credential type {ctype!r}")

    if body_template:
        lines.append(f"  const body = __renderTemplateObject({json.dumps(body_template)}, args);")
        lines.append(
            f"  return await http.call(url, {{ method: {json.dumps(method)}, headers, body }});"
        )
    else:
        lines.append(
            f"  return await http.call(url, {{ method: {json.dumps(method)}, headers }});"
        )
    lines.append("}")
    source_code = "\n".join(lines)

    # ─── Manifest ───────────────────────────────────────────────────
    manifest: dict = {"capabilities": {"net": {"hosts": [host]}}}
    if credential:
        manifest["capabilities"]["secrets"] = {"names": [credential["name"]]}

    log.info(
        "http_action generated: method=%s host=%s body_template=%s credential=%s source_bytes=%d",
        method, host, bool(body_template), bool(credential), len(source_code.encode()),
    )

    return {
        "source_code": source_code,
        "manifest": manifest,
        "parameters_schema": parameters_schema,
    }


register("http_action", _generate)
