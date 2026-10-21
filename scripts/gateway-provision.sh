#!/usr/bin/env bash
###############################################################################
# AgentOS gateway — provisioning / redeploy (SINGLE SOURCE OF TRUTH).
#
# Runs on BOTH paths so a fresh VM and a redeploy converge:
#   • first boot — invoked at the end of terraform's startup.sh
#   • redeploy   — invoked by cloudbuild.yaml on every push to your fork
#
# The gateway runs as the non-root `openclaw` user (like the internal setup):
# Claude Code REFUSES --permission-mode bypassPermissions under root/sudo, and
# the agents' claude-cli backend requires it — so the gateway (and every claude
# subprocess it spawns) MUST run unprivileged. Config + agent workspaces live in
# /home/openclaw/.openclaw.
#
# Idempotent. Does: pnpm install + build -> refresh rotated secrets -> write the
# full openclaw.json config (as openclaw) -> install CLI wrappers + Claude Code
# hooks -> (re)start the gateway units.
###############################################################################
set -euo pipefail
export HOME=/root   # this script runs as root; it drops to openclaw for config

OPENCLAW_USER=openclaw
GATEWAY_HOME=/home/openclaw
REPO_DIR=/opt/agentos-gateway
ENV_FILE=/etc/openclaw/env
PROVISION_ENV=/etc/openclaw/provision.env
OPENCLAW_CONFIG="$GATEWAY_HOME/.openclaw/openclaw.json"

LOG=/var/log/openclaw-provision.log
exec > >(tee -a "$LOG") 2>&1
echo "════════════════════════════════════════════════════════════════════"
echo "gateway provision $(date -u +%FT%TZ)"
echo "════════════════════════════════════════════════════════════════════"

PROCESSES_PER_VM=1
# shellcheck disable=SC1090
[ -f "$PROVISION_ENV" ] && . "$PROVISION_ENV"

id -u "$OPENCLAW_USER" &>/dev/null || useradd -m -s /bin/bash "$OPENCLAW_USER"

# Run an openclaw CLI command AS the gateway user (so ~/.openclaw resolves to
# /home/openclaw/.openclaw and the file is owned by openclaw).
oc() { sudo -u "$OPENCLAW_USER" -H bash -c "cd '$REPO_DIR' && node openclaw.mjs $*"; }
# jq runs as root on the config file; hand ownership back to openclaw after.
own_config() { chown "$OPENCLAW_USER:$OPENCLAW_USER" "$OPENCLAW_CONFIG"; }

# ── 1. Build (as root; deps are global) then hand the tree to openclaw ───────
cd "$REPO_DIR"
pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile
pnpm build
chown -R "$OPENCLAW_USER:$OPENCLAW_USER" "$REPO_DIR"

# ── 1b. Security-review scanners (the Security Council shells out to these) ───
# gitleaks (secrets), ripgrep/rg (SCA + infra file discovery), trivy (SCA/IaC),
# semgrep (SAST). Installed to /usr/local/bin as root so they're on the openclaw
# user's PATH too. Idempotent — each is skipped if already present. Non-fatal: a
# scanner that can't install just leaves that review aspect uncovered (which is
# exactly the "command not found" gap this closes).
echo "[scanners] ensuring security-review tools are installed"
export DEBIAN_FRONTEND=noninteractive
# One apt refresh up front so ripgrep + python3-pip (needed by semgrep) install
# cleanly on a fresh VM whose package lists may be stale.
apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y ripgrep python3-pip >/dev/null 2>&1 || true
if ! command -v gitleaks >/dev/null 2>&1; then
  _gl=8.18.4; _arch=$(uname -m); case "$_arch" in x86_64) _arch=x64;; aarch64|arm64) _arch=arm64;; esac
  curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${_gl}/gitleaks_${_gl}_linux_${_arch}.tar.gz" \
    | tar -xz -C /usr/local/bin gitleaks 2>/dev/null || true
  chmod 0755 /usr/local/bin/gitleaks 2>/dev/null || true
