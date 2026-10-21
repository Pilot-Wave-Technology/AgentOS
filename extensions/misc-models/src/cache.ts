/**
 * Fetches utility-model overrides from the AgentOS control panel's
 * `/internal/misc_model_settings` endpoint and writes them into the core
 * `src/agents/misc-model-overrides.ts` store, the same direction
 * `extensions/model-registry/src/cache.ts` already imports types FROM
 * `src/` — this plugin never holds its own copy of the data, it's a thin
 * fetch-and-validate layer over the shared core store.
 *
 * `refreshMiscModelCache()` fetches the endpoint and swaps the store
 * atomically; on any failure (HTTP non-2xx, non-JSON body, missing
 * top-level `overrides` object) the exception propagates and the
 * previous store contents are left in place.
 */
import {
  clearMiscModelOverrides,
  getMiscModelOverrides,
  setMiscModelOverrides,
} from "../../../src/agents/misc-model-overrides.js";

export function getCachedOverrides(): Record<string, string> {
  // For index.ts's log lines -- read from the shared store, which is
  // where the data lives. (This returned {} and every log said 0.)
  return getMiscModelOverrides();
}

export async function refreshMiscModelCache(url: string, token: string): Promise<void> {
  const endpoint = `${url.replace(/\/$/, "")}/internal/misc_model_settings`;
  const res = await fetch(endpoint, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new Error(`misc_model_settings fetch failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as unknown;
  if (
    typeof body !== "object" ||
    body === null ||
    typeof (body as { overrides?: unknown }).overrides !== "object" ||
    (body as { overrides?: unknown }).overrides === null
  ) {
    throw new Error("misc_model_settings response missing top-level 'overrides' object");
  }
  const raw = (body as { overrides: Record<string, unknown> }).overrides;
  const validated: Record<string, string> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (typeof value === "string" && value) {
      validated[key] = value;
    }
  }
  setMiscModelOverrides(validated);
}

export function clearMiscModelCache(): void {
  clearMiscModelOverrides();
}
