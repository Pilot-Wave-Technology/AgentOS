"""Generator for the TypeScript wizard.

Unlike the other three, this generator does NOT synthesize source — the
admin pastes it. The generator's job is to:
  1. Pass the source through unchanged
  2. Validate / pass through the admin-declared manifest
  3. Pass through the admin-declared parameters_schema

Wizard inputs:
    source_code:       str — admin-supplied TS, must define `async function execute(args)`
    manifest:          dict | None — admin-declared capability grants
    parameters_schema: dict | None — admin-declared schema for `args`

We do NOT inspect the source for static analysis here. If the admin
declares insufficient capabilities (e.g. they call net.fetch but didn't
grant net), the tool throws at runtime — clean failure mode.
"""
import logging

from app.services.custom_tools.generators import GeneratedTool, register

log = logging.getLogger(__name__)


def _generate(inputs: dict) -> GeneratedTool:
    source_code = inputs.get("source_code") or ""
    if not source_code.strip():
        raise ValueError("typescript: source_code is required")
    # The authoritative size cap lives in crud.create_custom_tool (200 KB) —
    # this is a fast-fail at generator time so admins get a clear error before
    # the request hits the DB layer.
    if len(source_code.encode()) > 200_000:
        raise ValueError("typescript: source_code exceeds 200 KB limit")

    manifest = inputs.get("manifest") or {}
    # Normalise — admin may submit just `{net: ...}` without the wrapping
    # `capabilities` key; either shape is accepted.
    if "capabilities" not in manifest and (
        "net" in manifest or "storage" in manifest or "secrets" in manifest or "mcp" in manifest
    ):
        manifest = {"capabilities": manifest}

    parameters_schema = inputs.get("parameters_schema") or {"type": "object", "properties": {}}

    caps = (manifest.get("capabilities") or {}).keys()
    log.info(
        "typescript generated: source_bytes=%d declared_capabilities=%s",
        len(source_code.encode()), list(caps),
    )

    return {
        "source_code": source_code,
        "manifest": manifest,
        "parameters_schema": parameters_schema,
    }


register("typescript", _generate)
