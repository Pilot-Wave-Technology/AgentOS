#!/bin/bash
#
# enforce-sentinel.sh — Claude Code Stop hook (assistant-workflow refactor Phase 2a).
#
# Ensures a PHASE turn ends with exactly one PHASE_TASK_* sentinel as its final
# line, so the control panel's sentinel parser always has a clean terminal state
# (no off-protocol / buried-sentinel → no 40-min janitor stall). If the sentinel
# is missing it exits 2 to BLOCK the stop, and stdout tells Claude to re-emit it.
#
# Scope: ONLY phase turns. A phase dispatch message starts with "[DISPATCH]" and a
# blocked-resume with "[ANSWER"; planning / discovery / chat turns have neither and
# are NOT expected to emit a sentinel, so they are left completely alone.
#
# Safety: FAILS OPEN on any error (no python, no transcript, parse failure, empty
# text) and GIVES UP after one forced retry (stop_hook_active) — so it can never
# wedge a turn. A genuinely missing sentinel then falls through to the control
# panel's off-protocol→failed path.
#
# Installed to /etc/openclaw/hooks/ by scripts/redeploy.sh; referenced from every
# story workspace .claude/settings.json (Stop hook) built by operator_provisioning.
#
# Stop hook stdin (JSON): {"transcript_path": "...", "stop_hook_active": bool, ...}
# Exit 0 → allow stop.  Exit 2 → block; stdout shown to Claude.

set -u

INPUT=$(cat 2>/dev/null || echo '{}')

# Parse + decide in python (portable, testable). Fail open if no interpreter.
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || exit 0

INPUT="$INPUT" "$PY" <<'PYEOF'
import os, json, re, sys

def fail_open():
    sys.exit(0)

try:
    data = json.loads(os.environ.get("INPUT", "{}") or "{}")
except Exception:
    fail_open()
if not isinstance(data, dict):
    fail_open()

# Already forced one retry and Claude still didn't comply → give up (no infinite loop).
if data.get("stop_hook_active") is True:
    fail_open()

tpath = data.get("transcript_path") or ""
if not tpath:
    fail_open()

try:
    with open(tpath, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = [ln for ln in f if ln.strip()]
except Exception:
    fail_open()


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return "\n".join(parts)
    return ""


last_user, last_asst = "", ""
for ln in raw_lines:
    try:
        rec = json.loads(ln)
    except Exception:
        continue
    if not isinstance(rec, dict):
        continue
    msg = rec.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if rec.get("type") == "user":
        last_user = text_of(content)
    elif rec.get("type") == "assistant":
        last_asst = text_of(content)

# Only enforce on PHASE turns (dispatch / blocked-resume). Otherwise leave alone.
lu = last_user.lstrip()
if not (lu.startswith("[DISPATCH]") or lu.startswith("[ANSWER")):
    fail_open()

if not last_asst.strip():
    fail_open()  # nothing to check

# The last non-empty line must be a valid sentinel.
last_line = ""
for ln in last_asst.splitlines():
    if ln.strip():
        last_line = ln
if re.match(r"^\s*PHASE_TASK_(DONE|BLOCKED|FAILED)\s+T?[A-Za-z0-9]", last_line):
    sys.exit(0)

print("Your reply did not end with a task sentinel. Your FINAL line must be EXACTLY one of:")
print("  PHASE_TASK_DONE T<id> — <one-line summary>")
print("  PHASE_TASK_BLOCKED T<id> — <one specific question>")
print("  PHASE_TASK_FAILED T<id> — <reason>")
print("Put ALL evidence / output / tracebacks ABOVE it, with NOTHING after the sentinel. Emit it now.")
sys.exit(2)
PYEOF
