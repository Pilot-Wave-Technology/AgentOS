"""Lifecycle webhook receiver for OpenClaw agent runs.

Receives agent.completed events fired by fireLifecycleWebhook() in the
gateway (both claude-cli and PI-embedded-runner paths) and writes one row
to token_usage per call.

Design principles:
  - Token counts are stored as-is from the API — no cost calculation here.
  - Cost is calculated at query time in dashboard_metrics by joining token_usage
    against model_pricing (time-aware LATERAL join).
  - thinking_tokens is stored separately even though it is a subset of
    output_tokens, so that future models with a different thinking rate are
    handled correctly without a schema change.

Extension point:
  When provider-specific cost formulas are needed (e.g. tiered pricing,
  per-hour cache charges), implement a CostCalculator subclass in
  app/services/cost_calculator.py and register it for the provider. The raw
  token columns in token_usage are the input to any calculator — this file
  and the schema do not change.
"""
import logging
import re
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.database import get_pool

_log = logging.getLogger(__name__)

router = APIRouter()

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


from app.internal_auth import verify_internal_bearer as _verify_auth  # SSOT: app/internal_auth.py


def _goal_id_from_session_key(session_key: str) -> str | None:
    """Extract goal_id from 'agent:{agentId}:{goal_id}-{threadId}' session key.

    goal_id may be a UUID (legacy format) or a short code like 'sdnvs' (current
    format). In the short-code format the segment looks like 'sdnvs-{thread_uuid}'
    — the goal code sits before the first UUID in the segment.

    Returns a string compatible with the TEXT token_calls.goal_id column.
    """
    parts = (session_key or "").split(":", 2)
    if len(parts) < 3:
        return None
    segment = parts[2]
    m = _UUID_RE.search(segment)
    if not m:
        return None
    if m.start() == 0:
        # Legacy: goal_id is the UUID itself (uuid-threaduuid format).
        return m.group(0).lower()
    # Current: goal_id is the short code before the first UUID, e.g. 'sdnvs'.
    goal_id = segment[: m.start()].rstrip("-")
    return goal_id if goal_id else None


