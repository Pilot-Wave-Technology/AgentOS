#!/bin/bash
set -e

export GEMINI_API_KEY=$(gcloud secrets versions access latest --secret=gemini-api-key --project=${GCP_PROJECT_ID})
export OPENCLAW_GATEWAY_TOKEN=${OPENCLAW_GATEWAY_TOKEN}

# MCP server — used by cli-runner to spawn the stdio tool server for claude-cli agents.
# OPENCLAW_MCP_SERVER_PATH: absolute path to the mcp-server.ts entry point.
# OPENCLAW_TSX_PATH: absolute path to the tsx binary for TypeScript execution.
# Both are resolved relative to the gateway's checkout directory.
export OPENCLAW_MCP_SERVER_PATH="$HOME/agentos-gateway/extensions/custom-tools/mcp-server.ts"
export OPENCLAW_TSX_PATH="$HOME/agentos-gateway/node_modules/.bin/tsx"

cd ~/agentos-gateway

# Pull latest changes, install deps, and rebuild
git pull origin openclaw-build
pnpm install --frozen-lockfile
pnpm build

# Start the gateway
node openclaw.mjs gateway
