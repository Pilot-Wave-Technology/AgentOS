"""Monitoring dashboard and audit log routes."""
import asyncio
import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

_log = logging.getLogger(__name__)
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role
from app.database import get_pool
from app.openclaw import gateway_health, gateway_admin
from app.services import dag_scheduler
from app.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_filters(templates.env)


@router.get("/goals/{pid}/monitor", response_class=HTMLResponse)
async def monitor_dashboard(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()

    # Fire all independent I/O in parallel:
    #   - gateway_health()       → Gateway status badge
    #   - _build_agents_data()   → Active Agents stat, Agent Status table, Tokens Used stat,
    #                               Live Sessions stat (returns all_sessions to avoid duplicate RPC)
    #   - active_tasks query     → Active Tasks stat + Active Tasks section
    #   - recent_tasks query     → Recent Tasks panel
    #   - activity query         → Recent Activity panel
    #   - goal query             → Page header / goal name
    (
        health,
        (agents, token_usage, sessions, sessions_error),
        active_task_rows,
        recent_task_rows,
        activity_rows,
        proj_row,
        executing_dags,
    ) = await asyncio.gather(
        gateway_health(),
        _build_agents_data(pid),
        pool.fetch(
            """SELECT * FROM tasks
               WHERE goal_id = $1 AND status = 'active'
                 AND state IN ('pending', 'dispatching', 'in_progress',
                               'blocked', 'awaiting_approval', 'halted')""",
            pid,
        ),
        pool.fetch(
            """SELECT * FROM tasks
               WHERE goal_id = $1 AND status = 'active'
                 AND review_target_id IS NULL
               ORDER BY created_at DESC LIMIT 10""",
            pid,
        ),
        pool.fetch(
            """SELECT event_type, timestamp, actor_type, payload
               FROM ledger_events
               WHERE context_goal_id = $1
                 AND (event_type LIKE 'task.%' OR event_type LIKE 'story.%'
                      OR event_type LIKE 'goal.%' OR event_type LIKE 'approval.%'
                      OR event_type LIKE 'execution.%')
               ORDER BY timestamp DESC
               LIMIT 5""",
            pid,
        ),
        pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid),
        # claude-code-phase DAGs don't register as gateway sessions (they run
        # over claude-cli sessionSpawn/resumeSessionId, not a chat.send
        # sessionKey) -- dag_scheduler's Postgres-backed state is the only
        # place that sees them. Same source the Task Queue admin tab uses.
        dag_scheduler.get_executing_dags(),
    )

    # goal_id on a DAG row comes straight from tasks.goal_id (set at task
    # creation from goals.id), not parsed back out of a lowercased gateway
    # sessionKey -- but compare case-insensitively anyway since goals.id is
    # canonically uppercase and pid is whatever case the URL was typed in.
    goal_dags = [d for d in executing_dags if (d.get("goal_id") or "").upper() == pid.upper()]

    active_tasks = [dict(r) for r in active_task_rows]
    recent_tasks = [dict(r) for r in recent_task_rows]
    activity = [dict(r) for r in activity_rows]
    goal = dict(proj_row)

    # "Active Agents" should mean currently active, not "was active sometime in
    # the last hour" -- _build_agents_data() deliberately keeps destroyed
    # agent_instances around for an hour so the *table* doesn't blank them out
    # mid-poll, but the stat tile shouldn't count them as active because of that.
    terminated_count = sum(1 for a in agents if a.get("gateway_status") == "terminated")
    active_agent_count = len(agents) - terminated_count

    # Tokens Used is a lifetime SUM over every token_calls row for this goal;
    # each agent's own "tokens" is only summed from currently-visible identities
    # (_build_agents_data excludes instances destroyed over an hour ago). Usage
    # from since-finished task agents still counts in the total but has no row
    # left to attribute to, so the two numbers diverge -- surface the gap
    # explicitly instead of leaving Tokens Used looking unreconciled against
    # what the Agent Status table shows.
    total_tokens = sum(token_usage.values())
    shown_tokens = sum(a["tokens"] for a in agents)
    historical_tokens = max(0, total_tokens - shown_tokens)

    agents_etag = _compute_agents_etag(agents)

    return templates.TemplateResponse(
        "monitor/index.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "health": health,
            "agents": agents,
            "active_tasks": active_tasks,
            "token_usage": token_usage,
            "agents_etag": agents_etag,
            "sessions": sessions,
            "sessions_error": sessions_error,
            "recent_tasks": recent_tasks,
            "activity": activity,
            "active_agent_count": active_agent_count,
            "terminated_count": terminated_count,
            "goal_dags": goal_dags,
            "total_tokens": total_tokens,
            "shown_tokens": shown_tokens,
            "historical_tokens": historical_tokens,
        },
    )


