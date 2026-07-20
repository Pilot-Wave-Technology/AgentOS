/**
 * Capability and SDK builder interfaces — the dependency-inversion seam.
 *
 * `executeTool` does NOT import concrete capability or SDK implementations.
 * It takes a `RuntimeBuilders` bundle whose default value (`defaultBuilders`
 * in defaults.ts) wires the current implementations.
 *
 * To replace a capability or SDK implementation:
 *   1. Write a new factory matching the relevant builder signature
 *   2. Override that single field in the builders bundle passed to executeTool
 *
 * Adding a new capability ("kv", "queue", "telemetry"):
 *   1. Define interface + grant type
 *   2. Add a builder field to CapabilityBuilders
 *   3. Wire in defaults.ts
 *   4. Reference in isolate.ts's context construction
 *
 * Nothing in capabilities/* or sdk/* imports from this file — the dependency
 * arrow points from the orchestrator (isolate.ts + defaults.ts) toward the
 * concrete modules, never the other way.
 */

import type { McpCapability } from "./capabilities/mcp.js";
import type { NetCapability } from "./capabilities/net.js";
import type {
  StorageReadCapability,
  StorageReadWriteCapability,
} from "./capabilities/storage.js";
import type { SecretsCapability } from "./capabilities/secrets.js";
import type { HttpSdk } from "./sdk/http.js";
import type { OpenApiSdk } from "./sdk/openapi.js";
import type {
  AgentCallContext,
  McpGrant,
  NetGrant,
  SecretsGrant,
  StorageGrant,
} from "./types.js";

/** Builders for first-class capabilities. Each builder may return undefined when no grant is provided. */
export interface CapabilityBuilders {
  net: (grant: NetGrant | undefined, ctx: AgentCallContext) => NetCapability | undefined;
  storage: (
    grant: StorageGrant | undefined,
    ctx: AgentCallContext,
  ) => StorageReadCapability | StorageReadWriteCapability | undefined;
  secrets: (
    grant: SecretsGrant | undefined,
    ctx: AgentCallContext,
  ) => SecretsCapability | undefined;
  mcp: (grant: McpGrant | undefined, ctx: AgentCallContext) => McpCapability | undefined;
}

/** Builders for SDK helper modules. Built on top of capabilities; add no privilege. */
export interface SdkBuilders {
  http: (net: NetCapability) => HttpSdk;
  openapi: (net: NetCapability) => OpenApiSdk;
}

/** Bundle passed to executeTool. All fields default via `defaultBuilders` in defaults.ts. */
export interface RuntimeBuilders {
  capabilities: CapabilityBuilders;
  sdk: SdkBuilders;
}
