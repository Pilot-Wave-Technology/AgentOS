/**
 * Per-call tool execution — orchestrates capability construction, source
 * wrapping, and engine execution.
 *
 * This module depends only on INTERFACES (engine.ts, factories.ts). The
 * concrete capability/SDK/engine implementations are bound in defaults.ts
 * and passed in via the optional `config` parameter. Callers that want to
 * swap an implementation (test mocks, isolated-vm migration, alt secret
 * backend) override the relevant field and leave the rest at default.
 *
 * Synchronous timeout is enforced by the engine. Async timeout is enforced
 * by the engine. Memory limits, hard async termination, and CPU quotas are
 * engine-specific properties — the node-vm default does not enforce them;
 * an isolated-vm or worker_threads engine would. See engines/node-vm.ts
 * for the current trade-offs.
 */

import { emit } from "./audit.js";
import { defaultBuilders, defaultEngine } from "./defaults.js";
import type { IsolateEngine } from "./engine.js";
import type { RuntimeBuilders } from "./factories.js";
import type { AgentCallContext, RuntimeTool } from "./types.js";
import { validate as validateAgainstSchema } from "./validate.js";

const DEFAULT_TIMEOUT_MS = 30_000;
const MAX_TIMEOUT_MS = 120_000;
const MIN_TIMEOUT_MS = 1_000;

export interface ExecuteOptions {
  /** Override individual builders (capabilities, SDK helpers). */
  builders?: Partial<RuntimeBuilders> & {
    capabilities?: Partial<RuntimeBuilders["capabilities"]>;
    sdk?: Partial<RuntimeBuilders["sdk"]>;
  };
  /** Override the execution engine (default: node:vm). */
  engine?: IsolateEngine;
}

export async function executeTool(
  tool: RuntimeTool,
  args: Record<string, unknown>,
  ctx: AgentCallContext,
  options: ExecuteOptions = {},
): Promise<unknown> {
  const builders = mergeBuilders(options.builders);
  const engine = options.engine ?? defaultEngine;

  const timeoutMs = Math.min(
    Math.max(tool.manifest.limits?.timeout_ms ?? DEFAULT_TIMEOUT_MS, MIN_TIMEOUT_MS),
    MAX_TIMEOUT_MS,
  );

  // Build only the capabilities the tool's manifest granted. The builder
  // returns undefined when the grant is undefined, so we never expose a
  // capability the tool didn't ask for.
  const net = builders.capabilities.net(tool.manifest.capabilities?.net, ctx);
  const storage = builders.capabilities.storage(tool.manifest.capabilities?.storage, ctx);
  const secrets = builders.capabilities.secrets(tool.manifest.capabilities?.secrets, ctx);
  const mcp = builders.capabilities.mcp(tool.manifest.capabilities?.mcp, ctx);

  // SDK helpers are only meaningful when their underlying capability is
  // granted. They add no privilege of their own.
  const http = net ? builders.sdk.http(net) : undefined;
  const openapi = net ? builders.sdk.openapi(net) : undefined;

  const contextObj: Record<string, unknown> = {
    args,
    console: makeSafeConsole(ctx),
    // validate is always available — adds no privilege, just a JSON-Schema
    // checker tools can use to fail loudly on bad args before doing work.
    // Generators emit validate(args.arguments, OP_SCHEMAS[op]) at the top of
    // generated source.
    validate: validateAgainstSchema,
    ...(net && { net }),
    ...(storage && { storage }),
    ...(secrets && { secrets }),
    ...(mcp && { mcp }),
    ...(http && { http }),
    ...(openapi && { openapi }),
    __tool_result: undefined,
  };

  const wrappedSource = wrapToolSource(tool.source_code);

  try {
    return await engine.run(wrappedSource, contextObj, {
      timeout_ms: timeoutMs,
      name: `tool:${tool.name}`,
    });
  } catch (err) {
    emit(ctx, {
      event: "tool.execute",
      outcome: "denied",
      reason: "engine_error",
      detail: { error: String(err) },
    });
    throw err;
  }
}

/** Merge user-provided builder overrides on top of the defaults, field by field. */
function mergeBuilders(override?: ExecuteOptions["builders"]): RuntimeBuilders {
  if (!override) return defaultBuilders;
  return {
    capabilities: { ...defaultBuilders.capabilities, ...(override.capabilities ?? {}) },
    sdk: { ...defaultBuilders.sdk, ...(override.sdk ?? {}) },
  };
}

/**
 * Wrap user source so that:
 *   1. `export default async function execute` is reduced to `async function execute`
 *      (vm.Script-style engines evaluate as scripts, not ES modules)
 *   2. After the user's source runs, we invoke execute(args) and stash the
 *      promise on the context as __tool_result for the engine to await.
 */
function wrapToolSource(source: string): string {
  const stripped = source.replace(
    /export\s+default\s+(async\s+)?function\s+execute\b/,
    "$1function execute",
  );
  return `
    "use strict";
    ${stripped}
    if (typeof execute !== "function") {
      throw new Error("tool source must define an 'execute' function");
    }
    __tool_result = (async () => execute(args))();
  `;
}

/** Console that logs through the audit pipeline instead of bare stdout. */
function makeSafeConsole(ctx: AgentCallContext) {
  const emitLog = (level: "log" | "warn" | "error", parts: unknown[]) => {
    emit(ctx, {
      event: `console.${level}`,
      outcome: "ok",
      detail: {
        message: parts.map((p) => (typeof p === "string" ? p : safeStringify(p))).join(" "),
      },
    });
  };
  return Object.freeze({
    log: (...args: unknown[]) => emitLog("log", args),
    warn: (...args: unknown[]) => emitLog("warn", args),
    error: (...args: unknown[]) => emitLog("error", args),
  });
}

function safeStringify(v: unknown): string {
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