fi
if ! command -v trivy >/dev/null 2>&1; then
  curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
    | sh -s -- -b /usr/local/bin >/dev/null 2>&1 || true
fi
if ! command -v semgrep >/dev/null 2>&1; then
  # semgrep is a large Python app; a bare `pip install` (even with
  # --break-system-packages) is flaky on fresh PEP-668 VMs. Install it in an
  # isolated venv (which bundles its own pip) and symlink the CLI onto the
  # global PATH — reliable and self-contained. Falls back to pip if venv is
  # unavailable.
  apt-get install -y python3-venv >/dev/null 2>&1 || true
  if python3 -m venv /opt/semgrep-venv >/dev/null 2>&1 \
     && /opt/semgrep-venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 \
     && /opt/semgrep-venv/bin/pip install -q semgrep >/dev/null 2>&1; then
    ln -sf /opt/semgrep-venv/bin/semgrep /usr/local/bin/semgrep
  else
    python3 -m pip install --quiet --break-system-packages semgrep >/dev/null 2>&1 \
      || python3 -m pip install --quiet semgrep >/dev/null 2>&1 || true
  fi
fi
# Surface the outcome loudly (non-fatal): a missing scanner silently drops the
# Security Council review aspect that shells out to it, so it must be visible in
# the provisioning log rather than failing at review time with "command not found".
_scan_missing=""
for _t in rg gitleaks trivy semgrep; do command -v "$_t" >/dev/null 2>&1 || _scan_missing="$_scan_missing $_t"; done
if [ -n "$_scan_missing" ]; then
  echo "[scanners] WARNING: not installed ->$_scan_missing (Security Council aspects using them will be skipped; install manually if you need full coverage)" >&2
else
  echo "[scanners] OK: rg gitleaks trivy semgrep all present"
fi

# ── 2. Refresh rotated secrets into the env file (root-owned; systemd reads it
#      before dropping to openclaw, so the gateway UID never touches it) ───────
refresh_secret() {  # env_key  secret_name
  local key="$1" secret="${2:-}" val tmp
  [ -z "$secret" ] && return 0
  val=$(gcloud secrets versions access latest --secret="$secret" --project="$PROJECT" 2>/dev/null || true)
  [ -z "$(printf %s "$val" | tr -d '[:space:]')" ] && return 0
  tmp=$(mktemp)
  grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  chmod 600 "$tmp"; chown root:root "$tmp"; mv "$tmp" "$ENV_FILE"
  echo "  refreshed $key"
}
if [ -f "$ENV_FILE" ] && [ -n "${PROJECT:-}" ]; then
  refresh_secret GEMINI_API_KEY         "${GEMINI_KEY_SECRET:-}"
  refresh_secret ANTHROPIC_API_KEY      "${ANTHROPIC_KEY_SECRET:-}"
  refresh_secret OPENAI_API_KEY         "${OPENAI_KEY_SECRET:-}"
  refresh_secret OPENCLAW_GATEWAY_TOKEN "${GATEWAY_TOKEN_SECRET:-}"
fi

# ── 3. openclaw.json configuration (as the openclaw user) ────────────────────
install -d -o "$OPENCLAW_USER" -g "$OPENCLAW_USER" "$GATEWAY_HOME/.openclaw"

# Strip stale sections the CLI would reject / that we rewrite wholesale below.
if [ -f "$OPENCLAW_CONFIG" ]; then
  jq 'del(.models)
    | del(.agents.defaults.compaction.reserveTokens)
    | del(.agents.defaults.compaction.memoryFlush.forceFlushTranscriptBytes)' \
    "$OPENCLAW_CONFIG" > /tmp/oc-config.json && mv /tmp/oc-config.json "$OPENCLAW_CONFIG"
  own_config
fi

