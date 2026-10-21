/**
 * Resolve provider credential health against GCP Secret Manager, gateway-side.
 *
 * AgentOS never resolves or stores a secret's actual value — only the
 * reference (project/secret id/version). This module is the only place in
 * the whole system that reads the actual key material, and it never sends
 * that value anywhere; it only ever reports whether resolution succeeded
 * (`healthy`/`error`), for AgentOS to display as a credential-status badge.
 *
 * The resolved value also feeds real auth now, via `env-injector.ts` —
 * NOT via `cfg.models.providers.<id>.apiKey` / `config.patch` (that would
 * require replicating `config.patch`'s merge/write/restart logic in-process
 * and touching disk-persisted gateway config — deliberately avoided).
 * Instead, for any provider id `model-auth.ts` already recognizes in
 * `PROVIDER_ENV_API_KEY_CANDIDATES`, we set that env var directly on
 * `process.env` in this running process. `resolveEnvApiKey` in
 * `model-auth.ts` reads `process.env` fresh on every call (no caching), so
 * this takes effect immediately with no restart and no cfg mutation. See
 * research/model_registry_secret_management_design.md in the AgentOS repo.
 *
 * Uses `@google-cloud/secret-manager`'s default Application Default
 * Credentials — this process's own GCP identity, scoped (by IAM, outside
 * this code) to `roles/secretmanager.secretAccessor` only. No dependency on
 * anything AgentOS holds.
 */
import { SecretManagerServiceClient } from "@google-cloud/secret-manager";
import type { ProviderCredentialRef } from "./types.js";

export interface CredentialHealth {
  healthy: boolean;
  error?: string;
}

let client: SecretManagerServiceClient | null = null;

function getClient(): SecretManagerServiceClient {
  if (!client) {
    client = new SecretManagerServiceClient();
  }
  return client;
}

/** Resolve the GCP project to read secrets from when the admin didn't type
 * one — deliberately the common case now: the reference only ever records
 * *this VM's own project* (via ADC's default project resolution, same
 * identity the client already authenticates with), never an operator-typed
 * value, so there's nothing to keep in sync if the VM moves projects. */
async function resolveDefaultProject(): Promise<string> {
  const projectId = await getClient().auth.getProjectId();
  if (!projectId) {
    throw new Error(
      "could not resolve this VM's own GCP project via ADC — set GOOGLE_CLOUD_PROJECT " +
        "or ensure the service account has project metadata access",
    );
  }
  return projectId;
}

/** Resolve one provider's secret, returning the plaintext value on success.
 * Never logs the value itself. Throws on any failure (not found, permission
 * denied, disabled version) — callers convert to a `CredentialHealth`. */
export async function resolveProviderSecret(ref: ProviderCredentialRef): Promise<string> {
  const project = ref.secretProject || (await resolveDefaultProject());
  const name = `projects/${project}/secrets/${ref.secretId}/versions/${ref.secretVersion || "latest"}`;
  const [version] = await getClient().accessSecretVersion({ name });
  const payload = version.payload?.data;
  if (!payload) {
    throw new Error("Secret Manager returned no payload data");
  }
  const value = typeof payload === "string" ? payload : Buffer.from(payload).toString("utf8");
  if (!value.trim()) {
    throw new Error("resolved secret value is empty");
  }
  return value;
}

/** Resolve every provider's credential health. Independent per provider —
 * one bad reference doesn't affect another's result. Returns a plain
 * status/error map; the resolved values themselves are discarded
 * immediately after use (by the reachability probe, if applicable) and
 * never retained in this map or logged. */
export async function resolveAllProviderCredentials(
  providerCredentials: Record<string, ProviderCredentialRef>,
): Promise<Record<string, CredentialHealth>> {
  const out: Record<string, CredentialHealth> = {};
  await Promise.all(
    Object.entries(providerCredentials).map(async ([providerId, ref]) => {
      try {
        await resolveProviderSecret(ref);
        out[providerId] = { healthy: true };
      } catch (err) {
        out[providerId] = { healthy: false, error: String((err as Error)?.message ?? err) };
      }
    }),
  );
  return out;
}
