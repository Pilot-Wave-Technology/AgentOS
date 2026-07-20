/**
 * mcp.call capability — server-allowlisted MCP tool invocation.
 *
 * Construction:
 *   const mcp = makeMcpCapability(manifest.capabilities.mcp, ctx);
 *
 * Per-call flow:
 *   1. Verify serverUrl is in grant.servers (exact match — no globbing)
 *   2. Open an MCP SSE client session
 *   3. Call the named tool with the supplied arguments
 *   4. Close the session before returning
 *
 * Each call opens a fresh connection. Tools that make multiple MCP calls
 * pay one round-trip handshake per call — acceptable for low-frequency
 * agent flows. Connection pooling can be added later behind the same
 * `McpCapability` interface without changing tool-source.
 *
 * The MCP SDK is loaded via dynamic import so tools that don't grant the
 * mcp capability don't pay the load cost.
 */

import { emit } from "../audit.js";
import type { AgentCallContext, McpGrant } from "../types.js";

export interface McpCallOptions {
  /** Headers to attach to BOTH the SSE handshake and the message POST. */
  headers?: Record<string, string>;
}

export interface McpCapability {
  call: (
    serverUrl: string,
    toolName: string,
    args: Record<string, unknown>,
    options?: McpCallOptions,
  ) => Promise<unknown>;
}

export function makeMcpCapability(
  grant: McpGrant | undefined,
  ctx: AgentCallContext,
): McpCapability | undefined {
  if (!grant) return undefined;

  const servers = new Set(grant.servers);

  return Object.freeze({
    call: async (serverUrl, toolName, args = {}, options) => {
      if (typeof serverUrl !== "string" || typeof toolName !== "string") {
        throw new Error("mcp.call: serverUrl and toolName must be strings");
      }
      if (!servers.has(serverUrl)) {
        emit(ctx, {
          event: "mcp.call",
          outcome: "denied",
          reason: "server_not_granted",
          detail: { server: serverUrl, tool: toolName, granted: [...servers] },
        });
        throw new Error(`mcp.call: server '${serverUrl}' not in tool manifest`);
      }

      emit(ctx, {
        event: "mcp.call",
        outcome: "ok",
        detail: {
          server: serverUrl,
          tool: toolName,
          // Header NAMES only — never values (which may carry secrets).
          header_keys: Object.keys(options?.headers ?? {}),
        },
      });

      // Dynamic import — only loaded if a tool actually uses MCP.
      const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
      const { SSEClientTransport } = await import(
        "@modelcontextprotocol/sdk/client/sse.js"
      );

      const headers = options?.headers ?? {};
      // Send the same headers on BOTH the SSE handshake (an HTTP GET via
      // eventSourceInit.fetch) and the message POST (requestInit). Some MCP
      // servers check on handshake; some on each message.
      const transport = new SSEClientTransport(new URL(serverUrl), {
        requestInit: { headers },
        eventSourceInit: {
          fetch: (input, init) =>
            globalThis.fetch(input, {
              ...(init ?? {}),
              headers: { ...(init?.headers ?? {}), ...headers },
            }),
        },
      } as Parameters<typeof SSEClientTransport>[1]);
      const client = new Client(
        { name: "agentos-tool-runtime", version: "1.0.0" },
        { capabilities: {} },
      );

      try {
        await client.connect(transport);
        const result = await client.callTool({ name: toolName, arguments: args });
        // Return the content payload — usually an array of { type, text } items,
        // which the agent runtime can unwrap further. Caller's job to interpret.
        return result.content;
      } finally {
        try {
          await client.close();
        } catch {
          // Best-effort close — already returning, don't mask the original outcome.
        }
      }
    },
  });
}
