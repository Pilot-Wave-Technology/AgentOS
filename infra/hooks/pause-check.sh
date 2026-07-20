#!/bin/bash
#
# pause-check.sh — Claude Code PreToolUse kill-switch pause gate.
#
# Installed by scripts/redeploy.sh to /etc/openclaw/hooks/ alongside the
# other Claude Code hooks. Referenced from .claude/settings.json in every
# story workspace (seeded by _build_default_settings_json in operator_provisioning.py).
#
# Runs before EVERY tool call during a managed claude-code-phase turn.
# If the system or goal is paused via the kill switch, exits 2 so Claude
# sees a tool block and must output PHASE_TASK_BLOCKED — stopping the turn
# at the next tool boundary without making further API calls.
#
# M7: Active for ALL managed claude sessions that have a STORY_ID — both
# phase turns (AGENT_TASK_ID set) and SHCA planning turns (AGENT_TASK_ID absent).
# Previously required AGENT_TASK_ID; now only requires AGENT_STORY_ID so planning
# turns are also gated during a system/goal pause.
#
# D4: Distinguishes auth failures (401/403) from infrastructure errors.
# Auth errors fail CLOSED (block the tool) — a revoked or invalid token should
# not silently pass work through. All other non-2xx errors fail OPEN so a
# slow or temporarily unavailable control panel never blocks unrelated work.
#
# Protocol (per Claude Code hook spec):
#   Receives JSON on stdin: {"tool_name": "...", "tool_input": {...}, ...}
#   Exit 0 → allow the tool call
#   Exit 2 → block the tool; stdout is shown to the model

set -u

TASK_ID="${AGENT_TASK_ID:-}"
STORY_ID="${AGENT_STORY_ID:-}"
CONTROL_PANEL_URL="${CONTROL_PANEL_URL:-}"
TOKEN="${OPENCLAW_GATEWAY_TOKEN:-}"

# Not a managed session — pass through immediately. TASK_ID is optional
# (planning turns have no task), but STORY_ID is required to identify the goal.
if [ -z "$STORY_ID" ] || [ -z "$CONTROL_PANEL_URL" ] || [ -z "$TOKEN" ]; then
    exit 0
fi

# Poll the control panel for the current pause state of this story's goal.
# Capture HTTP status separately so auth failures can be distinguished from
# infrastructure errors. 3-second timeout — hooks run synchronously before
# every tool call, so a slow response would add latency.
_TMP_BODY=$(mktemp)
HTTP_STATUS=$(curl -s -o "$_TMP_BODY" -w "%{http_code}" -m 3 \
    -H "Authorization: Bearer $TOKEN" \
    "${CONTROL_PANEL_URL}/internal/assistant/stories/${STORY_ID}/execution-status" \
    2>/dev/null)
BODY=$(cat "$_TMP_BODY" 2>/dev/null || echo "")
rm -f "$_TMP_BODY"

# Auth failures mean the token is wrong or revoked. Fail CLOSED — a bad
# token should not let work through the gate silently.
if [ "$HTTP_STATUS" = "401" ] || [ "$HTTP_STATUS" = "403" ]; then
    echo "PAUSE GATE AUTH ERROR (HTTP ${HTTP_STATUS})."
    echo ""
    echo "Cannot verify pause state — token rejected. Stopping as a precaution."
    echo ""
    if [ -n "$TASK_ID" ]; then
        echo "PHASE_TASK_BLOCKED T${TASK_ID} — Pause gate auth error (HTTP ${HTTP_STATUS}). Check OPENCLAW_GATEWAY_TOKEN."
    else
        echo "PHASE_TASK_BLOCKED — Pause gate auth error (HTTP ${HTTP_STATUS}). Check OPENCLAW_GATEWAY_TOKEN."
    fi
    exit 2
fi

# Any other non-2xx, curl timeout, or empty body: fail open (infrastructure issue).
if [ -z "$BODY" ]; then
    exit 0
fi

PAUSED=$(echo "$BODY" | grep -o '"paused":true' | head -1)

if [ -z "$PAUSED" ]; then
    exit 0  # not paused — proceed normally
fi

# Execution is paused. Extract scope and reason for a useful message.
SCOPE=$(echo "$BODY" | grep -o '"scope":"[^"]*"' | head -1 | sed 's/.*":"\([^"]*\)".*/\1/')
REASON=$(echo "$BODY" | grep -o '"reason":"[^"]*"' | head -1 | sed 's/.*":"\([^"]*\)".*/\1/')

echo "EXECUTION PAUSED (scope: ${SCOPE:-system}${REASON:+ — ${REASON}})."
echo ""
echo "Operator has paused execution. Stop all work immediately and output"
echo "the following sentinel on its own line, then stop:"
echo ""
if [ -n "$TASK_ID" ]; then
    echo "PHASE_TASK_BLOCKED T${TASK_ID} — Execution paused by operator. Resume to continue."
else
    echo "PHASE_TASK_BLOCKED — Execution paused by operator. Resume to continue."
fi
exit 2