# Gateway + agent defaults.
oc config set gateway.mode local
oc config set gateway.bind lan          # LAN bind so the control plane reaches it over the VPC
oc config set logging.consoleStyle json
oc config set agents.defaults.bootstrapMaxChars 40000
oc config set agents.defaults.bootstrapTotalMaxChars 300000
oc config set agents.defaults.memorySearch.provider gemini
oc config set agents.defaults.memorySearch.model gemini-embedding-001
oc config set agents.defaults.timeoutSeconds 7200
oc config set agents.defaults.heartbeat.every 30m
oc config set agents.defaults.model google/gemini-3.8-flash
oc config set session.reset.mode idle
oc config set session.reset.idleMinutes 86400

# Compaction / memory-v2.
oc config set agents.defaults.compaction.mode safeguard
# The model that writes compaction summaries. A BASE model, not a Model
# Registry variant: it must resolve from the first boot, before the
# registry plugin's first poll has supplied any variant. Set it with the
# terraform variable `compaction_model`.
oc config set agents.defaults.compaction.model "${COMPACTION_MODEL:-google/gemini-3.8-flash}"
oc config set agents.defaults.compaction.reserveTokensPct 0
oc config set agents.defaults.compaction.memoryFlush.flushThresholdPct 0.7
oc config set agents.defaults.compaction.memoryFlush.softThresholdPct 0
oc config set agents.defaults.compaction.maxHistoryShare 0.8
oc config set agents.defaults.compaction.keepRecentTokensPct 0.2
# Caps ONE preserved tool result. The kept-tail budget above does not bound
# the tool results paired with kept turns; on a tool-heavy agent they are
# most of what survives a compaction.
oc config set agents.defaults.compaction.preservedToolResultMaxTokens 1500
# Context pruning: trims old, large tool results from the REQUEST only
# (never the stored transcript, so compaction still sees the originals).
oc config set agents.defaults.contextPruning.mode cache-ttl
oc config set agents.defaults.compaction.aggregateTimeoutMs 300000
oc config set agents.defaults.compaction.recentTurnsPreserve 2
oc config set agents.defaults.compaction.keepRecentMode tokens
oc config set agents.defaults.compaction.asyncEnabled true
oc config set agents.defaults.compaction.asyncSoftTriggerPct 0.8
oc config set agents.defaults.compaction.asyncDebounceMs 5000
oc config set agents.defaults.compaction.asyncMaxConcurrent 4
oc config set agents.defaults.compaction.asyncMaxFailedAttempts 2
oc config set agents.defaults.compaction.memoryFlush.memoryFileMaxChars 30000
oc config set agents.defaults.compaction.memoryFlush.memoryFileTargetChars 24000
oc config set agents.defaults.compaction.memoryFlush.compactionSummary.maxTokens 4000
oc config set agents.defaults.compaction.memoryFlush.compactionSummary.maxChars 16000

# SYNTHETIC CONTEXT-WINDOW MODELS ARE NOT SET UP HERE, AND THAT IS NOT AN
# OMISSION. GO, the assistant, the councils and the research agents all pin
# ids like `google-flash38-500k/gemini-3.8-flash-500k` -- one model with its
# context window clamped -- and those are supplied at RUNTIME by the control
# panel's Model Registry: it serves GET /internal/model_catalog, and the
# gateway's `extensions/model-registry` polls it every five minutes with a
# self-heal loop that re-asserts them.
#
# This block used to merge a static `infra/config/models-synthetic.json`.
# That file was retired on 2026-08-06 in favour of the registry, and its own
# header says in as many words not to re-wire it into a boot script. It was
# read here behind an `if [ -f ]`, so once the file went the step became a
# silent no-op pointing at a path that no longer exists -- which is exactly
# the thing a deployer would find and spend an afternoon chasing when a
# template failed to resolve its model.
#
# WHAT TO EXPECT ON A FIRST BOOT: until the first poll succeeds, an agent
# pinned to a synthetic id has no model to resolve. That window closes by
# itself. If it does NOT, the fault is reachability between the gateway and
# the control panel -- check CONTROL_PANEL_URL and the registry extension's
# log, not this script.

