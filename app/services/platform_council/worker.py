"""Platform Research Council v2 — subagent spawn/dispatch + reaping.

Two shapes of subagent, both type 'PlatformResearch', gemini-2.5-flash-1m:

  PRODUCERS  (research-model, research-tool) — WARM, reused across runs under a
    stable gateway id (`research-model` / `research-tool`). On a cold spawn we
    push their template files; on every dispatch we refresh their docs/ inputs
    (segregated catalog/profile docs + prior findings) and kick off a turn that
    ends with the registry_write finish-tool. Reaped on idle > N min OR context
    >= 80%.

  REVIEWERS  (review-<finding8>) — EPHEMERAL and INDEPENDENT: one fresh agent
    per finding, so reviews never share context. Spawned with the finding inline
    in the kickoff; only `tool`/`mcp` findings get a repo clone (models are judged
    against the supportability gate in the prompt). They do ONE registry_verdict
    turn and are reaped shortly after going idle.

Branch wiring: the repo branch = env branch (dev->dev, prod->main).
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError
from app.services.platform_council import registry as _registry
from app.services.platform_council.inventory import (
    build_model_catalog_md, build_go_profile_md, build_shca_profile_md,
    build_tool_catalog_md,
)

log = logging.getLogger(__name__)

# Producers research the control-panel repo (most model/tool/agent config lives here).
# Repo/URL are deployment config (PLATFORM_REPO_NAME / PLATFORM_CLONE_URL) — not hardcoded.
from app.config import get_settings as _get_settings
PLATFORM_REPO_NAME = _get_settings().platform_repo_name
PLATFORM_CLONE_URL = _get_settings().platform_clone_url

PRODUCER_TEMPLATES = {"model": "research-model", "tool": "research-tool",
                      "skill": "research-skill"}
REVIEWER_TEMPLATE = "review-integration"

# Reviewer repo clone is only needed for code-level (tool/mcp) findings; model
# findings are judged against the supportability gate, no code required.
REVIEW_NEEDS_REPO = {"tool", "mcp"}

_AGENT_ACK_TIMEOUT = 60


def env_branch() -> str:
    """The repo branch this environment researches: prod -> main, else dev."""
    app_env = (getattr(get_settings(), "app_env", "") or "").lower()
    return "main" if app_env in ("production", "prod") else "dev"


def producer_instance_id(kind: str) -> str:
    """Stable, short gateway id per producer so the warm session is reused."""
    return f"research-{kind}"


def reviewer_instance_id(label: str) -> str:
    """Ephemeral reviewer id for a research×source group (short — gateway
    truncates ids). label is e.g. '<run8>-model' or '<run8>-tool'."""
    return f"review-{label}"[:48]


async def _mint_repo_token() -> str:
    try:
        from app.github_tokens import generate_installation_token
        tok = await generate_installation_token([PLATFORM_REPO_NAME],
                                                permissions={"contents": "read"})
        return (tok or {}).get("token") or ""
    except Exception as exc:
        log.info("platform: repo token mint failed (trying tokenless): %s", exc)
        return ""


async def _set_agent_file_retry(instance_id: str, filename: str, content: str,
                                attempts: int = 25) -> bool:
    """Push one workspace file, retrying on failure. agents.create returns before
    the gateway finishes its slow async config reload (~14s on dev), so an
    immediate set_agent_file hits "unknown agent id". Retrying the actual write is
    far more reliable than polling agents.list (which lags badly on dev). Returns
    True once the write lands, False after all attempts."""
    import asyncio as _asyncio
    for attempt in range(attempts):
        try:
            await gateway_admin.set_agent_file(instance_id, filename, content)
            return True
        except Exception as exc:
            if attempt == attempts - 1:
                log.warning("platform: set_agent_file %s/%s failed after %d tries: %s",
                            instance_id, filename, attempts, exc)
            else:
                await _asyncio.sleep(1)
    return False


# ── Producer data docs (refreshed on every dispatch) ─────────────────────────

async def _prior_findings_md(categories: list[str]) -> str:
    """Compact recap of existing registry findings in the given categories, so a
    producer builds on its prior work instead of repeating it."""
    seen: list[str] = []
    for cat in categories:
        rows = await _registry.search(category=cat, limit=50)
        for r in rows:
            seen.append(
                f"- [{r['category']}/{r.get('target_role','all')}] {r['title']} "
                f"(fingerprint `{r['fingerprint']}`, verdict {r['verdict']})"
            )
    if not seen:
        return "# Prior findings\n\n_(none yet — this is the first run)_\n"
    return ("# Prior findings (already in the registry — do NOT repeat; only add "
            "new items or material updates)\n\n" + "\n".join(seen[:120]) + "\n")


async def _push_producer_docs(pool, kind: str, instance_id: str) -> None:
    """Push/refresh the segregated docs/ inputs for a producer. GO and SHCA are
    SEPARATE files for the model producer."""
    if kind == "model":
        await gateway_admin.set_agent_file(
            instance_id, "docs/MODEL_CATALOG.md", (await build_model_catalog_md(pool))[:90000])
        await gateway_admin.set_agent_file(
            instance_id, "docs/GO_PROFILE.md", (await build_go_profile_md(pool))[:40000])
        await gateway_admin.set_agent_file(
            instance_id, "docs/SHCA_PROFILE.md", (await build_shca_profile_md(pool))[:40000])
        await gateway_admin.set_agent_file(
            instance_id, "docs/PRIOR_FINDINGS.md", (await _prior_findings_md(["model"]))[:40000])
    elif kind == "tool":
        await gateway_admin.set_agent_file(
            instance_id, "docs/TOOL_CATALOG.md", (await build_tool_catalog_md(pool))[:90000])
        await gateway_admin.set_agent_file(
            instance_id, "docs/PRIOR_FINDINGS.md",
            (await _prior_findings_md(["tool", "mcp"]))[:40000])
    elif kind == "skill":
        # Current skills (for gap analysis) + prior skill findings — the skill worker's
        # AGENTS.md reads both. Catalog is built inline from the skills table.
        try:
            rows = await pool.fetch(
                "SELECT name, applicable_to, description FROM skills "
                "WHERE status='active' ORDER BY name")
            body = "\n".join(
                f"- `{r['name']}` ({', '.join(r['applicable_to'] or []) or 'all'}) — "
                f"{r['description'] or ''}" for r in rows)
            catalog = "# Current agent skills\n\n" + (body or "_(none yet)_") + "\n"
        except Exception:
            catalog = "# Current agent skills\n\n_(unavailable)_\n"
        await gateway_admin.set_agent_file(instance_id, "docs/SKILL_CATALOG.md", catalog[:90000])
        await gateway_admin.set_agent_file(
            instance_id, "docs/PRIOR_FINDINGS.md", (await _prior_findings_md(["skill"]))[:40000])


def _producer_kickoff(kind: str, run_id: str | None, question: str) -> str:
    focus = (f"\n\nSPECIFIC FOCUS for this run: {question}\n" if question else "")
    run_line = (f"Your run_id is `{run_id}` — pass it as `run_id` in your registry_write call. "
                if run_id else "")
    return (
        f"Research request (kind: {kind}_research). {run_line}Follow your AGENTS.md "
        f"workflow exactly: read all your docs/ inputs first, call registry_search to "
        f"avoid duplicating existing findings, run your structured method, then submit "
        f"your ranked top findings with the registry_write finish-tool (this ends your "
        f"turn — there is no chat).{focus}"
    )


async def spawn_producer(kind: str, run_id: str | None = None,
                         question: str = "", include_prior: bool = True) -> dict:
    """Provision-or-reuse a producer (model|tool) and dispatch a research turn.

    Returns {"ok", "instance_id", "spawned", "reason"?}.
    """
    template_id = PRODUCER_TEMPLATES.get(kind)
    if not template_id:
        return {"ok": False, "reason": f"unknown producer kind {kind!r} "
                f"(expected one of {sorted(PRODUCER_TEMPLATES)})"}

    pool = get_pool()
    instance_id = producer_instance_id(kind)
    workspace = f"~/.openclaw/workspace-{instance_id}"
    session_key = f"agent:{instance_id}:platform-{kind}"

    tmpl = await pool.fetchrow(
        "SELECT id, model, tools_allow FROM templates WHERE id=$1 AND status='active'",
        template_id,
    )
    if tmpl is None:
        return {"ok": False, "reason": f"template {template_id!r} missing — run "
                "platform_v2_templates.sql"}
    files = await pool.fetch(
        "SELECT filename, content FROM template_files WHERE template_id=$1", template_id)
    files_map = {r["filename"]: r["content"] for r in files}

    # Decide cold vs warm by the GATEWAY's actual state (not a possibly-stale DB
    # row): always attempt agents.create. Created => fresh => push bootstrap.
    # "already exists" => warm => reuse.
    cold = False
    try:
        await gateway_admin.call("agents.create", {
            "name": instance_id, "workspace": workspace,
            "model": tmpl["model"], "tools": {"allow": list(tmpl["tools_allow"])},
        })
        cold = True
    except GatewayError as exc:
        if "already exists" not in str(exc):
            return {"ok": False, "reason": f"agents.create failed: {exc}"}

    if cold:
        # Push bootstrap files with retry — the first write absorbs the gateway's
        # slow async-reload "unknown agent id" lag and establishes readiness.
        if not await _set_agent_file_retry(instance_id, "AGENTS.md", files_map.get("AGENTS.md", "")):
            return {"ok": False, "instance_id": instance_id, "spawned": True,
                    "reason": f"{instance_id} not reachable for file push"}
        for fn in ("SOUL.md", "USER.md", "IDENTITY.md"):
            await _set_agent_file_retry(instance_id, fn, files_map.get(fn, ""))

    # Track/refresh the agents row (for the reaper + enumeration).
    await pool.execute(
        """
        INSERT INTO agents (id, name, type, scope, model, emoji, tools_allow, tools_deny,
                            memory_enabled, created_by, gateway_agent_id, workspace,
                            auto_created, is_template, status, template_id, last_active_at)
        VALUES ($1, $2, 'PlatformResearch', 'goal', $3, '🔬', $4, '{}', false, 'system',
                $1, $5, true, FALSE, 'active', $6, NOW())
        ON CONFLICT (id) DO UPDATE SET status='active', last_active_at=NOW()
        """,
        instance_id, f"Research: {kind}", tmpl["model"], list(tmpl["tools_allow"]),
        workspace, template_id,
    )

    # Refresh segregated docs on every dispatch (cold + warm).
    await _push_producer_docs(pool, kind, instance_id)

    try:
        await gateway_admin.call(
            "agent",
            {"agentId": instance_id, "sessionKey": session_key,
             "message": _producer_kickoff(kind, run_id, question),
             "idempotencyKey": f"prod-{kind}-{(run_id or '')}-{abs(hash(question)) % 10**8}"},
            timeout=_AGENT_ACK_TIMEOUT,
        )
    except Exception as exc:
        return {"ok": False, "instance_id": instance_id, "spawned": cold,
                "reason": f"dispatch failed: {exc}"}

    log.info("platform: dispatched %s producer (instance=%s, spawned=%s, run=%s)",
             kind, instance_id, cold, run_id)
    return {"ok": True, "instance_id": instance_id, "spawned": cold}


DISCOVERY_TEMPLATE = "platform-discovery"
DISCOVERY_INSTANCE = "platform-discovery"


async def spawn_discovery(trigger: str = "manual") -> dict:
    """Provision-or-reuse the Independent Platform Research scout (warm, fixed id) and
    dispatch a breadth-first discovery sweep. It logs ≤15 recent items across
    model/tool/skill to platform_discoveries via discovery_write. Returns
    {"ok", "instance_id", "spawned", "reason"?}."""
    import uuid as _uuid
    pool = get_pool()
    instance_id = DISCOVERY_INSTANCE
    workspace = f"~/.openclaw/workspace-{instance_id}"
    session_key = f"agent:{instance_id}:platform-discovery"

    tmpl = await pool.fetchrow(
        "SELECT id, model, tools_allow FROM templates WHERE id=$1 AND status='active'",
        DISCOVERY_TEMPLATE)
    if tmpl is None:
        return {"ok": False, "reason": f"template {DISCOVERY_TEMPLATE!r} missing — run "
                "platform_discovery_worker_template.sql"}
    files = await pool.fetch(
        "SELECT filename, content FROM template_files WHERE template_id=$1", DISCOVERY_TEMPLATE)
    files_map = {r["filename"]: r["content"] for r in files}

    cold = False
    try:
        await gateway_admin.call("agents.create", {
            "name": instance_id, "workspace": workspace,
            "model": tmpl["model"], "tools": {"allow": list(tmpl["tools_allow"])},
        })
        cold = True
    except GatewayError as exc:
        if "already exists" not in str(exc):
            return {"ok": False, "reason": f"agents.create failed: {exc}"}

    # Push bootstrap files every spawn (cold OR warm). The scout is stateless
    # (memory_enabled=False), so re-pushing is safe and lets template/prompt edits
    # take effect on the next sweep without recreating the warm agent.
    if not await _set_agent_file_retry(instance_id, "AGENTS.md", files_map.get("AGENTS.md", "")):
        return {"ok": False, "instance_id": instance_id, "spawned": cold,
                "reason": f"{instance_id} not reachable for file push"}
    for fn in ("SOUL.md", "USER.md", "IDENTITY.md"):
        await _set_agent_file_retry(instance_id, fn, files_map.get(fn, ""))

    await pool.execute(
        """
        INSERT INTO agents (id, name, type, scope, model, emoji, tools_allow, tools_deny,
                            memory_enabled, created_by, gateway_agent_id, workspace,
                            auto_created, is_template, status, template_id, last_active_at)
        VALUES ($1, 'Independent Platform Research', 'PlatformResearch', 'goal', $2, '🔭',
                $3, '{}', false, 'system', $1, $4, true, FALSE, 'active', $5, NOW())
        ON CONFLICT (id) DO UPDATE SET status='active', last_active_at=NOW()
        """,
        instance_id, tmpl["model"], list(tmpl["tools_allow"]), workspace, DISCOVERY_TEMPLATE,
    )

    kickoff = (
        "Run a discovery sweep now. Follow your AGENTS.md: for each lane (model, tool, "
        "skill) find what shipped in the last ~2 weeks, call discovery_search first to "
        "skip anything already logged, then log up to 15 entries total via discovery_write. "
        "That discovery_write call ends your turn.")
    try:
        await gateway_admin.call(
            "agent",
            {"agentId": instance_id, "sessionKey": session_key, "message": kickoff,
             "idempotencyKey": f"discovery-{trigger}-{_uuid.uuid4().hex[:8]}"},
            timeout=_AGENT_ACK_TIMEOUT,
        )
    except Exception as exc:
        return {"ok": False, "instance_id": instance_id, "spawned": cold,
                "reason": f"dispatch failed: {exc}"}

    log.info("platform: dispatched discovery sweep (instance=%s, spawned=%s, trigger=%s)",
             instance_id, cold, trigger)
    return {"ok": True, "instance_id": instance_id, "spawned": cold}


# ── Reviewer (one ephemeral agent per research×source group) ─────────────────

def _reviewer_kickoff(findings: list[dict]) -> str:
    needs_repo = any(f.get("category") in REVIEW_NEEDS_REPO for f in findings)
    repo_line = ("A read-only clone of the platform repo is under `repo/` — read the "
                 "relevant code to confirm integration paths. " if needs_repo else
                 "No repo clone (these are model findings — judge them against the "
                 "supportability gate in your AGENTS.md). ")
    import json as _json
    blocks = []
    for f in findings:
        blocks.append(
            f"--- finding id={f['id']} ---\n"
            f"category: {f.get('category')}\ntarget_role: {f.get('target_role')}\n"
            f"provider_family: {f.get('provider_family')}\ntitle: {f.get('title')}\n"
            f"summary: {f.get('summary')}\n"
            f"detail:\n{str(f.get('detail'))[:12000]}\n"
            f"evidence: {_json.dumps(f.get('evidence'))[:1500]}"
        )
    return (
        f"Integration review request. Review EACH of the {len(findings)} findings below "
        f"and call registry_verdict ONCE PER FINDING, using that finding's id. {repo_line}\n\n"
        f"You MUST call registry_verdict for EVERY finding id listed — that is the only way "
        f"your review is recorded. Work through them one by one; do not stop or reply until "
        f"you have submitted a verdict for all {len(findings)}.\n\n"
        f"===== FINDINGS TO REVIEW ({len(findings)}) =====\n" + "\n\n".join(blocks)
    )


async def spawn_reviewer(findings: list[dict], label: str) -> dict:
    """Spawn one fresh reviewer for a research×source GROUP and have it verdict ALL
    the group's findings in a single turn. `findings` are full registry rows;
    `label` is a short unique id component (e.g. '<run8>-model'). Ephemeral —
    reaped after idle."""
    findings = [f for f in (findings or []) if f.get("id")]
    if not findings:
        return {"ok": False, "reason": "no findings to review"}

    pool = get_pool()
    instance_id = reviewer_instance_id(label)
    workspace = f"~/.openclaw/workspace-{instance_id}"
    session_key = f"agent:{instance_id}:platform-review"
    branch = env_branch()
    needs_repo = any(f.get("category") in REVIEW_NEEDS_REPO for f in findings)

    tmpl = await pool.fetchrow(
        "SELECT id, model, tools_allow FROM templates WHERE id=$1 AND status='active'",
        REVIEWER_TEMPLATE,
    )
    if tmpl is None:
        return {"ok": False, "reason": f"template {REVIEWER_TEMPLATE!r} missing — run "
                "platform_v2_templates.sql"}
    files = await pool.fetch(
        "SELECT filename, content FROM template_files WHERE template_id=$1", REVIEWER_TEMPLATE)
    files_map = {r["filename"]: r["content"] for r in files}

    # Ephemeral: a previous reviewer for this finding may still exist (retry) —
    # reuse if so, else create fresh.
    cold = False
    try:
        await gateway_admin.call("agents.create", {
            "name": instance_id, "workspace": workspace,
            "model": tmpl["model"], "tools": {"allow": list(tmpl["tools_allow"])},
        })
        cold = True
    except GatewayError as exc:
        if "already exists" not in str(exc):
            return {"ok": False, "reason": f"agents.create failed: {exc}"}

    if cold:
        # Push bootstrap files with retry (first write establishes gateway
        # readiness past the slow async reload), THEN clone the repo.
        if not await _set_agent_file_retry(instance_id, "AGENTS.md", files_map.get("AGENTS.md", "")):
            return {"ok": False, "instance_id": instance_id, "spawned": True,
                    "reason": f"{instance_id} not reachable for file push"}
        for fn in ("SOUL.md", "USER.md", "IDENTITY.md"):
            await _set_agent_file_retry(instance_id, fn, files_map.get(fn, ""))
        if needs_repo:
            token = await _mint_repo_token()
            try:
                resp = await gateway_admin.call(
                    "agents.workspace.prepareRepo",
                    {"agentId": instance_id, "cloneUrl": PLATFORM_CLONE_URL,
                     "branch": branch, "token": token, "dest": "repo"},
                    timeout=300,
                )
                if not isinstance(resp, dict) or not resp.get("ok"):
                    return {"ok": False, "reason": f"prepareRepo failed: {resp}"}
            except Exception as exc:
                return {"ok": False, "reason": f"prepareRepo error: {exc}"}

    await pool.execute(
        """
        INSERT INTO agents (id, name, type, scope, model, emoji, tools_allow, tools_deny,
                            memory_enabled, created_by, gateway_agent_id, workspace,
                            auto_created, is_template, status, template_id, last_active_at)
        VALUES ($1, $2, 'PlatformResearch', 'goal', $3, '🔎', $4, '{}', false, 'system',
                $1, $5, true, FALSE, 'active', $6, NOW())
        ON CONFLICT (id) DO UPDATE SET status='active', last_active_at=NOW()
        """,
        instance_id, f"Review: {label}", tmpl["model"], list(tmpl["tools_allow"]),
        workspace, REVIEWER_TEMPLATE,
    )

    try:
        await gateway_admin.call(
            "agent",
            {"agentId": instance_id, "sessionKey": session_key,
             "message": _reviewer_kickoff(findings),
             "idempotencyKey": f"review-{label}"},
            timeout=_AGENT_ACK_TIMEOUT,
        )
    except Exception as exc:
        return {"ok": False, "instance_id": instance_id, "spawned": cold,
                "reason": f"dispatch failed: {exc}"}

    log.info("platform: dispatched reviewer %s for %d findings (label=%s, repo=%s)",
             instance_id, len(findings), label, needs_repo)
    return {"ok": True, "instance_id": instance_id, "spawned": cold}


# ── Nudges (re-prompt a worker that ended a turn without its finish tool) ────

async def nudge_producer(kind: str, run_id: str | None, attempt: int) -> bool:
    """Re-prompt a producer that hasn't recorded findings yet. Flash sometimes
    ends a turn without calling registry_write; the warm session still holds the
    research context, so this pushes it to make the tool call."""
    instance_id = producer_instance_id(kind)
    session_key = f"agent:{instance_id}:platform-{kind}"
    source = f"{kind}_research"
    run_line = f" run_id={run_id}" if run_id else ""
    msg = (
        "[System] You have NOT recorded any findings yet. Do not reply with prose and "
        "do not ask questions — call the registry_write tool NOW with your top findings "
        f"(source={source}{run_line}; for models at most 2 per role). That tool call is "
        "the ONLY way to finish — nothing is saved until you make it."
    )
    try:
        await gateway_admin.call("agent", {
            "agentId": instance_id, "sessionKey": session_key, "message": msg,
            "idempotencyKey": f"nudge-{kind}-{run_id}-{attempt}"}, timeout=_AGENT_ACK_TIMEOUT)
        log.info("platform: nudged producer %s (attempt %d, run=%s)", instance_id, attempt, run_id)
        return True
    except Exception as exc:
        log.warning("platform: nudge_producer %s failed: %s", instance_id, exc)
        return False


async def nudge_reviewer(label: str, attempt: int) -> bool:
    """Re-prompt a reviewer that hasn't verdicted all its findings yet."""
    instance_id = reviewer_instance_id(label)
    session_key = f"agent:{instance_id}:platform-review"
    msg = (
        "[System] You still have findings without a verdict. Do not reply with prose — "
        "call the registry_verdict tool NOW for each remaining finding id from your "
        "kickoff. That tool call is the ONLY way your review is recorded."
    )
    try:
        await gateway_admin.call("agent", {
            "agentId": instance_id, "sessionKey": session_key, "message": msg,
            "idempotencyKey": f"nudge-rev-{label}-{attempt}"}, timeout=_AGENT_ACK_TIMEOUT)
        log.info("platform: nudged reviewer %s (attempt %d)", instance_id, attempt)
        return True
    except Exception as exc:
        log.warning("platform: nudge_reviewer %s failed: %s", instance_id, exc)
        return False


