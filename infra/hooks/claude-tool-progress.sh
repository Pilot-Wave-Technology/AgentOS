#!/bin/bash
#
# claude-tool-progress.sh — Claude Code PostToolUse hook (Phase 4 Stage 3).
#
# Fires after every tool call Claude makes during an operator-mode story.
# Posts a small structured event to the control panel so the SHCA chat
# can render a live "Claude is doing X" progress card.
#
# Reads from stdin (the hook input JSON: { tool_name, tool_input, ... })
# and from env vars set by the gateway's cli-spawn:
#   AGENT_STORY_ID                  — current story id
#   AGENT_TASK_ID                   — current task id (empty during planning)
#   CONTROL_PANEL_URL             — control panel base URL
#   OPENCLAW_GATEWAY_TOKEN        — bearer for /internal/shca/* (same
#                                   token the gateway/IA wrappers use)
#
# Best-effort. Fire-and-forget; never blocks Claude's flow:
#   * Curl timeout = 3s
#   * Exits 0 unconditionally so the hook never denies a tool call
#   * Errors logged to stderr (visible in claude's stream-json output)

set -u

INPUT=$(cat 2>/dev/null || echo '{}')

# No story id, nothing to do.
if [ -z "${AGENT_STORY_ID:-}" ]; then
  exit 0
fi
if [ -z "${CONTROL_PANEL_URL:-}" ] || [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
  exit 0
fi

# Build a compact JSON body. jq is preferred but not strictly required
# — fall back to a minimal payload if missing.
if command -v jq >/dev/null 2>&1; then
  BODY=$(echo "$INPUT" | jq -c \
    --arg story "$AGENT_STORY_ID" \
    --arg task "${AGENT_TASK_ID:-}" \
    '{
       story_id: $story,
       task_id: $task,
       tool: (.tool_name // "unknown"),
       input: (.tool_input // {}),
       ts: now
     }')
else
  BODY="{\"story_id\":\"$AGENT_STORY_ID\",\"task_id\":\"${AGENT_TASK_ID:-}\",\"tool\":\"unknown\",\"input\":{}}"
fi

# Fire-and-forget. 3s timeout so a slow control panel never stalls Claude.
curl -s -o /dev/null --max-time 3 \
  -X POST "$CONTROL_PANEL_URL/internal/assistant/stories/$AGENT_STORY_ID/tool-progress" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  --data "$BODY" &

exit 0
