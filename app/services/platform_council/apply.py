"""Execution surface for the Platform Research Council's 4 no-code operations.

Each function wraps EXISTING platform machinery; all are Postgres writes plus a
runtime sync RPC (no deploy):
  apply_model_change   — UPDATE templates.model (template-only)
  apply_add_skill      — INSERT skills (+history) + skill_sync fan-out
  apply_create_credential — scaffold a credential shell (secret entered by a human)
  apply_add_tool       — tier-generate → create_custom_tool + tools.reload (may
                         scaffold + reference a credential)

Called by /internal/platform/apply/* (platform_internal), which the platform SHCA
invokes via the apply_* tools. Each returns {ok, ...} or {ok:false, reason}.
"""
from __future__ import annotations

import hashlib
import json
import logging

from app.database import get_pool

log = logging.getLogger(__name__)

_CRED_TYPES = ("bearer", "api_key", "basic")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 1. Change an agent template's model (template-only) ───────────────────────

async def apply_model_change(*, template_id: str, model: str) -> dict:
    from app.llm_models import is_supported
    if not is_supported(model):
        return {"ok": False, "reason": f"model {model!r} is not in the supported list"}
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id, model FROM templates WHERE id=$1 AND status='active'", template_id)
    if not row:
        return {"ok": False, "reason": f"template {template_id!r} not found"}
    old = row["model"] or ""
    if old == model:
        return {"ok": True, "template_id": template_id, "model": model, "unchanged": True}
    await pool.execute("UPDATE templates SET model=$1 WHERE id=$2", model, template_id)
    log.info("platform apply: template %s model %s -> %s", template_id, old, model)
    return {"ok": True, "template_id": template_id, "old_model": old, "model": model,
            "note": "Template model updated; future instances use it (live instances unchanged)."}


# ── 2. Add a skill ────────────────────────────────────────────────────────────

async def apply_add_skill(*, name: str, title: str, description: str, content: str,
                          applicable_to: list[str], created_by: str = "platform-shca") -> dict:
    name = (name or "").strip()
    content = (content or "").strip() + "\n"
    applicable_to = [str(a).strip() for a in (applicable_to or []) if str(a).strip()]
    if not (name and description and content.strip() and applicable_to):
        return {"ok": False, "reason": "name, description, content, and applicable_to are all required"}
    content_hash = _sha256(content)
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if await conn.fetchval("SELECT 1 FROM skills WHERE name=$1", name):
                    return {"ok": False, "reason": f"skill {name!r} already exists"}
                await conn.execute(
                    """INSERT INTO skills
                         (name, title, description, content, content_hash, version,
                          applicable_to, source_path, status, created_at, updated_at)
                       VALUES ($1,$2,$3,$4,$5,1,$6,NULL,'active',NOW(),NOW())""",
                    name, title or name, description, content, content_hash, applicable_to)
                await conn.execute(
                    """INSERT INTO skills_history
                         (skill_name, version, title, description, content, content_hash,
                          applicable_to, status, changed_by, change_reason)
                       VALUES ($1,1,$2,$3,$4,$5,$6,'active',$7,'created via platform integration')""",
                    name, title or name, description, content, content_hash, applicable_to, created_by)
    except Exception as exc:
        log.exception("platform apply: add_skill %s failed", name)
        return {"ok": False, "reason": f"skill insert failed: {exc}"}
    try:
        from app.services.skill_sync import schedule_propagation
        schedule_propagation(skill_name=name, content=content, old_applicable_to=[],
                             new_applicable_to=applicable_to, new_status="active")
    except Exception:
        log.exception("platform apply: skill propagation schedule failed for %s", name)
    log.info("platform apply: skill %s added (applicable_to=%s)", name, applicable_to)
    return {"ok": True, "name": name, "applicable_to": applicable_to}


# ── 3. Create a credential (scaffold only — human enters the secret) ─────────