def _summarise_ledger_event(event_type: str, payload: dict) -> str:
    """Build a one-line human-readable description of a ledger event payload."""
    t = event_type
    title = payload.get("title", "")
    if t == "task.created":
        return f"Task created: {title}"
    if t == "task.status_changed":
        prev, new = payload.get("previous_status", "?"), payload.get("new_status", "?")
        return f"{title}: {prev} → {new}" if title else f"{prev} → {new}"
    if t == "task.completed":
        return payload.get("outcome_summary") or "Task completed"
    if t == "task.failed":
        return payload.get("failure_reason") or "Task failed"
    if t == "task.cancelled":
        return payload.get("reason") or "Task cancelled"
    if t == "task.assigned":
        return f"Assigned to {payload.get('assignee', '?')}"
    if t == "story.created":
        return f"Story created: {title}"
    if t == "story.state_changed":
        from_s, to_s = payload.get("from_state", "?"), payload.get("to_state", "?")
        return f"{title}: {from_s} → {to_s}" if title else f"{from_s} → {to_s}"
    if t == "story.paused":
        return payload.get("reason") or "Story paused"
    if t == "story.resumed":
        return payload.get("reason") or "Story resumed"
    if t == "goal.created":
        return f"Goal created: {payload.get('name', '')}"
    if t == "goal.status_changed":
        prev, new = payload.get("previous_status", "?"), payload.get("new_status", "?")
        return f"{prev} → {new}"
    if t == "goal.member_added":
        return f"Member added: {payload.get('member_id', '?')} as {payload.get('role', '?')}"
    if t == "approval.requested":
        return payload.get("reason") or "Approval requested"
    if t == "approval.granted":
        return payload.get("notes") or "Approved"
    if t == "approval.denied":
        return payload.get("reason") or "Denied"
    if t == "escalation.requested":
        return payload.get("escalation_reason") or "Escalation requested"
    if t.startswith("execution."):
        label = t.replace("execution.", "").replace("_", " ").title()
        reason = payload.get("reason", "")
        return f"{label}: {reason}" if reason else label
    return ""


@router.get("/goals/{pid}/audit", response_class=HTMLResponse)
async def audit_log(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
    limit: int = 100,
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()

    # Query ledger_events for goal-relevant event categories only.
    # audit_log table is intentionally bypassed — it was never written to;
    # ledger_events is the real append-only event store.
    rows = await pool.fetch(
        """
        SELECT event_id, event_type, timestamp, actor_id::text AS actor_id,
               actor_type, payload, context_task_id
        FROM ledger_events
        WHERE context_goal_id = $1
          AND (
            event_type LIKE 'task.%'
            OR event_type LIKE 'story.%'
            OR event_type LIKE 'goal.%'
            OR event_type LIKE 'approval.%'
            OR event_type LIKE 'escalation.%'
            OR event_type LIKE 'execution.%'
          )
        ORDER BY timestamp DESC
        LIMIT $2
        """,
        pid, limit,
    )

    events = []
    for r in rows:
        ev = dict(r)
        payload = ev.get("payload") or {}
        if isinstance(payload, str):
            try:
                import json as _json
                payload = _json.loads(payload)
            except (ValueError, TypeError):
                payload = {}
        ev["summary"] = _summarise_ledger_event(ev["event_type"], payload)
        actor_type = ev.get("actor_type", "system")
        ev["actor_label"] = {
            "human": "Human",
            "shca": "Assistant",
            "independent_agent": "Agent",
            "system": "System",
        }.get(actor_type, actor_type)
        events.append(ev)

    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row)

    _log.info("audit_log: goal=%s events=%d", pid, len(events))

    return templates.TemplateResponse(
        "monitor/audit.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "events": events,
        },
    )


