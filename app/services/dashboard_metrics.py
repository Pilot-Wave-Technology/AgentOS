"""Data layer for the system + personal Dashboard page.

This module is the SOLE boundary between the dashboard and the OpenClaw
gateway. Every gateway RPC the dashboard needs is called from here and nowhere
else, so a future OpenClaw version change only touches this one adapter file
(see docs / plan: "keep the dependency boundary thin"). For v1 we call ONLY
existing gateway RPCs — no OpenClaw code changes are required.

Responsibilities:
  - collect(user)            assemble the whole dashboard payload
  - collect_personal(user)   "my goals / tasks / agents / activity" (all users)
  - collect_system()         system-wide health / cost / attention / fleet
                             / live / activity (admin & editor only)
  - humanize_event(...)      translate raw ledger event types to plain English
  - etag_for(...)            stable hash over volatile data (drives 304 caching)

Every read is bounded (aggregate or LIMIT) and read-only. Gateway calls are
wrapped so a slow or unreachable gateway degrades a single panel rather than
failing the whole page.
"""
import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.database import get_pool
from app.services import gateway_cache

_log = logging.getLogger(__name__)

# Gateway RPCs must return fast or the page degrades. The default
# GatewayAdmin.call timeout is 30s; the dashboard uses a much shorter budget so
# a stuck gateway turns into a red badge within ~8s instead of hanging.
_GATEWAY_TIMEOUT = 8.0

# A task is "open" for the personal panel when it is not in a terminal state.
# Static literal (no user input) — safe to inline into the query string.
_OPEN_TASK_STATE_FILTER = "state NOT IN ('done', 'failed', 'cancelled', 'rejected')"

# Event types excluded from the activity feed — high-frequency internal events
# (every chat message, every tool call) that dominate the feed without adding
# meaningful signal for a non-technical reader.
_ACTIVITY_SKIP = (
    "comms.message.posted",
    "comms.thread.refreshed",
    "msg.human_to_shca",
    "msg.shca_to_human",
    "msg.shca_to_claude",
    "msg.claude_to_shca",
    "claude.tool_call",
    "claude.tool_result",
    "claude.session_spawned",
    "checkpoint.written",
    "ws.worktree_deleted",
    "ws.worktree_created",   # too frequent in active dev; commits/PRs are more meaningful
)

# Plain-language labels for the activity feed. Events not listed fall back to
# humanize_event() (capitalised dotted string).
_EVENT_LABELS = {
    # Goals
    "goal.created":             "New goal created",
    "goal.updated":             "A goal was updated",
    "goal.completed":           "A goal was completed",
    "goal.cancelled":           "A goal was cancelled",
    "goal.member_added":        "Someone joined a goal",
    "goal.member_removed":      "Someone left a goal",
    "goal.standup_posted":      "A standup was posted",
    "goal.state_doc_updated":   "A goal's state doc was updated",
    # Stories
    "story.created":            "A new story was added",
    "story.state_changed":      "A story changed status",
    "story.phase_changed":      "A story moved to a new phase",
    "story.paused":             "A story was paused",
    "story.resumed":            "A story resumed",
    # Tasks
    "task.created":             "A task was created",
    "task.assigned":            "A task was assigned",
    "task.status_changed":      "A task changed status",
    "task.state_changed":       "A task changed status",
    "task.completed":           "A task finished",
    "task.failed":              "A task failed",
    "task.cancelled":           "A task was cancelled",
    "task.unblocked":           "A task was unblocked",
    "task.handoff":             "A task was handed off",
    "task.output_registered":   "Task output was recorded",
    # Plans
    "plan.proposed":            "A plan was proposed",
    "plan.approved":            "A plan was approved",
    "plan.auto_approved":       "A plan was auto-approved",
    "plan.rejected":            "A plan was rejected",
    "plan.replan_completed":    "A task was replanned",
    # Code / workspace
    "ws.commit_created":        "A commit was made",
    "ws.push_executed":         "Code was pushed",
    "ws.pr_opened":             "A pull request was opened",
    "ws.branch_created":        "A branch was created",
    "ws.branch_merged":         "A branch was merged",
    "ws.branch_abandoned":      "A branch was abandoned",
    # Diffs / review
    "diff.ready":               "Code changes are ready for review",
    "diff.approved":            "Code changes were approved",
    "diff.rejected":            "Code changes were rejected",
    "assessment.completed":     "An automated review finished",
    # Deployment
    "dep.executed":             "A deployment was run",
    "dep.approved":             "A deployment was approved",
    "dep.smoke_test_passed":    "Deployment tests passed",
    "dep.smoke_test_failed":    "Deployment tests failed",
    "dep.rolled_back":          "A deployment was rolled back",
    # Approvals / decisions / escalations
    "approval.requested":       "An approval was requested",
    "approval.granted":         "An approval was granted",
    "approval.denied":          "An approval was denied",
    "approval.escalated":       "An approval was escalated",
    "decision.recorded":        "A decision was recorded",
    "esc.requested":            "An escalation was raised",
    "esc.resolved":             "An escalation was resolved",
    "comms.question.raised":    "A question was raised",
    "comms.question.answered":  "A question was answered",
    # Phases
    "phase.started":            "A new phase started",
    "phase.completed":          "A phase was completed",
    # Agents / IAM
    "iam.agent.provisioned":    "An agent was started",
    "iam.agent.suspended":      "An agent was paused",
    "iam.agent.resumed":        "An agent resumed",
    "iam.agent.decommissioned": "An agent was retired",
    "iam.agent.deleted":        "An agent was removed",
    "iam.agent.updated":        "An agent's settings changed",
    "iam.user.created":         "A new user joined",
    "iam.signup.requested":     "Someone requested access",
    "iam.signup.approved":      "A signup was approved",
    "iam.signup.rejected":      "A signup was rejected",
    # GO orchestrator
    "go.task_created":          "The orchestrator created a task",
    "go.blocker_detected":      "A blocker was detected",
    "go.blocker_resolved":      "A blocker was resolved",
    "go.decision_requested":    "A decision was requested",
    # Resources
    "resource.goal.registered": "A resource was added",
    "resource.goal.scanned":    "A resource was scanned",
    "resource.goal.locked":     "A resource was locked",
    "resource.goal.unlocked":   "A resource was unlocked",
}

_ACTOR_LABELS = {
    "human": "a person",
    "shca": "an assistant",
    "independent_agent": "an agent",
    "goal_orchestrator": "the orchestrator",
    "system": "the system",
}

# Tailwind bg class for the colored dot in the activity feed, keyed by category.
_CATEGORY_DOT = {
    "task":     "bg-blue-500",
    "story":    "bg-emerald-500",
    "goal":     "bg-indigo-500",
    "agent":    "bg-purple-500",
    "approval": "bg-amber-500",
    "resource": "bg-cyan-500",
    "other":    "bg-gray-500",
}

def _event_category(event_type: str) -> str:
    prefix = event_type.split(".")[0]
    return {
        "task":     "task",
        "story":    "story",
        "goal":     "goal",
        "iam":      "agent",
        "approval": "approval",
        "resource": "resource",
    }.get(prefix, "other")


# ── Public API ────────────────────────────────────────────────────────────────

async def collect(user: dict) -> dict:
    """Assemble the full dashboard payload for the given user.

    The personal section is built for everyone. The system section is built only
    for admins and editors — a plain member never even triggers the system
    queries or gateway calls.
    """
    import time as _time
    _t0 = _time.monotonic()
    is_operator = user.get("system_role") in ("admin", "editor")
    _log.info(
        "dashboard.collect uid=%s role=%s operator=%s",
        user.get("uid"), user.get("system_role"), is_operator,
    )

    if is_operator:
        (personal, system), refreshed_at = await asyncio.gather(
            asyncio.gather(collect_personal(user), collect_system()),
            get_refreshed_at(),
        )
    else:
        personal, refreshed_at = await asyncio.gather(
            collect_personal(user),
            get_refreshed_at(),
        )
        system = None

    degraded = list(personal.get("degraded", []))
    if system:
        degraded.extend(system.get("degraded", []))

    _log.info("dashboard.collect total_ms=%d", int((_time.monotonic() - _t0) * 1000))
    return {
        "personal": personal,
        "system": system,
        "is_operator": is_operator,
        "degraded": degraded,
        "refreshed_at": refreshed_at,
        "etag": etag_for(personal, system, refreshed_at),
    }


