/**
 * MODEL REGISTRY — synthetic context-window variant auth resolution.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * THIS FILE IS OWNED BY THE MODEL REGISTRY FEATURE, NOT BY UPSTREAM OPENCLAW.
 *
 * It exists so the one behaviour the Model Registry needs from the gateway's
 * auth layer lives in a file we control, with a single delegating call in
 * `model-auth.ts`. Rebasing onto a new upstream release should only ever
 * conflict on that one call site, never on the logic below.
 * ─────────────────────────────────────────────────────────────────────────
 *
 * WHAT PROBLEM THIS SOLVES
 *
 * The AgentOS control plane exposes each real model at several context-window
 * sizes. The gateway has no per-agent context window — only one global
 * `agents.defaults.contextTokens` — so the registry works around that by
 * synthesizing a *fake provider* per (model, size) pair and giving each one a
 * `contextWindow`. Those ids look like:
 *
 *     anthropic-sonnet-200k      ← upstream `anthropic`, model shorthand `sonnet`
 *     anthropic-opus48-500k      ← upstream `anthropic`
 *     google-flash-1m            ← upstream `google`
 *
 * They are built by the control plane's emitter as
 * `<upstream>-<model shorthand>-<size>` (see `synthesizer.py` in the
 * `agentos` repo). They are not real providers and never will be in
 * upstream's `PROVIDER_ENV_API_KEY_CANDIDATES`, which is keyed by genuine
 * provider ids (`anthropic`, `google`, `openai`, …).
 *
 * So `resolveEnvApiKey("anthropic-sonnet-200k")` misses the table and returns
 * null, even though the credential for its real upstream (`anthropic`) is
 * sitting right there in the environment. Every variant then has to carry a
 * redundant `apiKey` label in `openclaw.json` naming the env var to read —
 * which in turn forces the control plane to store that env var name per
 * provider, purely to satisfy this lookup. This function removes the need for
 * all of that by resolving the variant back to its upstream.
 *
 * WHY IT IS SAFE
 *
 * 1. It only runs AFTER upstream's own lookup has already failed. It can turn
 *    a `null` into a key; it can never change or override a result upstream
 *    would have produced. Removing this file restores stock behaviour exactly.
 *
 * 2. The pattern below requires a trailing size segment (`-200k`, `-1m`), so it
 *    cannot fire for an ordinary provider id. A real provider called
 *    `openai-compatible` does NOT match and will never be silently handed
 *    `openai`'s credential — which is the failure mode a naive
 *    "strip the last segment" fallback would have.
 *
 * WHEN TO DELETE THIS
 *
 * The synthetic providers exist only because the gateway lacks a per-agent
 * context window. If that ever lands upstream, the registry stops synthesizing
 * providers, these ids disappear, and this file plus its one call site in
 * `model-auth.ts` can be removed with no replacement.
 */

/**
 * A synthesized context-window variant id: `<upstream>-<shorthand>-<size>`,
 * where size is a token count in the wire form the registry emits (`50k`,
 * `200k`, `1m`). The leading group is greedy so an upstream containing
 * hyphens (`foo-bar-baz-100k` → `foo-bar`) still resolves correctly.
 */
const VARIANT_PROVIDER_ID_RE = /^(.+)-[a-z0-9]+-(\d+[km])$/;

/**
 * The real upstream provider id behind a synthesized variant id, or null if
 * `providerId` is not a variant id at all (the overwhelmingly common case —
 * every genuine provider returns null here).
 *
 * Exported for tests and for anything that needs the same mapping; the auth
 * path itself should call `resolveVariantEnvApiKey` below rather than
 * re-implementing the candidate loop.
 */
export function resolveVariantUpstreamProviderId(providerId: string): string | null {
  const match = VARIANT_PROVIDER_ID_RE.exec(providerId);
  return match ? match[1] : null;
}

/**
 * Resolve a synthesized variant's API key by falling back to its upstream
 * provider's env-var candidates.
 *
 * Deliberately parameterised rather than importing the candidate table and
 * the value-reading logic directly: that keeps this file free of upstream
 * imports, so it cannot break on an upstream refactor of either, and keeps
 * the call site in `model-auth.ts` down to a few lines.
 *
 * @param providerId     Already-normalised provider id from the caller.
 * @param candidatesFor  Upstream's provider → env-var-names lookup.
 * @param pick           Upstream's "read this env var, return a result or
 *                       null" helper, so source strings stay consistent with
 *                       every other resolution path.
 * @returns The upstream's resolved credential, or null to leave the caller's
 *          existing behaviour untouched.
 */
export function resolveVariantEnvApiKey<T>(
  providerId: string,
  candidatesFor: (upstreamProviderId: string) => readonly string[] | undefined,
  pick: (envVar: string) => T | null,
): T | null {
  const upstream = resolveVariantUpstreamProviderId(providerId);
  if (!upstream) {
    return null;
  }
  for (const envVar of candidatesFor(upstream) ?? []) {
    const resolved = pick(envVar);
    if (resolved) {
      return resolved;
    }
  }
  return null;
}
