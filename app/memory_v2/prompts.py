"""Versioned prompt storage for SHCA + GO memory flush + compaction prompts.

Backed by the `memory_prompts` PG table (migration 062). The admin Memory
UI calls these helpers; SHCA + GO provisioning reads the active prompts
to populate per-agent compaction config on the gateway.

Schema is one row per (agent_type, prompt_kind, version), with exactly
one is_active=TRUE row per (agent_type, prompt_kind). New saves create a
new version row; activate flips is_active atomically.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from app.database import get_pool


_log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

AGENT_TYPES = ("SHCA", "GO")

# Must match the CHECK constraint in migration 062.
PROMPT_KINDS = (
    "flush_user_prompt",
    "flush_system_prompt",
    "compaction_summary_system_prompt",
    "story_complete_user_prompt",
    "story_complete_system_prompt",
    "pre_reap_user_prompt",
    "pre_reap_system_prompt",
    # MEMORY.md auto-consolidation prompts (Tier 2 of memory redesign).
    # Fire when the workspace MEMORY.md grows past memoryFileMaxChars.
    # Distinct from regular flush: focus is DROP + MERGE, not capture.
    "consolidation_user_prompt",
    "consolidation_system_prompt",
    # Rolling per-goal summary (migration 076). Reconciles the messages
    # since the last checkpoint + the authoritative story list into one
    # living "state of the goal" summary. GO-only.
    "goal_summary_user_prompt",
    "goal_summary_system_prompt",
)

# Per-agent-type valid prompt kinds. GO has no pre-reap variant because
# GOs aren't reaped by the SHCA cleanup watchdog.
PROMPT_KINDS_BY_AGENT_TYPE = {
    "SHCA": PROMPT_KINDS,
    "GO": tuple(k for k in PROMPT_KINDS if not k.startswith("pre_reap_")),
}


# ── Read API ─────────────────────────────────────────────────────────────────

async def get_active_prompt(agent_type: str, prompt_kind: str) -> Optional[str]:
    """Return the currently active prompt content, or None if no active row.

    Looks up `memory_prompts WHERE agent_type=$1 AND prompt_kind=$2
    AND is_active=TRUE`. The partial unique index guarantees at most one
    row matches.
    """
    if agent_type not in AGENT_TYPES:
        raise ValueError(f"invalid agent_type: {agent_type!r}")
    if prompt_kind not in PROMPT_KINDS:
        raise ValueError(f"invalid prompt_kind: {prompt_kind!r}")

    pool = get_pool()
    return await pool.fetchval(
        """SELECT content FROM memory_prompts
            WHERE agent_type = $1 AND prompt_kind = $2 AND is_active = TRUE""",
        agent_type, prompt_kind,
    )


async def get_all_active_prompts() -> dict[tuple[str, str], str]:
    """Return all active prompts as { (agent_type, prompt_kind): content }.

    Used by SHCA + GO provisioning to push compaction config to a new
    agent in one query.
    """
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT agent_type, prompt_kind, content FROM memory_prompts
            WHERE is_active = TRUE"""
    )
    return {(r["agent_type"], r["prompt_kind"]): r["content"] for r in rows}


async def list_versions(agent_type: str, prompt_kind: str, limit: int = 50) -> list[dict]:
    """Return version history for (agent_type, prompt_kind), most recent first.

    Each dict has: id, version, content, is_active, created_by,
    created_at, notes.
    """
    if agent_type not in AGENT_TYPES:
        raise ValueError(f"invalid agent_type: {agent_type!r}")
    if prompt_kind not in PROMPT_KINDS:
        raise ValueError(f"invalid prompt_kind: {prompt_kind!r}")

    pool = get_pool()
    rows = await pool.fetch(
        """SELECT id, version, content, is_active, created_by, created_at, notes
             FROM memory_prompts
            WHERE agent_type = $1 AND prompt_kind = $2
            ORDER BY version DESC
            LIMIT $3""",
        agent_type, prompt_kind, limit,
    )
    return [dict(r) for r in rows]