async def collect_personal(user: dict) -> dict:
    """"My world" across every goal the user belongs to. DB-only, low risk."""
    pool = get_pool()
    uid = user["uid"]
    degraded: list[str] = []

    try:
        goal_rows = await pool.fetch(
            """SELECT g.id, g.name, g.color_tag,
                      COALESCE(g.completion_pct, 0) AS completion_pct
                 FROM goals g
                 JOIN goal_members m ON m.goal_id = g.id
                WHERE m.uid = $1 AND g.status = 'active'
                ORDER BY g.created_at DESC""",
            uid,
        )
        goals = [dict(r) for r in goal_rows]
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard personal goals failed: %s", exc)
        goals = []
        degraded.append("my goals")

    goal_ids = [g["id"] for g in goals]

    # "My tasks" = all active tasks in the user's active goals.
    # Phase tasks (executor='claude-code-phase') are created by the GO agent via DAG ingest
    # and have no source_thread_id, so the old threads-JOIN approach returns zero.
    # Goal membership is the correct scope for the current architecture.
    try:
        task_row = await pool.fetchrow(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE t.state = 'blocked')           AS blocked,
                      count(*) FILTER (WHERE t.state = 'awaiting_approval') AS needs_approval
                 FROM tasks t
                 JOIN goals g ON g.id = t.goal_id
                 JOIN goal_members gm ON gm.goal_id = g.id AND gm.uid = $1
                WHERE g.status = 'active'
                  AND t.status = 'active'
                  AND t.state NOT IN ('done', 'failed', 'cancelled', 'rejected', 'halted', 'approved')""",
            uid,
        )
        open_total = task_row["total"]
        open_blocked = task_row["blocked"]
        open_needs_approval = task_row["needs_approval"] or 0
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard personal tasks failed: %s", exc)
        open_total = open_blocked = open_needs_approval = 0
        degraded.append("my tasks")

    agents: list[dict] = []
    try:
        actor_id = user.get("actor_id")
        if actor_id:
            from app.services.agent_registry.assistant_queries import list_user_assistant_agents
            agents = await list_user_assistant_agents(pool, uuid.UUID(str(actor_id)))
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard personal agents failed: %s", exc)
        degraded.append("my agents")

    # Goals-at-risk: per-goal blocked task count + time since last ledger event.
    # A goal is "at risk" when it has blocked tasks AND has been quiet for 48h,
    # or when it has 3+ blocked tasks regardless of recency.
    at_risk: list[dict] = []
    if goal_ids:
        try:
            risk_rows = await pool.fetch(
                """SELECT g.id,
                          count(t.id) FILTER (WHERE t.state = 'blocked') AS blocked,
                          le.last_event
                     FROM goals g
                     LEFT JOIN tasks t
                            ON t.goal_id = g.id AND t.status = 'active'
                     LEFT JOIN (
                            SELECT context_goal_id, max(timestamp) AS last_event
                            FROM   ledger_events
                            GROUP  BY context_goal_id
                     ) le ON le.context_goal_id = g.id
                    WHERE g.id = ANY($1::text[])
                    GROUP BY g.id, le.last_event""",
                goal_ids,
            )
            now = datetime.now(timezone.utc)
            goal_name = {g["id"]: g["name"] for g in goals}
            for r in risk_rows:
                blocked = r["blocked"] or 0
                last = r["last_event"]
                hours_quiet = (now - last).total_seconds() / 3600 if last else 9999
                if blocked >= 3 or (blocked > 0 and hours_quiet >= 48):
                    at_risk.append({
                        "id": r["id"],
                        "name": goal_name.get(r["id"], r["id"]),
                        "blocked": blocked,
                        "hours_quiet": int(hours_quiet),
                    })
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("dashboard goals-at-risk failed: %s", exc)

    # Standup digest: latest goal.standup_posted events across the user's goals.
    standups: list[dict] = []
    if goal_ids:
        try:
            srows = await pool.fetch(
                """SELECT le.context_goal_id, g.name AS goal_name,
                          le.timestamp, le.payload
                     FROM ledger_events le
                     LEFT JOIN goals g ON g.id = le.context_goal_id
                    WHERE le.event_type = 'goal.standup_posted'
                      AND le.context_goal_id = ANY($1::text[])
                    ORDER BY le.sequence_number DESC
                    LIMIT 5""",
                goal_ids,
            )
            for r in srows:
                p = r["payload"] or {}
                # Payload shape varies — try common field names defensively.
                summary = (p.get("summary") or p.get("content") or
                           p.get("text") or p.get("message") or "")
                standups.append({
                    "goal_name": r["goal_name"] or r["context_goal_id"],
                    "goal_id": r["context_goal_id"],
                    "ago": _ago(r["timestamp"]),
                    "summary": str(summary)[:200] if summary else "",
                })
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("dashboard standup digest failed: %s", exc)

    activity, latest_seq = await _recent_activity(goal_ids=goal_ids, limit=15)

    return {
        "goals": goals,
        "goals_count": len(goals),
        "open_tasks_total": open_total,
        "open_tasks_blocked": open_blocked,
        "agents": agents,
        "agents_count": len(agents),
        "open_tasks_needs_approval": open_needs_approval,
        "at_risk": at_risk,
        "standups": standups,
        "activity": activity,
        "latest_seq": latest_seq,
        "degraded": degraded,
    }


async def _timed(name: str, coro):
    """Wrap a coroutine to log its wall-clock duration at INFO level."""
    import time as _time
    t0 = _time.monotonic()
    result = await coro
    ms = int((_time.monotonic() - t0) * 1000)
    _log.info("dashboard.timing panel=%s ms=%d", name, ms)
    return result


async def collect_system() -> dict:
    """System-wide operator view. Gateway-backed panels degrade independently."""
    import time as _time
    t0 = _time.monotonic()

    # Run all independent sub-gathers concurrently. Each is wrapped in _timed
    # so individual panel durations appear in the logs — use these to identify
    # which panels are slow (gateway calls vs DB reads vs mat view reads).
    (health, cost, attention, fleet, live, review_quality, escalations, cron,
     sandbox, slos, audit_trail, approval_sla, session_previews,
     goal_velocity, channels, taximeter,
     (activity, latest_seq)) = await asyncio.gather(
        _timed("health",            _system_health()),
        _timed("cost",              _system_cost()),
        _timed("attention",         _system_attention()),
        _timed("fleet",             _system_fleet()),
        _timed("live",              _system_live()),
        _timed("review_quality",    _system_review_quality()),
        _timed("escalations",       _system_escalations()),
        _timed("cron",              _system_cron()),
        _timed("sandbox",           _system_sandbox()),
        _timed("slos",              _system_slos()),
        _timed("audit_trail",       _system_audit_trail()),
        _timed("approval_sla",      _system_approval_sla()),
        _timed("session_previews",  _system_session_previews()),
        _timed("goal_velocity",     _system_goal_velocity()),
        _timed("channels",          _system_channels()),
        _timed("taximeter",         _system_taximeter()),
        _timed("activity",          _recent_activity(goal_ids=None, limit=20)),
    )

    # Anomaly detection reuses already-computed cost values — no extra round-trip.
    anomalies = await _timed("anomalies", _system_anomalies(cost))

    _log.info("dashboard.timing total_system_ms=%d", int((_time.monotonic() - t0) * 1000))

    degraded: list[str] = []
    for part in (health, cost, attention, fleet, live, review_quality,
                 escalations, cron, sandbox, slos, audit_trail, approval_sla,
                 session_previews, goal_velocity, channels, taximeter, anomalies):
        degraded.extend(part.pop("_degraded", []))

    return {
        "health": health,
        "cost": cost,
        "attention": attention,
        "fleet": fleet,
        "live": live,
        "review_quality": review_quality,
        "escalations": escalations,
        "cron": cron,
        "sandbox": sandbox,
        "slos": slos,
        "audit_trail": audit_trail,
        "approval_sla": approval_sla,
        "session_previews": session_previews,
        "goal_velocity": goal_velocity,
        "channels": channels,
        "taximeter": taximeter,
        "anomalies": anomalies,
        "activity": activity,
        "latest_seq": latest_seq,
        "degraded": degraded,
    }


# ── System sub-gathers (each is self-contained and never raises) ────────────────

async def _system_health() -> dict:
    """Gateway up/down + version/uptime, DB reachability, connected nodes.

    Reads health/readiness/presence from gateway_cache (refreshed every 30s).
    Confirmed response shapes:
      health:    {ok, ts, durationMs, channels, agents, sessions, ...}
      readiness: {ready, failing, uptimeMs}
      presence:  stored as {items: [{host, ip, version, platform, ...}, ...]}
    """
    pool = get_pool()

    health_data, readiness_data, presence_data = await asyncio.gather(
        gateway_cache.read("health"),
        gateway_cache.read("readiness"),
        gateway_cache.read("system-presence"),
    )

    # health RPC returns 'ok' (bool); _ok is our sentinel added on write
    gateway_ok = bool(health_data.get("_ok"))
    gateway_error = health_data.get("error") if not gateway_ok and health_data else None

    # If the health RPC/HTTP fallbacks failed but the admin WS connection is alive,
    # the gateway IS reachable (phase dispatch, agent ops all route through it).
    # Treat it as online to avoid a misleading "Offline" banner.
    if not gateway_ok:
        try:
            from app.openclaw import gateway_admin as _gw_admin
            if _gw_admin._ws is not None and not _gw_admin._ws.closed:
                _log.debug("dashboard health: RPC failed but admin WS alive — reporting online")
                gateway_ok = True
                gateway_error = None
        except Exception:
            pass

    # version lives on presence items, not in the health response
    items = presence_data.get("items", [])
    version = items[0].get("version") if items else None

    # uptime comes from readiness
    uptime_ms = readiness_data.get("uptimeMs")
    uptime = uptime_ms / 1000 if uptime_ms is not None else None

    # nodes = connected gateway processes; fall back to 1 when presence RPC is
    # unavailable but the WS connection confirms the gateway is reachable.
    nodes = len(items) if items else (1 if gateway_ok else None)

    db_ok = True
    try:
        await pool.fetchval("SELECT 1")
    except Exception as exc:
        _log.warning("dashboard DB ping failed: %s", exc)
        db_ok = False

    verdict = "operational" if (gateway_ok and db_ok) else "down"

    return {
        "verdict": verdict,
        "gateway_ok": gateway_ok,
        "gateway_error": gateway_error,
        "version": version,
        "uptime": _format_uptime(uptime),
        "db_ok": db_ok,
        "nodes": nodes,
        "_degraded": [],
    }


# ── Cost calculation SQL ──────────────────────────────────────────────────────
#
# Default calculator: SQL LATERAL join — for each token record find the pricing
# row most recently effective at recorded_at, multiply token counts by rates.
#
# Cost formula (per record):
#   (input_tokens                              × input_rate
#  + (output_tokens - thinking_tokens)        × output_rate
#  + thinking_tokens                          × COALESCE(thinking_rate, output_rate)
#  + cache_read_tokens                        × cache_read_rate
#  + cache_write_tokens                       × cache_write_rate) / 1_000_000
#
# Thinking tokens are a subset of output_tokens (always ≤ output_tokens).
# When thinking_rate IS NULL the formula collapses to output_tokens × output_rate,
# which is correct for providers that bill thinking at the standard output rate.
# Set thinking_rate explicitly in model_pricing when a model charges differently.
#
# Extension point — provider-specific calculators (future):
#   For pricing rules that cannot be expressed as tokens × rate (e.g. tiered
#   pricing above a context threshold, per-hour cache storage), implement a
#   Python subclass of CostCalculator in app/services/cost_calculator.py and
#   register it for the provider. The raw token columns in token_usage are the
#   input. When a calculator is registered for a provider, it takes precedence
#   over this SQL formula for that provider's rows.
#
# Model name normalisation: date-version suffixes are stripped so one pricing
# row covers all patch releases (claude-sonnet-4-5-20251001 → claude-sonnet-4-5).

_COST_CTE = """
WITH token_records AS (
    SELECT t.recorded_at,
           t.agent_id,
           t.goal_id,
           t.story_id,
           t.run_id,
           t.session_key,
           -- Normalise provider: strip context-window suffix so synthetic
           -- variants (e.g. google-flash-50k) resolve to their base provider.
           -- Pattern: trailing -<word>-<N>k or -<N>m (e.g. -flash-50k, -pro-100k).
           REGEXP_REPLACE(t.provider, '-[^-]+-[0-9]+[km]$', '')  AS provider,
           -- Normalise model: strip context-window suffix (-50k … -1m) then
           -- date suffix (-YYYYMMDD) so one pricing row covers all variants.
           REGEXP_REPLACE(
               REGEXP_REPLACE(t.model, '-[0-9]+[km]$', ''),
               '-[0-9]{{8}}$', '')                               AS model,
           t.input_tokens                                      AS inp,
           t.output_tokens                                     AS out,
           t.thinking_tokens                                   AS thinking,
           t.cache_read_tokens                                 AS cr,
           t.cache_write_tokens                               AS cw,
           t.input_tokens + t.output_tokens
               + t.cache_read_tokens + t.cache_write_tokens   AS total_tokens,
           t.record_type
    FROM   token_calls t
    WHERE  t.recorded_at >= now() - interval '30 days'
      AND  t.record_type = 'call'
      {extra_filter}
),
priced AS (
    SELECT r.*,
           CASE WHEN p.id IS NOT NULL THEN
               (  r.inp                        * p.input_rate
                + (r.out - r.thinking)         * p.output_rate
                + r.thinking                   * COALESCE(p.thinking_rate, p.output_rate)
                + r.cr                         * p.cache_read_rate
                + r.cw                         * p.cache_write_rate
               ) / 1000000.0
           ELSE NULL END AS cost
    FROM   token_records r
    LEFT JOIN LATERAL (
        SELECT * FROM model_pricing
        WHERE  provider       = r.provider
          AND  model          = r.model
          AND  effective_from <= r.recorded_at
        ORDER  BY effective_from DESC
        LIMIT  1
    ) p ON true
)
"""


async def _db_cost_data() -> dict:
    """Read pre-aggregated cost data from second-level cost materialized views.

    All 6 reads are simple full-table scans on small pre-aggregated views —
    no JOINs, no SUBSTRING regex, no GROUP BY. The expensive work was done
    at refresh time by dashboard_mv_cost_panels.sql.

    mv_cost_by_task is deliberately not read here: it joins via
    agent_instances.task_id -> tasks.id, which never matches under operator
    mode (the only mode in real use) since SHCA does the work in its own
    session rather than dispatching a task-scoped IA with task_id set --
    always empty, so querying it is a wasted round-trip. See
    research/dashboard_cost_goal_attribution_gap.md Finding 3.

    Never raises — returns empty structure on failure.
    """
    pool = get_pool()
    try:
        (model_rows, goal_rows,
         totals_row,
         daily_rows, daily_provider_rows,
         story_rows) = await asyncio.gather(
            pool.fetch("SELECT * FROM mv_cost_by_model ORDER BY cost DESC NULLS LAST"),
            pool.fetch("SELECT * FROM mv_cost_by_goal   ORDER BY cost DESC NULLS LAST"),
            pool.fetchrow("SELECT * FROM mv_cost_totals WHERE row_id = 1"),
            pool.fetch("SELECT * FROM mv_cost_daily          ORDER BY day"),
            pool.fetch("SELECT * FROM mv_cost_daily_provider ORDER BY day, provider"),
            pool.fetch("SELECT * FROM mv_cost_by_story ORDER BY cost DESC NULLS LAST"),
        )

        week_cost  = float(totals_row["week_cost"]  or 0) if totals_row else 0.0
        today_cost = float(totals_row["today_cost"] or 0) if totals_row else 0.0

        _log.info(
            "db cost data: %d model rows, %d goal rows, %d daily rows, "
            "%d story rows, week=%.4f today=%.4f",
            len(model_rows), len(goal_rows), len(daily_rows),
            len(story_rows), week_cost, today_cost,
        )
        return {
            "model_rows":          model_rows,
            "goal_rows":           goal_rows,
            "daily_rows":          daily_rows,
            "daily_provider_rows": daily_provider_rows,
            "story_rows":          story_rows,
            "week_cost":           week_cost,
            "today_cost":          today_cost,
        }
    except Exception as exc:
        _log.warning("db cost data query failed: %s", exc)
        return {"model_rows": [], "goal_rows": [], "daily_rows": [],
                "daily_provider_rows": [], "story_rows": [],
                "week_cost": 0.0, "today_cost": 0.0}


def _prettify_agent_label(raw: str | None) -> str:
    """Convert raw gateway agent names to human-readable labels.

    Patterns:
      go-{goal_code}                           → Orchestrator · {GOAL}
      {username}-assistant-{goal_code}-{uuid}  → SHCA · {username} / {GOAL}
      ra-{stage}-{run_id[:8]}                  → Goal Review Council · {stage}
      sec-{aspect}-{run_id[:8]}                → Security Council · {aspect}
      claude-cli/sonnet, google/gemini-…       → kept as-is (already readable)
      anything else                            → kept as-is
    """
    if not raw:
        return "Unknown"
    import re as _re
    # GO agent: go-vsdns
    if raw.startswith("go-") and "/" not in raw:
        goal_code = raw[3:].upper()
        return f"Orchestrator · {goal_code}"
    # SHCA agent: {name}-assistant-{goal_code}-{uuid_fragment}
    if "-assistant-" in raw:
        parts = raw.split("-assistant-", 1)
        username = parts[0]
        # goal_code is the first segment after '-assistant-', before the UUID
        rest = parts[1]
        # strip the trailing uuid portion (last 5 uuid groups = 36 chars with dashes)
        # format: {goal_code}-{8hex}-{4hex}-{4hex}-{4hex}-{12hex}
        m = _re.search(r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{8,}", rest, _re.I)
        goal_code = rest[: m.start()].upper() if m else rest.upper()
        return f"Assistant · {username} / {goal_code}"
    # Goal Review Council reviewer: ra-{stage}-{run_id[:8]}, see
    # _safe_instance_id in services/council/provisioning.py. Transient,
    # one-shot -- provisioned, runs once, submits its review, deleted. The
    # trailing run-id is optional here: the cost views fold multiple runs of
    # the same stage together (regexp_replace strips it before GROUP BY), so
    # this also matches the already-folded "ra-{stage}" form directly.
    if raw.startswith("ra-"):
        stage = raw[3:]
        m = _re.match(r"^(.+)-([0-9a-f]{8})$", stage, _re.I)
        if m:
            stage = m.group(1)
        return f"Goal Review Council · {stage}"
    # Security Council reviewer: sec-{aspect}-{run_id[:8]}, see
    # _safe_instance_id in services/security/provisioning.py. Same
    # transient, one-shot, fold-tolerant pattern as above.
    if raw.startswith("sec-"):
        aspect = raw[4:]
        m = _re.match(r"^(.+)-([0-9a-f]{8})$", aspect, _re.I)
        if m:
            aspect = m.group(1)
        return f"Security Council · {aspect}"
    return raw


def _classify_unresolved(raw_agent_label: str | None, resolved: bool) -> str | None:
    """Why a cost-breakdown row's agent_label isn't a confident agent name,
    for the UI to explain the right thing instead of a generic "unresolved"
    flag. `resolved` must come from the query's own `a.name IS NOT NULL`
    check -- NOT inferred from raw_agent_label's shape. Verified against live
    data (research/dashboard_cost_goal_attribution_gap.md follow-up) that a
    per-thread SHCA instance id can EITHER resolve or not depending on
    whether that specific thread's agent_instances row still exists, so a
    pure string-pattern guess on raw_agent_label alone is unreliable -- the
    same shape can be a false positive.

    Returns:
      None             - resolved fine, or the GO "go-{goal_code}" case: that
                          one never resolves via the join either, but it's a
                          single deterministic row per goal with no
                          fragmentation risk, so it needs no caveat.
      "fragment"        - a per-thread SHCA instance id with no matching
                          agent_instances row -- likely the same person's
                          spend as another row on this list.
      "no_attribution"  - agent_id was NULL/empty at the source (e.g.
                          claude-code-phase tasks never record one -- see
                          record_phase_token_usage in internal_assistant.py)
                          or some other unrecognised raw shape -- not a real
                          agent identity at all, just grouped by model.
    """
    if resolved or not raw_agent_label:
        return None
    if raw_agent_label.startswith("go-") and "/" not in raw_agent_label:
        return None
    if "-assistant-" in raw_agent_label:
        return "fragment"
    return "no_attribution"


def _model_label(provider: str | None, model: str | None) -> str:
    """'provider/model' string for display, e.g. 'google/gemini-2.5-flash'."""
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider and not model:
        return ""
    if not model:
        return provider
    if not provider:
        return model
    return f"{provider}/{model}"


def _agent_display_label(prettified: str, provider: str | None, model: str | None) -> str:
    """Append the model to a prettified agent label for chart bar labels,
    e.g. 'Orchestrator · TE1T3' -> 'Orchestrator · TE1T3 (google/gemini-2.5-flash)'.
    Skipped when the label already *is* the provider/model string — that's
    the fallback case in the agent_label SQL COALESCE when neither a human
    name nor a raw agent_id was available, so showing it twice is just noise.
    """
    model_str = _model_label(provider, model)
    if not model_str or model_str.lower() in prettified.lower():
        return prettified
    return f"{prettified} ({model_str})"


def _disambiguate_agent_labels(rows: list[dict], label_key: str, model_key: str,
                                raw_key: str = "raw_agent_id") -> None:
    """Mutates a list of per-agent row dicts in place: appends a short suffix
    from raw_key to any (label_key, model_key) collision — two distinct
    agent_instances (e.g. an Assistant destroyed and reprovisioned mid-window
    by the Assistant Cleanup Watchdog, same model both times) that prettify
    to the identical display string. Keying on (label, model) rather than
    label alone means a legitimate same-agent model split — already visibly
    distinguished by its own model text — is never touched. Removes raw_key
    from every row when done, since it's a display artifact only.
    """
    key_counts: dict[tuple, int] = {}
    for a in rows:
        key = (a[label_key], a.get(model_key))
        key_counts[key] = key_counts.get(key, 0) + 1
    for a in rows:
        key = (a[label_key], a.get(model_key))
        if key_counts[key] > 1:
            a[label_key] = f"{a[label_key]} ({a[raw_key][-4:]})"
        del a[raw_key]


def _db_rows_to_gateway_fmt(model_rows) -> tuple[list, list]:
    """Convert token_usage model rows into gateway byProvider/byModel shape
    so _build_provider_breakdown() can merge them uniformly."""
    by_model: list[dict] = []
    by_provider: dict[str, dict] = {}
    for r in model_rows:
        prov = r["provider"] or "unknown"
        cost = float(r["cost"] or 0)
        tokens = int(r["tokens"] or 0)
        model_entry = {
            "provider": prov, "model": r["model"] or "unknown",
            "totals": {"totalCost": cost, "totalTokens": tokens},
        }
        by_model.append(model_entry)
        existing = by_provider.setdefault(
            prov,
            {"provider": prov, "totals": {"totalCost": 0.0, "totalTokens": 0}},
        )
        existing["totals"]["totalCost"] += cost
        existing["totals"]["totalTokens"] += tokens
    return list(by_provider.values()), by_model


# Shared between _system_cost() and goal_cost_breakdown() so both "Daily
# spend by provider" charts (system-wide and per-goal) use identical colors.
_PROV_COLORS = {"google": "#34d399", "anthropic": "#818cf8",
                "claude-cli": "#60a5fa", "google-pro": "#f59e0b"}


def _build_daily_provider_datasets(rows: list, all_days: list[str]) -> tuple[str, str]:
    """Pivot (day, provider, cost) rows into per-provider Chart.js stacked-bar
    datasets -- one series per provider, gaps filled with 0 across all_days
    so a quiet day doesn't just vanish from the x-axis. Shared by the
    system-wide and per-goal "Daily spend by provider" charts.

    Returns plain (unescaped) JSON strings -- these get embedded in a
    single-quoted HTML data-attribute, so Jinja's default autoescape must run
    on them (it HTML-entity-encodes both '"' and "'"; the browser decodes
    entities before JS ever reads .dataset.x). Do not wrap in Markup() --
    that disables autoescaping and lets a literal "'" in the data (e.g. a
    provider label) break out of the attribute early.
    """
    prov_days: dict[str, dict] = {}
    for r in rows:
        p = r["provider"] or "unknown"
        prov_days.setdefault(p, {})
        prov_days[p][str(r["day"])] = float(r["cost"] or 0)
    datasets = json.dumps([
        {"label": prov,
         "data": [round(prov_days[prov].get(d, 0.0), 6) for d in all_days],
         "backgroundColor": _PROV_COLORS.get(prov, "#6b7280"),
         "borderWidth": 0, "borderRadius": 2}
        for prov in sorted(prov_days)
    ])
    labels = json.dumps(all_days)
    return labels, datasets


async def _system_cost() -> dict:
    """$ spend broken down by provider/model and by goal.

    Reads exclusively from PostgreSQL (token_calls + model_pricing).
    No gateway dependency — all session types write to token_calls via
    the lifecycle webhook (api.call per LLM response for PI sessions,
    agent.completed per CLI invocation for IA tasks).

    All 9 DB queries fire in one parallel batch: _db_cost_data() launches
    its 7 internal queries while top_tools and top_agents run alongside them,
    so total wall-clock = slowest single query, not sum of two phases.
    """
    pool = get_pool()
    # All 9 result sets come from pre-aggregated mat views — no live JOINs.
    # Attribution rides in the SAME batch. It is two more reads against
    # pre-aggregated views, so it costs nothing in wall-clock — the gather
    # is already waiting on the slowest of the others.
    from app.services import cost_attribution

    db, tool_rows_raw, agent_rows_raw, by_role, thread_rows = await asyncio.gather(
        _db_cost_data(),
        pool.fetch("SELECT * FROM mv_cost_top_tools  ORDER BY calls DESC"),
        pool.fetch("SELECT * FROM mv_cost_top_agents ORDER BY cost  DESC NULLS LAST"),
        cost_attribution.spend_by_role(),
        cost_attribution.threads(),
    )

    # ── Provider/model breakdown ──────────────────────────────────────────────
    by_provider_raw, by_model_raw = _db_rows_to_gateway_fmt(db["model_rows"])
    providers = _build_provider_breakdown(by_provider_raw, by_model_raw)

    # ── Totals ────────────────────────────────────────────────────────────────
    month_cost   = sum(_num(r["cost"])   for r in db["model_rows"])
    month_tokens = sum(int(r["tokens"] or 0) for r in db["model_rows"])
    week_cost    = db["week_cost"]
    today_cost   = db["today_cost"]

    # ── Forecast: 7-day trailing average × 30 ────────────────────────────────
    forecast_str = _money((week_cost / 7) * 30) if week_cost > 0 else None

    # ── Token type breakdown ──────────────────────────────────────────────────
    input_tokens       = sum(int(r["inp"] or 0) for r in db["model_rows"])
    output_tokens      = sum(int(r["out"] or 0) for r in db["model_rows"])
    cache_read_tokens  = sum(int(r["cr"]  or 0) for r in db["model_rows"])
    cache_write_tokens = sum(int(r["cw"]  or 0) for r in db["model_rows"])

    # ── Top goals with per-agent breakdown ────────────────────────────────────
    # mv_cost_by_goal now has a `phase` column (dashboard_mv_cost_by_goal_
    # fold_and_phase.sql) -- claude-code-phase work spread across coder/
    # reviewer/tester/etc emits one row per phase instead of one blended row,
    # same as goal_cost_breakdown()'s live nested detail. Accumulate per
    # (agent_id, provider, model) first so the top-level agent total is
    # correct, then attach each row's phase (if any) as a nested sub-row.
    goals_map: dict[str, dict] = {}
    for r in db["goal_rows"]:
        gid = r["goal_id"]
        cost = _num(r["cost"])
        tokens = int(r["tokens"] or 0)
        if cost <= 0 and tokens <= 0:
            continue
        if gid not in goals_map:
            goals_map[gid] = {
                "goal_id":    gid,
                "title":      r["title"] or gid or "Unknown goal",
                "cost":       0.0,
                "tokens":     0,
                "agents":     [],
                "_agents_by_key": {},
            }
        goal = goals_map[gid]
        goal["cost"]   += cost
        goal["tokens"] += tokens
        if not r["agent_id"]:
            continue
        key = (r["agent_id"], r["provider"], r["model"])
        agent = goal["_agents_by_key"].get(key)
        if agent is None:
            agent = {
                "raw_agent_id": r["agent_id"],
                "agent_id":     _prettify_agent_label(r["agent_id"]),
                "model_str":    _model_label(r["provider"], r["model"]),
                "cost":         0.0,
                "tokens":       0,
                "phases":       [],
            }
            goal["_agents_by_key"][key] = agent
            goal["agents"].append(agent)
        agent["cost"]   += cost
        agent["tokens"] += tokens
        if r["phase"]:
            agent["phases"].append({
                "phase":      r["phase"],
                "cost_str":   _money(cost),
                "tokens_str": _abbrev(tokens),
            })

    for g in goals_map.values():
        del g["_agents_by_key"]
        for a in g["agents"]:
            a["cost_str"]   = _money(a["cost"])
            a["tokens_str"] = _abbrev(a["tokens"])
        _disambiguate_agent_labels(g["agents"], "agent_id", "model_str")

    top_goals = sorted(goals_map.values(), key=lambda g: g["cost"], reverse=True)[:5]
    for g in top_goals:
        g["cost_str"]   = _money(g["cost"])
        g["tokens_str"] = _abbrev(g["tokens"])

    # "Top goals" is a top-5 cut of the same 30-day total shown above it —
    # disclose the gap explicitly rather than let the visible rows look like
    # they should (but don't) sum to month_cost. Covers both goals beyond the
    # top 5 and any cost this window couldn't attribute to a goal at all.
    goals_shown_cost = sum(g["cost"] for g in top_goals)
    goals_other_cost = month_cost - goals_shown_cost
    goals_other_str = _money(goals_other_cost) if goals_other_cost > 0.005 else None

    # ── Top tools + top agents — results from the parallel gather above ──────────
    top_tools: list[dict] = [
        {"name": r["tool"] or "?", "count": int(r["calls"] or 0)}
        for r in (tool_rows_raw or [])
    ]
    # mv_cost_top_agents now has a `phase` column (dashboard_mv_cost_top_
    # agents_fold_and_phase.sql) -- an agent with claude-code-phase work
    # across multiple phases (or a Council/Security reviewer across
    # multiple runs) emits one row per phase/run instead of one blended row.
    # "Top agents" is just a bar chart (no nested detail like Top goals by
    # cost has), so fold those back down to one total per prettified label
    # here -- the view's own ranking already guarantees all of a qualifying
    # agent's phase rows are present, not truncated by an unrelated row.
    _agents_by_key: dict[tuple, dict] = {}
    for r in (agent_rows_raw or []):
        pretty = _prettify_agent_label(r["agent_label"])
        key = (pretty, r["provider"], r["model"])
        entry = _agents_by_key.get(key)
        if entry is None:
            entry = {
                "agent": _agent_display_label(pretty, r["provider"], r["model"]),
                "cost":  0.0,
                "tokens": 0,
            }
            _agents_by_key[key] = entry
        entry["cost"]   += float(r["cost"] or 0)
        entry["tokens"] += int(r["tokens"] or 0)

    top_agents: list[dict] = sorted(
        _agents_by_key.values(), key=lambda a: a["cost"], reverse=True)[:5]
    for a in top_agents:
        a["cost_str"]   = _money(a["cost"])
        a["tokens_str"] = _abbrev(a["tokens"])

    # mv_cost_top_agents has no `goals`-style exclusion — every dollar of
    # month_cost lands in exactly one agent-label bucket — so this gap is
    # purely "other agents beyond the top 5", never unattributed cost.
    agents_shown_cost = sum(a["cost"] for a in top_agents)
    agents_other_cost = month_cost - agents_shown_cost
    agents_other_str = _money(agents_other_cost) if agents_other_cost > 0.005 else None

    pie_labels = json.dumps([p["provider"] for p in providers])
    pie_values = json.dumps([p["cost"]     for p in providers])

    # ── Chart data (JSON-safe for Chart.js) ───────────────────────────────────
    # mv_cost_daily / mv_cost_daily_provider only emit a row for a day that had
    # at least one call — a zero-spend day produces no row at all, not a
    # cost=0 row. Deriving the x-axis from those rows alone (the old
    # behaviour) silently collapsed the 30-day window down to only the days
    # with activity (~23 of 30 in practice), which reads as "this chart isn't
    # showing 30 days". Build the full 30-day calendar range explicitly, in
    # UTC to match the views' own `(recorded_at AT TIME ZONE 'UTC')::date`,
    # and default every missing day to 0.
    _today_utc = datetime.now(timezone.utc).date()
    _all_days: list[str] = [str(_today_utc - timedelta(days=i)) for i in range(29, -1, -1)]

    _daily_cost_by_day = {str(r["day"]): float(r["cost"] or 0) for r in db["daily_rows"]}
    daily_labels = json.dumps(_all_days)
    daily_costs  = json.dumps([round(_daily_cost_by_day.get(d, 0.0), 6) for d in _all_days])

    stacked_labels, stacked_datasets = _build_daily_provider_datasets(
        db["daily_provider_rows"], _all_days)

    agent_labels = json.dumps([a["agent"] for a in top_agents])
    agent_costs  = json.dumps([round(a["cost"], 6) for a in top_agents])

    goal_labels  = json.dumps([g["title"][:30] for g in top_goals])
    goal_costs   = json.dumps([round(g["cost"], 6) for g in top_goals])

    # Story chart. mv_cost_by_story already caps at 10 rows (baked into the
    # view), but the chart's fixed-height canvas (h-32, matching the other
    # hbar charts in this panel) only has room for about 5 legible labels
    # before Chart.js's y-axis autoSkip starts hiding tick labels while still
    # drawing every bar -- same top-5 cut the sibling Top agents/Top goals
    # charts already use (which is why THEY don't show this symptom). "Other"
    # here is only the remainder of the view's own top-10, not the true total
    # beyond that -- there's no cheap unbounded query to diff against, unlike
    # month_cost for agents/goals above, so it's a partial-but-honest
    # disclosure, not a full one.
    _CHART_ROW_LIMIT = 5
    _story_rows_shown = db["story_rows"][:_CHART_ROW_LIMIT]
    _story_rows_rest  = db["story_rows"][_CHART_ROW_LIMIT:]
    story_labels = json.dumps([r["story_title"][:30] if r["story_title"] else "?" for r in _story_rows_shown])
    story_costs  = json.dumps([round(float(r["cost"] or 0), 6) for r in _story_rows_shown])
    _stories_other_cost = sum(float(r["cost"] or 0) for r in _story_rows_rest)
    stories_other_str = (
        _money(_stories_other_cost) + f" across {len(_story_rows_rest)} more stories"
        if _stories_other_cost > 0.005 else None
    )

    _log.info(
        "cost dashboard: month=%s week=%s today=%s tokens=%d providers=%d goals=%d",
        _money(month_cost), _money(week_cost), _money(today_cost),
        month_tokens, len(providers), len(top_goals),
    )

    return {
        "source":                 "db",
        # WHO spent it, and WHICH conversation caused it. Both were absent
        # until the kernel started writing to token_calls: the executor's
        # spend lived in step_journal and the verifier's nowhere, so this
        # page reported the two cheapest spenders and looked complete.
        "by_role":                by_role,
        "threads":                thread_rows,
        "month_str":              _money(month_cost) if month_cost > 0 else None,
        "week_str":               _money(week_cost),
        "today_str":              _money(today_cost),
        "tokens_str":             _abbrev(month_tokens),
        "forecast_str":           forecast_str,
        "cache_savings_str":      None,
        "input_tokens_str":       _abbrev(input_tokens)       if input_tokens       > 0 else None,
        "output_tokens_str":      _abbrev(output_tokens)      if output_tokens      > 0 else None,
        "cache_read_tokens_str":  _abbrev(cache_read_tokens)  if cache_read_tokens  > 0 else None,
        "cache_write_tokens_str": _abbrev(cache_write_tokens) if cache_write_tokens > 0 else None,
        "providers":              providers,
        "top_tools":              top_tools,
        "top_agents":             top_agents,
        "agents_shown_count":     len(top_agents),
        "top_goals":              top_goals,
        "goals_other_str":        goals_other_str,
        "agents_other_str":       agents_other_str,
        "model_labels_json":      pie_labels,
        "model_values_json":      pie_values,
        "daily_labels_json":      daily_labels,
        "daily_costs_json":       daily_costs,
        "stacked_labels_json":    stacked_labels,
        "stacked_datasets_json":  stacked_datasets,
        "agent_labels_json":      agent_labels,
        "agent_costs_json":       agent_costs,
        "goal_labels_json":       goal_labels,
        "goal_costs_json":        goal_costs,
        "story_labels_json":      story_labels,
        "story_costs_json":       story_costs,
        "stories_other_str":      stories_other_str,
        "stories_shown_count":    len(_story_rows_shown),
        "note":                   None,
        "_degraded":              [],
        }


def _build_provider_breakdown(by_provider_raw: list, by_model_raw: list) -> list[dict]:
    """Group models under their provider, both sorted by cost descending."""
    providers = []
    for p in by_provider_raw:
        pname = p.get("provider") or "unknown"
        pt = p.get("totals") or {}
        pcost = _num(pt.get("totalCost"))
        ptokens = _num(pt.get("totalTokens"))

        models = []
        for m in by_model_raw:
            if (m.get("provider") or "unknown") != pname:
                continue
            mt = m.get("totals") or {}
            mcost = _num(mt.get("totalCost"))
            mtokens = _num(mt.get("totalTokens"))
            models.append({
                "model": m.get("model") or "unknown",
                "cost": mcost,
                "tokens": mtokens,
                "cost_str": _money(mcost),
                "tokens_str": _abbrev(mtokens),
            })
        models.sort(key=lambda x: x["cost"], reverse=True)

        providers.append({
            "provider": pname,
            "cost": pcost,
            "tokens": ptokens,
            "cost_str": _money(pcost),
            "tokens_str": _abbrev(ptokens),
            "models": models,
        })

    providers.sort(key=lambda x: x["cost"], reverse=True)
    return providers


def _cache_savings(totals: dict) -> str | None:
    """Estimate the $ saved by prompt caching: what the cache-read tokens would
    have cost at the full input rate, minus what they actually cost. Derives the
    input rate from inputCost/input. Returns None when the data isn't present.
    """
    cache_read = _num(totals.get("cacheRead"))
    if cache_read <= 0:
        return None
    input_tokens = _num(totals.get("input"))
    input_cost = _num(totals.get("inputCost"))
    cache_cost = _num(totals.get("cacheReadCost"))
    if input_tokens <= 0 or input_cost <= 0:
        return None
    input_rate = input_cost / input_tokens
    saved = cache_read * input_rate - cache_cost
    return _money(saved) if saved > 0 else None


async def _system_review_quality() -> dict:
    """Reviewer-agent quality leaderboard.

    Primary source: mv_dashboard_review_quality (RA-agent review records).
    Fallback when that view is empty (claude-phases model, no RA tasks):
    reads phase-task outcomes from the last 30 days, grouped by phase name,
    so the panel shows meaningful data under the current architecture.
    """
    pool = get_pool()
    try:
        rows = await pool.fetch(
            "SELECT * FROM mv_dashboard_review_quality ORDER BY reviews DESC",
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard review-quality query failed: %s", exc)
        return {"rows": [], "mode": "ra", "_degraded": ["reviewer quality"]}

    if rows:
        out = []
        for r in rows:
            reviews = r["reviews"] or 0
            approved = r["approved"] or 0
            out.append({
                "agent": r["reviewer_agent_id"],
                "reviews": reviews,
                "approval_pct": round(100 * approved / reviews) if reviews else 0,
                "avg_score": int(r["avg_score"]) if r["avg_score"] is not None else 0,
            })
        return {"rows": out, "mode": "ra", "_degraded": []}

    # No RA reviews — system uses claude phases. Show phase completion stats instead.
    # Uses updated_at (always stamped on state change) so tasks without completed_at
    # are still captured correctly.
    _log.debug("dashboard review-quality: mv empty, falling back to phase task stats")
    try:
        phase_rows = await pool.fetch(
            """SELECT
                   t.phase                                                         AS phase_name,
                   count(*)                                                        AS total,
                   count(*) FILTER (WHERE t.state = 'done')                       AS done_count,
                   count(*) FILTER (WHERE t.state = 'failed')                     AS failed_count
               FROM tasks t
               JOIN goals g ON g.id = t.goal_id AND g.status = 'active'
              WHERE t.executor = 'claude-code-phase'
                AND t.state IN ('done', 'failed')
                AND t.phase IS NOT NULL
                AND t.updated_at > now() - interval '30 days'
              GROUP BY t.phase
              ORDER BY total DESC
              LIMIT 8""",
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard phase-quality fallback query failed: %s", exc)
        return {"rows": [], "mode": "phases", "_degraded": ["reviewer quality"]}

    out = []
    for r in phase_rows:
        total = r["total"] or 0
        done = r["done_count"] or 0
        out.append({
            "agent": r["phase_name"],
            "reviews": total,
            "approval_pct": round(100 * done / total) if total else 0,
            "avg_score": None,
        })
    return {"rows": out, "mode": "phases", "_degraded": []}


def _scoring_constants() -> dict:
    """Every number the "How is this calculated?" panel quotes.

    One dict, built from the live tables, so the explainer renders the
    rules rather than restating them. A test asserts each rendered
    value equals its constant, which is what makes changing a weight a
    one-line change instead of a one-line change plus a paragraph
    somebody has to notice.
    """
    from app.services.taximeter.categories import (  # noqa: PLC0415
        CATEGORY_WEIGHTS)
    from app.services.taximeter.v2.normalise2 import (  # noqa: PLC0415
        CLAMP_HIGH, CLAMP_LOW)
    from app.services.taximeter.v2.points import (  # noqa: PLC0415
        DECISION_POINTS, PRIORITY_POINTS, STEWARD_POINTS)

    return {
        "categories": CATEGORY_WEIGHTS,
        "decisions": DECISION_POINTS,
        "steward": STEWARD_POINTS,
        "priority": PRIORITY_POINTS,
        "clamp_low": CLAMP_LOW,
        "clamp_high": CLAMP_HIGH,
    }


async def _score_rows(window_weeks: int, goal_id: str | None,
                      sort: str, direction: str) -> list[dict]:
    """The people rows, from whichever scorer this environment runs.

    ONE DECISION POINT, read from the setting each call rather than
    captured at import: a restart is not needed to switch, and a test
    can flip it without reloading a module. The two scorers return the
    same row shape, so everything downstream is unchanged.
    """
    from app.config import get_settings  # noqa: PLC0415

    if get_settings().taximeter_scoring == "v2":
        from app.services.taximeter.v2.activity import (  # noqa: PLC0415
            compute_activity_v2)
        return await compute_activity_v2(get_pool(), window_weeks, goal_id,
                                         sort, direction)
    from app.services.taximeter.scoring import compute_activity  # noqa: PLC0415
    return await compute_activity(window_weeks, goal_id, sort, direction)


async def _system_taximeter(
    window_weeks: int = 12, goal_id: str | None = None, view: str = "people",
    sort: str = "overall", direction: str = "desc",
) -> dict:
    """Per-human raw activity, browsable person-first or goal-first, and
    sortable by any category or the blended `overall` score (default) — see
    app/services/taximeter/README.md and research/TAXIMETER_DESIGN.md for
    the full design.

    `view="people"` (default): rows are per-user, aggregated across their
    goals (or just one, via `goal_id`).
    `view="goals"`: goal-first browsing, same pattern as the agent
    registry's Templates/Instances toggle. With no `goal_id` yet, returns
    the goal-picker landing list (`goal_list`: name + contributor count per
    goal). Once a goal is picked, re-runs the SAME per-person computation
    as People view (`compute_activity`), scoped to just that goal — so you
    see each person's individual contribution within it, not one aggregate
    number for the whole goal (an earlier version summed everyone into a
    single blob per goal; that told you nothing about *people*, which is
    the point of this feature).

    Delegates entirely to app/services/taximeter/scoring.py; this function
    only exists to give the feature a slot in collect_system()'s gather and
    the standard degrade-gracefully contract every other panel here follows.
    Also fetches the goal picker's option list and, in `scoring`, every
    constant the "How is this calculated?" modal quotes -- the category
    weights, the decision and priority points, the clamp bounds -- so
    the panel renders the rules rather than restating them, and cannot
    describe weights the scorer is no longer using.
    """
    from app.services.taximeter.scoring import (  # noqa: PLC0415
        fetch_goal_list_with_contributors, fetch_taximeter_goals,
    )
    try:
        goals = await fetch_taximeter_goals()
        if view == "goals" and not goal_id:
            rows, goal_list = [], await fetch_goal_list_with_contributors(window_weeks)
        else:
            rows, goal_list = await _score_rows(window_weeks, goal_id, sort, direction), []
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard taximeter query failed: %s", exc)
        return {
            "rows": [], "goal_list": [], "goals": [],
            "window_weeks": window_weeks, "goal_id": goal_id, "view": view,
            "sort": sort, "dir": direction, "sort_next": {},
            "_degraded": ["taximeter"],
        }
    selected_goal_name = next((g["name"] for g in goals if g["goal_id"] == goal_id), None)
    return {
        "rows": rows, "goal_list": goal_list, "goals": goals,
        # THE NUMBERS THE EXPLAINER QUOTES, from the same tables the
        # scorer used. The modal used to state the weights and the
        # decision points as prose, so changing a weight meant
        # remembering to edit a paragraph -- and the version that was
        # not remembered is the one people read. Rendered, they cannot
        # disagree with the maths.
        "scoring": _scoring_constants(),
        "window_weeks": window_weeks,
        "goal_id": goal_id, "view": view, "selected_goal_name": selected_goal_name,
        "sort": sort, "dir": direction, "sort_next": _taximeter_sort_next_dirs(sort, direction),
        "_degraded": [],
    }


async def _system_anomalies(cost: dict) -> dict:
    """Detect cost spikes and task-failure spikes vs recent baselines.

    Relies on the daily cost array already computed by _system_cost (passed in
    to avoid a second gateway round-trip) and a fresh DB count of today's
    failed tasks vs the 7-day rolling average.
    """
    pool = get_pool()
    anomalies: list[dict] = []

    # Cost spike: today > 1.5× 7-day average (only when gateway data is present).
    daily = []
    if cost.get("source") == "gateway":
        # Reconstruct daily costs from the sessions.usage call already made.
        # We can't re-use the raw array here, but today_cost / week_cost are
        # already in the cost dict so we can derive the avg without extra I/O.
        today_raw = _num(cost.get("today_str", "").replace("$", "").replace(",", ""))
        week_raw = _num(cost.get("week_str", "").replace("$", "").replace(",", ""))
        if week_raw > 0:
            week_avg = week_raw / 7
            if week_avg > 0 and today_raw > week_avg * 1.5:
                pct = round((today_raw / week_avg - 1) * 100)
                anomalies.append({
                    "kind": "cost",
                    "msg": f"Spend today (${today_raw:,.2f}) is {pct}% above the 7-day daily average (${week_avg:,.2f}).",
                    "level": "amber",
                })

    # Failure-rate spike: today's failed tasks vs 7-day rolling average.
    try:
        row = await pool.fetchrow(
            """SELECT
                 count(*) FILTER (
                   WHERE t.state = 'failed'
                     AND t.updated_at >= now() - interval '24 hours') AS today_failed,
                 count(*) FILTER (
                   WHERE t.state = 'failed'
                     AND t.updated_at >= now() - interval '7 days') AS week_failed
               FROM tasks t
               JOIN goals g ON g.id = t.goal_id AND g.status = 'active'
               WHERE t.status = 'active'""",
        )
        today_f = row["today_failed"] or 0
        week_avg_f = (row["week_failed"] or 0) / 7
        if week_avg_f >= 1 and today_f > week_avg_f * 2:
            anomalies.append({
                "kind": "failures",
                "msg": f"{today_f} tasks failed today vs a daily average of {week_avg_f:.1f}.",
                "level": "red" if today_f >= 5 else "amber",
            })
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard anomaly failure check failed: %s", exc)

    return {"anomalies": anomalies, "_degraded": []}


async def _system_escalations() -> dict:
    """Top goals by escalation count — reads from mv_dashboard_escalations.
    Source: ledger_events esc.requested / esc.resolved / esc.timed_out.
    """
    pool = get_pool()
    try:
        rows = await pool.fetch(
            "SELECT * FROM mv_dashboard_escalations ORDER BY total DESC",
        )
    except Exception as exc:
        _log.warning("dashboard escalations query failed: %s", exc)
        return {"rows": [], "_degraded": ["escalations"]}

    out = []
    for r in rows:
        total = r["total"] or 0
        timed_out = r["timed_out"] or 0
        timeout_pct = round(100 * timed_out / total) if total else 0
        out.append({
            "goal_name": r["goal_name"] or r["goal_id"],
            "total": total,
            "resolved": r["resolved"] or 0,
            "timed_out": timed_out,
            "timeout_pct": timeout_pct,
        })
    return {"rows": out, "_degraded": []}


async def _system_cron() -> dict:
    """Scheduled automations from the OpenClaw gateway (cron.list + cron.runs).
    cron.list returns { jobs: [{id, name, enabled, schedule, nextRunAtMs,
    lastRunAtMs, ...}], total }. cron.runs returns recent run log entries.
    Reads from gateway_cache (refreshed every 30s by background task) instead
    of calling the gateway live — eliminates the ~550ms round-trip per load.
    Degrades gracefully when the cache is empty or stale.
    """
    try:
        result = await gateway_cache.read("cron.list")
        jobs_raw = result.get("jobs") or []
        jobs = []
        for j in jobs_raw:
            schedule = j.get("schedule") or {}
            if isinstance(schedule, dict):
                kind = schedule.get("kind", "")
                if kind == "every":
                    sched_str = _format_ms_interval(schedule.get("everyMs"))
                elif kind == "cron":
                    sched_str = schedule.get("expr", "cron")
                elif kind == "at":
                    sched_str = schedule.get("at", "once")
                else:
                    sched_str = str(schedule)
            else:
                sched_str = str(schedule)

            next_run = j.get("nextRunAtMs")
            last_run = j.get("lastRunAtMs")
            jobs.append({
                "id": j.get("id", ""),
                "name": j.get("name") or j.get("id", "?"),
                "enabled": bool(j.get("enabled", True)),
                "schedule_str": sched_str,
                "next_run_str": _ago_ms(next_run, future=True) if next_run else "—",
                "last_run_str": _ago_ms(last_run) if last_run else "never",
            })
        return {"jobs": jobs, "total": result.get("total", len(jobs)), "_degraded": []}
    except Exception as exc:
        _log.warning("dashboard cron.list failed: %s", exc)
        return {"jobs": [], "total": 0, "_degraded": ["cron"]}


async def _system_slos() -> dict:
    """Five SLO tiles: completion rate, autonomy, recovery rate, p95 latency,
    average task duration — all over the last 30 days.
    """
    pool = get_pool()
    degraded: list[str] = []

    async def _db_slos():
        return await pool.fetchrow("SELECT * FROM mv_dashboard_slos WHERE row_id = 1")

    async def _gateway_latency():
        try:
            usage = await gateway_cache.read("sessions.usage")
            lat = (usage.get("aggregates") or {}).get("latency") or {}
            avg_ms = _num(lat.get("avgMs"))
            p95_ms = _num(lat.get("p95Ms"))
            return (
                _format_ms(avg_ms) if avg_ms > 0 else None,
                _format_ms(p95_ms) if p95_ms > 0 else None,
            )
        except Exception as exc:
            _log.debug("dashboard latency fetch skipped: %s", exc)
            return None, None

    # DB read and gateway latency call run in parallel — previously sequential,
    # which made slos cost DB_time + gateway_time (~2s total).
    slos_row, (latency_avg_str, latency_p95_str) = await asyncio.gather(
        _db_slos(), _gateway_latency()
    )

    try:
        completed  = slos_row["completed"]  or 0
        failed     = slos_row["failed"]     or 0
        escalated  = slos_row["escalated"]  or 0
        attempted  = slos_row["attempted"]  or 0
        exhausted  = slos_row["exhausted"]  or 0
        total = completed + failed
        completion_rate  = round(100 * completed / total) if total else None
        autonomy_pct     = round(100 * max(0, completed - int(escalated)) / completed) if completed else None
        recovery_rate    = round(100 * (attempted - exhausted) / attempted) if attempted else None
        avg_duration_h   = float(slos_row["avg_h"] or 0)
        avg_duration_str = _format_hours(avg_duration_h) if avg_duration_h > 0 else None
        p95_duration_h   = float(slos_row["p95_h"] or 0)
        p95_duration_str = _format_hours(p95_duration_h) if p95_duration_h > 0 else None
    except Exception as exc:
        _log.warning("dashboard SLOs query failed: %s", exc)
        completed = failed = attempted = exhausted = 0
        completion_rate = autonomy_pct = recovery_rate = None
        escalated = 0
        avg_duration_str = None
        p95_duration_str = None
        degraded.append("SLOs")

    return {
        "completed": completed,
        "failed": failed,
        "completion_rate": completion_rate,
        "escalated": int(escalated),
        "autonomy_pct": autonomy_pct,
        "recovery_rate": recovery_rate,
        "attempted": attempted,
        "exhausted": exhausted,
        "latency_avg_str": latency_avg_str,
        "latency_p95_str": latency_p95_str,
        "avg_duration_str": avg_duration_str,
        "p95_duration_str": p95_duration_str,
        "_degraded": degraded,
    }


async def _system_audit_trail() -> dict:
    """Ledger chain integrity check. Compares chain_head.last_sequence against
    the max sequence_number in ledger_events — they should always match since
    rows can't be deleted (immutability trigger). Shows total event count and
    last-recorded timestamp.
    """
    pool = get_pool()
    try:
        # chain_head columns: id, last_event_id, last_hash, event_count, updated_at
        # Integrity check: event_count (successful appends) must equal count(*) in
        # ledger_events. We do NOT compare against max(sequence_number) because
        # BIGSERIAL consumes a slot even on rolled-back transactions, so gaps in the
        # sequence are normal and do not indicate a missing event.
        row = await pool.fetchrow(
            """SELECT ch.event_count, ch.updated_at,
                      (SELECT count(*) FROM ledger_events) AS actual_count
               FROM chain_head ch""",
        )
        if not row:
            return {"total": 0, "total_str": "0", "intact": True,
                    "last_event_ago": "—", "_degraded": []}

        total = row["event_count"] or 0
        actual = row["actual_count"] or 0
        intact = (total == actual) or total == 0
        return {
            "total": total,
            "total_str": _abbrev(total),
            "intact": intact,
            "actual_count": actual,
            "last_event_ago": _ago(row["updated_at"]) if row["updated_at"] else "—",
            "_degraded": [] if intact else ["ledger gap"],
        }
    except Exception as exc:
        _log.warning("dashboard audit trail check failed: %s", exc)
        return {"total": 0, "total_str": "—", "intact": True,
                "last_event_ago": "—", "_degraded": ["audit trail"]}


async def _system_approval_sla() -> dict:
    """How long tasks have been sitting in awaiting_approval state, grouped
    by goal. Reads from mv_dashboard_approval_sla_summary and
    mv_dashboard_approval_sla_goals (pre-aggregated at refresh time).
    """
    pool = get_pool()
    try:
        summary, per_goal = await asyncio.gather(
            pool.fetchrow("SELECT * FROM mv_dashboard_approval_sla_summary WHERE row_id = 1"),
            pool.fetch("SELECT * FROM mv_dashboard_approval_sla_goals ORDER BY oldest_h DESC NULLS LAST"),
        )
        avg_h = float(summary["avg_h"] or 0)
        max_h = int(summary["max_h"] or 0)
        return {
            "waiting": summary["waiting"] or 0,
            "avg_str": _format_hours(avg_h) if avg_h > 0 else "—",
            "max_str": _format_hours(max_h) if max_h > 0 else "—",
            "goals": [dict(r) for r in per_goal],
            "_degraded": [],
        }
    except Exception as exc:
        _log.warning("dashboard approval SLA failed: %s", exc)
        return {"waiting": 0, "avg_str": "—", "max_str": "—",
                "goals": [], "_degraded": ["approval SLA"]}


async def _system_deployments() -> dict:
    """Deployment activity over the last 30 days from dep.* + ws.pr_opened
    ledger events. Gives a quick success-rate headline and a recent event list.
    """
    pool = get_pool()
    try:
        stats = await pool.fetchrow(
            """SELECT
                 count(*) FILTER (WHERE event_type = 'dep.executed')           AS deployed,
                 count(*) FILTER (WHERE event_type = 'dep.rolled_back')        AS rolled_back,
                 count(*) FILTER (WHERE event_type = 'dep.smoke_test_passed')  AS smoke_passed,
                 count(*) FILTER (WHERE event_type = 'dep.smoke_test_failed')  AS smoke_failed,
                 count(*) FILTER (WHERE event_type = 'ws.pr_opened')           AS prs_opened
               FROM ledger_events
               WHERE event_type IN ('dep.executed','dep.rolled_back',
                                    'dep.smoke_test_passed','dep.smoke_test_failed',
                                    'ws.pr_opened')
                 AND timestamp > now() - interval '30 days'""",
        )
        recent = await pool.fetch(
            """SELECT le.event_type, le.context_goal_id,
                      g.name AS goal_name, le.timestamp
               FROM ledger_events le
               LEFT JOIN goals g ON g.id = le.context_goal_id
               WHERE le.event_type IN ('dep.executed','dep.rolled_back',
                                       'dep.smoke_test_passed','dep.smoke_test_failed',
                                       'ws.pr_opened')
                 AND le.timestamp > now() - interval '30 days'
               ORDER BY le.sequence_number DESC
               LIMIT 12""",
        )
        deployed = stats["deployed"] or 0
        rolled_back = stats["rolled_back"] or 0
        total = deployed + rolled_back
        success_rate = round(100 * deployed / total) if total else None

        events = [
            {
                "label": humanize_event(r["event_type"]),
                "goal": r["goal_name"] or r["context_goal_id"] or "—",
                "ago": _ago(r["timestamp"]),
                "ok": r["event_type"] not in ("dep.rolled_back", "dep.smoke_test_failed"),
            }
            for r in recent
        ]
        return {
            "deployed": deployed,
            "rolled_back": rolled_back,
            "smoke_passed": stats["smoke_passed"] or 0,
            "smoke_failed": stats["smoke_failed"] or 0,
            "prs_opened": stats["prs_opened"] or 0,
            "success_rate": success_rate,
            "events": events,
            "_degraded": [],
        }
    except Exception as exc:
        _log.warning("dashboard deployments query failed: %s", exc)
        return {"deployed": 0, "rolled_back": 0, "smoke_passed": 0,
                "smoke_failed": 0, "prs_opened": 0, "success_rate": None,
                "events": [], "_degraded": ["deployments"]}


def _format_ms(ms: float) -> str:
    """Human-readable latency from milliseconds."""
    if ms < 1000:
        return f"{int(ms)}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms / 60_000)}m {int((ms % 60_000) / 1000)}s"


def _format_hours(h: float) -> str:
    """Human-readable duration from a float number of hours."""
    if h <= 0:
        return "—"
    if h < 1:
        return f"{int(h * 60)}m"
    if h < 24:
        return f"{h:.1f}h"
    return f"{h / 24:.1f}d"


def _parse_session_key(key: str) -> tuple[str, str]:
    """Extract (agent_id, goal_id) from 'agent:{agentId}:{goal_id}-{thread_id}'.

    goal_id may be a bare UUID (legacy sessionKey format) or today's short
    code followed by a UUID thread id, e.g. 's1vsd-{thread_uuid}' -- same
    dual-format parsing as _goal_id_from_session_key in
    routers/agent_lifecycle.py. Returns ('', '') on malformed keys.
    """
    parts = key.split(":", 2)
    if len(parts) < 3:
        return "", ""
    agent_id = parts[1]
    segment = parts[2]
    import re as _re
    m = _re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", segment, _re.I)
    if not m:
        return agent_id, ""
    if m.start() == 0:
        goal_id = m.group(0)
    else:
        goal_id = segment[: m.start()].rstrip("-")
    return agent_id, goal_id.upper()


def _base_agent_id(agent_id: str, goal_id: str) -> str | None:
    """Decode a per-thread/per-task instance id back to its base agents.id
    by stripping the trailing '-{goal_id}-{thread_or_run_id}' the gateway
    appends per session (openclaw.py, agent id format
    '{agent_id}-{goal_id}-{thread_id}'). Same fold as the mv_cost_by_goal /
    mv_cost_top_agents views' "a2" join and _build_agents_data() in
    monitor.py -- a bare agents.id (e.g. GO's 'go-{code}') has no such
    suffix and is returned unchanged by the regex (sub is a no-op), so this
    only returns a *different* candidate when one was actually decoded.
    Returns None if agent_id/goal_id are missing or no suffix was stripped.
    """
    if not agent_id or not goal_id:
        return None
    # Only the thread-suffixed form is decoded here. An assistant that shares
    # one home is `{agent}-{goal}` with no third part (Stage K), which is the
    # same shape as GO's own bare `agents.id` — `go-{goal}` — so folding it
    # would turn `go-ker14` into `go`. `_base_agent_candidates` below adds that
    # possibility as an extra lookup candidate instead of guessing here.
    import re as _re
    pattern = _re.compile(r"-" + _re.escape(goal_id.lower()) + r"-[0-9a-f-]+$", _re.I)
    stripped = pattern.sub("", agent_id)
    return stripped if stripped != agent_id else None


def _base_agent_candidates(agent_id: str, goal_id: str) -> list[str]:
    """Every id this session key might resolve to, most specific first.

    A candidate that is not a real agent simply fails to join, so an extra
    one costs nothing; a wrong single answer would label one agent's cost
    with another agent's name.
    """
    from app.services.assistant_instance import base_agent_candidates
    return base_agent_candidates(agent_id, goal_id)


async def _system_session_previews() -> dict:
    """Live session snapshots. Calls sessions.list with includeLastMessage=true
    so the lastMessagePreview field arrives in one round-trip (no per-session
    calls needed). Capped at 5 sessions to limit gateway transcript I/O.
    Session labels are resolved from the DB (agent name + goal name) rather
    than relying on the gateway's displayName which is often absent.
    """
    try:
        result = await gateway_cache.read("sessions.list")
        sessions_raw = (result.get("sessions") or []) if isinstance(result, dict) else []
        if not sessions_raw:
            return {"previews": [], "_degraded": []}

        # Batch-resolve agent and goal names from DB in two queries. A
        # per-thread SHCA session id is "{base_agent_id}-{goal_id}-{thread_
        # uuid}" -- never a bare agents.id -- so also collect each session's
        # decoded base id (same fold used by _build_agents_data() in
        # monitor.py and the mv_cost_* views' "a2" join) and look both up
        # together, same one round-trip.
        agent_ids = []
        goal_ids  = []
        for s in sessions_raw:
            aid, gid = _parse_session_key(s.get("key") or "")
            if aid:
                agent_ids.append(aid)
                agent_ids.extend(_base_agent_candidates(aid, gid))
            if gid:
                goal_ids.append(gid)

        pool = get_pool()
        agent_names: dict[str, str] = {}
        goal_names:  dict[str, str] = {}
        try:
            if agent_ids:
                rows = await pool.fetch(
                    "SELECT id, name FROM agents WHERE id = ANY($1::text[])", agent_ids,
                )
                agent_names = {r["id"]: r["name"] for r in rows}
            if goal_ids:
                rows = await pool.fetch(
                    "SELECT id, name FROM goals WHERE id = ANY($1::text[])", goal_ids,
                )
                goal_names = {r["id"]: r["name"] for r in rows}
        except Exception as exc:
            _log.debug("session preview name lookup failed: %s", exc)

        previews = []
        for s in sessions_raw:
            key = s.get("key") or ""
            agent_id, goal_id = _parse_session_key(key)
            base_agent_id = _base_agent_id(agent_id, goal_id)
            agent_name = (
                agent_names.get(agent_id, "")
                or (agent_names.get(base_agent_id, "") if base_agent_id else "")
            )
            goal_name  = goal_names.get(goal_id, "")

            # GO's own agents.name is already "GO — {goal_name}" (set at
            # provisioning, go_agent.py) -- combining it with goal_name below
            # would duplicate the goal title. Normalize to a fixed label the
            # same way goal_thread.py already does for GO's message sender
            # name, so it composes cleanly into "{agent} — {goal}" instead.
            if agent_name.startswith("GO —"):
                agent_name = "Goal Orchestrator"

            if agent_name and goal_name:
                label = f"{agent_name} — {goal_name}"
            elif agent_name:
                label = agent_name
            elif goal_name:
                label = goal_name
            else:
                # agent_id here is a per-thread/per-task instance id (e.g.
                # "{user}-assistant-{goal}-{thread_uuid}", "ra-progress-
                # {run_id}"), not a bare agents.id -- the exact-match lookup
                # above never resolves it. _prettify_agent_label() is a pure
                # string decode (no DB join needed) that already turns these
                # into readable labels elsewhere; reuse it here instead of
                # falling all the way through to the raw gateway key.
                label = (s.get("displayName") or s.get("label") or
                         s.get("derivedTitle") or
                         (_prettify_agent_label(agent_id) if agent_id else "") or
                         key[:24] or "session")

            snippet    = s.get("lastMessagePreview") or ""
            model      = s.get("model") or s.get("modelProvider") or ""
            updated_ms = s.get("updatedAt")
            previews.append({
                "key":      key,
                "label":    label,
                "snippet":  snippet[:120] if snippet else "",
                "model":    model,
                "ago":      _ago_ms(updated_ms) if updated_ms else "—",
                # True if the exact-match DB lookup found a name, OR the
                # pattern decode above recognized the shape and actually
                # transformed it (prettified != raw) -- either way we have a
                # confident, human-meaningful label, not just the raw
                # gateway key falling through untouched.
                "resolved": bool(
                    agent_name or goal_name
                    or (agent_id and _prettify_agent_label(agent_id) != agent_id)
                ),
            })
        return {"previews": previews, "_degraded": []}
    except Exception as exc:
        _log.warning("dashboard session previews failed: %s", exc)
        return {"previews": [], "_degraded": ["session previews"]}


async def _system_goal_velocity() -> dict:
    """Goals with deadlines: are they on pace?

    For each active goal that has a deadline, compute:
      expected_pct = elapsed_time / total_duration * 100
      delta        = completion_pct - expected_pct
    Pace: ahead (delta > 10), on_pace (-10 ≤ delta ≤ 10), behind (delta < -10).
    Goals without a deadline are excluded — no meaningful pace to compute.
    """
    pool = get_pool()
    try:
        rows = await pool.fetch(
            """SELECT id, name, color_tag, completion_pct, deadline, created_at
               FROM goals
               WHERE status = 'active' AND deadline IS NOT NULL
               ORDER BY deadline ASC
               LIMIT 10""",
        )
        now = datetime.now(timezone.utc)
        goals = []
        for r in rows:
            pct = float(r["completion_pct"] or 0)
            deadline = r["deadline"]
            created = r["created_at"]
            if not (deadline and created):
                continue
            total_secs = (deadline - created).total_seconds()
            elapsed_secs = (now - created).total_seconds()
            if total_secs <= 0:
                continue
            expected_pct = min(100.0, max(0.0, elapsed_secs / total_secs * 100))
            delta = pct - expected_pct
            days_left = max(0, int((deadline - now).total_seconds() / 86400))
            if delta > 10:
                pace = "ahead"
            elif delta >= -10:
                pace = "on_pace"
            else:
                pace = "behind"
            goals.append({
                "id": r["id"],
                "name": r["name"],
                "color_tag": r["color_tag"] or "#6366f1",
                "pct": round(pct),
                "expected_pct": round(expected_pct),
                "delta": round(delta),
                "pace": pace,
                "days_left": days_left,
            })
        return {"goals": goals, "_degraded": []}
    except Exception as exc:
        _log.warning("dashboard goal velocity failed: %s", exc)
        return {"goals": [], "_degraded": ["goal velocity"]}


async def _system_channels() -> dict:
    """Platform channel connectivity — reads from mv_dashboard_channels.
    Shows each configured integration (Slack, Notion, etc.) with goal count
    and active binding count — all from our own DB, not the gateway.
    """
    pool = get_pool()
    try:
        rows = await pool.fetch(
            "SELECT * FROM mv_dashboard_channels ORDER BY platform",
        )
        channels = []
        for r in rows:
            goals = int(r["connected_goals"] or 0)
            bindings = int(r["active_bindings"] or 0)
            last = r["last_activity"] or r["updated_at"]
            channels.append({
                "id":        r["platform"],
                "label":     r["platform"].capitalize(),
                "name":      r["workspace_id"] or r["platform"],
                "connected": goals > 0,
                "last_in":   _ago(last) if last else "—",
                "detail":    f"{goals} goal{'s' if goals != 1 else ''} · {bindings} binding{'s' if bindings != 1 else ''}",
                "error":     None if goals > 0 else "No goals connected",
            })
        return {"channels": channels, "_degraded": []}
    except Exception as exc:
        _log.warning("dashboard channels query failed: %s", exc)
        return {"channels": [], "_degraded": ["channels"]}


def _format_ms_interval(ms) -> str:
    if not ms:
        return "?"
    secs = int(_num(ms)) // 1000
    if secs >= 86400:
        return f"every {secs // 86400}d"
    if secs >= 3600:
        return f"every {secs // 3600}h"
    if secs >= 60:
        return f"every {secs // 60}m"
    return f"every {secs}s"


def _ago_ms(ms, future: bool = False) -> str:
    """Format an epoch-milliseconds timestamp as a relative string."""
    if not ms:
        return "—"
    try:
        ts = datetime.fromtimestamp(_num(ms) / 1000, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = (ts - now) if future else (now - ts)
        secs = int(abs(diff.total_seconds()))
        if secs < 60:
            return "just now" if not future else "in <1m"
        mins = secs // 60
        if mins < 60:
            return f"in {mins}m" if future else f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"in {hours}h" if future else f"{hours}h ago"
        days = hours // 24
        return f"in {days}d" if future else f"{days}d ago"
    except Exception:
        return "—"


async def _system_sandbox() -> dict:
    """Container sandbox health via the insights extension RPC.

    The insights plugin (agent_os_openclaw/extensions/insights/) must be enabled:
      node openclaw.mjs config set plugins.entries.insights.enabled true

    Reads from gateway_cache (refreshed every 30s) instead of calling live
    — eliminates the ~252ms round-trip per dashboard load.
    If the method is not registered, the cache holds {} and we return
    the 'not_configured' fallback — never crashing the page.
    """
    try:
        result = await gateway_cache.read("sandbox.status")
        mode = result.get("mode", "unknown")
        enabled = result.get("enabled")
        return {
            "configured": enabled is not None,
            "enabled": bool(enabled),
            "mode": mode,
            "runtime": result.get("runtime"),
            "runtime_ok": bool(result.get("runtime_ok")),
            "active": int(result.get("active") or 0),
            "image": result.get("image", ""),
            "image_available": bool(result.get("image_available")),
            "error": result.get("error"),
            "_degraded": [],
        }
    except Exception as exc:
        _log.debug("sandbox.status unavailable (insights plugin not enabled?): %s", exc)
        return {
            "configured": False, "enabled": False, "mode": None,
            "runtime": None, "runtime_ok": False, "active": 0,
            "image": None, "image_available": False, "error": None,
            "_degraded": [],
        }


async def _system_attention() -> dict:
    """Actionable operator inbox. Rows link out where the destination is
    admin-safe and membership-agnostic; goal-scoped rows (a specific goal's
    tasks/stories) are shown without a link since the viewing admin may not
    be a member of that goal."""
    pool = get_pool()
    degraded: list[str] = []

    async def _count(sql: str) -> int:
        try:
            return await pool.fetchval(sql) or 0
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("dashboard attention count failed (%s): %s", sql.split("FROM")[-1].strip(), exc)
            degraded.append("needs-attention")
            return 0

    signups = await _count("SELECT count(*) FROM signup_requests WHERE status = 'pending'")
    joins = await _count("SELECT count(*) FROM join_requests WHERE status = 'pending'")
    sync_errors = await _count(
        "SELECT count(*) FROM agent_gateway_bindings WHERE last_sync_error IS NOT NULL"
    )
    drift = await _count(
        """SELECT count(*) FROM agents
            WHERE status = 'active'
              AND gateway_synced_version IS NOT NULL
              AND gateway_synced_version < config_version"""
    )
    # Only count work under active (non-deleted) goals — blocked tasks in a
    # deleted or archived goal are no longer actionable.
    awaiting_approval = await _count(
        """SELECT count(*) FROM tasks t JOIN goals g ON g.id = t.goal_id
            WHERE t.status = 'active' AND t.state = 'awaiting_approval' AND g.status = 'active'"""
    )
    blocked_tasks = await _count(
        """SELECT count(*) FROM tasks t JOIN goals g ON g.id = t.goal_id
            WHERE t.status = 'active' AND t.state = 'blocked' AND g.status = 'active'"""
    )
    failed_tasks = await _count(
        """SELECT count(*) FROM tasks t JOIN goals g ON g.id = t.goal_id
            WHERE t.status = 'active' AND t.state = 'failed' AND g.status = 'active'"""
    )
    blocked_stories = await _count(
        """SELECT count(*) FROM stories s JOIN goals g ON g.id = s.goal_id
            WHERE s.state = 'blocked' AND g.status = 'active'"""
    )

    # Build the visible item list (only non-zero rows, in priority order).
    # Rows below don't get an href: they'd deep-link into a specific goal's
    # board, and the viewing admin may not be a member of that goal --
    # goal-membership visibility is an intentionally un-widened boundary
    # (non-members, even platform admins, don't get a look into a goal's
    # content), so the entry point is left unlinked rather than dangling
    # a link that 403s for most admins. Only rows that go somewhere
    # membership-agnostic and admin-safe keep a link.
    candidates = [
        (signups,           "person waiting for access",        "people waiting for access",         "/admin/signup-requests"),
        (joins,             "pending request to join a goal",    "pending requests to join goals",     "/goals"),
        (awaiting_approval, "task waiting for your approval",   "tasks waiting for approval",         None),
        (sync_errors,       "agent failing to sync",             "agents failing to sync",             "/agent-registry"),
        (drift,             "agent needs a config sync",         "agents need a config sync",          "/agent-registry"),
        (blocked_tasks,     "blocked task",                      "blocked tasks",                      None),
        (failed_tasks,      "failed task",                       "failed tasks",                       None),
        (blocked_stories,   "blocked story",                     "blocked stories",                    None),
    ]
    items = [
        {"count": n, "label": f"{n} {singular if n == 1 else plural}", "href": href}
        for (n, singular, plural, href) in candidates if n > 0
    ]

    # Keyed "rows" (not "items") so Jinja's `attention.rows` resolves to this
    # list rather than dict.items().
    return {
        "rows": items,
        "total": sum(i["count"] for i in items),
        "_degraded": degraded,
    }


async def _system_fleet() -> dict:
    """Cross-goal counts that summarize the registries into a single row."""
    pool = get_pool()
    degraded: list[str] = []
    try:
        active_goals = await pool.fetchval("SELECT count(*) FROM goals WHERE status = 'active'") or 0
        type_rows = await pool.fetch(
            "SELECT type, count(*) AS n FROM agents WHERE status = 'active' AND NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = agents.id) GROUP BY type ORDER BY type"
        )
        agents_by_type = {r["type"]: r["n"] for r in type_rows}
        templates = await pool.fetchval("SELECT count(*) FROM templates WHERE status = 'active'") or 0
        active_users = await pool.fetchval("SELECT count(*) FROM users WHERE status = 'active'") or 0
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard fleet failed: %s", exc)
        return {
            "active_goals": 0, "agents_by_type": {}, "agents_total": 0,
            "templates": 0, "active_users": 0, "_degraded": ["fleet"],
        }

    return {
        "active_goals": active_goals,
        "agents_by_type": agents_by_type,
        "agents_total": sum(agents_by_type.values()),
        "templates": templates,
        "active_users": active_users,
        "_degraded": degraded,
    }


async def _system_live() -> dict:
    """What is running right now: active gateway sessions + dispatching tasks."""
    degraded: list[str] = []

    sessions = 0
    try:
        sd = await gateway_cache.read("sessions.list")
        sess_list = sd.get("sessions", []) if isinstance(sd, dict) else []
        sessions = len(sess_list)
    except Exception as exc:
        _log.warning("dashboard live sessions failed: %s", exc)
        degraded.append("live sessions")

    running_tasks = queued_tasks = 0
    try:
        pool = get_pool()
        row = await pool.fetchrow(
            """SELECT
                 count(*) FILTER (WHERE t.state IN ('in_progress','dispatching')) AS running,
                 count(*) FILTER (WHERE t.state IN ('pending','ready'))            AS queued
               FROM tasks t
               JOIN goals g ON g.id = t.goal_id
               WHERE t.status = 'active' AND g.status = 'active'""",
        )
        running_tasks = row["running"] or 0
        queued_tasks = row["queued"] or 0
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard running/queued tasks failed: %s", exc)

    return {"sessions": sessions, "running_tasks": running_tasks,
            "queued_tasks": queued_tasks, "_degraded": degraded}


# ── Shared helpers ──────────────────────────────────────────────────────────

async def _recent_activity(goal_ids, limit: int) -> tuple[list[dict], int | None]:
    """Latest ledger events as plain-language rows. When goal_ids is provided
    the feed is scoped to those goals (personal); when None it is system-wide.

    Returns (rows, latest_sequence_number). latest_seq feeds the ETag so the
    feed only re-renders when a new event lands.
    """
    pool = get_pool()
    skip = list(_ACTIVITY_SKIP)
    try:
        if goal_ids is not None:
            if not goal_ids:
                return [], None
            rows = await pool.fetch(
                """SELECT le.event_type, le.actor_type, le.context_goal_id,
                          le.timestamp, le.sequence_number, g.name AS goal_name
                     FROM ledger_events le
                     LEFT JOIN goals g ON g.id = le.context_goal_id
                    WHERE le.context_goal_id = ANY($1::text[])
                      AND le.event_type != ALL($2::text[])
                    ORDER BY le.sequence_number DESC
                    LIMIT $3""",
                goal_ids, skip, limit,
            )
        else:
            rows = await pool.fetch(
                """SELECT le.event_type, le.actor_type, le.context_goal_id,
                          le.timestamp, le.sequence_number, g.name AS goal_name
                     FROM ledger_events le
                     LEFT JOIN goals g ON g.id = le.context_goal_id
                    WHERE le.event_type != ALL($1::text[])
                    ORDER BY le.sequence_number DESC
                    LIMIT $2""",
                skip, limit,
            )
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard activity feed failed: %s", exc)
        return [], None

    out = []
    for r in rows:
        cat = _event_category(r["event_type"])
        out.append({
            "text": humanize_event(r["event_type"], r["actor_type"]),
            "actor": _ACTOR_LABELS.get(r["actor_type"], r["actor_type"]),
            "goal_name": r["goal_name"],
            "ago": _ago(r["timestamp"]),
            "dot_class": _CATEGORY_DOT.get(cat, "bg-gray-500"),
        })
    latest_seq = rows[0]["sequence_number"] if rows else None
    return out, latest_seq


def humanize_event(event_type: str, actor_type: str | None = None) -> str:
    """Translate a raw dotted ledger event type into a plain-English phrase."""
    label = _EVENT_LABELS.get(event_type)
    if label:
        return label
    # Unknown type — humanize the dotted string so it is still readable.
    return event_type.replace(".", " ").replace("_", " ").strip().capitalize()


def etag_for(personal: dict, system: dict | None, refreshed_at=None) -> str:
    """Stable hash over volatile data counts and the last mat-view refresh
    timestamp. Including refreshed_at means a manual refresh always invalidates
    the ETag even when underlying counts haven't changed.
    """
    proj = {
        "p_goals": personal.get("goals_count"),
        "p_tasks": personal.get("open_tasks_total"),
        "p_blocked": personal.get("open_tasks_blocked"),
        "p_agents": personal.get("agents_count"),
        "p_seq": personal.get("latest_seq"),
    }
    if system:
        health = system.get("health", {})
        cost = system.get("cost", {})
        attention = system.get("attention", {})
        fleet = system.get("fleet", {})
        live = system.get("live", {})
        proj.update({
            "s_verdict": health.get("verdict"),
            "s_db": health.get("db_ok"),
            "s_nodes": health.get("nodes"),
            "s_cost_month": cost.get("month_str"),
            "s_cost_tokens": cost.get("tokens_str"),
            "s_attention": attention.get("total"),
            "s_goals": fleet.get("active_goals"),
            "s_agents": fleet.get("agents_total"),
            "s_users": fleet.get("active_users"),
            "s_sessions": live.get("sessions"),
            "s_running": live.get("running_tasks"),
            "s_seq": system.get("latest_seq"),
        })
    if refreshed_at is not None:
        proj["refreshed_at"] = str(refreshed_at)
    blob = json.dumps(proj, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()


# ── Materialized view refresh ────────────────────────────────────────────────

# Base views: refresh first — cost views depend on mv_token_calls_priced.
_MAT_VIEWS_BASE = [
    "mv_token_calls_priced",
    "mv_dashboard_escalations",
    "mv_dashboard_review_quality",
    "mv_dashboard_slos",
    "mv_dashboard_approval_sla_summary",
    "mv_dashboard_approval_sla_goals",
    "mv_dashboard_channels",
    # Reads raw token_calls directly (not mv_token_calls_priced), so it has
    # no ordering dependency on the rest of this list — placed here anyway
    # since it's conceptually a "base" view, not a derived cost view.
    "mv_taximeter_human_activity",
    # v2 sits BESIDE v1 and changes no score until the scorer is switched
    # over. It has to be here from the moment it exists: a view that is
    # created and never added to this list is refreshed exactly once, at
    # creation, and then silently serves a frozen snapshot for ever --
    # which looks like working software until somebody notices the
    # numbers stopped moving.
    "mv_taximeter_v2",
]

# Second-level cost views: depend on mv_token_calls_priced — refresh after base.
_MAT_VIEWS_COST = [
    "mv_cost_by_model",
    "mv_cost_by_goal",
    "mv_cost_totals",
    "mv_cost_daily",
    "mv_cost_daily_provider",
    "mv_cost_by_task",
    "mv_cost_by_story",
    "mv_cost_top_agents",
    "mv_cost_top_tools",
    # Per-conversation attribution. Registered here rather than left to a
    # manual refresh for the reason the whole list exists: a panel absent
    # from it does not error, it silently freezes at whatever it held when
    # it was created — which on a cost page reads as spend having stopped.
    "mv_cost_by_thread",
]

# Full list for cache_meta tracking (order doesn't matter for reads).
_MAT_VIEWS = _MAT_VIEWS_BASE + _MAT_VIEWS_COST


async def _refresh_one(pool, view_name: str) -> None:
    """Refresh a single materialized view on a dedicated connection.

    Delegates to the refresh_mat_view() SECURITY DEFINER function so the
    call runs as the function owner (migration superuser) rather than the
    app user. The app user only has SELECT on mat views; REFRESH requires
    ownership, which the migration user holds.

    ONE VIEW'S FAILURE IS ITS OWN. These are gathered, and a bare
    `gather` propagates the first exception -- so one broken view
    aborted the whole refresh, left every OTHER view stale, and skipped
    the timestamp update, which is the one signal that says how old the
    dashboard is. The panel then showed old numbers under a fresh
    `refreshed_at`, or no update at all, and nothing named the view
    that did it.

    Swallowed and logged WITH THE VIEW NAME, so the next refresh still
    rebuilds everything else and somebody reading the log knows which
    one to look at.
    """
    try:
        async with pool.acquire() as conn:
            _log.info("dashboard.refresh starting view=%s", view_name)
            await conn.execute("SELECT refresh_mat_view($1)", view_name)
            _log.info("dashboard.refresh done view=%s", view_name)
    except Exception:                                       # noqa: BLE001
        _log.exception("dashboard.refresh FAILED view=%s — the other views "
                       "still refresh and the timestamp still moves",
                       view_name)


async def _refresh_gateway_cache_guarded() -> None:
    """The gateway half of a refresh, unable to take the rest with it."""
    try:
        await gateway_cache.refresh_gateway_cache()
    except Exception:                                       # noqa: BLE001
        _log.exception("dashboard.refresh FAILED gateway_cache — the "
                       "materialized views still refresh")


async def refresh_materialized_views() -> datetime:
    """Refresh all dashboard mat views and gateway cache.

    Two-phase mat view refresh:
      Phase 1 — base views + gateway cache in parallel (gateway I/O overlaps
                with the mv_token_calls_priced LATERAL join work)
      Phase 2 — cost aggregation views in parallel (safe now that
                mv_token_calls_priced is current)

    Returns the new refreshed_at timestamp.
    """
    pool = get_pool()
    _log.info("dashboard.refresh triggered base=%d cost=%d",
              len(_MAT_VIEWS_BASE), len(_MAT_VIEWS_COST))

    # Phase 1: base mat views + gateway cache (independent, all parallel)
    #
    # THE GATEWAY REFRESH IS GUARDED TOO. Each view now swallows its own
    # failure, but this call sat beside them unguarded in the same
    # `gather` -- so a gateway that was down took every materialized
    # view with it and skipped the timestamp, which is precisely the
    # failure the per-view guard was added to stop. The gateway and the
    # database are different dependencies; one being unreachable is not
    # a reason to serve stale numbers from the other.
    await asyncio.gather(
        *[_refresh_one(pool, v) for v in _MAT_VIEWS_BASE],
        _refresh_gateway_cache_guarded(),
    )

    # Phase 2: cost views — need mv_token_calls_priced from phase 1
    await asyncio.gather(*[_refresh_one(pool, v) for v in _MAT_VIEWS_COST])

    refreshed_at = datetime.now(timezone.utc)
    await pool.execute(
        "UPDATE dashboard_cache_meta SET refreshed_at = $1", refreshed_at
    )
    _log.info("dashboard.refresh complete refreshed_at=%s", refreshed_at)
    return refreshed_at


async def get_refreshed_at() -> datetime | None:
    """Return the oldest refreshed_at across mat views and gateway cache.

    The oldest timestamp is the conservative bound — data is no fresher than
    the stalest source. Returns None if the tracking tables have no rows yet
    (pre-migration state; dashboard degrades gracefully).
    """
    pool = get_pool()
    try:
        mv_ts, gw_ts = await asyncio.gather(
            pool.fetchval("SELECT min(refreshed_at) FROM dashboard_cache_meta"),
            gateway_cache.get_min_refreshed_at(),
        )
        candidates = [ts for ts in (mv_ts, gw_ts) if ts is not None]
        return min(candidates) if candidates else None
    except Exception as exc:
        _log.debug("get_refreshed_at failed (pre-migration?): %s", exc)
        return None


# ── Per-panel data fetchers (used by lazy-loading panel endpoints) ───────────
#
# Each function returns the minimal dict needed by its template partial.
# They call the same underlying _system_* functions so panel endpoints and
# full-page loads always produce identical output.

async def fetch_panel_cost() -> dict:
    """Data for dashboard/partials/system_cost.html."""
    return await _system_cost()


async def fetch_panel_escalations() -> dict:
    """Data for dashboard/partials/system_escalations.html."""
    return await _system_escalations()


async def fetch_panel_channels() -> dict:
    """Data for dashboard/partials/system_channels.html."""
    return await _system_channels()


async def fetch_panel_approval_sla() -> dict:
    """Data for dashboard/partials/system_approval_sla.html."""
    return await _system_approval_sla()


async def fetch_panel_slos() -> dict:
    """Data for dashboard/partials/system_slos.html."""
    return await _system_slos()


async def fetch_panel_reviews() -> dict:
    """Data for dashboard/partials/system_reviews.html."""
    return await _system_review_quality()


async def fetch_panel_taximeter(
    window_weeks: int = 12, goal_id: str | None = None, view: str = "people",
    sort: str = "overall", direction: str = "desc",
) -> dict:
    """Data for dashboard/partials/system_taximeter.html. Unlike the other
    fetch_panel_* wrappers, this one takes parameters — the period presets,
    goal picker, People/Goals toggle, and column sort let an admin re-run
    the same query over a different lookback, scope, aggregation, or order,
    without reloading the whole dashboard."""
    return await _system_taximeter(window_weeks, goal_id, view, sort, direction)


async def fetch_taximeter_goal_breakdown(
    uid: str, window_weeks: int = 12, goal_id: str | None = None,
) -> dict:
    """Data for dashboard/partials/_taximeter_goal_breakdown.html — lazy-loaded
    into the per-person modal when a name is clicked, not pre-computed for
    every row up front.

    v5: one row per goal this person is active in, with that goal's own
    Oversight/Throughput/Efficiency/Overall scores (`compute_goal_drilldown`)
    — replaces v4's separate point-math breakdown + bar-chart pair with a
    single table. When the main table is already scoped to one goal, the
    drill-down narrows to just that goal's row so the modal stays
    representative of the row being clicked rather than showing every goal
    when only one is in view.

    A "Contribute" second view (what share of a person's own overall score
    traces back to each goal) was built and shipped alongside this
    2026-07-08, then removed the same day once the design was finalized —
    see `TAXIMETER_V5_REDESIGN.md` for the history if it's ever revisited.
    """
    # THE SAME SCORER AS THE TABLE. This ran v1 while the rows above
    # ran v2, so a name and the breakdown behind it disagreed on one
    # page -- which reads as a bug in the numbers rather than in the
    # wiring, and is worse than either being wrong on its own.
    from app.config import get_settings  # noqa: PLC0415
    try:
        if get_settings().taximeter_scoring == "v2":
            from app.services.taximeter.v2.drilldown2 import (  # noqa: PLC0415
                compute_goal_drilldown_v2)
            drilldown = await compute_goal_drilldown_v2(
                get_pool(), uid, window_weeks)
        else:
            from app.services.taximeter.scoring import (  # noqa: PLC0415
                compute_goal_drilldown)
            drilldown = await compute_goal_drilldown(uid, window_weeks)
        goals = drilldown["goals"]
        if goal_id:
            goals = [g for g in goals if g["goal_id"] == goal_id]
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("dashboard taximeter goal-breakdown query failed uid=%s: %s", uid, exc)
        return {"uid": uid, "goals": [], "goal_id": goal_id, "_degraded": True}
    return {"uid": uid, "goals": goals, "goal_id": goal_id, "_degraded": False}


# ── Formatting (kept here so templates stay logic-free) ─────────────────────

def _taximeter_sort_next_dirs(current_sort: str, current_dir: str) -> dict[str, str]:
    """For each Taximeter column, the direction clicking its header would
    switch to: flip if it's already the active sort column, otherwise start
    at that column's natural default (ascending for "name" — A first;
    descending — highest first — for "overall" and every category)."""
    from app.services.taximeter.scoring import SORTABLE_COLUMNS  # noqa: PLC0415
    flipped = "asc" if current_dir == "desc" else "desc"
    return {
        col: flipped if col == current_sort else ("asc" if col == "name" else "desc")
        for col in SORTABLE_COLUMNS
    }


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _abbrev(n) -> str:
    n = _num(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _daily_cost(entry: dict) -> float:
    """Extract a day's cost from a usage.cost daily entry, tolerating shapes."""
    if not isinstance(entry, dict):
        return 0.0
    t = entry.get("totals") if isinstance(entry.get("totals"), dict) else entry
    return _num(t.get("totalCost") if "totalCost" in t else t.get("cost"))


