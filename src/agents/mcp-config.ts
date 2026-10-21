/**
 * Builds the inline --mcp-config JSON string for a claude-cli subprocess.
 *
 * The MCP server (extensions/custom-tools/mcp-server.ts) is spawned as a
 * stdio child of the claude process. It exposes exactly the tools in the
 * agent's resolved allow-list — nothing more.
 *
 * The MCP server path is read from OPENCLAW_MCP_SERVER_PATH, which must be
 * set in start-openclaw.sh to the ABSOLUTE path of mcp-server.ts:
 *
 *   export OPENCLAW_MCP_SERVER_PATH="$HOME/agentos-gateway/extensions/custom-tools/mcp-server.ts"
 *
 * tsx is used to execute the TypeScript entry point. Its path is resolved via
 * OPENCLAW_TSX_PATH (also set in start-openclaw.sh) or falls back to the tsx
 * binary inside the workspace's node_modules.
 *
 * Callers pass:
 *   - agentId      — gateway agent ID (becomes MCP_AGENT_ID in the subprocess env)
 *   - workspaceDir — absolute path to the agent workspace
 *   - sessionKey   — session identifier for audit events
 *   - toolsAllow   — resolved allow-list (profile-expanded, cancellation applied)
 *   - token        — narrow-scope token for control panel read access
 *   - controlPanelUrl — base URL for the AgentOS control panel
 *
 * Returns null when the MCP server path is not configured (OPENCLAW_MCP_SERVER_PATH
 * not set), allowing callers to skip MCP injection gracefully during rollout.
 */

import path from "node:path";
import { fileURLToPath } from "node:url";

export interface McpAgentContext {
  agentId: string;
  workspaceDir: string;
  sessionKey: string;
  toolsAllow: string[];
  /** Per-run token for the MCP subprocess to reach the control panel.
   *  NOT the gateway token (which is scrubbed from the subprocess env). */
  token: string;
  controlPanelUrl: string;
}

/**
 * Resolve the absolute path to the MCP server TypeScript file.
 *
 * Priority:
 *   1. OPENCLAW_MCP_SERVER_PATH env var (explicit, set by start-openclaw.sh)
 *   2. Relative from this file's location (works in dev without env var)
 */
function resolveMcpServerPath(): string | null {
  const explicit = process.env.OPENCLAW_MCP_SERVER_PATH?.trim();
  if (explicit) return explicit;

  // Dev fallback: resolve relative to this compiled file.
  // Compiled output is dist/agents/mcp-config.js; the server is at
  // extensions/custom-tools/mcp-server.ts from the repo root.
  try {
    const here = fileURLToPath(import.meta.url);
    const repoRoot = path.resolve(path.dirname(here), "..", "..");
    return path.join(repoRoot, "extensions", "custom-tools", "mcp-server.ts");
  } catch {
    return null;
  }
}

/**
 * Resolve the tsx executable path.
 *
 * Priority:
 *   1. OPENCLAW_TSX_PATH env var
 *   2. node_modules/.bin/tsx relative to the repo root (set by pnpm install)
 */
function resolveTsxPath(): string {
  const explicit = process.env.OPENCLAW_TSX_PATH?.trim();
  if (explicit) return explicit;

  try {
    const here = fileURLToPath(import.meta.url);
    const repoRoot = path.resolve(path.dirname(here), "..", "..");
    return path.join(repoRoot, "node_modules", ".bin", "tsx");
  } catch {
    return "tsx"; // fallback to PATH lookup
  }
}

/**
 * Build the inline JSON string for `--mcp-config`.
 *
 * Returns null when:
 *   - The MCP server path cannot be resolved (OPENCLAW_MCP_SERVER_PATH not set
 *     and the dev fallback fails) — callers skip MCP injection.
 *   - toolsAllow is empty — no tools to serve, MCP server would be empty.
 */
export function buildMcpConfigJson(ctx: McpAgentContext): string | null {
  if (ctx.toolsAllow.length === 0) {
    return null; // nothing to serve; don't spawn a server for nothing
  }

  const serverPath = resolveMcpServerPath();
  if (!serverPath) {
    return null;
  }

  const tsxPath = resolveTsxPath();

  const config = {
    mcpServers: {
      "agentos-tools": {
        type: "stdio",
        // Run via tsx so the TypeScript file needs no pre-compilation step.
        command: "node",
        args: ["--import", `${tsxPath}/esm`, serverPath],
        env: {
          CONTROL_PANEL_URL: ctx.controlPanelUrl,
          OPENCLAW_MCP_TOKEN: ctx.token,
          MCP_AGENT_ID: ctx.agentId,
          MCP_WORKSPACE_PATH: ctx.workspaceDir,
          MCP_SESSION_KEY: ctx.sessionKey,
          MCP_TOOLS_ALLOW: JSON.stringify(ctx.toolsAllow),
        },
      },
    },
  };

  return JSON.stringify(config);
}