async def record_token_usage(body: dict) -> None:
    """Persist token usage from a lifecycle webhook body to token_calls.

    Called by tasks.py's lifecycle handler (the active /internal/agents/lifecycle
    endpoint). Handles api.call, agent.completed (CLI only), and session.ended.
    Never raises — failures are logged and silently dropped so the main
    lifecycle handler is never blocked by token tracking errors.
    """
    event      = body.get("event", "")
    provider   = (body.get("provider") or body.get("model_provider") or "").lower().strip()
    agent_id   = body.get("agent_id") or None
    session_key = body.get("session_key") or None
    usage      = body.get("usage") or {}

    if event not in ("api.call", "agent.completed", "session.ended"):
        return
    if not usage:
        return

    is_cli = provider.endswith("-cli")
    if event == "agent.completed" and not is_cli:
        return  # PI sessions use api.call — skip accumulated turn total

    model    = (body.get("model") or "").lower().strip()
    run_id   = body.get("run_id") or None
    goal_id  = _goal_id_from_session_key(session_key or "")

    inp      = int(usage.get("input_tokens")       or 0)
    out      = int(usage.get("output_tokens")      or 0)
    thinking = int(usage.get("thinking_tokens")    or 0)
    cr       = int(usage.get("cache_read_tokens")  or 0)
    cw       = int(usage.get("cache_write_tokens") or 0)

    # Gemini (and OpenAI-compatible) providers report input_tokens INCLUSIVE of
    # cache_read_tokens (cached prefix is a subset of input). Anthropic reports
    # them as disjoint fields. Normalise to the Anthropic convention so that
    # input_tokens in token_calls always means "fresh non-cached tokens" and the
    # cost formula (inp×input_rate + cr×cache_read_rate) never double-bills.
    if provider in ("google",) or provider.startswith("openai"):
        inp = max(0, inp - cr)

    if inp == 0 and out == 0 and cr == 0 and cw == 0:
        return

    record_type = "session" if event == "session.ended" else "call"
    num_turns   = int(usage.get("num_turns") or 0) or None

    try:
        pool = get_pool()
        await pool.execute(
            """INSERT INTO token_calls
                   (provider, model, goal_id, agent_id, run_id, session_key,
                    input_tokens, output_tokens, thinking_tokens,
                    cache_read_tokens, cache_write_tokens,
                    record_type, num_turns)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            provider, model, goal_id, agent_id, run_id, session_key,
            inp, out, thinking, cr, cw, record_type, num_turns,
        )
        _log.info(
            "token usage recorded: event=%s provider=%s model=%s goal=%s agent=%s "
            "in=%d out=%d thinking=%d cr=%d cw=%d",
            event, provider, model, goal_id, agent_id,
            inp, out, thinking, cr, cw,
        )
    except Exception as exc:
        _log.warning("token_calls insert failed: %s", exc)


@router.post("/lifecycle")
async def agent_lifecycle(request: Request):
    """Receive agent lifecycle events from the OpenClaw gateway."""
    _verify_auth(request)

    body = await request.json()
    event = body.get("event")
    agent_id = body.get("agent_id") or None
    session_key = body.get("session_key") or None
    _log.info("agent lifecycle received: event=%s agent=%s", event, agent_id)

    # Events handled:
    #   api.call       — per individual LLM API response (PI-embedded-runner only)
    #   agent.completed — per CLI invocation (claude-cli providers only; PI sessions
    #                     use api.call instead, so agent.completed from non-CLI providers
    #                     is ignored here to avoid double-counting)
    #   session.ended  — cumulative session summary (one per session lifetime)
    if event not in ("api.call", "agent.completed", "session.ended"):
        _log.debug("agent lifecycle skipped: unrecognised event=%s", event)
        return {"ok": True}

    usage = body.get("usage") or {}
    if not usage:
        _log.debug("agent lifecycle skipped: no usage in event=%s agent=%s", event, agent_id)
        return {"ok": True}

    provider = (body.get("provider") or body.get("model_provider") or "").lower().strip()
    model    = (body.get("model") or "").lower().strip()
    run_id   = body.get("run_id") or None
    goal_id  = _goal_id_from_session_key(session_key or "")

    # agent.completed from non-CLI providers is the accumulated turn total.
    # PI sessions already send api.call per API response — storing agent.completed
    # on top would double-count. CLI providers (claude-cli, codex-cli) have no
    # api.call alternative so agent.completed is their only source.
    is_cli = provider.endswith("-cli")
    if event == "agent.completed" and not is_cli:
        _log.debug(
            "agent lifecycle skipped: agent.completed for non-CLI provider=%s "
            "(covered by api.call events)", provider,
        )
        return {"ok": True}

    inp      = int(usage.get("input_tokens")       or 0)
    out      = int(usage.get("output_tokens")      or 0)
    thinking = int(usage.get("thinking_tokens")    or 0)
    cr       = int(usage.get("cache_read_tokens")  or 0)
    cw       = int(usage.get("cache_write_tokens") or 0)

    if inp == 0 and out == 0 and cr == 0 and cw == 0:
        _log.debug("agent lifecycle skipped: all-zero tokens event=%s agent=%s", event, agent_id)
        return {"ok": True}

    record_type = "session" if event == "session.ended" else "call"
    num_turns   = int(usage.get("num_turns") or 0) or None

    pool = get_pool()
    await pool.execute(
        """INSERT INTO token_calls
               (provider, model, goal_id, agent_id, run_id, session_key,
                input_tokens, output_tokens, thinking_tokens,
                cache_read_tokens, cache_write_tokens,
                record_type, num_turns)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
        provider, model, goal_id, agent_id, run_id, session_key,
        inp, out, thinking, cr, cw, record_type, num_turns,
    )

    _log.info(
        "token usage recorded: event=%s provider=%s model=%s goal=%s agent=%s "
        "in=%d out=%d thinking=%d cr=%d cw=%d",
        event, provider, model, goal_id, agent_id,
        inp, out, thinking, cr, cw,
    )
    return {"ok": True}
