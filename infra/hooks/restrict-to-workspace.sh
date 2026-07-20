#!/bin/bash
#
# restrict-to-workspace.sh — Claude Code PreToolUse hook (Phase 3 of SHCA refactor).
#
# Layer B of the workspace restriction model (see SHCA_REFACTOR_PLAN.md). Layer A is
# the permissions allowlist/denylist in .claude/settings.json. This script is a
# defensive backstop that catches path-escape attempts the permission patterns miss
# (e.g. symlink resolution, ../ traversal, alternate path encodings).
#
# Protocol (per Claude Code hook spec):
#   - Receives JSON on stdin: {"tool_name": "Edit", "tool_input": {...}, ...}
#   - Exit 0 → allow the tool call
#   - Exit non-zero → deny; stderr is shown to the model so it can adjust
#
# Bound: $CLAUDE_WORKSPACE_ROOT must be set (the SHCA spawn helper sets it to the
# story workspace path). Without it, the hook is permissive (logs a warning) so
# we don't break agents that didn't get the env var.
#
# Path validations:
#   Edit/Write/Read: tool_input.file_path must resolve (via realpath -m) to a
#     path inside $CLAUDE_WORKSPACE_ROOT (or equal it).
#   Bash: tool_input.command is scanned for:
#     - `cd` to absolute paths (/...)
#     - `cd` to home (~ or ~/...)
#     - `cd` with .../../ traversal that resolves outside the workspace
#
# Installed by scripts/redeploy.sh to /etc/openclaw/hooks/restrict-to-workspace.sh
# and referenced from .claude/settings.json in each story workspace (seeded by
# pwt_worktree's seedStoryWorkspaceLayout).

set -u

# Read JSON tool call from stdin.
INPUT=$(cat)

# Parse with jq; if jq is missing, fail open (log to stderr but allow).
if ! command -v jq >/dev/null 2>&1; then
  echo "[restrict-to-workspace] jq not installed — failing open" >&2
  exit 0
fi

TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
WORKSPACE="${CLAUDE_WORKSPACE_ROOT:-}"

# No workspace bound set → log + allow (won't break legacy agents).
if [ -z "$WORKSPACE" ]; then
  echo "[restrict-to-workspace] CLAUDE_WORKSPACE_ROOT not set — failing open" >&2
  exit 0
fi

# Canonicalize workspace root (resolve any symlinks).
WS_REAL=$(realpath -m "$WORKSPACE" 2>/dev/null || echo "$WORKSPACE")

# Helper: is_inside <path>  → echoes "yes" / "no"
is_inside() {
  local p="$1"
  local real
  real=$(realpath -m "$p" 2>/dev/null || echo "$p")
  case "$real" in
    "$WS_REAL"|"$WS_REAL"/*) echo "yes" ;;
    *) echo "no" ;;
  esac
}

case "$TOOL" in
  Edit|Write|Read|NotebookEdit)
    FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
    if [ -z "$FILE" ]; then
      # No path? Pass-through (other validators may catch it).
      exit 0
    fi
    # Resolve relative paths against the workspace.
    case "$FILE" in
      /*) ABS="$FILE" ;;
      *)  ABS="$WS_REAL/$FILE" ;;
    esac
    if [ "$(is_inside "$ABS")" != "yes" ]; then
      echo "DENIED by restrict-to-workspace hook: $TOOL path '$FILE' resolves outside workspace $WS_REAL" >&2
      exit 1
    fi
    ;;

  Bash)
    CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
    if [ -z "$CMD" ]; then
      exit 0
    fi

    # Deny cd to absolute paths, home dir, or env-vars that could expand outside.
    # Look for cd commands at the start of the command or after ;/&&/||/|.
    # Whitespace-tolerant grep:
    if echo "$CMD" | grep -qE '(^|[[:space:]]|;|&&|\|\||\|)[[:space:]]*cd[[:space:]]+(/|~|\$HOME|\$\{HOME\})'; then
      echo "DENIED by restrict-to-workspace hook: Bash cd to absolute or home path is not allowed in this workspace" >&2
      exit 1
    fi

    # Deny cd .. that would land outside the workspace. We can't fully simulate
    # bash here without executing it, so we deny any `cd ..` chain longer than
    # the workspace depth (cheap, conservative).
    DOTS=$(echo "$CMD" | grep -oE '(^|[[:space:]]|;|&&|\|\||\|)[[:space:]]*cd[[:space:]]+(\.\./)+\.?' | head -1 || true)
    if [ -n "$DOTS" ]; then
      # Count ../ segments
      UP_COUNT=$(echo "$DOTS" | grep -oE '\.\./' | wc -l)
      # Count workspace depth from filesystem root
      WS_DEPTH=$(echo "$WS_REAL" | awk -F'/' '{print NF - 1}')
      if [ "$UP_COUNT" -ge "$WS_DEPTH" ]; then
        echo "DENIED by restrict-to-workspace hook: Bash 'cd $DOTS' would escape workspace $WS_REAL" >&2
        exit 1
      fi
    fi
    ;;

  *)
    # Other tools pass through — their permissions are handled by settings.json
    # and (eventually) tool-specific validators.
    ;;
esac

exit 0
