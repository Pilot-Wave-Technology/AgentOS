/**
 * Composition root for the runtime — wires the default implementations of
 * every interface defined in factories.ts and engine.ts.
 *
 * This is the ONE file that imports concrete implementations of every layer.
 * Migration / swap-out: change the binding here, not anywhere else.
 *
 * Imported once at plugin startup; the bundles it exports are stable across
 * the process lifetime and reused per call.
 */

import { makeMcpCapability } from "./capabilities/mcp.js";
import { makeNetCapability } from "./capabilities/net.js";
import { makeSecretsCapability } from "./capabilities/secrets.js";
import { makeStorageCapability } from "./capabilities/storage.js";
import { makeNodeVmEngine } from "./engines/node-vm.js";
import type { IsolateEngine } from "./engine.js";
import type { RuntimeBuilders } from "./factories.js";
import { makeHttpSdk } from "./sdk/http.js";
import { makeOpenApiSdk } from "./sdk/openapi.js";

export const defaultBuilders: RuntimeBuilders = {
  capabilities: {
    net: makeNetCapability,
    storage: makeStorageCapability,
    secrets: makeSecretsCapability,
    mcp: makeMcpCapability,
  },
  sdk: {
    http: makeHttpSdk,
    openapi: makeOpenApiSdk,
  },
};

export const defaultEngine: IsolateEngine = makeNodeVmEngine();
