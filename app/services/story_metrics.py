"""Story completion metrics — snapshot token/cost usage from the gateway
when an operator-mode story transitions to a terminal state (done/cancelled/rejected).

For ia_dispatch stories, agent_metrics is populated by fireLifecycleWebhook in
the gateway's cli-runner on each IA task completion. For operator stories, that
path is never triggered — SHCA drives Claude Code via shca.claude.invoke, which
bypasses cli-runner entirely. This module fills the gap by calling sessions.usage
at story completion time to capture a session-level summary row.

The agent_id written to agent_metrics is the SHCA instance id
({template_id}-{goal_id}-{thread_id}) so per-agent queries in the dashboard
correctly attribute cost to the operator session rather than to the template agent.
"""
import json
import logging

from app.openclaw import make_goal_session_key

_log = logging.getLogger(__name__)


async def record_story_session_metrics(
    *,
    goal_id: str,
    story_id: str,
    template_agent_id: str,
    thread_id: str,
) -> None:
    """Snapshot session-level token/cost usage for an operator-mode story.

    Called fire-and-forget when a story enters a terminal state. Gateway
    failures are logged and swallowed — they must never block the state
    transition or redirect.
    """
    from app.database import get_pool
    from app.openclaw import gateway_admin, GatewayError

    instance_id = f"{template_agent_id}-{goal_id}-{thread_id}"
    session_key = make_goal_session_key(instance_id, goal_id, thread_id)

    _log.info(
        "story_metrics: fetching session usage for story=%s key=%s",
        story_id, session_key,
    )

    try:
        result = await gateway_admin.call(
            "sessions.usage",
            {"key": session_key},
            timeout=30,
        )
    except GatewayError as exc:
        _log.warning(
            "story_metrics: sessions.usage RPC failed for story=%s: %s",
            story_id, exc,
        )
        return
    except Exception as exc:
        _log.warning(
            "story_metrics: unexpected error for story=%s: %s",
            story_id, exc,
        )
        return

    sessions = result.get("sessions") or []
    if not sessions:
        _log.info(
            "story_metrics: no session data returned for story=%s key=%s",
            story_id, session_key,
        )
        return

    # sessions.usage returns sessions matching the key filter — take the first.
    # If multiple entries somehow match, the first is the most-recently-updated.
    session_entry = sessions[0]
    usage = session_entry.get("usage") or {}

    inp = int(usage.get("input") or 0)
    out = int(usage.get("output") or 0)
    cr = int(usage.get("cacheRead") or 0)
    cw = int(usage.get("cacheWrite") or 0)
    total = int(usage.get("totalTokens") or (inp + out + cr + cw))
    cost_usd = usage.get("totalCost") or None
    if cost_usd is not None:
        cost_usd = float(cost_usd)
        if cost_usd == 0.0:
            cost_usd = None
    duration_ms = usage.get("durationMs") or None
    provider = (session_entry.get("modelProvider") or "").lower() or None
    model = (session_entry.get("model") or "").lower() or None

    if total == 0 and cost_usd is None:
        _log.info(
            "story_metrics: zero usage for story=%s — skipping insert",
            story_id,
        )
        return

    pool = get_pool()
    rows_written = 0

    async def _insert(metric: str, value, dimensions: dict) -> None:
        nonlocal rows_written
        await pool.execute(
            "INSERT INTO agent_metrics (metric, agent_id, goal_id_text, value, dimensions) "
            "VALUES ($1, $2, $3, $4, $5)",
            metric, instance_id, goal_id, value, json.dumps(dimensions),
        )
        rows_written += 1

    def _dims(**kwargs) -> dict:
        return {k: v for k, v in kwargs.items() if v is not None}

    # Token totals
    if total > 0:
        await _insert("session.tokens", total, _dims(
            provider=provider, model=model,
            session_key=session_key,
            input=inp, output=out,
            cache_read=cr, cache_write=cw,
            story_id=story_id,
        ))

    # Cost
    if cost_usd is not None:
        await _insert("session.cost", cost_usd, _dims(
            provider=provider, model=model,
            story_id=story_id,
        ))

    # Per-tool call counts
    tool_usage = usage.get("toolUsage") or {}
    for tool in tool_usage.get("tools") or []:
        name = tool.get("name") or "unknown"
        count = int(tool.get("count") or 0)
        if count > 0:
            await _insert("session.tool", count, _dims(tool=name, story_id=story_id))

    # Latency stats
    latency = usage.get("latency") or {}
    if latency.get("avgMs") is not None:
        await _insert("session.latency", latency["avgMs"], _dims(
            p95_ms=latency.get("p95Ms"),
            min_ms=latency.get("minMs"),
            max_ms=latency.get("maxMs"),
            count=latency.get("count"),
            story_id=story_id,
        ))

    # Message breakdown
    msg = usage.get("messageCounts") or {}
    if msg.get("total"):
        await _insert("session.messages", msg["total"], _dims(
            user=msg.get("user"),
            assistant=msg.get("assistant"),
            tool_calls=msg.get("toolCalls"),
            errors=msg.get("errors"),
            story_id=story_id,
        ))

    # Session duration
    if duration_ms is not None:
        await _insert("session.duration", duration_ms, _dims(story_id=story_id))

    _log.info(
        "story_metrics: recorded %d rows for story=%s tokens=%d cost_usd=%s",
        rows_written, story_id, total,
        f"{cost_usd:.6f}" if cost_usd is not None else "None",
    )
