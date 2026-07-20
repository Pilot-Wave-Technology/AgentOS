/**
 * In-memory cache of custom tool definitions fetched from the AgentOS control panel.
 *
 * getCachedTools() returns the cached list with zero I/O — this is called on every
 * chat.send via the registerTool factory.
 *
 * refreshCache() fetches /internal/custom-tools and replaces the list atomically.
 * Called at plugin startup, on every tools.reload RPC, and every 5 min as a
 * self-heal in case a reload signal was dropped.
 *
 * Execution itself happens via the capability runtime (runtime/isolate.ts) —
 * the cache holds source_code + manifest on each def, and the plugin's runTool
 * passes those into the runtime.
 */
import type { CustomToolDef } from "./types.js";

let toolCache: CustomToolDef[] = [];

export function getCachedTools(): CustomToolDef[] {
  return toolCache;
}

export async function refreshCache(controlPanelUrl: string, token: string): Promise<void> {
  const url = `${controlPanelUrl.replace(/\/$/, "")}/internal/custom-tools`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new Error(`custom-tools list fetch failed: HTTP ${res.status}`);
  }
  const defs = (await res.json()) as CustomToolDef[];
  toolCache = defs;
}
