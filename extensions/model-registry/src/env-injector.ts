/**
 * Bridges a resolved Secret Manager value into real gateway auth, without
 * touching `cfg.models.providers` / `config.patch` / disk-persisted config
 * at all.
 *
 * `model-auth.ts`'s `resolveApiKeyForProvider` calls `resolveEnvApiKey(provider)`
 * before it ever looks at cfg — that function reads `process.env` fresh on
 * every call, keyed by a fixed candidate list per provider id
 * (`PROVIDER_ENV_API_KEY_CANDIDATES`). Setting the right env var on this
 * process's own `process.env` is therefore sufficient for real auth to pick
 * up a rotated/new secret immediately — no restart, no gateway config write.
 *
 * Only applies to provider ids `model-auth.ts` already recognizes (the
 * table below is a hand-picked subset of `PROVIDER_ENV_API_KEY_CANDIDATES` —
 * the actual "API key", never an OAuth token entry, since injecting a value
 * under an OAuth-token env var would be semantically wrong). A provider id
 * outside this table has no known injection target; its credential health
 * still reports, but real auth for it isn't bridged by this mechanism.
 */
import { createSubsystemLogger } from "../../../src/logging/subsystem.js";

const log = createSubsystemLogger("model-registry:env-injector");

/** The literal API-key env var name per provider id — deliberately NOT
 * reusing `PROVIDER_ENV_API_KEY_CANDIDATES` wholesale, since several of its
 * entries list an OAuth-token env var first; injecting a resolved API key
 * under that name would misrepresent it as an OAuth token. */
const PROVIDER_API_KEY_ENV_VAR: Record<string, string> = {
  anthropic: "ANTHROPIC_API_KEY",
  google: "GEMINI_API_KEY",
  openai: "OPENAI_API_KEY",
  voyage: "VOYAGE_API_KEY",
  groq: "GROQ_API_KEY",
  deepgram: "DEEPGRAM_API_KEY",
  cerebras: "CEREBRAS_API_KEY",
  xai: "XAI_API_KEY",
  openrouter: "OPENROUTER_API_KEY",
  litellm: "LITELLM_API_KEY",
  moonshot: "MOONSHOT_API_KEY",
  minimax: "MINIMAX_API_KEY",
  nvidia: "NVIDIA_API_KEY",
  mistral: "MISTRAL_API_KEY",
  together: "TOGETHER_API_KEY",
};

/** Provider ids this process has actually injected an env var for, so a
 * later refresh can tell "no longer present" apart from "never was" —
 * only the former needs clearing. */
const injectedProviderIds = new Set<string>();

/** Set the resolved secret value into this process's own env, for the
 * one provider id it belongs to. No-op (returns false) for a provider id
 * with no known env var target — caller decides whether that's worth
 * logging. Never logs the value itself. */
export function injectResolvedApiKey(providerId: string, apiKey: string): boolean {
  const envVar = PROVIDER_API_KEY_ENV_VAR[providerId];
  if (!envVar) {
    return false;
  }
  process.env[envVar] = apiKey;
  injectedProviderIds.add(providerId);
  log.debug(`injected resolved secret for provider="${providerId}" into process.env.${envVar}`);
  return true;
}

/** Unset the injected env var for any provider id this process
 * previously injected but that is no longer in `currentProviderIds` —
 * the case that matters is a provider just marked 'revoked' in AgentOS,
 * which makes `emitter.build_provider_credentials_payload` exclude it
 * from the next catalog fetch entirely (see that function's docstring).
 * Without this, a value injected on an earlier refresh would keep
 * sitting in `process.env` forever — Node does not auto-expire it, and
 * nothing else in this plugin ever unsets an env var. Call this on
 * EVERY refresh cycle, including when `currentProviderIds` is empty
 * (e.g. every provider got revoked) — an empty keep-set must still
 * clear everything previously injected, not skip the check. */
export function clearInjectedApiKeysExcept(currentProviderIds: Iterable<string>): void {
  const keep = new Set(currentProviderIds);
  for (const providerId of [...injectedProviderIds]) {
    if (keep.has(providerId)) {
      continue;
    }
    const envVar = PROVIDER_API_KEY_ENV_VAR[providerId];
    if (envVar) {
      delete process.env[envVar];
      log.warn(
        `cleared previously-injected process.env.${envVar} for provider="${providerId}" ` +
          `— no longer present in providerCredentials (likely revoked)`,
      );
    }
    injectedProviderIds.delete(providerId);
  }
}
