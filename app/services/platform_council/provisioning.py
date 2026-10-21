"""Provision the Platform Research Council v2 main agent — the 'platform-steward'.

The steward is a GO-shaped persistent agent of type 'Platform' on the singleton
__platform__ goal: own thread + memory_v2 + compaction + GO-like resources, but
with research/registry/story tools and its own (non-GO) prompts. Mirrors
go_agent.create_go_for_goal, reading the 'platform-steward' template from
templates/template_files.

Producers + reviewers (the subagents) are provisioned in worker.py.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from app import local_dev
from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError
from app.ledger.journal import append_event
from app.ledger.schema import AGENT_PROVISIONED, AGENT_CONTEXT_BOUND
from app.services.agent_registry.helpers import enroll_agent_principal
from app.services.platform_council import STEWARD_AGENT_ID, PLATFORM_GOAL_ID

_log = logging.getLogger(__name__)

STEWARD_TEMPLATE_ID = "platform-steward"
from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR
from app.ledger.actors import human_actor


async def _push_agent_workspace(gw_agent_id: str, files_map: dict, identity_md: str,
                                memory_agent_type: str = "GO", who: str = "agent",
                                reheal: dict | None = None) -> bool:
    """Push a platform agent's prompt files into its gateway workspace, with per-file
    retry. set_agent_file can transiently fail ("unknown agent id") while the gateway
    finishes its slow async reload after agents.create, and the agents.list poll
    proved unreliable — so we retry the actual write. Idempotent: safe to call every
    startup, so the generic default AGENTS.md the gateway seeds gets overwritten with
    ours and template edits propagate. Returns True if AGENTS.md landed.

    reheal: agents.create params (name/workspace/model/tools) to re-issue between
    retries. writeConfigFile has no mutual exclusion on the gateway, so an unrelated
    agent's own concurrent config write can clobber this agent's config entry mid-loop
    (after the one-shot self-heal in agents.create's "already exists" branch already
    ran) -- re-issuing agents.create here (tolerating "already exists", which itself
    now self-heals the workspace dir) gives every remaining file a fresh chance to
    recover instead of failing for the rest of this run."""
    import asyncio as _asyncio
    files = {
        "AGENTS.md": files_map.get("AGENTS.md", ""),
        "SOUL.md": files_map.get("SOUL.md", ""),
        "USER.md": files_map.get("USER.md", ""),
        "IDENTITY.md": identity_md,
        "HEARTBEAT.md": "",   # clear the gateway default (no inbox-polling heartbeat)
        "BOOTSTRAP.md": "",   # clear the default "birth certificate" first-run flow
    }
    ok_agents = False
    for fn, content in files.items():
        for attempt in range(20):
            try:
                await gateway_admin.set_agent_file(gw_agent_id, fn, content)
                if fn == "AGENTS.md":
                    ok_agents = True
                break
            except Exception as exc:
                if attempt == 19:
                    _log.warning("Platform %s: set_agent_file %s failed after retries: %s", who, fn, exc)
                else:
                    if reheal is not None:
                        try:
                            await gateway_admin.call("agents.create", reheal)
                        except GatewayError as reheal_exc:
                            if "already exists" not in str(reheal_exc):
                                _log.debug("Platform %s: mid-loop reheal failed for %s: %s",
                                          who, gw_agent_id, reheal_exc)
                        except Exception:
                            pass
                    await _asyncio.sleep(1)
    try:
        from app.memory_v2 import provisioning as _mv2
        await _mv2.setup_memory_v2_for_agent(agent_id=gw_agent_id, agent_type=memory_agent_type)
    except Exception:
        _log.exception("Platform %s: memory_v2 hook failed for %s — non-fatal", who, gw_agent_id)
    _log.info("Platform %s %s workspace pushed (AGENTS.md ok=%s)", who, gw_agent_id, ok_agents)
    return ok_agents


async def _push_steward_workspace(gw_agent_id: str, model_label: str, files_map: dict,
                                  workspace_path: str = "", model: str = "",
                                  tools_allow: list | None = None) -> bool:
    identity_md = (
        "# Identity\n\n**Name:** Platform Steward\n"
        "**Type:** Standing platform-improvement advisor (admins-only)\n"
        f"**Model:** {model_label}\n\n"
        "The admins in this thread are rendered live in your per-turn context. You "
        "read the research registry, orchestrate research/review, and create "
        "implementation stories; you advise, you do not implement.\n"
    )
    reheal = (
        {"name": gw_agent_id, "workspace": workspace_path, "model": model,
         "tools": {"allow": tools_allow}}
        if workspace_path and model and tools_allow is not None else None
    )
    return await _push_agent_workspace(gw_agent_id, files_map, identity_md,
                                       memory_agent_type="GO", who="steward", reheal=reheal)


async def _reconcile_gateway_tools(gw_agent_id: str, template_id: str) -> None:
    """Push the template's resolved tool allow/deny to the LIVE gateway agent.

    Critical: agents.create is idempotent — an agent created before its allow-list
    was set (or created with an empty allow) keeps an EMPTY gateway allow-list,
    which the gateway treats as "all tools allowed" (the platform steward ended up
    with 85+ tools incl. exec/write/gateway/apply_* this way). agents.create won't
    fix it ("already exists" → skip); only an explicit agents.update (set_agent_tools)
    does. Runs every startup so the live agent always matches its template scope."""
    try:
        from app.services.agent_registry.helpers import resolve_agent_tools
        pool = get_pool()
        eff_allow, eff_deny = await resolve_agent_tools(pool, gw_agent_id, template_id, [], [])
        if not eff_allow:
            # A platform agent's allow-list is never legitimately empty (it always
            # has a real curated list from its template) -- an empty result here
            # means resolution silently failed upstream (missing/invalid templates
            # row, or an exception swallowed inside resolve_agent_tools) and fell
            # back to the empty instance defaults this call passes in ([], []).
            # The gateway treats an empty allow-list as "all tools allowed", so
            # pushing it would silently grant every tool (incl. apply_*) instead of
            # leaving whatever scope the agent already has. Skip the push instead.
            _log.warning(
                "Gateway tools reconcile for %s resolved an EMPTY allow-list "
                "(template=%s) -- skipping push to avoid granting all tools; "
                "existing gateway tool scope left untouched", gw_agent_id, template_id)
            return
        await gateway_admin.set_agent_tools(gw_agent_id, eff_allow, eff_deny)
        await pool.execute(
            "UPDATE agents SET tools_allow=$1, tools_deny=$2 WHERE id=$3",
            eff_allow, eff_deny, gw_agent_id)
        _log.info("Reconciled gateway tools for %s: allow=%d deny=%d",
                  gw_agent_id, len(eff_allow), len(eff_deny))
    except Exception:
        _log.exception("Gateway tools reconcile failed for %s (non-fatal)", gw_agent_id)


async def create_platform_steward_for_goal(
    goal_id: str = PLATFORM_GOAL_ID,
    goal_name: str = "Platform Improvement",
    created_by_uid: str = "",
    model: str = "",
    force_recreate: bool = False,
) -> dict:
    """Provision the Platform Steward for the __platform__ goal from the
    'platform-steward' template. Idempotent — returns the existing row if active,
    reconciling its model to the template if it drifted.

    force_recreate: delete the gateway agent first and run the full create path, so
    it is rebuilt from scratch with the template's tool scope (agents.create honors
    `tools`; agents.update does not). Drops the gateway workspace/memory — used by
    the once-per-build recreate.

    Returns {"agent_id", "gateway_agent_id", "workspace"}.
    """
    from app.llm_models import is_supported, label_for
    from app.services.model_registry.dropdowns import is_model_registered_v2

    gw_agent_id = STEWARD_AGENT_ID                       # short fixed id (one per env)
    workspace_path = local_dev.agent_workspace_path(gw_agent_id)
    display_name = "Platform Steward"

    pool = get_pool()

    if force_recreate:
        try:
            await gateway_admin.call("agents.delete", {"agentId": gw_agent_id})
            _log.info("Platform steward %s: force-recreate — deleted gateway agent", gw_agent_id)
        except Exception as exc:
            if "not found" not in str(exc).lower():
                _log.warning("Platform steward force-recreate delete failed for %s: %s", gw_agent_id, exc)

    tmpl = await pool.fetchrow(
        "SELECT id, type, name, model, tools_allow, tools_deny FROM templates "
        "WHERE id = $1 AND status = 'active'",
        STEWARD_TEMPLATE_ID,
    )
    if tmpl is None:
        raise RuntimeError(
            f"Templates row {STEWARD_TEMPLATE_ID!r} missing — run platform_v2_steward.sql first")
    if not model or (not is_supported(model)
                     and not await is_model_registered_v2(pool, model)):
        model = tmpl["model"]
    model_label = label_for(model)
    tools_allow = list(tmpl["tools_allow"])
    tools_deny = list(tmpl["tools_deny"])

    template_rows = await pool.fetch(
        "SELECT filename, content FROM template_files WHERE template_id = $1", STEWARD_TEMPLATE_ID)
    files_map = {r["filename"]: r["content"] for r in template_rows}
    if "AGENTS.md" not in files_map:
        raise RuntimeError("platform-steward template_files missing AGENTS.md")

    existing = await pool.fetchrow(
        "SELECT id AS gateway_agent_id, workspace, model, actor_id FROM agents "
        "WHERE id = $1 AND status = 'active' LIMIT 1",
        gw_agent_id,
    )
    if existing and not force_recreate:
        # Ensure the gateway agent EXISTS with the correct model + tool scope. If it
        # was lost (gateway wipe/restart), the session path (_ensure_gateway_agent)
        # would otherwise recreate it with the gemini default + NO tools. agents.create
        # is idempotent — "already exists" is a no-op (config drift on an existing
        # agent can't be fixed by agents.update here, only by recreate-on-build).
        try:
            await gateway_admin.call("agents.create", {
                "name": gw_agent_id,
                "workspace": existing["workspace"] or workspace_path,
                "model": model, "tools": {"allow": tools_allow}})
            _log.info("Platform steward %s gateway agent created (was missing) model=%s",
                      gw_agent_id, model)
        except GatewayError as exc:
            if "already exists" not in str(exc):
                _log.warning("Platform steward ensure-create failed for %s: %s", gw_agent_id, exc)
        if existing["model"] != model:
            try:
                await gateway_admin.call("agents.update", {"agentId": gw_agent_id, "model": model})
                await pool.execute("UPDATE agents SET model=$1 WHERE id=$2", model, gw_agent_id)
                _log.info("Platform steward model reconciled %s -> %s", existing["model"], model)
            except Exception:
                _log.exception("Platform steward model reconcile failed (non-fatal)")
        else:
            _log.info("Platform steward already exists (gateway=%s) — reusing", gw_agent_id)
        # Repair a row whose actor principal was never enrolled (e.g. a bare row
        # created by another path before provisioning ran) — otherwise message
        # persistence hits the ledger's actor_id NOT NULL.
        if existing["actor_id"] is None:
            repair_actor_id = uuid.uuid4()
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE agents SET actor_id=$1 WHERE id=$2 AND actor_id IS NULL",
                            repair_actor_id, gw_agent_id)
                        await enroll_agent_principal(
                            conn, actor_id=repair_actor_id, agent_id=gw_agent_id,
                            display_name=display_name, gateway_agent_id=gw_agent_id,
                            workspace=existing["workspace"] or workspace_path, model=model,
                            agent_type="Platform", scope="goal", emoji="🛰️",
                            tools_allow=list(tmpl["tools_allow"]), tools_deny=list(tmpl["tools_deny"]),
                            memory_enabled=True, created_by=created_by_uid)
                _log.info("Platform steward actor_id repaired (%s) for %s", repair_actor_id, gw_agent_id)
            except Exception:
                _log.exception("Platform steward actor repair failed (non-fatal)")
        # Enforce the template's tool scope on the live gateway agent (removes the
        # stale all-tools grant — see _reconcile_gateway_tools).
        await _reconcile_gateway_tools(gw_agent_id, STEWARD_TEMPLATE_ID)
        # Refresh the workspace prompt files every startup — overwrites the gateway's
        # generic default AGENTS.md with ours and propagates template edits.
        await _push_steward_workspace(gw_agent_id, model_label, files_map,
                                      workspace_path=existing["workspace"] or workspace_path,
                                      model=model, tools_allow=tools_allow)
        return {"agent_id": existing["gateway_agent_id"],
                "gateway_agent_id": existing["gateway_agent_id"],
                "workspace": existing["workspace"] or workspace_path}

    # 1. Create the gateway agent.
    try:
        await gateway_admin.call("agents.create", {
            "name": gw_agent_id, "workspace": workspace_path,
            "model": model, "tools": {"allow": tools_allow},
        })
    except GatewayError as exc:
        if "already exists" in str(exc):
            _log.info("Platform steward %s already on gateway, reusing", gw_agent_id)
        else:
            raise

    # 2. Create the DB principal FIRST — this is pure database state (agents row +
    #    actor enrollment + agent_contexts) that must NOT depend on gateway-reload
    #    timing. agents.create is applied asynchronously by the gateway, so an
    #    earlier version gated this behind an agents.list readiness poll and threw
    #    on timeout BEFORE the INSERT — leaving the steward with no agents row,
    #    which cascaded into SHCA mis-routing + null-actor/`uid=system` crashes.
    #    The row never depends on the gateway; only set_agent_file does (step 3).
    actor_id = uuid.uuid4()
    # Platform provenance: the human who triggered provisioning, else system.
    creator_actor_id, creator_actor_type = await human_actor(created_by_uid)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn, event_type=AGENT_PROVISIONED,
                payload={"template_id": STEWARD_TEMPLATE_ID, "agent_id": gw_agent_id,
                         "name": display_name, "type": "Platform", "scope": "goal",
                         "model": model, "gateway_agent_id": gw_agent_id, "goal_id": goal_id},
                actor_id=creator_actor_id, actor_type=creator_actor_type,
                context_goal_id=goal_id,
            )
            inserted_actor_id = await conn.fetchval(
                """
                INSERT INTO agents
                    (id, actor_id, name, type, scope, model, emoji, tools_allow, tools_deny,
                     memory_enabled, created_by, gateway_agent_id, workspace, auto_created,
                     is_template, status, template_id)
                VALUES ($1, $2, $3, 'Platform', 'goal', $4, '🛰️', $5, $6, true, $7, $1, $8,
                        true, FALSE, 'active', $9)
                ON CONFLICT (id) DO NOTHING
                RETURNING actor_id
                """,
                gw_agent_id, actor_id, display_name, model, tools_allow, tools_deny,
                created_by_uid, workspace_path, STEWARD_TEMPLATE_ID,
            )
            # Enroll the actor principal (actors + agent_configs + bindings) so
            # message persistence has a non-null actor_id — exactly as the GO does.
            if inserted_actor_id:
                await enroll_agent_principal(
                    conn, actor_id=inserted_actor_id, agent_id=gw_agent_id,
                    display_name=display_name, gateway_agent_id=gw_agent_id,
                    workspace=workspace_path, model=model, agent_type="Platform",
                    scope="goal", emoji="🛰️", tools_allow=tools_allow, tools_deny=tools_deny,
                    memory_enabled=True, created_by=created_by_uid)
            await append_event(
                conn, event_type=AGENT_CONTEXT_BOUND,
                payload={"template_id": STEWARD_TEMPLATE_ID, "agent_id": gw_agent_id,
                         "gateway_agent_id": gw_agent_id, "goal_id": goal_id,
                         "role": "platform_steward"},
                actor_id=creator_actor_id, actor_type=creator_actor_type,
                context_goal_id=goal_id,
            )
            await conn.execute(
                """
                INSERT INTO agent_contexts
                    (goal_id, agent_id, additional_instructions, skills, workspace_files, added_by)
                VALUES ($1, $2, '', '{}', '{}', $3)
                ON CONFLICT (goal_id, agent_id) DO NOTHING
                """,
                goal_id, gw_agent_id, created_by_uid,
            )

    # 3. Push the workspace prompt files (retry-based, robust to the gateway's slow
    #    async reload after agents.create). The DB row above is already committed,
    #    so this never blocks provisioning; files retry on the next startup too.
    await _push_steward_workspace(gw_agent_id, model_label, files_map,
                                  workspace_path=workspace_path, model=model,
                                  tools_allow=tools_allow)
    await _reconcile_gateway_tools(gw_agent_id, STEWARD_TEMPLATE_ID)

    _log.info("Platform steward %s provisioned (model=%s)", gw_agent_id, model)
    return {"agent_id": gw_agent_id, "gateway_agent_id": gw_agent_id, "workspace": workspace_path}


PLATFORM_SHCA_AGENT_ID = "platform-shca"
PLATFORM_SHCA_TEMPLATE_ID = "platform-shca"


async def create_platform_shca_for_goal(goal_id: str = PLATFORM_GOAL_ID,
                                        created_by_uid: str = "",
                                        force_recreate: bool = False) -> dict:
    """Provision the singleton, GOAL-BOUND Platform Integration Assistant on the
    __platform__ goal. It is NOT a per-user assistant and never enters anyone's
    assistant pool (that would leak the apply_* tools to their other goals) — it is
    attached ONLY to __platform__ via agent_contexts. Idempotent; same DB-first +
    best-effort-push pattern as the steward.

    force_recreate: delete the gateway agent first so the agents.create below rebuilds
    it from scratch with the template's tool scope (agents.create honors `tools`;
    agents.update does not). Drops the gateway workspace/memory — used by the
    once-per-build recreate."""
    from app.llm_models import label_for
    gw_agent_id = PLATFORM_SHCA_AGENT_ID
    workspace_path = local_dev.agent_workspace_path(gw_agent_id)
    display_name = "Platform Integration Assistant"
    pool = get_pool()

    tmpl = await pool.fetchrow(
        "SELECT id, model, tools_allow, tools_deny FROM templates WHERE id=$1 AND status='active'",
        PLATFORM_SHCA_TEMPLATE_ID)
    if tmpl is None:
        raise RuntimeError(f"Templates row {PLATFORM_SHCA_TEMPLATE_ID!r} missing — run "
                           "platform_shca_template.sql first")
    model = tmpl["model"]
    model_label = label_for(model)
    tools_allow = list(tmpl["tools_allow"])
    tools_deny = list(tmpl["tools_deny"])
    template_rows = await pool.fetch(
        "SELECT filename, content FROM template_files WHERE template_id=$1", PLATFORM_SHCA_TEMPLATE_ID)
    files_map = {r["filename"]: r["content"] for r in template_rows}

    existing = await pool.fetchrow(
        "SELECT id, actor_id, model FROM agents WHERE id=$1 AND status='active'", gw_agent_id)

    # Once-per-build recreate: drop the gateway agent so agents.create below rebuilds
    # it fresh WITH the template tool scope (agents.update can't change tools).
    if force_recreate:
        try:
            await gateway_admin.call("agents.delete", {"agentId": gw_agent_id})
            _log.info("Platform SHCA %s: force-recreate — deleted gateway agent", gw_agent_id)
        except Exception as exc:
            if "not found" not in str(exc).lower():
                _log.warning("Platform SHCA force-recreate delete failed for %s: %s", gw_agent_id, exc)

    # 1. Gateway agent (idempotent).
    try:
        await gateway_admin.call("agents.create", {
            "name": gw_agent_id, "workspace": workspace_path,
            "model": model, "tools": {"allow": tools_allow}})
    except GatewayError as exc:
        if "already exists" not in str(exc):
            raise

    # 2. DB principal + goal binding FIRST (never gated on gateway readiness).
    if not existing:
        actor_id = uuid.uuid4()
        # Platform provenance: the human who triggered provisioning, else system.
        pa_actor_id, pa_actor_type = await human_actor(created_by_uid)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn, event_type=AGENT_PROVISIONED,
                    payload={"template_id": PLATFORM_SHCA_TEMPLATE_ID, "agent_id": gw_agent_id,
                             "name": display_name, "type": "SHCA", "scope": "goal",
                             "model": model, "gateway_agent_id": gw_agent_id, "goal_id": goal_id},
                    actor_id=pa_actor_id, actor_type=pa_actor_type, context_goal_id=goal_id)
                inserted_actor_id = await conn.fetchval(
                    """
                    INSERT INTO agents
                        (id, actor_id, name, type, scope, model, emoji, tools_allow, tools_deny,
                         memory_enabled, created_by, gateway_agent_id, workspace, auto_created,
                         is_template, status, template_id)
                    VALUES ($1,$2,$3,'PlatformAssistant','goal',$4,'🛠️',$5,$6,true,$7,$1,$8,true,FALSE,'active',$9)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING actor_id
                    """,
                    gw_agent_id, actor_id, display_name, model, tools_allow, tools_deny,
                    created_by_uid, workspace_path, PLATFORM_SHCA_TEMPLATE_ID)
                if inserted_actor_id:
                    await enroll_agent_principal(
                        conn, actor_id=inserted_actor_id, agent_id=gw_agent_id,
                        display_name=display_name, gateway_agent_id=gw_agent_id,
                        workspace=workspace_path, model=model, agent_type="PlatformAssistant",
                        scope="goal", emoji="🛠️", tools_allow=tools_allow, tools_deny=tools_deny,
                        memory_enabled=True, created_by=created_by_uid)
                await conn.execute(
                    """INSERT INTO agent_contexts
                         (goal_id, agent_id, additional_instructions, skills, workspace_files, added_by)
                       VALUES ($1,$2,'','{}','{}',$3) ON CONFLICT (goal_id, agent_id) DO NOTHING""",
                    goal_id, gw_agent_id, created_by_uid)
    else:
        # Reconcile a drifted model to the template (mirrors the steward) so an
        # admin editing the template model propagates to the live singleton.
        if existing["model"] != model:
            try:
                await gateway_admin.call("agents.update", {"agentId": gw_agent_id, "model": model})
                await pool.execute("UPDATE agents SET model=$1 WHERE id=$2", model, gw_agent_id)
                _log.info("Platform SHCA model reconciled %s -> %s", existing["model"], model)
            except Exception:
                _log.exception("Platform SHCA model reconcile failed (non-fatal)")
        # Ensure the goal binding (in case it was cleared) — never re-owns/re-pools it.
        await pool.execute(
            """INSERT INTO agent_contexts (goal_id, agent_id, added_by)
               VALUES ($1,$2,$3) ON CONFLICT (goal_id, agent_id) DO NOTHING""",
            goal_id, gw_agent_id, created_by_uid)

    # 2b. Enforce the template's tool scope on the live gateway agent.
    await _reconcile_gateway_tools(gw_agent_id, PLATFORM_SHCA_TEMPLATE_ID)

    # 3. Best-effort workspace push (retry-based).
    identity_md = (
        "# Identity\n\n**Name:** Platform Integration Assistant\n"
        "**Type:** Admins-only executor on the Platform Improvement goal\n"
        f"**Model:** {model_label}\n\n"
        "You execute the 4 no-code integration operations from steward-created stories "
        "via the apply_* tools; you never handle secrets (a human enters those).\n")
    shca_reheal = {"name": gw_agent_id, "workspace": workspace_path, "model": model,
                   "tools": {"allow": tools_allow}}
    await _push_agent_workspace(gw_agent_id, files_map, identity_md,
                               memory_agent_type="SHCA", who="shca", reheal=shca_reheal)

    # 4. Push the PlatformAssistant-scoped skills (e.g. platform_tool_authoring) to
    # <workspace>/skills/<name>/ — AFTER the workspace push above, which retries
    # until the gateway has finished its async post-create config reload, so
    # set_agent_skill no longer races ahead and hits "unknown agent id". This is
    # the SOLE delivery path for this singleton: skill_sync's startup reconcile
    # skips it (its query excludes rows where templates.id = agents.id).
    try:
        skill_rows = await pool.fetch(
            "SELECT name, content FROM skills "
            "WHERE status='active' AND 'PlatformAssistant' = ANY(applicable_to)")
        pushed = 0
        for sk in skill_rows:
            for attempt in range(5):
                try:
                    await gateway_admin.set_agent_skill(gw_agent_id, sk["name"], sk["content"])
                    pushed += 1
                    break
                except GatewayError as exc:
                    if "unknown agent" in str(exc).lower() and attempt < 4:
                        await asyncio.sleep(1)
                        continue
                    raise
        if skill_rows:
            _log.info("Platform SHCA %s: pushed %d/%d skill(s)", gw_agent_id, pushed, len(skill_rows))
    except Exception:
        _log.exception("Platform SHCA skill push failed (non-fatal)")

    _log.info("Platform SHCA %s provisioned (model=%s)", gw_agent_id, model)
    return {"agent_id": gw_agent_id, "gateway_agent_id": gw_agent_id, "workspace": workspace_path}
