/**
 * In-memory cache of provider definitions fetched from the AgentOS control
 * panel's `/internal/model_catalog` endpoint.
 *
 * `getCachedProviders()` returns the cached map with zero I/O — this is what
 * the plugin's `resolve_providers` hook handler returns on every
 * `planOpenClawModelsJson` pass. The runner enforces a 500 ms soft budget
 * per handler; the hook path MUST NOT perform I/O.
 *
 * `refreshCache()` fetches the endpoint and swaps the cache atomically —
 * on any failure (HTTP non-2xx, non-JSON body, missing top-level
 * `providers` object) the exception propagates and the previous cache is
 * left in place. Called on `gateway_start`, every 5 min as a self-heal,
 * and on the `models.reload` gateway RPC.
 *
 * A shallow clone is returned from `getCachedProviders()` so a
 * mis-behaving downstream consumer cannot mutate the module-scope map
 * between passes.
 */
import type { ProviderConfig } from "../../../src/agents/models-config.providers.js";
import { assertCatalogShape, type ProviderCredentialRef } from "./types.js";

let cache: Record<string, ProviderConfig> = {};
// Secret references (never resolved values) + flat model ids for the
// current cache generation — read by index.ts's refresh flow to drive
// secret-resolver.ts + model-reachability.ts. Not exposed alongside
// getCachedProviders() since these serve a different consumer
// (credential/reachability refresh, not the resolve_providers hook).
let cachedCredentialRefs: Record<string, ProviderCredentialRef> = {};
let cachedModelIds: string[] = [];

export function getCachedProviders(): Record<string, ProviderConfig> {
  return { ...cache };
}

export function getCachedCredentialRefs(): Record<string, ProviderCredentialRef> {
  return { ...cachedCredentialRefs };
}

/** Base-model registry ids (e.g. "anthropic/claude-opus-5") known from
 * the current cache generation's `models` field — used to know which
 * ids to probe reachability for, and which absent-from-the-provider's-
 * list ids should be marked unreachable rather than left unknown. */
export function getCachedModelIds(): string[] {
  return [...cachedModelIds];
}

export async function refreshCache(url: string, token: string): Promise<void> {
  const endpoint = `${url.replace(/\/$/, "")}/internal/model_catalog`;
  const res = await fetch(endpoint, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new Error(`model_catalog fetch failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as unknown;
  const validated = assertCatalogShape(body);
  cache = validated.providers;
  cachedCredentialRefs = validated.providerCredentials ?? {};
  cachedModelIds = Object.keys(validated.models ?? {});
}

export function clearCache(): void {
  cache = {};
  cachedCredentialRefs = {};
  cachedModelIds = [];
}