async def get_version_by_id(prompt_id: uuid.UUID) -> Optional[dict]:
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT id, agent_type, prompt_kind, version, content,
                  is_active, created_by, created_at, notes
             FROM memory_prompts WHERE id = $1""",
        prompt_id,
    )
    return dict(row) if row else None


# ── Write API (admin only — gated by router) ─────────────────────────────────

async def save_new_version(
    *,
    agent_type: str,
    prompt_kind: str,
    content: str,
    created_by: uuid.UUID,
    notes: Optional[str] = None,
    activate: bool = False,
) -> dict:
    """Create a new version row. If activate=True, atomically flip is_active.

    Returns the new row as a dict.
    """
    if agent_type not in AGENT_TYPES:
        raise ValueError(f"invalid agent_type: {agent_type!r}")
    if prompt_kind not in PROMPT_KINDS_BY_AGENT_TYPE[agent_type]:
        raise ValueError(
            f"prompt_kind {prompt_kind!r} not valid for agent_type {agent_type!r}"
        )
    if not content.strip():
        raise ValueError("content cannot be empty")

    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        next_version = await conn.fetchval(
            """SELECT COALESCE(MAX(version), 0) + 1 FROM memory_prompts
                WHERE agent_type = $1 AND prompt_kind = $2""",
            agent_type, prompt_kind,
        )

        if activate:
            # Deactivate the current active row (if any) first
            await conn.execute(
                """UPDATE memory_prompts SET is_active = FALSE
                    WHERE agent_type = $1 AND prompt_kind = $2 AND is_active = TRUE""",
                agent_type, prompt_kind,
            )

        row = await conn.fetchrow(
            """INSERT INTO memory_prompts
                 (agent_type, prompt_kind, version, content, is_active,
                  created_by, notes)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id, agent_type, prompt_kind, version, content,
                         is_active, created_by, created_at, notes""",
            agent_type, prompt_kind, next_version, content, activate,
            created_by, notes,
        )

        _log.info(
            "memory_prompts: saved %s/%s v%d (active=%s) by %s",
            agent_type, prompt_kind, next_version, activate, created_by,
        )
        return dict(row)


async def activate_version(prompt_id: uuid.UUID, actor_id: uuid.UUID) -> dict:
    """Atomically flip is_active on the target row, deactivating the prior
    active row for the same (agent_type, prompt_kind).

    Returns the activated row.
    """
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        target = await conn.fetchrow(
            """SELECT id, agent_type, prompt_kind, is_active
                 FROM memory_prompts WHERE id = $1 FOR UPDATE""",
            prompt_id,
        )
        if not target:
            raise ValueError(f"prompt not found: {prompt_id}")
        if target["is_active"]:
            return dict(target)  # idempotent — already active

        await conn.execute(
            """UPDATE memory_prompts SET is_active = FALSE
                WHERE agent_type = $1 AND prompt_kind = $2
                  AND is_active = TRUE""",
            target["agent_type"], target["prompt_kind"],
        )
        row = await conn.fetchrow(
            """UPDATE memory_prompts SET is_active = TRUE
                WHERE id = $1
               RETURNING id, agent_type, prompt_kind, version, content,
                         is_active, created_by, created_at, notes""",
            prompt_id,
        )

        _log.info(
            "memory_prompts: activated %s/%s v%d by %s",
            row["agent_type"], row["prompt_kind"], row["version"], actor_id,
        )
        return dict(row)


# ── Substitution helper ──────────────────────────────────────────────────────

def render_prompt(template: str, context: dict) -> str:
    """Light Python str.format() substitution for prompt templates.

    Templates use {placeholder} syntax. Missing keys are left as-is
    (defensive — better to render a slightly off prompt than to crash
    a flush turn). Returns the rendered text.
    """
    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    try:
        return template.format_map(_SafeDict(context))
    except Exception:
        _log.exception("render_prompt failed; returning unrendered template")
        return template


# ── Gateway sync ─────────────────────────────────────────────────────────────

# Per-agent memory-system tunings pushed alongside the flush prompts.
# These default values can be overridden at the call-site (e.g. SHCA gets
# different headroom than IA workers) but defaults work for our
# long-lived assistant model.
DEFAULT_SOFT_THRESHOLD_PCT = 0.20            # fire flush at 80% of context window
DEFAULT_COMPACTION_SUMMARY_MAX_TOKENS = 5000
DEFAULT_COMPACTION_SUMMARY_MAX_CHARS = 20000
# MEMORY.md auto-consolidation thresholds: fires a silent consolidation
# turn (different prompt than regular flush) when the file grows past
# memoryFileMaxChars, targeting memoryFileTargetChars (80% of cap).
DEFAULT_MEMORY_FILE_MAX_CHARS = 30000
DEFAULT_MEMORY_FILE_TARGET_CHARS = 24000
# Compaction summarizer model. Gemini Flash by default — cheap (~$0.20
# per compaction vs $1-2 for Pro) and plenty good for narrative summary.
# Per-agent override allowed via the same admin path.
DEFAULT_SUMMARIZER_MODEL = "google/gemini-3.8-flash"
# Activate upstream LLM-based safeguard pathway when Pi auto-compacts.
# "default" leaves Pi's built-in truncation in place; "safeguard" engages
# the openclaw fork's compaction-safeguard extension which runs an LLM
# summarization with quality audits.
DEFAULT_COMPACTION_MODE = "safeguard"


async def build_compaction_config_for_agent_type(
    agent_type: str,
    *,
    soft_threshold_pct: float = DEFAULT_SOFT_THRESHOLD_PCT,
    summary_max_tokens: int = DEFAULT_COMPACTION_SUMMARY_MAX_TOKENS,
    summary_max_chars: int = DEFAULT_COMPACTION_SUMMARY_MAX_CHARS,
    memory_file_max_chars: int = DEFAULT_MEMORY_FILE_MAX_CHARS,
    memory_file_target_chars: int = DEFAULT_MEMORY_FILE_TARGET_CHARS,
    summarizer_model: str = DEFAULT_SUMMARIZER_MODEL,
    compaction_mode: str = DEFAULT_COMPACTION_MODE,
) -> Optional[dict]:
    """Read the active flush prompts for an agent type from PG and return
    the gateway-shaped `compaction` config object.

    Shape matches `AgentEntrySchema.compaction` in agent_os_openclaw's
    zod-schema.agent-runtime.ts (memory_v2 patch). Returns None if no
    flush prompts are active for this agent type — caller should treat
    that as "use gateway defaults."

      {
        "memoryFlush": {
          "prompt":           "<flush_user_prompt content>",
          "systemPrompt":     "<flush_system_prompt content>",
          "softThresholdPct": 0.20,
          "compactionSummary": {"maxTokens": 5000, "maxChars": 20000}
        }
      }
    """
    if agent_type not in AGENT_TYPES:
        return None

    flush_user = await get_active_prompt(agent_type, "flush_user_prompt")
    flush_system = await get_active_prompt(agent_type, "flush_system_prompt")
    consolidation_user = await get_active_prompt(agent_type, "consolidation_user_prompt")
    consolidation_system = await get_active_prompt(agent_type, "consolidation_system_prompt")
    summary_system = await get_active_prompt(agent_type, "compaction_summary_system_prompt")
    if not flush_user and not flush_system:
        return None

    cfg: dict = {"memoryFlush": {}}
    # Top-level compaction tunings: mode + summarizer model + summary prompt.
    # These activate Pi's safeguard pathway and let our per-agent prompt
    # + chosen model drive the summarization at auto-compaction time.
    cfg["mode"] = compaction_mode
    cfg["summarizerModel"] = summarizer_model
    if summary_system:
        cfg["summaryPrompt"] = summary_system
    if flush_user:
        cfg["memoryFlush"]["prompt"] = flush_user
    if flush_system:
        cfg["memoryFlush"]["systemPrompt"] = flush_system
    cfg["memoryFlush"]["softThresholdPct"] = soft_threshold_pct
    cfg["memoryFlush"]["compactionSummary"] = {
        "maxTokens": summary_max_tokens,
        "maxChars": summary_max_chars,
    }
    cfg["memoryFlush"]["memoryFileMaxChars"] = memory_file_max_chars
    cfg["memoryFlush"]["memoryFileTargetChars"] = memory_file_target_chars
    if consolidation_user:
        cfg["memoryFlush"]["consolidationPrompt"] = consolidation_user
    if consolidation_system:
        cfg["memoryFlush"]["consolidationSystemPrompt"] = consolidation_system
    return cfg


async def apply_prompts_to_new_agent(agent_id: str, agent_type: str) -> bool:
    """Push the active compaction prompts to a freshly-created gateway agent.

    Best-effort. Called from SHCA + GO provisioning right after the
    gateway agent is created. Returns True if the gateway accepted the
    update; False on any failure (provisioning continues — the agent
    just uses the gateway defaults until prompts are pushed manually).
    """
    cfg = await build_compaction_config_for_agent_type(agent_type)
    if not cfg:
        _log.info(
            "memory_v2.prompts: no active flush prompts for %s — skipping push for %s",
            agent_type, agent_id,
        )
        return False

    try:
        from app.openclaw import gateway_admin
        await gateway_admin.call(
            "agents.update",
            {"agentId": agent_id, "compaction": cfg},
            timeout=15,
        )
        _log.info(
            "memory_v2.prompts: pushed flush prompts to %s (type=%s)",
            agent_id, agent_type,
        )
        return True
    except Exception as exc:
        # "agent not found" is benign: the DB still has an active row for an
        # agent that no longer exists on the gateway (e.g. removed in a rebuild
        # while its goal stayed active). Log it quietly without a traceback so it
        # doesn't read as an error; everything else gets the full exception.
        if "not found" in str(exc).lower():
            _log.warning(
                "memory_v2.prompts: skip %s (type=%s) — not present on gateway",
                agent_id, agent_type,
            )
        else:
            _log.exception(
                "memory_v2.prompts: push failed for %s (type=%s) — agent will "
                "use gateway defaults until re-pushed",
                agent_id, agent_type,
            )
        return False
