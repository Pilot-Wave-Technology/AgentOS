"""Unified memory_v2 provisioning hook.

Single entry point that every SHCA + GO creation path calls after the
gateway agent has been created. Handles all the memory_v2 setup steps:

  - SHCA: USER_MEMORY.md cross-goal symlink, workspace-files restore,
          MEMORY.md scaffold + memory/ dir seeding, per-agent flush prompt push
  - GO:   MEMORY.md scaffold + memory/ dir seeding, per-agent flush
          prompt push (no USER_MEMORY.md, no snapshot restore — GOs are
          goal-scoped, not user-scoped)
  - IA:   no-op (IAs are ephemeral, no memory)

All steps are feature-flagged (via settings.memory_v2_*) and best-effort.
Failure of any one step is logged and skipped; provisioning continues so
agent creation can't be blocked by a memory_v2 issue.

This is the ONLY place memory_v2 hooks should be wired into provisioning.
Adding new SHCA/GO creation paths? Call this helper from there.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import get_settings


_log = logging.getLogger(__name__)


GO_MEMORY_SEED = """# GO Memory

<!-- Durable long-term memory for the Goal Orchestrator. -->
<!-- Memory-flush turns append distilled (≤500 char) insights here. -->
<!-- Consolidation fires automatically when this file exceeds 30KB. -->

## Patterns
<!-- Story sizing, dependency patterns, team capacity observations. -->
<!-- e.g. "Backend stories average 3-4 days when scoped <500 LOC." -->

## Decisions
<!-- Major orchestration decisions and their rationale. -->
<!-- e.g. "PSF01: split planning vs execution stories to reduce rework." -->

## Stakeholders & Constraints
<!-- Deadlines, compliance gates, sign-off requirements. -->
<!-- e.g. "Compliance review required for any auth changes." -->

## Recent
<!-- Auto-curated recent insights, oldest first. Consolidation will -->
<!-- promote stable items into the sections above. -->
"""

SHCA_MEMORY_SEED = """# SHCA Memory

<!-- Durable long-term memory for the System Human Coordination Agent. -->
<!-- Memory-flush turns append distilled (≤500 char) insights here. -->
<!-- Consolidation fires automatically when this file exceeds 30KB. -->

## User preferences
<!-- Communication style, escalation thresholds, format preferences. -->

## Project conventions
<!-- Repository conventions, naming, branch strategy specific to this user/goal. -->

## Working context
<!-- Active mental models, in-progress decisions still being shaped. -->