def _format_uptime(value) -> str | None:
    """Render an uptime in seconds (or a pre-formatted string) as e.g. '3d 4h'."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    secs = int(_num(value))
    if secs <= 0:
        return None
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


async def goal_cost_breakdown(goal_id: str) -> dict:
    """Per-agent and per-story cost breakdown for a single goal (last 30 days).

    Used by the goal cost breakdown panel on the Cost tab. Never raises —
    returns empty structure on failure.
    """
    pool = get_pool()
    try:
        # Per-agent breakdown — resolve IA instances to template names.
        # Goal match is case-insensitive: make_goal_session_key() lowercases the
        # goal code into GO/SHCA-chat session keys before it reaches token_calls,
        # while goals.id itself is always uppercase (_validate_goal_id()) — a
        # plain `=` here silently drops every GO/chat-session row for the goal.
        agent_rows = await pool.fetch(
            # sql: fragment -- the `priced` CTE is prepended from _COST_CTE at runtime
            _COST_CTE.format(extra_filter="AND UPPER(t.goal_id::text) = UPPER($1)") + """
            SELECT COALESCE(a.name, a2.name, assignee.display_name, NULLIF(p.agent_id, ''),
                            p.provider || '/' || p.model) AS agent_label,
                   -- Explicit resolution flag, true if ANY join found a name
                   -- -- verified against live data before adding this, see
                   -- research/dashboard_cost_goal_attribution_gap.md follow-up.
                   (a.name IS NOT NULL OR a2.name IS NOT NULL
                    OR assignee.display_name IS NOT NULL) AS resolved,
                   p.provider,
                   p.model,
                   SUM(p.inp)          AS inp,
                   SUM(p.cr)           AS cr,
                   SUM(p.cw)           AS cw,
                   SUM(p.out)          AS out,
                   SUM(p.total_tokens) AS tokens,
                   SUM(p.cost)         AS cost
            FROM   priced p
            LEFT JOIN agent_instances ai ON (ai.id::text = p.agent_id
                                          OR ai.gateway_agent_id = p.agent_id)
            LEFT JOIN agents           a  ON a.id = ai.template_id
            -- Folds every per-thread/per-task instance of the SAME identity
            -- back together even when its own agent_instances row is gone
            -- (the "a" join above only resolves if that specific instance
            -- row still exists). Strips the trailing "-{goal_code}-{thread_
            -- or task-id}" a per-thread/per-task id always has (see
            -- make_goal_session_key in openclaw.py and _safe_instance_id in
            -- services/council/provisioning.py) back to the shared base
            -- agent_id, then resolves THAT directly against agents.id.
            -- Confirmed against live data (2 rounds) before adding this:
            -- one real person's Assistant was fragmented across 4 rows in
            -- goal S1VSD, all sharing this same decoded base id.
            LEFT JOIN agents           a2 ON a2.id = regexp_replace(
                                          p.agent_id,
                                          '-' || lower(p.goal_id) || '-[0-9a-f-]+$',
                                          '')
            -- claude-code-phase rows never set agent_id at all (see
            -- record_phase_token_usage in internal_assistant.py -- it tracks
            -- by run_id/story_id, not agent). These attribute instead to the
            -- human the story is assigned to. COALESCE(p.story_id,
            -- ph.story_id): some of these rows have story_id set directly,
            -- others only carry run_id and the story link has to come from
            -- that phase task's own story_id column -- confirmed both shapes
            -- occur in real data before adding this fallback.
            LEFT JOIN tasks            ph ON ph.id = p.run_id
            LEFT JOIN stories          s3 ON s3.id = COALESCE(p.story_id, ph.story_id)
            LEFT JOIN users            assignee ON assignee.uid = s3.assigned_to
            GROUP  BY agent_label, resolved, p.provider, p.model
            ORDER  BY SUM(p.cost) DESC NULLS LAST
            LIMIT  20
        """, goal_id)

        # Phase-level detail for assignee-attributed rows only (claude-code-
        # phase work) -- nested under each person's row in the template via
        # <details>/<summary>, same collapse pattern as "Top goals by cost".
        # Not merged into agent_rows above: that query aggregates cost/tokens
        # across ALL phases per person, this one breaks it back out by phase
        # for the expandable detail. Matched back to its parent row by
        # agent_label text -- safe here because both queries build agent_label
        # with the identical COALESCE/join chain, so the same person always
        # produces the same string in both.
        phase_rows = await pool.fetch(
            # sql: fragment -- the `priced` CTE is prepended from _COST_CTE at runtime
            _COST_CTE.format(extra_filter="AND UPPER(t.goal_id::text) = UPPER($1)") + """
            SELECT COALESCE(a.name, a2.name, assignee.display_name, NULLIF(p.agent_id, ''),
                            p.provider || '/' || p.model) AS agent_label,
                   -- COALESCE to a sentinel rather than filtering ph.phase IS
                   -- NOT NULL: this query must cover every row agent_rows
                   -- does (same join graph, phase added on top), or usage
                   -- with no resolvable phase silently disappears from the
                   -- nested breakdown while still counting in the parent
                   -- row's total -- exactly the mismatch a user caught live
                   -- (John Doe's row totalled $10.19 but its phases only
                   -- summed to $5.60).
                   COALESCE(ph.phase, 'Other work') AS phase,
                   SUM(p.total_tokens)  AS tokens,
                   SUM(p.cost)          AS cost
            FROM   priced p
            LEFT JOIN agent_instances ai ON (ai.id::text = p.agent_id
                                          OR ai.gateway_agent_id = p.agent_id)
            LEFT JOIN agents           a  ON a.id = ai.template_id
            LEFT JOIN agents           a2 ON a2.id = regexp_replace(
                                          p.agent_id,
                                          '-' || lower(p.goal_id) || '-[0-9a-f-]+$',
                                          '')
            LEFT JOIN tasks            ph ON ph.id = p.run_id
            LEFT JOIN stories          s3 ON s3.id = COALESCE(p.story_id, ph.story_id)
            LEFT JOIN users            assignee ON assignee.uid = s3.assigned_to
            -- Scoped to claude-code-phase/assignee-attributed rows only --
            -- without this, every agent (including GO and normal SHCA
            -- identities, which never have a phase or an assignee) would get
            -- a phase_rows entry too, turning every row in the template into
            -- an unwanted collapsible wrapper around a single "no specific
            -- phase" child instead of the plain row it should stay as.
            WHERE  assignee.display_name IS NOT NULL OR ph.phase IS NOT NULL
            GROUP  BY agent_label, phase
            ORDER  BY SUM(p.cost) DESC NULLS LAST
        """, goal_id)

        # Per-story breakdown. token_calls.session_key never actually contains
        # the ':task-<uuid>' pattern in real data (confirmed 0/21,161 rows —
        # see TAXIMETER_V5_REDESIGN.md); the gateway always echoes back the
        # goal/chat-session key format instead. token_calls.story_id is the
        # correct attribution column — populated directly from the stories
        # table by POST /stories/{story_id}/token-usage at write time (same
        # fix already applied to the top-level mv_cost_by_story view in
        # dashboard_mv_cost_by_story_story_id_fix.sql).
        story_rows = await pool.fetch(
            _COST_CTE.format(
                extra_filter="AND UPPER(t.goal_id::text) = UPPER($1) AND t.story_id IS NOT NULL"
            # sql: fragment -- the `priced` CTE is prepended from _COST_CTE at runtime
            ) + """
            SELECT s.title AS story_title,
                   SUM(p.total_tokens) AS tokens,
                   SUM(p.cost)         AS cost
            FROM   priced p
            JOIN   stories s ON s.id = p.story_id
            GROUP  BY s.id, s.title
            ORDER  BY SUM(p.cost) DESC NULLS LAST
            LIMIT  10
        """, goal_id)

        # Daily cost for this goal (last 30 days).
        # Previously 14 days, which caused goals with no recent spend to show no
        # Daily Spend chart while still having by-agent data (all-time query above).
        daily_rows = await pool.fetch(
            _COST_CTE.format(
                extra_filter="AND UPPER(t.goal_id::text) = UPPER($1) AND t.recorded_at >= now() - interval '30 days'"
            # sql: fragment -- the `priced` CTE is prepended from _COST_CTE at runtime
            ) + """
            SELECT (recorded_at AT TIME ZONE 'UTC')::date AS day,
                   provider,
                   COALESCE(SUM(cost), 0)                 AS cost
            FROM   priced
            GROUP  BY day, provider
            ORDER  BY day
        """, goal_id)

        total_cost  = sum(_num(r["cost"])   for r in agent_rows)
        total_tokens = sum(int(r["tokens"] or 0) for r in agent_rows)

        # Phase-level detail, keyed by the same raw COALESCE label agent_rows
        # produces (matched BEFORE _disambiguate_agent_labels touches "label"
        # below, so a collision suffix never breaks the match).
        _phases_by_label: dict[str, list[dict]] = {}
        for r in phase_rows:
            _phases_by_label.setdefault(r["agent_label"], []).append({
                "phase":      r["phase"],
                "cost_str":   _money(float(r["cost"] or 0)),
                "tokens_str": _abbrev(int(r["tokens"] or 0)),
            })

        agents = [
            {
                "raw_agent_id":    r["agent_label"],
                "label":           _prettify_agent_label(r["agent_label"]),
                "unresolved_kind": _classify_unresolved(r["agent_label"], r["resolved"]),
                "provider":        _model_label(r["provider"], r["model"]) or "?",
                "cost":            float(r["cost"] or 0),
                "cost_str":        _money(float(r["cost"] or 0)),
                "tokens_str":      _abbrev(int(r["tokens"] or 0)),
                "inp":             int(r["inp"] or 0),
                "cr":              int(r["cr"] or 0),
                "cw":              int(r["cw"] or 0),
                "out":             int(r["out"] or 0),
                "phases":          _phases_by_label.get(r["agent_label"], []),
            }
            for r in agent_rows
        ]
        # Same label+model collision as _system_cost()'s goals_map (two
        # distinct agent_instances prettifying identically) — see
        # _disambiguate_agent_labels docstring.
        _disambiguate_agent_labels(agents, "label", "provider")
        stories = [
            {
                "title":      r["story_title"] or "Untitled story",
                "cost":       float(r["cost"] or 0),
                "cost_str":   _money(float(r["cost"] or 0)),
                "tokens_str": _abbrev(int(r["tokens"] or 0)),
            }
            for r in story_rows
        ]

        # Same top-5 cut + overflow disclosure as the system-wide "Cost by
        # story" chart: story_rows already caps at 10 (SQL LIMIT above), but
        # the chart's own fixed-height canvas only has room for ~5 legible
        # labels before Chart.js's y-axis autoSkip hides some while still
        # drawing every bar. "Other" here is the remainder of this goal's own
        # top-10, not a true beyond-10 total -- same partial-but-honest
        # caveat as the system-wide version.
        _STORY_CHART_LIMIT = 5
        _stories_shown = stories[:_STORY_CHART_LIMIT]
        _stories_rest  = stories[_STORY_CHART_LIMIT:]
        _stories_other_cost = sum(s["cost"] for s in _stories_rest)
        stories_other_str = (
            _money(_stories_other_cost) + f" across {len(_stories_rest)} more stories"
            if _stories_other_cost > 0.005 else None
        )

        # Daily spend by provider (30d), same full-calendar gap-filling as the
        # system-wide chart -- a quiet day shouldn't just vanish from the
        # x-axis.
        _today_utc = datetime.now(timezone.utc).date()
        _all_days: list[str] = [str(_today_utc - timedelta(days=i)) for i in range(29, -1, -1)]
        daily_labels_json, daily_datasets_json = _build_daily_provider_datasets(daily_rows, _all_days)

        import json as _json
        agent_labels_json  = _json.dumps([a["label"][:28]  for a in agents])
        agent_costs_json   = _json.dumps([round(a["cost"], 6) for a in agents])
        story_labels_json  = _json.dumps([s["title"][:28]  for s in _stories_shown])
        story_costs_json   = _json.dumps([round(s["cost"], 6) for s in _stories_shown])

        _log.info(
            "goal_cost_breakdown goal=%s total=%s agents=%d stories=%d",
            goal_id, _money(total_cost), len(agents), len(stories),
        )
        return {
            "goal_id":            goal_id,
            "total_cost_str":     _money(total_cost) if total_cost > 0 else None,
            "total_tokens_str":   _abbrev(total_tokens) if total_tokens > 0 else None,
            "agents":             agents,
            "stories":            stories,
            "agent_labels_json":  agent_labels_json,
            "agent_costs_json":   agent_costs_json,
            "story_labels_json":  story_labels_json,
            "story_costs_json":   story_costs_json,
            "stories_other_str":  stories_other_str,
            "stories_shown_count": len(_stories_shown),
            "daily_labels_json":  daily_labels_json,
            "daily_datasets_json": daily_datasets_json,
        }
    except Exception as exc:
        _log.warning("goal_cost_breakdown failed goal=%s: %s", goal_id, exc)
        return {
            "goal_id": goal_id, "total_cost_str": None, "total_tokens_str": None,
            "agents": [], "stories": [],
            "agent_labels_json": "[]", "agent_costs_json": "[]",
            "story_labels_json": "[]", "story_costs_json": "[]",
            "daily_labels_json": "[]", "daily_costs_json": "[]",
        }


def _ago(ts) -> str:
    """Relative 'time ago' for a timezone-aware ledger timestamp."""
    if not isinstance(ts, datetime):
        return ""
    now = datetime.now(timezone.utc)
    secs = int((now - ts).total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"
