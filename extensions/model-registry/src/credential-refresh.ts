/**
 * Orchestrates secret-resolver.ts + model-reachability.ts into the
 * combined report AgentOS's `gateway_sync._persist_credential_report`
 * expects back from the `models.reload` RPC response:
 *
 *   { credentials: {providerId: {healthy, error?}},
 *     modelReachability: {modelId: boolean} }
 *
 * The resolved secret value only ever exists in a local variable inside
 * `refreshCredentialsAndReachability`'s loop body (used immediately to
 * call that provider's list-models endpoint) plus, via `injectResolvedApiKey`,
 * this process's own `process.env` — the real auth bridge (see
 * env-injector.ts). It is never returned, logged, or persisted to disk.
 */
import { clearInjectedApiKeysExcept, injectResolvedApiKey } from "./env-injector.js";
import { probeProviderModels } from "./model-reachability.js";
import { resolveProviderSecret } from "./secret-resolver.js";
import type { ProviderCredentialRef } from "./types.js";

export interface CredentialRefreshReport {
  credentials: Record<string, { healthy: boolean; error?: string }>;
  modelReachability: Record<string, boolean>;
}

export async function refreshCredentialsAndReachability(
  providerCredentials: Record<string, ProviderCredentialRef>,
  knownModelIds: string[],
): Promise<CredentialRefreshReport> {
  const credentials: CredentialRefreshReport["credentials"] = {};
  const modelReachability: CredentialRefreshReport["modelReachability"] = {};

  await Promise.all(
    Object.entries(providerCredentials).map(async ([providerId, ref]) => {
      const idsForProvider = knownModelIds.filter((id) => id.startsWith(`${providerId}/`));

      let apiKey: string;
      try {
        apiKey = await resolveProviderSecret(ref);
      } catch (err) {
        credentials[providerId] = { healthy: false, error: String((err as Error)?.message ?? err) };
        // No key to check with — leave reachability unset (not false) for
        // this provider's models rather than falsely marking them red;
        // AgentOS's set_models_gateway_reachability_v2 only overwrites ids
        // it's explicitly told about, so omitting these leaves their
        // last-known status in place.
        return;
      }

      credentials[providerId] = { healthy: true };

      // Bridge into real auth: for a provider id model-auth.ts already
      // recognizes, this makes the resolved secret immediately usable for
      // actual inference calls — no cfg mutation, no restart. See
      // env-injector.ts's docstring.
      injectResolvedApiKey(providerId, apiKey);

      let reachableIds: string[] | null;
      try {
        reachableIds = await probeProviderModels(providerId, apiKey);
      } catch {
        // Credential resolved fine but the probe itself failed (network
        // blip, provider outage) — don't report any reachability for this
        // provider's models this cycle rather than guessing.
        return;
      }
      // `apiKey` goes out of scope after this point; nothing above or
      // below retains it.
      if (reachableIds === null) {
        return; // no probe adapter for this provider id — not applicable
      }

      // Providers' own list-models endpoints return dated snapshot ids
      // (e.g. `claude-sonnet-4-5-20250929`), but the registry stores the
      // bare alias (`claude-sonnet-4-5`) as `model_name` — the same alias
      // actually used for inference. An exact match would mark every
      // native Anthropic model unreachable even though it works fine.
      // Strip the trailing snapshot date before comparing, mirroring the
      // fold-back regex `dashboard_metrics.py`'s cost CTE already uses.
      const reachableSet = new Set(reachableIds.map((id) => id.replace(/-\d{8}$/, "")));
      for (const modelId of idsForProvider) {
        const rawModelName = modelId.slice(providerId.length + 1);
        modelReachability[modelId] = reachableSet.has(rawModelName);
      }
    }),
  );

  // Unset any previously-injected env var for a provider that dropped
  // out of `providerCredentials` since the last cycle — the case that
  // matters is one just marked 'revoked' in AgentOS (excluded from the
  // payload entirely, see emitter.build_provider_credentials_payload).
  // Unconditional, even when providerCredentials is empty — that case
  // (e.g. every provider got revoked) must still clear everything.
  clearInjectedApiKeysExcept(Object.keys(providerCredentials));

  return { credentials, modelReachability };
}