# ── Reaping ──────────────────────────────────────────────────────────────────
WORKER_CONTEXT_REAP_PCT = 0.80       # producers: reap at >= 80% context
_REVIEWER_IDLE_SECONDS = 15 * 60     # reviewers: one short turn, reap soon after idle


def _producer_idle_seconds() -> int:
    mins = getattr(get_settings(), "platform_worker_idle_minutes", 30) or 30
    return int(mins) * 60


async def _session_context_pct(instance_id: str, session_suffix: str) -> float | None:
    """Live context-usage fraction from sessions.status, or None. totalTokens / contextTokens."""
    try:
        resp = await gateway_admin.call(
            "sessions.status", {"key": f"agent:{instance_id}:{session_suffix}"}, timeout=15)
    except Exception as exc:
        log.debug("platform: sessions.status failed for %s: %s", instance_id, exc)
        return None
    if not isinstance(resp, dict):
        return None
    total, window = resp.get("totalTokens"), resp.get("contextTokens")
    if isinstance(total, (int, float)) and isinstance(window, (int, float)) and window > 0:
        return float(total) / float(window)
    return None


async def reap_idle_workers() -> int:
    """Reap PlatformResearch subagents. Reviewers (review-*) are ephemeral —
    reaped after a short idle. Producers (research-*) are warm — reaped on idle >
    N min OR context >= 80%. Best-effort. Called periodically by the watchdog."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, EXTRACT(EPOCH FROM (NOW() - COALESCE(last_active_at, NOW()))) AS idle_secs
          FROM agents
         WHERE type = 'PlatformResearch' AND status = 'active'
        """
    )
    reaped = 0
    producer_idle = _producer_idle_seconds()
    for r in rows:
        instance_id = r["id"]
        idle_secs = float(r["idle_secs"] or 0)
        reason = None

        if instance_id.startswith("review-"):
            if idle_secs >= _REVIEWER_IDLE_SECONDS:
                reason = f"reviewer idle {round(idle_secs/60)}m"
        elif instance_id.startswith("research-"):
            kind = instance_id[len("research-"):]
            if idle_secs >= producer_idle:
                reason = f"producer idle {round(idle_secs/60)}m"
            else:
                pct = await _session_context_pct(instance_id, f"platform-{kind}")
                if pct is not None and pct >= WORKER_CONTEXT_REAP_PCT:
                    reason = f"context {round(pct*100)}%"
        if not reason:
            continue

        try:
            await gateway_admin.call("agents.delete", {"agentId": instance_id})
        except Exception as exc:
            # Not found on the gateway == already deleted → treat as success and
            # just finalize the DB below (no warning, no retry). Only a real error
            # (e.g. a timeout) is worth a warning.
            if "not found" in str(exc).lower():
                log.debug("platform: %s already absent on gateway — finalizing", instance_id)
            else:
                log.warning("platform: agents.delete failed for %s: %s", instance_id, exc)
        try:
            # agents.status CHECK is ('active','deleted') — NOT 'destroyed' (that's
            # agent_instances). Writing 'destroyed' here threw every tick, so the row
            # stayed 'active' and got re-reaped endlessly (log spam + gateway hammering).
            await pool.execute(
                "UPDATE agents SET status='deleted' WHERE id=$1 AND status='active'",
                instance_id)
            await pool.execute("DELETE FROM agent_gateway_bindings WHERE agent_id=$1", instance_id)
        except Exception:
            log.exception("platform: failed to mark %s deleted", instance_id)
        reaped += 1
        log.info("platform: reaped %s (%s)", instance_id, reason)
    return reaped