## Recent
<!-- Auto-curated recent insights, oldest first. -->
"""


async def _seed_memory_files(agent_id: str, agent_type: str) -> None:
    """Provision MEMORY.md in the agent's workspace.

    Seeded at agent creation so memory-flush turns have an existing
    MEMORY.md to append distilled insights into.

    The memory/ directory is NOT pre-created here — the gateway's
    agents.files.set allowlist (BOOTSTRAP_FILE_NAMES + MEMORY_FILE_NAMES)
    rejects nested paths like memory/.gitkeep. Instead, the flush
    write-wrapper at pi-tools.read.ts appendMemoryFlushContent calls
    fs.mkdir(..., {recursive: true}) before writing
    memory/YYYY-MM-DD.md, so the directory gets created lazily on the
    first flush write. That path is sufficient for our needs.

    set_agent_file overwrites if the file exists. The original code
    assumed this only ran at first creation — but it ALSO runs on
    re-provision paths: the SHCA ghost-recovery branch in
    openclaw._provision_shca_instance fires whenever agent_files is empty
    (always true for these agents), and a control-panel restart rebuilds
    the session. Unguarded, that re-seed clobbers the MEMORY.md the agent
    has been appending to via flush turns. So we read the current
    MEMORY.md first and SKIP the seed when it already holds real
    (non-scaffold) content — seeding is a one-time provisioning step; the
    flush tools own MEMORY.md thereafter. (The GO path,
    openclaw._ensure_gateway_agent, already gates on `created`, which is
    why GO memory survives deploys; this brings SHCA parity.)
    """
    from app.openclaw import gateway_admin

    seed = GO_MEMORY_SEED if agent_type == "GO" else SHCA_MEMORY_SEED

    # Guard: never clobber an existing populated MEMORY.md. Treat absent /
    # empty / exact-scaffold content as "not yet seeded" (seed it); anything
    # else is accumulated memory and must be preserved. The read API
    # (agents.files.get) result shape can vary, so parse defensively; on any
    # error (e.g. file not present) fall through and seed — safe for new agents.
    try:
        existing = await gateway_admin.get_agent_file(agent_id, "MEMORY.md")
        existing_content = ""
        if isinstance(existing, dict):
            existing_content = (
                existing.get("content")
                or (existing.get("file") or {}).get("content")
                or existing.get("text")
                or ""
            )
        elif isinstance(existing, str):
            existing_content = existing
        if existing_content.strip() and existing_content.strip() != seed.strip():
            _log.info(
                "memory_v2.setup: MEMORY.md already populated for %s (%d chars) "
                "— skipping seed to preserve accumulated memory.",
                agent_id, len(existing_content),
            )
            return
    except Exception:
        _log.debug(
            "memory_v2.setup: MEMORY.md read failed for %s (treating as absent) "
            "— will seed.", agent_id,
        )

    await gateway_admin.set_agent_file(agent_id, "MEMORY.md", seed)
    _log.info(
        "memory_v2.setup: seeded MEMORY.md for %s (type=%s); memory/ will be "
        "created lazily on first flush write.",
        agent_id, agent_type,
    )


async def setup_memory_v2_for_agent(
    *,
    agent_id: str,
    agent_type: str,
    user_uid: Optional[str] = None,
) -> None:
    """Run all memory_v2 hooks for a newly-created gateway agent.

    Idempotent for already-set-up agents (each step does its own check).

    Args:
        agent_id: gateway agent id (e.g. "prateek-shca-PSF01-...")
        agent_type: "SHCA" | "GO" | "IA" — drives which hooks fire
        user_uid: required for SHCA (cross-goal USER_MEMORY.md target);
                  ignored for GO / IA
    """
    if agent_type not in ("SHCA", "GO", "IA"):
        _log.warning(
            "setup_memory_v2_for_agent: unknown agent_type=%r — skipping (agent=%s)",
            agent_type, agent_id,
        )
        return
    if agent_type == "IA":
        return  # IAs are ephemeral; no memory_v2 state

    settings = get_settings()

    # 1. USER_MEMORY.md symlink — SHCA only, requires user_uid.
    if agent_type == "SHCA" and getattr(settings, "memory_v2_user_memory_enabled", False):
        if not user_uid:
            _log.warning(
                "memory_v2.setup: SHCA %s has no user_uid — USER_MEMORY.md skipped",
                agent_id,
            )
        else:
            try:
                from app.memory_v2 import user_memory as _user_memory
                await _user_memory.setup_user_memory_for_agent(
                    agent_id=agent_id, user_uid=user_uid,
                )
            except Exception:
                _log.exception(
                    "memory_v2.setup: USER_MEMORY.md setup failed for %s — non-fatal",
                    agent_id,
                )

    # 2. Workspace files restore — SHCA only, only if a prior snapshot exists.
    if agent_type == "SHCA" and getattr(settings, "memory_v2_snapshot_enabled", False):
        try:
            from app.memory_v2 import snapshot as _snapshot
            if await _snapshot.has_prior_snapshot(agent_id):
                restored = await _snapshot.restore_agent_workspace(agent_id)
                _log.info(
                    "memory_v2.setup: restored %d workspace files for %s",
                    restored, agent_id,
                )
        except Exception:
            _log.exception(
                "memory_v2.setup: snapshot restore failed for %s — non-fatal",
                agent_id,
            )

    # 3. MEMORY.md + memory/ scaffold seeding — SHCA + GO. Idempotent at
    # the agent-creation boundary (set_agent_file overwrites, but this is
    # only called on FIRST creation, so it never clobbers accumulated
    # MEMORY.md content for an existing agent).
    if getattr(settings, "memory_v2_flush_enabled", False):
        try:
            await _seed_memory_files(agent_id=agent_id, agent_type=agent_type)
        except Exception:
            _log.exception(
                "memory_v2.setup: MEMORY.md seeding failed for %s (type=%s) — non-fatal",
                agent_id, agent_type,
            )

    # 4. Per-agent flush prompts push — SHCA + GO.
    if getattr(settings, "memory_v2_flush_enabled", False):
        try:
            from app.memory_v2 import prompts as _prompts
            await _prompts.apply_prompts_to_new_agent(
                agent_id=agent_id, agent_type=agent_type,
            )
        except Exception:
            _log.exception(
                "memory_v2.setup: prompt push failed for %s (type=%s) — non-fatal",
                agent_id, agent_type,
            )
