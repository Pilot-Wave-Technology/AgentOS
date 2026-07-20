"""Activation layer for the Platform Research Council v2.

- ensure_platform_council(): idempotent startup hook — seeds the registry tools,
  gives the __platform__ goal a thread bound to the steward via the normal
  goal-thread machinery, and provisions the Sonnet-200k steward.
- worker_reaper_loop(): periodic subagent reaping (producers + reviewers).
- review_backstop_loop(): event-driven integration review of orphan-pending findings.
- scheduler_loop(): autonomous scheduled sweeps — OFF by default.

All gated on settings.platform_council_enabled so it stays dormant until enabled
per-environment (dev first).
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from app.config import get_settings
from app.database import get_pool
from app.openclaw import gateway_admin
from app.services.platform_council import PLATFORM_GOAL_ID, STEWARD_AGENT_ID
from app.services.platform_council.provisioning import create_platform_steward_for_goal
from app.services.platform_council import worker as _worker

log = logging.getLogger(__name__)

_REAP_INTERVAL_SECONDS = 60


def is_enabled() -> bool:
    return bool(getattr(get_settings(), "platform_council_enabled", False))


async def ensure_platform_council() -> None:
    """Idempotent: ensure the registry tools are seeded, the __platform__ goal has
    a thread bound to the steward, and the steward is provisioned. Safe to call on
    every startup."""
    if not is_enabled():
        return
    pool = get_pool()
    goal = await pool.fetchrow(
        "SELECT id, name, go_thread_id, go_agent_id FROM goals WHERE id=$1",
        PLATFORM_GOAL_ID,
    )
    if not goal:
        log.warning("platform: __platform__ goal missing — run platform_council_goal_seed.sql")
        return

    # 0. Decide whether to recreate the platform agents (steward + platform-shca)
    #    from scratch this boot. This is the only reliable way to refresh their
    #    gateway tool scope — agents.update's `tools` field is a no-op on the gateway,
    #    only agents.create honors it. Gated on the build sha so it fires ONCE per
    #    deploy (an atomic claim on platform_recreate_state), not per container start.
    #    Flag off (PLATFORM_AGENTS_RECREATE_ON_BUILD=false) → never recreate.
    settings = get_settings()
    force_recreate = False
    if getattr(settings, "platform_agents_recreate_on_build", False):
        sha = (getattr(settings, "platform_build_sha", "") or "").strip()
        if not sha:
            log.warning("platform: recreate-on-build enabled but PLATFORM_BUILD_SHA is empty — "
                        "recreating every boot (set it to gate once-per-build)")
            force_recreate = True
        else:
            try:
                claimed = await pool.fetchval(
                    """UPDATE platform_recreate_state
                          SET last_build_sha = $1, recreated_at = now()
                        WHERE id = 'singleton' AND last_build_sha IS DISTINCT FROM $1
                        RETURNING id""",
                    sha,
                )
                force_recreate = claimed is not None
                if force_recreate:
                    log.info("platform: build %s — recreating platform agents (steward + shca)", sha)
            except Exception:
                log.exception("platform: recreate-claim failed — skipping recreate this boot")

    # 1. Seed the custom tools FIRST — the steward/worker templates list these in
    #    tools_allow, so they must exist on the gateway before any agents.create.
    try:
        await ensure_platform_tools()
    except Exception:
        log.exception("platform: tool seeding failed — will retry next startup")

    # 2. Ensure a goal thread exists (reuses the standard goal-thread machinery;
    #    the steward behaves like the goal's agent on it).
    go_thread_id = goal["go_thread_id"]
    if not go_thread_id:
        go_thread_id = str(uuid.uuid4())
        await pool.execute(
            """INSERT INTO threads (id, goal_id, agent_id, created_by, title, status)
               VALUES ($1, $2, $3, 'system', 'Platform Research', 'active')
               ON CONFLICT (id) DO NOTHING""",
            go_thread_id, PLATFORM_GOAL_ID, STEWARD_AGENT_ID,
        )

    # 3. Point the goal at the steward as its agent (so /goals/__platform__/thread
    #    dispatches to it like a GO).
    await pool.execute(
        "UPDATE goals SET go_thread_id=$1, go_agent_id=$2 WHERE id=$3",
        go_thread_id, STEWARD_AGENT_ID, PLATFORM_GOAL_ID,
    )

    # 4. Provision the steward (idempotent).
    try:
        await create_platform_steward_for_goal(
            goal_id=PLATFORM_GOAL_ID, goal_name=goal["name"] or "Platform Improvement",
            force_recreate=force_recreate,
        )
        log.info("platform: council ensured (thread=%s, steward=%s)",
                 go_thread_id, STEWARD_AGENT_ID)
    except Exception:
        log.exception("platform: steward provisioning failed — will retry next startup")

    # 5. Provision the goal-bound Platform Integration Assistant (platform-shca).
    #    The sole assistant on __platform__ — it executes in-scope integration
    #    stories via the apply_* tools. Idempotent.
    try:
        from app.services.platform_council.provisioning import create_platform_shca_for_goal
        await create_platform_shca_for_goal(goal_id=PLATFORM_GOAL_ID, force_recreate=force_recreate)
    except Exception:
        log.exception("platform: platform-shca provisioning failed — will retry next startup")


_CREDENTIAL_NAME = "platform-internal-token"


def _tool_specs(base: str) -> list[dict]:
    """The Platform Research Council v2 registry tools (custom http_action).
    NO 'pwt' in any name. All params are strings — the http_action body templater
    renders strings only, so structured payloads (registry_write entries) are
    passed as a JSON-array string and parsed server-side. base = control_panel_url
    (no trailing slash).

    Granting (by template tools_allow): producers get registry_write +
    registry_search; the reviewer gets registry_verdict + registry_search +
    registry_get; the steward gets registry_search + registry_get only (read-only).
    The spawn/story tools are seeded in a later step alongside their endpoints.
    """
    P = base.rstrip("/") + "/internal/platform"
    def schema(props, required):
        return {"type": "object",
                "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
                "required": required}
    return [
        {"name": "registry_search", "label": "Search the platform registry",
         "description": "List registry findings. All filters optional (empty = no filter). "
                        "Returns lean rows; use registry_get for full detail. Call this BEFORE "
                        "researching to avoid duplicating existing findings.",
         "url": f"{P}/registry?category={{{{category}}}}&verdict={{{{verdict}}}}"
                f"&target_role={{{{target_role}}}}&impl_status={{{{impl_status}}}}"
                f"&fingerprint={{{{fingerprint}}}}&limit={{{{limit}}}}",
         "method": "GET", "body": None,
         "schema": schema({"category": "model|tool|mcp|skill (optional)",
                           "verdict": "pending|adopt_now|out_of_scope|adopt_with_build|not_applicable|rejected (optional)",
                           "target_role": "GO|SHCA|IA|RA|council|all (optional)",
                           "impl_status": "none|story_created|implemented (optional)",
                           "fingerprint": "exact dedup key (optional)",
                           "limit": "max rows (optional, default 100)"}, [])},
        {"name": "registry_get", "label": "Get a registry finding",
         "description": "Fetch one finding by id, with full detail, evidence, impact, verdict "
                        "and adoption_path.",
         "url": f"{P}/registry/item?id={{{{id}}}}", "method": "GET", "body": None,
         "schema": schema({"id": "the finding id"}, ["id"])},
        {"name": "registry_write", "label": "Write research findings",
         "description": "Producer finish-tool (ends your turn): submit your ranked findings "
                        "for this run. `entries` is a JSON ARRAY STRING; each item: "
                        "{fingerprint, category(model|tool|mcp|skill), title, summary, detail, "
                        "evidence(list of urls), impact(object), provider_family, target_role}. "
                        "fingerprint is the role-aware dedup key e.g. 'model:claude-haiku-5:GO', "
                        "'mcp:firecrawl' or 'skill:tdd' — re-using one updates that finding instead "
                        "of duplicating. Cap: at most 2 models PER ROLE for model_research; 5 total "
                        "for tool_research / skill_research.",
         "url": f"{P}/registry/write", "method": "POST",
         "body": {"source": "{{source}}", "entries": "{{entries}}", "run_id": "{{run_id}}"},
         "schema": schema({"source": "model_research | tool_research | skill_research",
                           "entries": "JSON array string of finding objects",
                           "run_id": "this run's id if provided in your kickoff (optional)"},
                          ["source", "entries"])},
        {"name": "registry_verdict", "label": "Record integration verdict",
         "description": "Integration reviewer: write your INDEPENDENT verdict into a finding's row. "
                        "verdict=adopt_now ONLY if the finding maps to one of the 4 no-code ops "
                        "(change_template_model|add_skill|add_tool|create_credential) — then you MUST "
                        "also set op_type + op_params (the execution spec). Anything needing code, a "
                        "migration, infra, or a new provider = out_of_scope. op_params is a JSON object "
                        "string. For models, supported_today=true only if provider family is gemini/anthropic.",
         "url": f"{P}/registry/verdict", "method": "POST",
         "body": {"finding_id": "{{finding_id}}", "verdict": "{{verdict}}",
                  "adoption_path": "{{adoption_path}}", "supported_today": "{{supported_today}}",
                  "reviewer_notes": "{{reviewer_notes}}", "op_type": "{{op_type}}",
                  "op_params": "{{op_params}}"},
         "schema": schema({"finding_id": "the finding id being reviewed",
                           "verdict": "adopt_now|out_of_scope|not_applicable|rejected",
                           "adoption_path": "HOW to adopt (which op), or why out of scope",
                           "supported_today": "true|false (models: provider family already wired?)",
                           "reviewer_notes": "your reasoning",
                           "op_type": "change_template_model|add_skill|add_tool|create_credential (adopt_now only)",
                           "op_params": "JSON object string: the execution args for op_type"},
                          ["finding_id", "verdict"])},
        {"name": "research_spawn", "label": "Queue on-demand research",
         "description": "Steward only: request research on a SPECIFIC topic. This ENQUEUES it at "
                        "the FRONT of the research queue (kind=model|tool|skill); the deterministic "
                        "drainer runs it one-at-a-time end-to-end and the reviewer verdicts it — you "
                        "do NOT run or review it yourself. Just tell the user it is queued. Use only "
                        "when a human asks for NEW research; for 'what can we integrate?' read the "
                        "registry instead.",
         "url": f"{P}/research/spawn", "method": "POST",
         "body": {"kind": "{{kind}}", "topic": "{{topic}}"},
         "schema": schema({"kind": "model | tool | skill",
                           "topic": "the specific thing to research"}, ["kind", "topic"])},
        {"name": "review_spawn", "label": "Spawn integration review",
         "description": "Steward only: run integration review. Pass `sweep_id` (from a research_spawn "
                        "you started) to review that research's findings; or `finding_id` to review "
                        "one finding; or neither to review ALL pending findings.",
         "url": f"{P}/review/spawn", "method": "POST",
         "body": {"sweep_id": "{{sweep_id}}", "finding_id": "{{finding_id}}"},
         "schema": schema({"sweep_id": "review this sweep's pending findings (optional)",
                           "finding_id": "review one finding (optional)"}, [])},
        {"name": "research_status", "label": "Check research queue status",
         "description": "Steward: check whether a research/review sweep is already in flight. "
                        "Call this BEFORE research_spawn/review_spawn — if busy=true, do NOT "
                        "dispatch; tell the user a research is already running and they must wait.",
         "url": f"{P}/research/status", "method": "GET", "body": None,
         "schema": schema({}, [])},
        {"name": "story_create", "label": "Create implementation story",
         "description": "Steward only: create a platform-improvement story. For a registry finding "
                        "pass `finding_id` (allowed ONLY if the reviewer marked it adopt_now); for a "
                        "free-form request pass `title` + `description`. The story enters the normal "
                        "board → planning → execution pipeline.",
         "url": f"{P}/story/create", "method": "POST",
         "body": {"finding_id": "{{finding_id}}", "title": "{{title}}",
                  "description": "{{description}}", "acceptance_criteria": "{{acceptance_criteria}}"},
         "schema": schema({"finding_id": "registry finding to implement (optional)",
                           "title": "story title (optional if finding_id given)",
                           "description": "what to build (optional if finding_id given)",
                           "acceptance_criteria": "done criteria (optional)"}, [])},

        # ── Discovery feed — Independent Platform Research writes; steward reads. ──
        {"name": "discovery_write", "label": "Log platform discoveries",
         "description": "Independent Platform Research finish-tool (ends your turn): log up to 15 "
                        "newly-released items. `entries` is a JSON ARRAY STRING; each item: "
                        "{fingerprint, category(model|tool|skill), title, summary, url, released_at, source}. "
                        "fingerprint is the dedup key e.g. 'model:gpt-5' — re-logging refreshes, never duplicates.",
         "url": f"{P}/discovery/write", "method": "POST",
         "body": {"entries": "{{entries}}"},
         "schema": schema({"entries": "JSON array string of discovery objects (<=15)"}, ["entries"])},
        {"name": "discovery_search", "label": "Search the discovery feed",
         "description": "List recent discoveries (what shipped lately). Filters optional: "
                        "category(model|tool|skill), status(new|researching|reviewed|failed|dismissed). "
                        "'reviewed' = fully researched + verdicted; 'failed' = research/review failed.",
         "url": f"{P}/discovery?category={{{{category}}}}&status={{{{status}}}}&limit={{{{limit}}}}",
         "method": "GET", "body": None,
         "schema": schema({"category": "model|tool|skill (optional)",
                           "status": "new|researching|reviewed|failed|dismissed (optional)",
                           "limit": "max rows (optional)"}, [])},
        {"name": "discovery_get", "label": "Get a discovery",
         "description": "Fetch one discovery by id (full detail + link + status).",
         "url": f"{P}/discovery/item?id={{{{id}}}}", "method": "GET", "body": None,
         "schema": schema({"id": "the discovery id"}, ["id"])},

        # ── Execution tools — the 4 no-code operations (granted to the platform
        #    SHCA only, via its goal-scoped grant; the SHCA calls these to execute
        #    in-scope integration stories). ─────────────────────────────────────
        {"name": "apply_model_change", "label": "Change a template's model",
         "description": "Change an agent template's model (template-only; future instances). "
                        "template_id is 'go' (GO) or 'default-shca' (SHCA). model must be a "
                        "supported model id.",
         "url": f"{P}/apply/model_change", "method": "POST",
         "body": {"template_id": "{{template_id}}", "model": "{{model}}"},
         "schema": schema({"template_id": "go | default-shca | <template id>",
                           "model": "supported model id"}, ["template_id", "model"])},
        {"name": "apply_add_skill", "label": "Add a skill",
         "description": "Add a reusable skill (prompt fragment). applicable_to is a JSON array "
                        "string of agent types, e.g. [\"SHCA\",\"GO\"].",
         "url": f"{P}/apply/add_skill", "method": "POST",
         "body": {"name": "{{name}}", "title": "{{title}}", "description": "{{description}}",
                  "content": "{{content}}", "applicable_to": "{{applicable_to}}"},
         "schema": schema({"name": "skill name (lowercase_snake)", "title": "short title",
                           "description": "one line", "content": "the skill markdown",
                           "applicable_to": "JSON array string of agent types"},
                          ["name", "description", "content", "applicable_to"])},
        {"name": "apply_create_credential", "label": "Scaffold a credential",
         "description": "Create a credential SHELL (no secret) for a tool to use. A human enters "
                        "the secret later in Settings → Credentials. type = bearer|api_key|basic; "
                        "header_name is required for api_key.",
         "url": f"{P}/apply/create_credential", "method": "POST",
         "body": {"name": "{{name}}", "type": "{{type}}", "header_name": "{{header_name}}"},
         "schema": schema({"name": "credential name/label", "type": "bearer|api_key|basic",
                           "header_name": "header for api_key (optional otherwise)"},
                          ["name", "type"])},
        {"name": "apply_add_tool", "label": "Add a tool / MCP server",
         "description": "Add a custom tool. tier = http_action|mcp_server|openapi|typescript. `spec` "
                        "is a JSON object string, tier-specific: http_action {url, method, "
                        "parameters_schema, body_template}; mcp_server {server_url, transport?, "
                        "tools:[names]}; openapi {server_url, operations:[{operation_id,method,path,"
                        "description}]}; typescript {source_code, parameters_schema}. `credential` "
                        "(optional JSON object {name,type,header_name}) scaffolds a pending credential.",
         "url": f"{P}/apply/add_tool", "method": "POST",
         "body": {"tier": "{{tier}}", "name": "{{name}}", "label": "{{label}}",
                  "description": "{{description}}", "spec": "{{spec}}", "credential": "{{credential}}"},
         "schema": schema({"tier": "http_action | mcp_server | openapi | typescript", "name": "tool name",
                           "label": "short label", "description": "what it does",
                           "spec": "JSON object string (tier-specific — see description)",
                           "credential": "JSON object string {name,type,header_name} (optional)"},
                          ["tier", "name", "label", "description", "spec"])},
    ]


async def ensure_platform_tools() -> None:
    """Idempotently seed the platform council's custom http_action tools (registry_*,
    research_spawn/review_spawn, story_create, apply_*) + their shared bearer
    credential, then tools.reload so the gateway picks them up. Requires
    control_panel_url to be set."""
    settings = get_settings()
    base = (getattr(settings, "control_panel_url", "") or "").strip()
    if not base:
        log.warning("platform: control_panel_url unset — skipping tool seeding "
                    "(set CONTROL_PANEL_URL)")
        return
    pool = get_pool()

    # Shared bearer credential = the OpenClaw auth token the /internal/* routers expect.
    # DO UPDATE (not DO NOTHING): openclaw_auth_token can rotate after this row is
    # first seeded; a plain insert-if-missing would leave the credential frozen at
    # the OLD token, so every platform tool call would 401 (rendered as a /login
    # redirect by the 401 handler) even though the token is "present". Refresh the
    # value on every startup so the credential self-heals on rotation. Same row id
    # is kept, so tools linked via credential_id stay linked.
    from app.services.custom_tools.credential import encrypt_value
    await pool.execute(
        """INSERT INTO tool_credentials (name, credential_type, encrypted_value, created_by)
           VALUES ($1, 'bearer', $2, 'system')
           ON CONFLICT (name) DO UPDATE
             SET encrypted_value = EXCLUDED.encrypted_value, updated_at = NOW()""",
        _CREDENTIAL_NAME, encrypt_value(settings.openclaw_auth_token or ""),
    )
    cred_id = await pool.fetchval(
        "SELECT id FROM tool_credentials WHERE name = $1", _CREDENTIAL_NAME,
    )

    from app.services.custom_tools import crud as _ct_crud
    from app.services.custom_tools.generators.http_action import _generate

    created = updated = 0
    for spec in _tool_specs(base):
        gen_inputs = {
            "url": spec["url"], "method": spec["method"],
            "headers": {"Content-Type": "application/json"},
            "body_template": spec["body"],
            "credential": {"name": _CREDENTIAL_NAME, "type": "bearer"},
            "parameters_schema": spec["schema"],
        }
        gen = _generate(gen_inputs)
        config = {"url": spec["url"], "method": spec["method"],
                  "headers": {"Content-Type": "application/json"}, "body_template": spec["body"]}
        existing = await _ct_crud.get_custom_tool(pool, spec["name"])
        if existing:
            # Update-on-change: the specs are the source of truth. Patch drift in the
            # LLM-facing fields (label/description/parameters_schema) so enum + wording
            # fixes actually reach the live gateway. (The old create-only path froze
            # stale specs forever; url/method/body execution config is intentionally
            # left alone to avoid every-boot churn from generator normalisation.)
            desired = {"label": spec["label"], "description": spec["description"],
                       "parameters_schema": gen["parameters_schema"]}
            diff = {k: v for k, v in desired.items() if existing.get(k) != v}
            if diff:
                try:
                    await _ct_crud.update_custom_tool(pool, spec["name"], diff)
                    updated += 1
                except Exception:
                    log.exception("platform: failed to update tool %s", spec["name"])
            continue
        try:
            await _ct_crud.create_custom_tool(
                pool, name=spec["name"], label=spec["label"], description=spec["description"],
                tier="http_action", parameters_schema=gen["parameters_schema"], config=config,
                source_code=gen["source_code"], created_by="system",
                credential_id=str(cred_id) if cred_id else None, manifest=gen["manifest"],
                is_core=True,
            )
            created += 1
        except Exception:
            log.exception("platform: failed to seed tool %s", spec["name"])

    if created or updated:
        try:
            await gateway_admin.call("tools.reload", {}, timeout=30)
        except Exception:
            log.warning("platform: tools.reload failed (5-min refresh will catch up)")
    log.info("platform: tool seeding done (%d new, %d updated)", created, updated)


async def worker_reaper_loop() -> None:
    """Periodic warm-session reaping (idle > 30 min OR context >= 80%)."""
    if not is_enabled():
        return
    log.info("platform: worker reaper loop started (every %ds)", _REAP_INTERVAL_SECONDS)
    while True:
        try:
            await _worker.reap_idle_workers()
        except Exception:
            log.exception("platform: worker reaper tick failed")
        await asyncio.sleep(_REAP_INTERVAL_SECONDS)


_REVIEW_BACKSTOP_INTERVAL_SECONDS = 120


async def review_backstop_loop() -> None:
    """Event-driven integration review: periodically sweep for any finding left
    `verdict='pending'` with no live reviewer and spawn one. This is what makes
    integration review run INDEPENDENTLY of the producer fan-out — it catches
    findings from a failed sweep, ad-hoc writes, or a reviewer that died."""
    if not is_enabled():
        return
    log.info("platform: review backstop loop started (every %ds)", _REVIEW_BACKSTOP_INTERVAL_SECONDS)
    while True:
        try:
            from app.services.platform_council import runs as _runs
            await _runs.review_orphan_pending()
            await _runs.nudge_stalled_sweeps()
        except Exception:
            log.exception("platform: review backstop tick failed")
        await asyncio.sleep(_REVIEW_BACKSTOP_INTERVAL_SECONDS)


_SCHEDULER_INTERVAL_SECONDS = 3600  # check hourly; act only when a sweep is due


async def scheduler_loop() -> None:
    """Platform scheduler (checked hourly) — OFF by default; no-op unless
    platform_council_enabled AND platform_discovery_schedule_enabled:
      * weekly Independent Platform Research discovery sweep (breadth scout) that
        writes new rows to platform_discoveries.
    Draining those discoveries into research is the research dispatcher's job (the
    deterministic queue drainer in the council-worker) — NOT a scheduled deep-dive."""
    if not is_enabled():
        return
    log.info("platform: scheduler loop started (hourly; discovery=%s)",
             bool(getattr(get_settings(), "platform_discovery_schedule_enabled", False)))
    while True:
        try:
            if await discovery_schedule_enabled():
                await _maybe_run_weekly_discovery()
        except Exception:
            log.exception("platform: scheduler tick failed")
        await asyncio.sleep(_SCHEDULER_INTERVAL_SECONDS)


async def discovery_schedule_enabled() -> bool:
    """Effective weekly-discovery gate: the runtime DB override in
    platform_schedule_state.discovery_enabled if set (not NULL), else the env
    flag platform_discovery_schedule_enabled. Lets the Council Runs admin tab
    toggle the sweep without a redeploy (get_settings() is @lru_cache)."""
    try:
        pool = get_pool()
        override = await pool.fetchval(
            "SELECT discovery_enabled FROM platform_schedule_state WHERE id='singleton'")
        if override is not None:
            return bool(override)
    except Exception:
        log.debug("platform: discovery override read failed — falling back to env flag")
    return bool(getattr(get_settings(), "platform_discovery_schedule_enabled", False))


async def _maybe_run_weekly_discovery() -> None:
    pool = get_pool()
    interval_days = int(getattr(get_settings(), "platform_discovery_interval_days", 7) or 7)
    last = await pool.fetchval(
        "SELECT last_discovery_at FROM platform_schedule_state WHERE id='singleton'")
    if last is not None:
        due = await pool.fetchval(
            "SELECT $1::timestamptz < NOW() - ($2 || ' days')::interval", last, str(interval_days))
        if not due:
            return
    from app.services.platform_council import worker as _worker
    res = await _worker.spawn_discovery(trigger="scheduled")
    await pool.execute(
        "UPDATE platform_schedule_state SET last_discovery_at=NOW() WHERE id='singleton'")
    log.info("platform: weekly discovery sweep dispatched: %s", res)