# Model registry: cache retention + 1M-context betas + claude-cli backend.
jq '.agents.defaults.models["anthropic/claude-haiku-4-5"]  = {"params": {"cacheRetention": "short"}}
  | .agents.defaults.models["anthropic/claude-sonnet-4-5"] = {"params": {"cacheRetention": "short"}}
  | .agents.defaults.models["anthropic/claude-opus-4-7"]   = {"params": {"cacheRetention": "short", "betas": ["context-1m-2025-08-07"]}}
  | .agents.defaults.models["google/gemini-3.8-flash"]     = {}
  | .agents.defaults.models["google/gemini-embedding-001"] = {}
  | .agents.defaults.cliBackends["claude-cli"] = {
      "command": "claude",
      "clearEnv": [],
      "reliability": { "watchdog": {
        "fresh":  {"noOutputTimeoutMs": 3600000},
        "resume": {"noOutputTimeoutMs": 3600000}
      } }
    }' "$OPENCLAW_CONFIG" > /tmp/oc-config.json && mv /tmp/oc-config.json "$OPENCLAW_CONFIG"
own_config

oc config validate
chown -R "$OPENCLAW_USER:$OPENCLAW_USER" "$GATEWAY_HOME/.openclaw"

# ── 4. CLI wrappers (IAs call these) + Claude Code hooks (workspace sandbox) ─
if [ -d "$REPO_DIR/scripts/wrappers" ]; then
  echo "  installing CLI wrappers to /usr/local/bin"
  for w in "$REPO_DIR"/scripts/wrappers/*; do
    [ -f "$w" ] && install -m 0755 -o root -g root "$w" "/usr/local/bin/$(basename "$w")"
  done
fi
if [ -d "$REPO_DIR/infra/hooks" ]; then
  echo "  installing Claude Code hooks to /etc/openclaw/hooks"
  mkdir -p /etc/openclaw/hooks
  for h in "$REPO_DIR"/infra/hooks/*.sh; do
    [ -f "$h" ] && install -m 0755 -o root -g root "$h" "/etc/openclaw/hooks/$(basename "$h")"
  done
fi

# ── 5. (Re)start the gateway units (owned/run by openclaw via the drop-in) ───
echo "[restart] $PROCESSES_PER_VM process(es)"
for i in $(seq 0 $((PROCESSES_PER_VM - 1))); do
  PORT=$((18789 + i))
  touch "/var/log/openclaw-${PORT}.log"
  chown "$OPENCLAW_USER:$OPENCLAW_USER" "/var/log/openclaw-${PORT}.log"
  systemctl enable "openclaw-gateway@${PORT}.service" --quiet || true
  systemctl restart "openclaw-gateway@${PORT}.service"
done
sleep 8
FAILED=0
for i in $(seq 0 $((PROCESSES_PER_VM - 1))); do
  PORT=$((18789 + i))
  STATE=$(systemctl is-active "openclaw-gateway@${PORT}.service" || true)
  echo "  openclaw-gateway@${PORT}: $STATE"
  if [ "$STATE" != "active" ]; then
    FAILED=1
    echo "  ---- last 50 lines of /var/log/openclaw-${PORT}.log ----"
    tail -n 50 "/var/log/openclaw-${PORT}.log" 2>/dev/null || echo "  (log not found)"
    echo "  ---- systemctl status ----"
    systemctl status "openclaw-gateway@${PORT}.service" --no-pager -l 2>/dev/null | tail -15 || true
    echo "  ---- end diagnostics for port ${PORT} ----"
  fi
done
echo "════════════════════════════════════════════════════════════════════"
echo "gateway provision complete $(date -u +%FT%TZ) (failed=$FAILED)"
echo "════════════════════════════════════════════════════════════════════"
exit $FAILED
