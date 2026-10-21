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
# Claude's hook stdin can be empty for some tool invocations (closed pipe,
# a spawn-timing race, etc.). An empty $INPUT here isn't caught by the
# `|| echo '{}'` above (cat on empty stdin still exits 0) -- and
# `echo "" | jq` feeds jq a bare newline, which is invalid JSON, so jq
# fails and produces no stdout. Left unguarded, that silently posts an
# EMPTY request body (the control panel logs raw body={} when this
# happens). Normalize before jq ever sees it.
if [ -z "$INPUT" ]; then
  INPUT='{}'
fi

# No story id, nothing to do.
if [ -z "${AGENT_STORY_ID:-}" ]; then
  exit 0
fi
if [ -z "${CONTROL_PANEL_URL:-}" ] || [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
  exit 0
fi

# Build a compact JSON body. jq is preferred but not strictly required
# — fall back to a minimal payload if missing OR if jq still fails on
# genuinely malformed (non-empty) input -- never post an empty body.
BODY=""
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
     }' 2>/dev/null)
fi
if [ -z "$BODY" ]; then
  BODY="{\"story_id\":\"$AGENT_STORY_ID\",\"task_id\":\"${AGENT_TASK_ID:-}\",\"tool\":\"unknown\",\"input\":{}}"
fi

# Write BODY to a temp file and hand it to curl via --data-binary @file rather
# than --data "$BODY" (a command-line argument) -- tool_input routinely
# contains non-ASCII content (an edited file with an em dash, smart quote,
# etc.); passing that as an argv element through a shell that spawns a
# native binary can mangle the encoding en route (same failure mode found
# and fixed in validate-plan-dag.sh: on Windows via Git Bash -> curl.exe,
# this corrupted the JSON badly enough that the server read the whole body
# as empty -- "raw body={}" -- which is exactly what surfaces as tool=unknown
# with no other symptom). Piping the exact bytes through a file sidesteps
# that argv/encoding boundary completely, on every platform.
BODY_FILE=$(mktemp)
printf '%s' "$BODY" > "$BODY_FILE"

# Fire-and-forget. 3s timeout so a slow control panel never stalls Claude.
# curl + cleanup run together in the backgrounded subshell so BODY_FILE
# survives until curl has actually read it, instead of getting removed by
# the parent script before the background job gets a chance to run.
(
  curl -s -o /dev/null --max-time 3 \
    -X POST "$CONTROL_PANEL_URL/internal/assistant/stories/$AGENT_STORY_ID/tool-progress" \
    -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "@$BODY_FILE"
  rm -f "$BODY_FILE"
) &

exit 0
