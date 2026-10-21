/**
 * Per-model gateway reachability — is a specific model id actually usable
 * with a given (already-resolved) provider credential, not just "does the
 * credential resolve at all"?
 *
 * Uses each provider's own list-models endpoint — a metadata call, not a
 * billed generation request (see
 * research/model_registry_secret_management_design.md in the AgentOS repo
 * for why: probing via a real minimal completion would cost real tokens
 * per model per refresh cycle; this is free/cheap by comparison).
 *
 * Providers with no adapter here (anything other than `anthropic`/`google`
 * today) return `null` — "not checked", not "unreachable". The caller must
 * not treat a missing adapter as a red mark.
 */

export type ListModelsProbe = (apiKey: string) => Promise<string[]>;

const PROBES: Record<string, ListModelsProbe> = {
  anthropic: async (apiKey: string): Promise<string[]> => {
    const res = await fetch("https://api.anthropic.com/v1/models", {
      headers: { "x-api-key": apiKey, "anthropic-version": "2023-06-01" },
    });
    if (!res.ok) {
      throw new Error(`Anthropic /v1/models returned HTTP ${res.status}`);
    }
    const body = (await res.json()) as { data?: Array<{ id?: string }> };
    return (body.data ?? []).map((m) => m.id).filter((id): id is string => Boolean(id));
  },

  google: async (apiKey: string): Promise<string[]> => {
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models?key=${encodeURIComponent(apiKey)}`,
    );
    if (!res.ok) {
      throw new Error(`Google models.list returned HTTP ${res.status}`);
    }
    const body = (await res.json()) as { models?: Array<{ name?: string }> };
    // Google returns "models/gemini-2.5-flash" — strip the "models/" prefix
    // to match the bare model id our registry uses.
    return (body.models ?? [])
      .map((m) => m.name?.replace(/^models\//, ""))
      .filter((id): id is string => Boolean(id));
  },
};

/** Probe one provider's reachable model list. Returns `null` (not an
 * error) when no adapter exists for this provider id — callers should
 * skip recording reachability for that provider entirely rather than
 * marking every model under it red. Throws on an actual probe failure
 * (network error, non-2xx, bad body) so the caller can distinguish
 * "couldn't check" from "checked, provider offers nothing". */
export async function probeProviderModels(
  providerId: string,
  apiKey: string,
): Promise<string[] | null> {
  const probe = PROBES[providerId];
  if (!probe) {
    return null;
  }
  return probe(apiKey);
}
