/**
 * Shared in-process store for utility-model overrides, written by
 * `extensions/misc-models/`'s plugin and read by any core call site that
 * needs a purpose-scoped Gemini override instead of its own hardcoded
 * default (e.g. `agents/tools/web-search.ts`'s grounded-search model).
 *
 * Lives in `src/agents/` (core), not the extension itself, so core code
 * never has to import back into `extensions/` — the same direction
 * `extensions/model-registry/src/cache.ts` already imports FROM `src/`
 * (see its `ProviderConfig` import), never the reverse. The plugin calls
 * `setMiscModelOverrides()` after every successful fetch from
 * `agentos`'s `/internal/misc_model_settings` endpoint; this module
 * has no knowledge of HTTP, polling, or the control-panel contract.
 *
 * A purpose absent here means "the caller's own hardcoded default" — see
 * `getMiscModelOverride`'s fallback parameter — not an error.
 */
// ON A GLOBAL, NOT A MODULE VARIABLE. The extension that writes this and
// the core code that reads it can be loaded as two copies of this module
// (extensions go through their own loader), and two copies of a module
// variable are two stores: the writer fills one, the reader reads the
// other, and every override silently does nothing. `Symbol.for` is the
// same key in every copy -- the pattern `session-write-lock.ts` uses.
const STATE_KEY = Symbol.for("openclaw.miscModelOverrides");
type State = { overrides: Record<string, string> };

function state(): State {
  const g = globalThis as unknown as Record<symbol, State | undefined>;
  return (g[STATE_KEY] ??= { overrides: {} });
}

export function setMiscModelOverrides(next: Record<string, string>): void {
  state().overrides = { ...next };
}

export function getMiscModelOverride(purposeKey: string, fallback: string): string {
  return state().overrides[purposeKey] ?? fallback;
}

export function getMiscModelOverrides(): Record<string, string> {
  return { ...state().overrides };
}

export function clearMiscModelOverrides(): void {
  state().overrides = {};
}
