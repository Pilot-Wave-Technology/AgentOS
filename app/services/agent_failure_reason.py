"""Recover why a reviewer agent's run ended, for the stage `error` column.

WHY THIS EXISTS
---------------
When a council or security stage never calls its submit tool, the stage is failed
with a single generic sentence ("agent run ended without calling submit_review").
That sentence is identical for two very different situations:

  1. the model's turn ERRORED (e.g. Gemini rejected it), and
  2. the model finished a perfectly normal turn but simply didn't call the tool.

Operators could not tell those apart from the stage record, which is how a
~1-in-3 provider failure rate went unnoticed for weeks — every failure looked
like the agent being lazy. See
`research/GEMINI_OPAQUE_AGENT_FAILURES_2026-08-16.md`.

HOW
---
The gateway already persists each turn to the session transcript, and the
control plane can already read it through the existing `sessions.get` RPC (the
same route `app/memory_v2/routers.py` uses for compaction summaries). No gateway
change and no new plumbing: we read the tail of the transcript and report what
the last assistant turn actually did.

KNOWN LIMITATION — read this before "improving" the message
-----------------------------------------------------------
For Gemini, the persisted `errorMessage` is the constant "An unknown error
occurred", because `pi-ai` collapses ~15 distinct finish reasons into it and
`pi-agent-core` writes the message to the transcript BEFORE the gateway's
error handler can enrich it (`pi-agent-core/dist/agent.js:393-395`). The real
reason (e.g. MALFORMED_FUNCTION_CALL) is recovered by the gateway-side
diagnostic and reaches the LOGS only.

So this still answers "did the model error, or did it just not call the tool?",
which is the distinction that was missing — but it will not name the provider's
reason until that enrichment is made durable in the transcript. When it is, this
module needs no change; the better text simply starts appearing.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# The last assistant turn is at the tail, so a small window is enough. Kept
# deliberately tight: this runs on the failure path of every stage, and a large
# transcript fetch would be pure overhead for one field.
_TAIL_MESSAGES = 12

# Bound what we paste into the stage row — the column is free text that surfaces
# in the UI, and a provider error can be arbitrarily long.
_MAX_REASON_CHARS = 300


def _role_of(entry) -> str | None:
    if not isinstance(entry, dict):
        return None
    message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    return message.get("role") if isinstance(message, dict) else None


def _summarize_last_assistant(messages: list) -> str | None:
    """Describe the final assistant turn, or None if nothing useful is present."""
    entries = messages or []
    for idx in range(len(entries) - 1, -1, -1):
        entry = entries[idx]
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue

        stop_reason = message.get("stopReason")
        error_message = (message.get("errorMessage") or "").strip()
        if stop_reason == "error" or error_message:
            detail = error_message or "no detail recorded"
            return f"the model's final turn ERRORED: {detail[:_MAX_REASON_CHARS]}"

        content = message.get("content") or []
        kinds = {c.get("type") for c in content if isinstance(c, dict)}

        # An empty turn answering a tool result is NORMAL — it is how an agent
        # signals "my work is done", not a fault. Measured 2026-08-16: 109/109
        # empty turns were exactly this shape, and reading them as a failure is
        # what produced a phantom second failure mode in the gateway diagnostic
        # before it was corrected. Here it matters because the agent DID call a
        # tool; if the stage still failed, the submission was rejected (ok:false)
        # rather than never attempted — a completely different thing to chase.
        answered_a_tool_result = idx > 0 and _role_of(entries[idx - 1]) == "toolResult"
        if not kinds and answered_a_tool_result:
            return (
                "the model called its tool and then ended its turn — if the stage "
                "still failed, the submission was rejected rather than never made"
            )

        if not kinds:
            return "the model's final turn completed normally but returned nothing"
        if "toolCall" not in kinds:
            return (
                "the model's final turn completed normally and produced output, "
                "but called no tool"
            )
        return None  # it DID call a tool — the failure is elsewhere
    return None


async def describe_last_agent_failure(session_key: str) -> str | None:
    """Best-effort one-line description of how the agent's last turn ended.

    Returns None when nothing conclusive is available. Never raises: this only
    decorates an error message that is already being written, so a failure here
    must not mask the original failure.
    """
    from app.openclaw import gateway_admin

    try:
        got = await gateway_admin.call(
            "sessions.get", {"key": session_key, "limit": _TAIL_MESSAGES}, timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — decorative; never mask the real failure
        log.info("failure-reason: sessions.get failed for %s: %s", session_key, exc)
        return None

    try:
        return _summarize_last_assistant((got or {}).get("messages") or [])
    except Exception as exc:  # noqa: BLE001
        log.warning("failure-reason: could not summarize %s: %s", session_key, exc)
        return None