@router.get("/goals/{pid}/monitor/agents-partial", response_class=HTMLResponse)
async def agents_partial(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """HTMX polling target -- returns 304 when agent state hasn't changed."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    agents, *_ = await _build_agents_data(pid)

    etag = _compute_agents_etag(agents)

    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304)

    html = templates.TemplateResponse(
        "partials/agents_table.html",
        {"request": request, "agents": agents, "pid": pid},
    )
    html.headers["ETag"] = etag
    return html


def _compute_agents_etag(agents: list[dict]) -> str:
    """Hash of each agent's id/status/token-total. Shared by the initial page
    render and the polling endpoint so both agree on what "unchanged" means."""
    state_str = json.dumps(
        [{"id": a["id"], "status": a.get("gateway_status"), "tok": a.get("tokens", 0)}
         for a in agents],
        sort_keys=True,
    )
    return hashlib.md5(state_str.encode()).hexdigest()


def _agent_id_from_session(sess: dict) -> str:
    """Extract agentId from a gateway session object.

    sessionKey format: "agent:{agentId}:{goalId}-{threadId}" (make_goal_session_key).
    The gateway returns no top-level goal_id/agent_id fields — parse the key directly.
    """
    sk = sess.get("sessionKey") or ""
    if sk.startswith("agent:"):
        parts = sk.split(":", 2)
        if len(parts) >= 2:
            return parts[1]
    return ""


async def _build_agents_data(
    pid: str,
) -> tuple[list[dict], dict[str, int], list[dict], str | None]:
    """Fetch agents, token usage, and gateway sessions for a goal in parallel,
    then fold everything down to one row per agent IDENTITY (agents.id).

    A single identity can have many agent_instances rows: SHCA gives each
    conversation thread its own instance for isolated gateway-side history
    (see make_goal_session_key's comment in openclaw.py), and IA/RA get a new
    instance per task dispatch. Without folding these back to their shared
    template_id, the same person's Assistant would show up once per thread
    it has ever had, and "Active Agents" would count lifetime thread/task
    history instead of distinct agents -- mirrors the identity-level
    counting dashboard_metrics._system_fleet() already does system-wide.

    Returns (agents, token_usage, sessions, sessions_error) -- sessions is
    already filtered to gateway sessions belonging to this goal (matched
    against every identity's own id AND every instance's gateway_agent_id,
    since a live per-thread/per-task session registers under the instance's
    id, not the shared identity's) so the caller can derive the Live
    Sessions count without making a second gateway RPC or re-deriving the
    id set from the folded (identity-only) agents list.
    token_usage is still keyed by the raw token_calls.agent_id (identity id
    for long-lived context agents, instance id for per-thread/per-task ones)
    for the page-wide Tokens Used sum; each identity's own aggregated total
    is on its "tokens" key.
    """
    pool = get_pool()

    async def _fetch_sessions() -> tuple[list[dict], str | None]:
        try:
            _sd = await gateway_admin.get_sessions()
            return _sd.get("sessions", []) if isinstance(_sd, dict) else [], None
        except Exception as exc:
            _log.warning("Failed to list gateway sessions: %s", exc)
            return [], str(exc)

    # sessions, context agents, instance agents, and token_usage — all independent
    (all_sessions, sessions_error), context_rows, instance_rows, usage_rows = await asyncio.gather(
        _fetch_sessions(),
        # SHCA and GO identities, one row per (goal, identity) via agent_contexts.
        # For long-lived context agents the gateway registers them under agents.id.
        pool.fetch(
            """SELECT a.*, ac.additional_instructions, ac.skills, ac.workspace_files
               FROM agent_contexts ac
               JOIN agents a ON a.id = ac.agent_id
               WHERE ac.goal_id = $1 AND a.status = 'active'""",
            pid,
        ),
        # Every dispatched task AND every SHCA per-conversation thread gets its
        # own agent_instances row, tied back to a shared identity via
        # template_id. Include recently-destroyed instances (last hour) so a
        # just-finished task doesn't vanish from the table mid-poll.
        pool.fetch(
            """SELECT ai.id, ai.gateway_agent_id, ai.task_id, ai.goal_id,
                      ai.worker_type, ai.created_at, ai.destroyed_at, ai.status,
                      a.id AS identity_id, a.name, a.type, a.model
               FROM agent_instances ai
               JOIN agents a ON a.id = ai.template_id
               WHERE ai.goal_id = $1
                 AND (ai.status = 'active'
                      OR ai.destroyed_at > now() - interval '1 hour')""",
            pid,
        ),
        pool.fetch(
            # UPPER() on both sides: goals.id is always uppercase
            # (_validate_goal_id in routers/goals.py) but goal/chat-session
            # token_calls rows get their goal_id from a lowercased gateway
            # sessionKey (make_goal_session_key in openclaw.py) -- same
            # case-normalisation this project already applies in
            # dashboard_metrics.goal_cost_breakdown() and mv_cost_by_goal.
            """SELECT agent_id,
                      COALESCE(SUM(input_tokens + output_tokens
                                   + cache_read_tokens + cache_write_tokens), 0) AS total
               FROM token_calls
               WHERE UPPER(goal_id::text) = UPPER($1) AND record_type = 'call'
               GROUP BY agent_id""",
            pid,
        ),
    )

    # agentId in sessionKey:
    #   context agents → agents.id (same as gateway_agent_id for long-lived agents)
    #   instance agents → agent_instances.gateway_agent_id
    active_agent_ids = {_agent_id_from_session(s) for s in all_sessions} - {""}

    # Normalize token_usage keys to str — agent_instances.id is UUID but token_calls.agent_id is TEXT
    token_usage = {str(r["agent_id"]): int(r["total"]) for r in usage_rows if r["agent_id"]}

    # Live gateway sessions can only ever UPGRADE a status to "busy" — absence
    # from that list just means the control panel's own WS to the gateway idled
    # out (30 min, see SessionManager), not that the agent is gone.
    #
    # goal_gateway_ids collects every id this goal could plausibly appear
    # under on the gateway: identity ids (context agents register under
    # agents.id directly) AND every instance's own gateway_agent_id (a live
    # per-thread SHCA session or per-task IA/RA session registers under the
    # INSTANCE's id, never the shared identity's) -- used below to filter
    # all_sessions down to this goal's own live sessions.
    identities: dict[str, dict] = {}
    goal_gateway_ids: set[str] = set()

    def _identity(identity_id: str, name: str, type_: str, model: str) -> dict:
        return identities.setdefault(identity_id, {
            "id": identity_id, "name": name, "type": type_, "model": model,
            "busy": False, "has_context": False, "live_instances": 0,
            "total_instances": 0, "tokens": 0, "last_active_at": None,
        })

    def _bump_last_active(entry: dict, at) -> None:
        """Track the latest activity timestamp seen for this identity, so the
        table can show the most recently active agents first. There's no true
        "last message" timestamp available without a new query -- this uses
        agents.created_at (identity creation, a floor for context-only
        identities with no instances) and agent_instances.created_at/
        destroyed_at (per-thread/task start/end) as an honest proxy."""
        if at is not None and (entry["last_active_at"] is None or at > entry["last_active_at"]):
            entry["last_active_at"] = at

    for r in context_rows:
        identity_id = str(r["id"])
        entry = _identity(identity_id, r["name"], r["type"], r["model"])
        entry["has_context"] = True
        entry["tokens"] += token_usage.get(identity_id, 0)
        goal_gateway_ids.add(identity_id)
        if identity_id in active_agent_ids:
            entry["busy"] = True
        _bump_last_active(entry, r.get("created_at"))

    for r in instance_rows:
        identity_id = str(r["identity_id"])
        entry = _identity(identity_id, r["name"], r["type"], r["model"])
        entry["total_instances"] += 1
        if r["status"] != "destroyed":
            entry["live_instances"] += 1
        gw_id = r.get("gateway_agent_id") or ""
        if gw_id:
            goal_gateway_ids.add(gw_id)
            if gw_id in active_agent_ids:
                entry["busy"] = True
        # token_calls.agent_id can be written as EITHER the instance's own
        # UUID (ai.id) OR its gateway-facing string (gateway_agent_id) --
        # same ambiguity the live cost-breakdown queries handle with
        # "ai.id::text = p.agent_id OR ai.gateway_agent_id = p.agent_id".
        # Looking up only str(r["id"]) here always missed for per-thread
        # SHCA instances (token_calls.agent_id is the gateway string for
        # those, never the bare UUID), silently showing 0 tokens despite
        # real usage -- caught live: an identity with 51 instances showing
        # "0 tok" while its context-resolved sibling (GO) showed correctly.
        entry["tokens"] += token_usage.get(str(r["id"]), 0) + token_usage.get(gw_id, 0)
        # destroyed_at (when set) reflects when this instance actually
        # stopped being active, later and more relevant than its own
        # created_at; still-active instances only have created_at.
        _bump_last_active(entry, r.get("destroyed_at") or r.get("created_at"))

    agents = []
    for entry in identities.values():
        if entry["busy"]:
            entry["gateway_status"] = "busy"
        elif entry["has_context"] or entry["live_instances"] > 0:
            entry["gateway_status"] = "idle"
        else:
            entry["gateway_status"] = "terminated"
        agents.append(entry)

    # Neither context_rows nor instance_rows above has an ORDER BY -- Postgres
    # makes no ordering guarantee without one, so the same query can return
    # rows in a different order across polls even though nothing in the data
    # changed, making this list (and its pagination) visibly reshuffle every
    # 5s. "Agent Status" should read as a recency-ordered view: currently-busy
    # agents first (a real-time signal), then most-recently-touched by
    # last_active_at, with name as the final deterministic tiebreak so the
    # order never shuffles between polls with no real change.
    agents.sort(key=lambda a: (
        not a["busy"],
        -(a["last_active_at"].timestamp() if a.get("last_active_at") else 0),
        (a.get("name") or "").lower(),
    ))

    sessions = [s for s in all_sessions if _agent_id_from_session(s) in goal_gateway_ids]

    return agents, token_usage, sessions, sessions_error


@router.get("/health")
async def health_check():
    """Cloud Run health check endpoint."""
    return {"status": "ok"}
