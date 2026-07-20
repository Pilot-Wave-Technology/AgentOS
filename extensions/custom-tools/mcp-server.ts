/**
 * Stdio MCP server — wraps the existing custom-tools capability runtime for
 * use by claude-cli IA agent subprocesses.
 *
 * This file is a standalone entry point. It is NOT loaded as an OpenClaw plugin;
 * it is spawned as a child process by the `claude -p` subprocess via --mcp-config.
 * The plugin (index.ts) handles native agents via direct executeTool() calls;
 * this server handles claude-cli agents over stdio JSON-RPC.
 *
 * Invocation (assembled by the gateway in cli-runner.ts):
 *
 *   --mcp-config '{"mcpServers":{"agentos-tools":{"type":"stdio","command":"node",
 *     "args":["--import","tsx/esm","/abs/path/extensions/custom-tools/mcp-server.ts"],
 *     "env":{"CONTROL_PANEL_URL":"...","OPENCLAW_MCP_TOKEN":"...",
 *            "MCP_AGENT_ID":"...","MCP_WORKSPACE_PATH":"...",
 *            "MCP_SESSION_KEY":"...","MCP_TOOLS_ALLOW":"[\"t1\",\"t2\"]"}}}}'
 *   --strict-mcp-config
 *
 * The server:
 *   1. Reads config from env vars (URL, token, allow-list, agent context).
 *   2. Fetches the tool list from the control panel on startup (fail-fast).
 *   3. Filters the catalog to only the tools in MCP_TOOLS_ALLOW.
 *   4. Handles tools/list and tools/call over stdio via @modelcontextprotocol/sdk.
 *   5. Executes tools via executeTool() — the SAME V8-isolate runtime the native
 *      plugin uses; no duplicate implementation.
 *
 * Security notes:
 *   - OPENCLAW_GATEWAY_TOKEN is stripped from the subprocess env by cli-runner.ts
 *     (clearEnv). This server uses a separate OPENCLAW_MCP_TOKEN with a narrower
 *     scope (read tools + fetch credentials only).
 *   - The allow-list is enforced here AND at the capability runtime level (manifest
 *     controls which hosts/secrets/storage are accessible per tool).
 *   - All output goes to stdout (MCP messages) or stderr (logs). Never mix them.
 *
 * Run requirements:
 *   - tsx must be available: `node --import tsx/esm <this-file>`
 *     tsx is a devDependency of the openclaw workspace and available in node_modules.
 *   - OPENCLAW_MCP_SERVER_PATH env var (set in start-openclaw.sh) points to this
 *     file so cli-runner.ts can reference it without dynamic path resolution.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { getCachedTools, refreshCache } from "./src/cache.js";
import { executeTool } from "./src/runtime/isolate.js";
import type { AgentCallContext } from "./src/runtime/types.js";

// ── Config from environment ──────────────────────────────────────────────────

const CONTROL_PANEL_URL = (process.env.CONTROL_PANEL_URL ?? "").replace(/\/$/, "");
// OPENCLAW_MCP_TOKEN is a separate credential from OPENCLAW_GATEWAY_TOKEN.
// The gateway token is scrubbed from the subprocess env; this token is explicitly
// passed with a narrower scope (read tool catalog + fetch per-call credentials).
const TOKEN = process.env.OPENCLAW_MCP_TOKEN ?? "";
const AGENT_ID = process.env.MCP_AGENT_ID ?? "unknown";
const WORKSPACE_PATH = process.env.MCP_WORKSPACE_PATH ?? "";
const SESSION_KEY = process.env.MCP_SESSION_KEY ?? "";

// Allow-list — JSON array of tool names the agent is authorised to call.
// Empty set means no tools are served. The gateway passes the agent's resolved
// allow-list (after profile expansion and cancellation) into this env var.
const allowSet: Set<string> = (() => {
  const raw = process.env.MCP_TOOLS_ALLOW ?? "[]";
  try {
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return new Set<string>(parsed.filter((v): v is string => typeof v === "string"));
    }
    return new Set<string>();
  } catch {
    process.stderr.write(`[mcp-server] warn: MCP_TOOLS_ALLOW is not valid JSON — serving no tools\n`);
    return new Set<string>();
  }
})();

// ── Startup validation ───────────────────────────────────────────────────────

if (!CONTROL_PANEL_URL) {
  process.stderr.write("[mcp-server] fatal: CONTROL_PANEL_URL not set\n");
  process.exit(1);
}
if (!TOKEN) {
  process.stderr.write("[mcp-server] fatal: OPENCLAW_MCP_TOKEN not set\n");
  process.exit(1);
}

const allowList = [...allowSet];
process.stderr.write(
  `[mcp-server] starting — agent=${AGENT_ID} allowed_tools=${allowList.length === 0 ? "(none)" : allowList.join(",")}\n`,
);

// Fail-fast: if the control panel is unreachable on startup the agent run
// should error rather than silently having no tools available.
try {
  await refreshCache(CONTROL_PANEL_URL, TOKEN);
  const loaded = getCachedTools().length;
  process.stderr.write(`[mcp-server] loaded ${loaded} tool(s) from control panel\n`);
} catch (err) {
  process.stderr.write(`[mcp-server] fatal: could not load tool cache — ${String(err)}\n`);
  process.exit(1);
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Return only tools that are in the allow-list AND have executable source. */
function getServableTools() {
  return getCachedTools().filter(
    (def) =>
      allowSet.has(def.name) &&
      typeof def.source_code === "string" &&
      def.manifest !== undefined,
  );
}

