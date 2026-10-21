#!/bin/bash
#
# validate-plan-dag.sh — Claude Code PreToolUse hook (Phase 4 Stage 5).
#
# Scope: Claude Code CLI sessions only (the gateway's shca.claude.invoke
# path). Other openclaw agents — SHCA, IA, GO — never load this .claude/
# settings.json, so the hook only fires for claude-cli writes/edits.
#
# Validates plan-file writes against the dag_parser before they hit disk:
#   1. Path discipline — content with the "## Task DAG" marker must be
#      written to repo/docs/plans/<story_id>/cycle-NN-<slug>.md
#   2. Schema validation — cycle plan files must contain a valid
#      "## Task DAG" YAML block matching the canonical schema
#
# Anything else (SCOPE.md, code files, etc.) passes through.
#
# Protocol (per Claude Code hook spec):
#   - Receives JSON on stdin: {"tool_name": "Edit"|"Write", "tool_input": {...}, ...}
#   - Exit 0 → allow the tool call
#   - Exit non-zero → deny; stderr is shown to the model
#
# Env vars set by the gateway's cli-spawn:
#   AGENT_STORY_ID                  — current story id (used to derive cwd)
#   CONTROL_PANEL_URL             — control panel base URL
#   OPENCLAW_GATEWAY_TOKEN        — bearer for /internal/shca/*
#
# Curl timeout: 60s (1 minute) per user spec. Failing open on any infra
# error so a flaky control panel doesn't block legitimate plan writes.
#
# Installed by scripts/redeploy.sh to /etc/openclaw/hooks/validate-plan-dag.sh
# and wired in .claude/settings.json under PreToolUse → Edit|Write.

set -u

INPUT=$(cat 2>/dev/null || echo '{}')

# Need jq for stable JSON handling.
if ! command -v jq >/dev/null 2>&1; then
  echo "[validate-plan-dag] jq not installed — failing open" >&2
  exit 0
fi

TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Only gate on writes/edits. Reads, Bash, etc. pass through.
case "$TOOL" in
  Write|Edit) ;;
  *) exit 0 ;;
esac

# Extract proposed file_path and content from tool_input.
# For Write: tool_input.{file_path, content}
# For Edit:  tool_input.{file_path, new_string} — the new file content after
#            the edit applies. Conservative: validate as if the new_string
#            were the whole file content (some Edit replacements may be
#            partial; in that case the schema check is a no-op and the
#            path check still applies).
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [ "$TOOL" = "Write" ]; then
  CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
else
  CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
fi

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# No control-panel URL set → can't validate; fail open.
if [ -z "${CONTROL_PANEL_URL:-}" ] || [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
  echo "[validate-plan-dag] CONTROL_PANEL_URL or OPENCLAW_GATEWAY_TOKEN missing — failing open" >&2
  exit 0
fi

CWD="$PWD"

# Build request body. jq handles escaping content correctly for JSON.
BODY=$(jq -nc \
  --arg tool "$TOOL" \
  --arg file_path "$FILE_PATH" \
  --arg content "$CONTENT" \
  --arg cwd "$CWD" \
  '{tool: $tool, file_path: $file_path, content: $content, cwd: $cwd}')

# Write BODY to a temp file and hand it to curl via --data-binary @file rather
# than --data "$BODY" (a command-line argument). A plan file's content is
# free-form prose and routinely contains non-ASCII characters (em dashes,
# smart quotes, etc.); passing those as an argv element through a shell that
# spawns a native binary can mangle the encoding en route (observed on
# Windows via Git Bash -> curl.exe: an em dash in the content silently
# corrupted the JSON enough that the server read `tool` as empty and skipped
# validation entirely, with no error surfaced anywhere). Piping the exact
# bytes through a file sidesteps that argv/encoding boundary completely, on
# every platform.
BODY_FILE=$(mktemp)
printf '%s' "$BODY" > "$BODY_FILE"

# 60-second timeout. If the control panel is unreachable, fail open so we
# don't block writes on infra trouble — the DB-side ingest will still
# validate at plan-approval time as the existing backstop.
RESPONSE=$(curl -s -w "\n%{http_code}" --max-time 60 \
  -X POST "$CONTROL_PANEL_URL/internal/assistant/validate-plan" \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "@$BODY_FILE" 2>/dev/null)
CURL_STATUS=$?
rm -f "$BODY_FILE"
if [ "$CURL_STATUS" -ne 0 ]; then
  echo "[validate-plan-dag] curl failed (network/timeout) — failing open" >&2
  exit 0
fi

# Split off status code (last line) from body.
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY_RESP=$(echo "$RESPONSE" | sed '$d')

# 200 → valid (or skipped). Allow.
if [ "$HTTP_CODE" = "200" ]; then
  exit 0
fi

# Anything other than 400 → infra issue. Fail open with a warning.
if [ "$HTTP_CODE" != "400" ]; then
  echo "[validate-plan-dag] control panel returned HTTP $HTTP_CODE — failing open" >&2
  exit 0
fi

# 400 → real validation failure. Surface the parser error + canonical
# schema to Claude via stderr, then block the tool call.
ERROR=$(echo "$BODY_RESP" | jq -r '.error // "DAG validation failed (no detail)"')
SCHEMA=$(echo "$BODY_RESP" | jq -r '.schema_example // ""')
ALLOWED_PHASES=$(echo "$BODY_RESP" | jq -r '.allowed_phases // [] | join(", ")')

{
  echo "DENIED by validate-plan-dag hook."
  echo
  echo "Reason: $ERROR"
  if [ -n "$ALLOWED_PHASES" ]; then
    echo
    echo "Allowed phases for this DAG: $ALLOWED_PHASES"
  fi
  if [ -n "$SCHEMA" ]; then
    echo
    echo "Copy this canonical schema exactly — substitute your own task content:"
    echo
    echo "$SCHEMA"
  fi
} >&2

exit 2