async def apply_create_credential(*, name: str, credential_type: str,
                                  header_name: str | None = None,
                                  created_by: str = "platform-shca") -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "reason": "name required"}
    if credential_type not in _CRED_TYPES:
        return {"ok": False, "reason": f"credential_type must be one of {_CRED_TYPES}"}
    if credential_type == "api_key" and not header_name:
        return {"ok": False, "reason": "api_key credentials require a header_name"}
    pool = get_pool()
    try:
        from app.services.custom_tools.crud import scaffold_tool_credential
        cred = await scaffold_tool_credential(
            pool, name=name, credential_type=credential_type,
            header_name=header_name, created_by=created_by)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "credential": {"id": str(cred["id"]), "name": cred["name"], "status": "pending"},
            "note": ("Credential scaffolded WITHOUT a secret. A human must enter the secret in "
                     "Settings → Credentials before any tool using it can run.")}


# ── 4. Add a tool / MCP server (may scaffold + reference a credential) ────────

async def apply_add_tool(*, tier: str, name: str, label: str, description: str,
                         spec: dict, credential: dict | None = None,
                         created_by: str = "platform-shca") -> dict:
    """Add a custom tool. `spec` carries the tier-specific generator inputs.
    If `credential` ({name,type,header_name}) is given, a pending credential is
    scaffolded first and the tool is wired to it (secret entered by a human later).
    """
    name = (name or "").strip()
    spec = spec or {}
    if not name or not label:
        return {"ok": False, "reason": "name and label required"}
    if tier not in ("mcp_server", "http_action", "openapi", "typescript"):
        return {"ok": False, "reason": f"tier {tier!r} not supported (use "
                "'http_action', 'mcp_server', 'openapi', or 'typescript')"}
    pool = get_pool()
    from app.services.custom_tools.crud import (
        get_custom_tool, create_custom_tool, delete_tool_credential)
    if await get_custom_tool(pool, name):
        return {"ok": False, "reason": f"tool {name!r} already exists"}

    # Normalize mcp tools: accept ["search","fetch"] OR [{name, input_schema}]
    # (an LLM naturally sends a list of names) → the {name, description,
    # input_schema} shape the generator requires.
    mcp_tools: list[dict] = []
    if tier == "mcp_server":
        for t in (spec.get("tools") or []):
            if isinstance(t, str) and t.strip():
                mcp_tools.append({"name": t.strip(), "description": "", "input_schema": {}})
            elif isinstance(t, dict) and t.get("name"):
                mcp_tools.append({"name": t["name"], "description": t.get("description", ""),
                                  "input_schema": t.get("input_schema") or {}})
            else:
                return {"ok": False, "reason": "each mcp tool must be a name string or a "
                        "{name, input_schema} object"}
        if not mcp_tools:
            return {"ok": False, "reason": "mcp_server requires spec.tools — a non-empty list "
                    "of the server's tool names"}

    # Optional credential: scaffold a pending shell + pass its meta to the generator.
    credential_id = None
    cred_meta = None
    cred_note = ""
    if credential:
        cred_res = await apply_create_credential(
            name=credential.get("name") or f"{name}-credential",
            credential_type=credential.get("type") or "bearer",
            header_name=credential.get("header_name"), created_by=created_by)
        if not cred_res.get("ok"):
            return {"ok": False, "reason": f"credential scaffold failed: {cred_res.get('reason')}"}
        credential_id = cred_res["credential"]["id"]
        cred_meta = {"name": cred_res["credential"]["name"],
                     "type": credential.get("type") or "bearer",
                     "header_name": credential.get("header_name")}
        cred_note = (f" Credential '{cred_meta['name']}' scaffolded (pending) — a human must "
                     f"enter its secret in Settings → Credentials before the tool can run.")

    async def _cleanup_credential():
        # Roll back the scaffolded credential if the tool never got created, so a
        # retry isn't blocked by a leftover pending credential of the same name.
        if credential_id:
            try:
                await delete_tool_credential(pool, credential_id)
            except Exception:
                log.warning("platform apply: could not clean up scaffolded credential %s "
                            "after add_tool failure", credential_id)

    # Tier generator → source_code + manifest; per-tier config mirrors the wizard.
    try:
        if tier == "mcp_server":
            from app.services.custom_tools.generators.mcp_server import _generate
            gen_inputs = {"server_url": spec.get("server_url"),
                          "transport": spec.get("transport", "sse"),
                          "tools": mcp_tools, "tool_label": label, "credential": cred_meta}
            gen = _generate(gen_inputs)
            config = {"transport": spec.get("transport", "sse"),
                      "server_url": spec.get("server_url"), "tools": mcp_tools}
        elif tier == "http_action":
            from app.services.custom_tools.generators.http_action import _generate
            gen_inputs = {"url": spec.get("url"), "method": spec.get("method", "POST"),
                          "headers": spec.get("headers") or {},
                          "body_template": spec.get("body_template"), "credential": cred_meta,
                          "parameters_schema": spec.get("parameters_schema") or {"type": "object", "properties": {}}}
            gen = _generate(gen_inputs)
            config = {"url": spec.get("url"), "method": spec.get("method", "POST"),
                      "headers": spec.get("headers") or {}, "body_template": spec.get("body_template")}
        elif tier == "openapi":
            # Import selected operations from an OpenAPI-described service. The
            # agent must resolve the concrete operations (method + path) itself
            # (read the spec / docs) and pass them explicitly.
            from app.services.custom_tools.generators.openapi import _generate
            operations = spec.get("operations") or []
            if not operations:
                await _cleanup_credential()
                return {"ok": False, "reason": "openapi requires spec.operations — a non-empty "
                        "list of {operation_id, method, path, description}"}
            gen_inputs = {"server_url": spec.get("server_url"), "operations": operations,
                          "tool_label": label, "credential": cred_meta}
            gen = _generate(gen_inputs)
            config = {"server_url": spec.get("server_url"), "operations": operations}
        else:  # typescript (Deno sandbox) — agent supplies the source
            from app.services.custom_tools.generators.typescript import _generate
            source = spec.get("source_code") or ""
            if not source.strip():
                await _cleanup_credential()
                return {"ok": False, "reason": "typescript requires spec.source_code — TS that "
                        "defines `async function execute(args)`"}
            gen_inputs = {"source_code": source, "manifest": spec.get("manifest") or {},
                          "parameters_schema": spec.get("parameters_schema")
                          or {"type": "object", "properties": {}}}
            gen = _generate(gen_inputs)
            config = {"runtime": "deno"}
    except Exception as exc:
        await _cleanup_credential()
        return {"ok": False, "reason": f"tool generation failed: {exc}"}

    try:
        await create_custom_tool(
            pool, name=name, label=label, description=description, tier=tier,
            parameters_schema=gen["parameters_schema"], config=config,
            source_code=gen["source_code"], created_by=created_by,
            credential_id=credential_id, manifest=gen["manifest"])
    except ValueError as exc:
        await _cleanup_credential()
        return {"ok": False, "reason": str(exc)}
    except Exception as exc:
        # Any other failure (e.g. DB error) must still roll back the scaffolded
        # credential, or a leftover pending shell of the same name blocks a retry.
        await _cleanup_credential()
        log.exception("platform apply: create_custom_tool failed for %s", name)
        return {"ok": False, "reason": f"tool creation failed: {exc}"}

    # Make the gateway pick it up (no redeploy).
    try:
        from app.openclaw import gateway_admin
        await gateway_admin.call("tools.reload", {}, timeout=30)
    except Exception:
        log.warning("platform apply: tools.reload failed for %s (5-min refresh will catch up)", name)
    log.info("platform apply: tool %s added (tier=%s, credential=%s)", name, tier, bool(credential))
    return {"ok": True, "name": name, "tier": tier,
            "credential_id": credential_id, "note": (cred_note or "Tool added and live.").strip()}