// ── MCP server ───────────────────────────────────────────────────────────────

const server = new Server(
  { name: "agentos-tools", version: "1.0.0" },
  { capabilities: { tools: {} } },
);

// tools/list — return the allow-list-filtered catalog in MCP schema format.
server.setRequestHandler(ListToolsRequestSchema, async () => {
  const tools = getServableTools().map((def) => ({
    name: def.name,
    description: def.description,
    inputSchema: {
      type: "object" as const,
      ...(def.parameters_schema ?? {}),
    },
  }));
  process.stderr.write(`[mcp-server] tools/list → ${tools.length} servable tool(s)\n`);
  return { tools };
});

// tools/call — route to the capability runtime (same V8 isolate the plugin uses).
server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args = {} } = req.params;

  // Double-check the allow-list even though the client shouldn't be able to call
  // unlisted tools after tools/list — belt-and-suspenders against a misbehaving client.
  if (!allowSet.has(name)) {
    process.stderr.write(`[mcp-server] denied: tool '${name}' not in allow-list for agent=${AGENT_ID}\n`);
    return {
      isError: true,
      content: [{ type: "text" as const, text: `Tool '${name}' is not authorised for this agent session.` }],
    };
  }

  const def = getCachedTools().find((d) => d.name === name);
  if (!def) {
    return {
      isError: true,
      content: [{ type: "text" as const, text: `Tool '${name}' not found in cache. The tool list may be stale.` }],
    };
  }
  if (!def.source_code || !def.manifest) {
    return {
      isError: true,
      content: [{ type: "text" as const, text: `Tool '${name}' has no executable source (tier may not support direct execution).` }],
    };
  }

  const ctx: AgentCallContext = {
    tool_name: name,
    agent_id: AGENT_ID,
    workspace_path: WORKSPACE_PATH,
    control_panel_url: CONTROL_PANEL_URL,
    control_panel_token: TOKEN,
    session_key: SESSION_KEY,
  };

  process.stderr.write(`[mcp-server] tools/call start name=${name} agent=${AGENT_ID}\n`);

  try {
    const result = await executeTool(
      { name: def.name, source_code: def.source_code, manifest: def.manifest },
      args as Record<string, unknown>,
      ctx,
    );

    const text =
      result === undefined || result === null
        ? ""
        : typeof result === "string"
          ? result
          : JSON.stringify(result, null, 2);

    process.stderr.write(`[mcp-server] tools/call ok name=${name}\n`);
    return {
      isError: false,
      content: [{ type: "text" as const, text }],
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    process.stderr.write(`[mcp-server] tools/call error name=${name}: ${msg}\n`);
    return {
      isError: true,
      content: [{ type: "text" as const, text: `Tool '${name}' execution failed: ${msg}` }],
    };
  }
});

// ── Connect stdio transport and serve ────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
process.stderr.write("[mcp-server] connected and ready\n");
