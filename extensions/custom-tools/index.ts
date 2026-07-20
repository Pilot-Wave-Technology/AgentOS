import type { OpenClawPluginApi } from "../../src/plugins/types.js";
import { ErrorCodes, errorShape } from "../../src/gateway/protocol/index.js";
import { getCachedTools, refreshCache } from "./src/cache.js";
import { initAuditSink, stopAuditSink } from "./src/runtime/audit.js";
import { executeTool as executeRuntimeTool } from "./src/runtime/isolate.js";
import type { AgentCallContext } from "./src/runtime/types.js";

// Self-heal interval: if a tools.reload signal is dropped (gateway briefly
// unreachable when an admin saved a tool), the periodic refresh picks up
// the new tool list within REFRESH_INTERVAL_MS without any operator action.
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

function getControlPanelConfig(): { url: string; token: string } {
  const url = (process.env.CONTROL_PANEL_URL ?? "").replace(/\/$/, "");
  const token = process.env.OPENCLAW_GATEWAY_TOKEN ?? "";
  return { url, token };
}

const plugin = {
  id: "custom-tools",
  name: "Custom Tools",
  description:
    "Dynamically loads admin-defined tools from the AgentOS control panel. " +
    "Tools are fetched at startup, refreshed via tools.reload RPC on every " +
    "admin save, and re-fetched every 5 minutes as a safety net.",

  register(api: OpenClawPluginApi) {
    let refreshTimer: ReturnType<typeof setInterval> | null = null;

    // Fetch tool list on gateway start; warn-and-continue if unavailable (tools just won't load).
    api.on("gateway_start", async () => {
      const { url, token } = getControlPanelConfig();
      if (!url) {
        api.logger.warn("custom-tools: CONTROL_PANEL_URL not set — custom tools disabled");
        return;
      }

      // Initialise the audit sink so capability events forward to AgentOS.
      // Falls back to stdout-only when the sink can't reach AgentOS — events
      // are still observable via journalctl / Cloud Logging.
      initAuditSink({ url, token });

      try {
        await refreshCache(url, token);
        api.logger.info(`custom-tools: loaded ${getCachedTools().length} tool(s) from control panel`);
      } catch (err) {
        // Non-fatal: gateway continues without dynamic tools; periodic refresh below will retry.
        api.logger.warn(`custom-tools: initial cache load failed — ${String(err)}`);
      }

      // Schedule periodic refresh — self-heal in case a tools.reload RPC was missed.
      refreshTimer = setInterval(async () => {
        try {
          await refreshCache(url, token);
          api.logger.debug(`custom-tools: periodic refresh ok — ${getCachedTools().length} tool(s)`);
        } catch (err) {
          api.logger.debug(`custom-tools: periodic refresh failed (will retry) — ${String(err)}`);
        }
      }, REFRESH_INTERVAL_MS);
    });

    // Stop the periodic refresh + audit flusher when the gateway shuts down.
    api.on("gateway_stop", () => {
      if (refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
      }
      stopAuditSink();
    });

    // Tool factory — called on every chat.send; returns cached tools (zero I/O).
    api.registerTool((ctx) => {
      const { url, token } = getControlPanelConfig();
      if (!url) return [];

      const sessionKey = ctx.sessionKey ?? "";
      const agentId = ctx.agentId ?? sessionKey;
      const workspacePath = ctx.workspaceDir ?? "";

      return getCachedTools().map((def) => ({
        name: def.name,
        label: def.label,
        description: def.description,
        parameters: def.parameters_schema,
        execute: async (_toolCallId: string, params: Record<string, unknown>) => {
          const result = await runTool(def, params, {
            url,
            token,
            sessionKey,
            agentId,
            workspacePath,
          });
          const text =
            typeof result === "string" ? result : JSON.stringify(result, null, 2);
          return {
            content: [{ type: "text" as const, text }],
            details: result,
          };
        },
      }));
    });

    // ── Per-tool execution ─────────────────────────────────────────────────────
    //
    // Every tool is executed locally via the capability runtime. Tools without
    // source_code + manifest are misconfigured (or pre-runtime legacy rows that
    // should be re-created via the wizards) — we reject them clearly rather
    // than silently falling back.
    //
    // workspacePath comes from ctx.workspaceDir on the plugin context — this is
    // what the gateway sets per agent invocation. Empty string is passed
    // through when no workspace is bound (test-execute, agents without one);
    // storage capability throws immediately if a tool tries to use it without
    // a workspace, which is the desired fail-closed behaviour.
    async function runTool(
      def: ReturnType<typeof getCachedTools>[number],
      params: Record<string, unknown>,
      cfg: {
        url: string;
        token: string;
        sessionKey: string;
        agentId: string;
        workspacePath: string;
      },
    ): Promise<unknown> {
      if (!def.source_code || !def.manifest) {
        throw new Error(
          `tool '${def.name}' is missing source_code or manifest — ` +
            `re-create it through the import wizard`,
        );
      }
      const ctx: AgentCallContext = {
        tool_name: def.name,
        agent_id: cfg.agentId,
        workspace_path: cfg.workspacePath,
        control_panel_url: cfg.url,
        control_panel_token: cfg.token,
        session_key: cfg.sessionKey,
      };
      return await executeRuntimeTool(
        { name: def.name, source_code: def.source_code, manifest: def.manifest },
        params,
        ctx,
      );
    }

    // tools.reload RPC — called by AgentOS after any create/update/disable action.
    // Signal-only: no payload, just refreshes the in-memory cache.
    api.registerGatewayMethod("tools.reload", async ({ respond }) => {
      const { url, token } = getControlPanelConfig();
      if (!url) {
        respond(
          false,
          undefined,
          errorShape(ErrorCodes.UNAVAILABLE, "CONTROL_PANEL_URL not set"),
        );
        return;
      }
      try {
        await refreshCache(url, token);
        const count = getCachedTools().length;
        respond(true, { ok: true, count });
        api.logger.info(`custom-tools: cache refreshed — ${count} tool(s) active`);
      } catch (err) {
        respond(false, undefined, errorShape(ErrorCodes.UNAVAILABLE, String(err)));
      }
    });

    // tools.test_execute RPC — admin-initiated tool test from the AgentOS UI.
    // No agent context (no session_key, no workspace_path) — storage-using
    // tools will fail with a clear error. Bound by the gateway's own auth
    // (operator.admin scope) since this comes in over the admin WS.
    api.registerGatewayMethod("tools.test_execute", async ({ params, respond }) => {
      const p = (params ?? {}) as { toolName?: unknown; args?: unknown };
      const toolName = typeof p.toolName === "string" ? p.toolName : undefined;
      const args = (p.args && typeof p.args === "object" ? p.args : {}) as Record<string, unknown>;
      if (!toolName) {
        respond(
          false,
          undefined,
          errorShape(ErrorCodes.INVALID_REQUEST, "toolName (string) required"),
        );
        return;
      }
      const def = getCachedTools().find((t) => t.name === toolName);
      if (!def) {
        respond(
          false,
          undefined,
          errorShape(ErrorCodes.INVALID_REQUEST, `tool '${toolName}' not in cache`),
        );
        return;
      }
      if (!def.source_code || !def.manifest) {
        respond(
          false,
          undefined,
          errorShape(
            ErrorCodes.UNAVAILABLE,
            `tool '${toolName}' missing source_code/manifest — re-create via wizard`,
          ),
        );
        return;
      }
      const { url, token } = getControlPanelConfig();
      const ctx: AgentCallContext = {
        tool_name: def.name,
        agent_id: "admin-test",
        workspace_path: "",
        control_panel_url: url,
        control_panel_token: token,
        session_key: "admin-test",
      };
      try {
        const result = await executeRuntimeTool(
          { name: def.name, source_code: def.source_code, manifest: def.manifest },
          args,
          ctx,
        );
        // Mirror the legacy execute endpoint's shape so callers don't have to
        // distinguish tool-success from tool-error from RPC payload alone.
        respond(true, { ok: true, result });
      } catch (err) {
        respond(true, { ok: false, error: String(err) });
      }
    });
  },
};

export default plugin;
