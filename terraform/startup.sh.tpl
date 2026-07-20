#!/bin/bash
# AgentOS gateway VM startup — rendered by Terraform. Idempotent on reboot.
#
# This does the minimum to make the VM ready, then hands off to the single
# provisioning script (scripts/gateway-provision.sh) which owns build + full
# openclaw config + models + wrappers/hooks + starting the gateway. That SAME
# script is re-run by cloudbuild.yaml on every push to your fork, so a fresh
# boot and a redeploy converge on identical state.
#
# Steps: install deps -> clone the fork -> write env + provision metadata ->
# install the systemd unit + control-panel-url timer -> run gateway-provision.sh.
#
# Logs: /var/log/openclaw-startup.log (this) + /var/log/openclaw-provision.log
set -euo pipefail
exec > >(tee -a /var/log/openclaw-startup.log) 2>&1
export HOME=/root

PROJECT="${project_id}"
REPO_URL="${repo_url}"
REPO_BRANCH="${repo_branch}"
GATEWAY_TOKEN_SECRET="${gateway_token_secret}"
ANTHROPIC_KEY_SECRET="${anthropic_key_secret}"
GEMINI_KEY_SECRET="${gemini_key_secret}"
OPENAI_KEY_SECRET="${openai_key_secret}"
CONTROL_PANEL_URL_SECRET="${control_panel_url_secret}"
REPO_TOKEN_SECRET="${repo_token_secret}"
ARCHIVE_BUCKET="${archive_bucket}"
PROCESSES_PER_VM=${processes_per_vm}
ENABLE_CRD="${enable_chrome_remote_desktop}"

REPO_DIR=/opt/agentos-gateway

echo "=== AgentOS gateway startup $(date -u +%FT%TZ) ==="

# 1. System deps ------------------------------------------------------------
if ! command -v node &>/dev/null || [ "$(node -v | cut -d. -f1)" != "v22" ]; then
  apt-get update
  apt-get install -y curl ca-certificates gnupg jq build-essential git
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
  npm install -g pnpm@10.23.0
fi
command -v jq &>/dev/null || apt-get install -y jq
command -v claude &>/dev/null || npm install -g @anthropic-ai/claude-code

# The gateway runs as this NON-root user. Claude Code refuses
# --permission-mode bypassPermissions under root, and the agents' claude-cli
# backend requires it — so the gateway (and every claude subprocess) must be
# unprivileged. Agent workspaces + config live in /home/openclaw/.openclaw.
id -u openclaw &>/dev/null || useradd -m -s /bin/bash openclaw

# 2. Clone / update the fork ------------------------------------------------
# Public repo -> plain clone. Private repo -> a token in the gateway-repo-token
# secret is injected into the https URL (x-access-token). Blank secret = public.
# provision.sh chowns the repo to openclaw, so mark it safe for root git ops
# (startup + the cloudbuild redeploy `git pull` run as root).
git config --system --get-all safe.directory 2>/dev/null | grep -qx "$REPO_DIR" \
  || git config --system --add safe.directory "$REPO_DIR"
CLONE_URL="$REPO_URL"
# tr strips the inert whitespace placeholder used for "no token" (public fork).
REPO_TOKEN=$(gcloud secrets versions access latest --secret="$REPO_TOKEN_SECRET" --project="$PROJECT" 2>/dev/null | tr -d '[:space:]')
if [ -n "$REPO_TOKEN" ]; then
  CLONE_URL=$(echo "$REPO_URL" | sed -E "s#https://#https://x-access-token:$REPO_TOKEN@#")
fi
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone --branch "$REPO_BRANCH" --depth 1 "$CLONE_URL" "$REPO_DIR"
else
  git -C "$REPO_DIR" remote set-url origin "$CLONE_URL"
  git -C "$REPO_DIR" fetch --depth 1 origin "$REPO_BRANCH"
  git -C "$REPO_DIR" reset --hard "origin/$REPO_BRANCH"
fi
# For a PRIVATE repo the remote keeps the token so later `git pull` (redeploy)
# still authenticates. It lives only in root-owned /opt/agentos-gateway/.git/config
# on a VM whose SA can already read that same token from Secret Manager — no new
# exposure. For a PUBLIC repo CLONE_URL has no token, so nothing is stored.

# 3. Fetch secrets + write /etc/openclaw/env --------------------------------
GEMINI_API_KEY=$(gcloud secrets versions access latest --secret="$GEMINI_KEY_SECRET" --project="$PROJECT")
ANTHROPIC_API_KEY=$(gcloud secrets versions access latest --secret="$ANTHROPIC_KEY_SECRET" --project="$PROJECT")
OPENCLAW_GATEWAY_TOKEN=$(gcloud secrets versions access latest --secret="$GATEWAY_TOKEN_SECRET" --project="$PROJECT")
OPENAI_API_KEY=""
if [ -n "$OPENAI_KEY_SECRET" ]; then
  OPENAI_API_KEY=$(gcloud secrets versions access latest --secret="$OPENAI_KEY_SECRET" --project="$PROJECT" 2>/dev/null || echo "")
