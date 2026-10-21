type CustomEntryLike = { type?: unknown; customType?: unknown; data?: unknown };

export const CACHE_TTL_CUSTOM_TYPE = "openclaw.cache-ttl";

export type CacheTtlEntryData = {
  timestamp: number;
  provider?: string;
  modelId?: string;
};

const CACHE_TTL_NATIVE_PROVIDERS = new Set(["anthropic", "moonshot", "zai"]);
const OPENROUTER_CACHE_TTL_MODEL_PREFIXES = [
  "anthropic/",
  "moonshot/",
  "moonshotai/",
  "zai/",
] as const;

function isOpenRouterCacheTtlModel(modelId: string): boolean {
  return OPENROUTER_CACHE_TTL_MODEL_PREFIXES.some((prefix) => modelId.startsWith(prefix));
}

/** Base provider for a (possibly synthetic) provider key: the segment before
 *  the first "-". Synthetic context-window variants are registered under keys
 *  like "anthropic-sonnet-100k" (base "anthropic"), each wrapping a real
 *  upstream model. Cache-TTL eligibility is a property of the BASE provider, so
 *  synthetic Anthropic/moonshot/zai variants inherit it. "google-flash-50k"
 *  resolves to base "google", which is intentionally NOT eligible — cache-TTL
 *  is an Anthropic-family prompt-cache mechanism, not a Gemini one. */
export function baseProviderKey(provider: string): string {
  const dash = provider.indexOf("-");
  return dash > 0 ? provider.slice(0, dash) : provider;
}

export function isCacheTtlEligibleProvider(provider: string, modelId: string): boolean {
  const normalizedProvider = provider.toLowerCase();
  const normalizedModelId = modelId.toLowerCase();
  // Native cache-TTL providers, INCLUDING their synthetic context-window
  // variants (e.g. "anthropic-sonnet-100k" -> base "anthropic"). google/*
  // synthetic variants resolve to base "google" and stay ineligible.
  if (CACHE_TTL_NATIVE_PROVIDERS.has(baseProviderKey(normalizedProvider))) {
    return true;
  }
  if (normalizedProvider === "openrouter" && isOpenRouterCacheTtlModel(normalizedModelId)) {
    return true;
  }
  if (normalizedProvider === "kilocode" && normalizedModelId.startsWith("anthropic/")) {
    return true;
  }
  return false;
}

export function readLastCacheTtlTimestamp(sessionManager: unknown): number | null {
  const sm = sessionManager as { getEntries?: () => CustomEntryLike[] };
  if (!sm?.getEntries) {
    return null;
  }
  try {
    const entries = sm.getEntries();
    let last: number | null = null;
    for (let i = entries.length - 1; i >= 0; i--) {
      const entry = entries[i];
      if (entry?.type !== "custom" || entry?.customType !== CACHE_TTL_CUSTOM_TYPE) {
        continue;
      }
      const data = entry?.data as Partial<CacheTtlEntryData> | undefined;
      const ts = typeof data?.timestamp === "number" ? data.timestamp : null;
      if (ts && Number.isFinite(ts)) {
        last = ts;
        break;
      }
    }
    return last;
  } catch {
    return null;
  }
}

export function appendCacheTtlTimestamp(sessionManager: unknown, data: CacheTtlEntryData): void {
  const sm = sessionManager as {
    appendCustomEntry?: (customType: string, data: unknown) => void;
  };
  if (!sm?.appendCustomEntry) {
    return;
  }
  try {
    sm.appendCustomEntry(CACHE_TTL_CUSTOM_TYPE, data);
  } catch {
    // ignore persistence failures
  }
}
