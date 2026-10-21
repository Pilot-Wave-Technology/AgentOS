/**
 * Wire shape returned by the AgentOS control panel's
 * `/internal/model_catalog` endpoint.
 *
 * The plugin expects a top-level `providers` object keyed by provider id;
 * each value is a `ProviderConfig` — the same shape openclaw already writes
 * into `<agent-dir>/models.json` under `providers[<id>]`. Additional
 * top-level metadata (e.g. `contractVersion`, `generatedAt`) is ignored;
 * only `providers` reaches the merge pipeline.
 */
import type { ProviderConfig } from "../../../src/agents/models-config.providers.js";

/** A GCP Secret Manager reference — never a resolved value. Read by
 * `secret-resolver.ts`; the actual secret payload never crosses into
 * AgentOS at any point (see
 * research/model_registry_secret_management_design.md in the AgentOS repo).
 */
export interface ProviderCredentialRef {
  secretProject: string | null;
  secretId: string;
  secretVersion: string;
}

export interface ModelCatalogResponse {
  providers: Record<string, ProviderConfig>;
  /** Flat base-model registry, keyed by id (e.g. "anthropic/claude-opus-5").
   * Only `id` is read by this plugin (to know which ids to probe
   * reachability for) — the other wire fields are AgentOS's concern. */
  models?: Record<string, { id: string }>;
  /** Keyed by real upstream provider id (e.g. "anthropic"), a different
   * granularity from `providers` (keyed by variant/alias id) — mirrors
   * AgentOS's `emitter.build_provider_credentials_payload`. Optional so
   * an older AgentOS deployment without this field still parses. */
  providerCredentials?: Record<string, ProviderCredentialRef>;
}

/**
 * Narrow structural guard for `/internal/model_catalog` responses.
 *
 * Deep field-level validation is intentionally deferred — the models-config
 * merge pipeline downstream normalises `apiKey` markers, enforces source-
 * managed secrets, and drops unknown provider fields. This guard's only
 * job is to protect the module-scope cache from a malformed body (e.g.
 * HTML error page, empty response, wrong root shape) that would otherwise
 * be assigned into `cache` and surface as odd behaviour on the next
 * `planOpenClawModelsJson` pass.
 */
export function assertCatalogShape(value: unknown): ModelCatalogResponse {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(
      "model_catalog response is not a JSON object (expected `{ providers: {...} }`)",
    );
  }
  const providers = (value as { providers?: unknown }).providers;
  if (!providers || typeof providers !== "object" || Array.isArray(providers)) {
    throw new Error(
      "model_catalog response is missing a top-level `providers` object map",
    );
  }
  return value as ModelCatalogResponse;
}