fi

mkdir -p /etc/openclaw
cat > /etc/openclaw/env <<EOF
GEMINI_API_KEY=$GEMINI_API_KEY
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
OPENAI_API_KEY=$OPENAI_API_KEY
OPENCLAW_GATEWAY_TOKEN=$OPENCLAW_GATEWAY_TOKEN
CONTROL_PANEL_URL=
PROCESSES_PER_VM=$PROCESSES_PER_VM
AGENT_ARCHIVE_BUCKET=$ARCHIVE_BUCKET
ANTHROPIC_BETAS=context-1m-2025-08-07
EOF
chmod 600 /etc/openclaw/env

# Provisioning metadata for gateway-provision.sh (project + secret NAMES so a
# redeploy can re-fetch rotated secrets without a VM recreate).
cat > /etc/openclaw/provision.env <<EOF
PROJECT=$PROJECT
GATEWAY_TOKEN_SECRET=$GATEWAY_TOKEN_SECRET
ANTHROPIC_KEY_SECRET=$ANTHROPIC_KEY_SECRET
GEMINI_KEY_SECRET=$GEMINI_KEY_SECRET
OPENAI_KEY_SECRET=$OPENAI_KEY_SECRET
CONTROL_PANEL_URL_SECRET=$CONTROL_PANEL_URL_SECRET
PROCESSES_PER_VM=$PROCESSES_PER_VM
EOF
chmod 600 /etc/openclaw/provision.env

# 4. systemd unit — runs as the NON-root openclaw user (Claude Code refuses
#    bypassPermissions under root). HOME=/home/openclaw so the gateway reads
#    /home/openclaw/.openclaw/openclaw.json. systemd reads EnvironmentFile as root
#    BEFORE dropping to openclaw, so /etc/openclaw/env stays root:root 600.
cat > /etc/systemd/system/openclaw-gateway@.service <<EOF
[Unit]
Description=AgentOS gateway on port %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=openclaw
Group=openclaw
Environment=HOME=/home/openclaw
WorkingDirectory=$REPO_DIR
EnvironmentFile=/etc/openclaw/env
Environment=OPENCLAW_GATEWAY_PORT=%i
ExecStart=/usr/bin/node openclaw.mjs gateway
StandardOutput=append:/var/log/openclaw-%i.log
StandardError=append:/var/log/openclaw-%i.log
Restart=on-failure
RestartSec=10
ProtectSystem=false

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# 5. control-panel-url refresh timer (poll Secret Manager every 60s) --------
cat > /usr/local/bin/refresh-control-panel-url.sh <<'SCRIPT'
#!/bin/bash
set -uo pipefail
SECRET="$1"; ENV_FILE=/etc/openclaw/env
LATEST=$(gcloud secrets versions access latest --secret="$SECRET" 2>/dev/null || echo "")
# Treat empty OR the whitespace placeholder (pre-stage-2) as "not set yet".
[ -z "$(printf %s "$LATEST" | tr -d '[:space:]')" ] && exit 0
CURRENT=$(grep -E '^CONTROL_PANEL_URL=' "$ENV_FILE" | cut -d= -f2- || echo "")
[ "$LATEST" = "$CURRENT" ] && exit 0
TMP=$(mktemp)
awk -v new="CONTROL_PANEL_URL=$LATEST" '/^CONTROL_PANEL_URL=/{print new;next}{print}' "$ENV_FILE" > "$TMP"
chmod 600 "$TMP"; mv "$TMP" "$ENV_FILE"
mapfile -t UNITS < <(systemctl list-units 'openclaw-gateway@*.service' --no-legend --no-pager --plain 2>/dev/null | awk '{print $1}')
for unit in "$${UNITS[@]}"; do systemctl restart "$unit"; done
logger -t openclaw-refresh "CONTROL_PANEL_URL updated; restarted $${#UNITS[@]} unit(s)"
SCRIPT
chmod 755 /usr/local/bin/refresh-control-panel-url.sh

cat > /etc/systemd/system/refresh-control-panel-url.service <<EOF
[Unit]
Description=Refresh CONTROL_PANEL_URL from Secret Manager
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/refresh-control-panel-url.sh ${control_panel_url_secret}
EOF

cat > /etc/systemd/system/refresh-control-panel-url.timer <<'EOF'
[Unit]
Description=Poll Secret Manager for control panel URL changes
[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
Unit=refresh-control-panel-url.service
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now refresh-control-panel-url.timer

# 6. Provision (build + full config + models + wrappers/hooks + start) -------
# The single source of truth, shared with the Cloud Build redeploy path.
bash "$REPO_DIR/scripts/gateway-provision.sh"

touch /var/lib/openclaw-system-ready
echo "=== AgentOS gateway startup complete $(date -u +%FT%TZ) ==="
