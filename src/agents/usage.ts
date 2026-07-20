export type UsageLike = {
  input?: number;
  output?: number;
  cacheRead?: number;
  cacheWrite?: number;
  total?: number;
  // Common alternates across providers/SDKs.
  inputTokens?: number;
  outputTokens?: number;
  promptTokens?: number;
  completionTokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  // Moonshot/Kimi uses cached_tokens for cache read count (explicit caching API).
  cached_tokens?: number;
  // Kimi K2 uses prompt_tokens_details.cached_tokens for automatic prefix caching.
  prompt_tokens_details?: { cached_tokens?: number };
  // Some agents/logs emit alternate naming.
  totalTokens?: number;
  total_tokens?: number;
  cache_read?: number;
  cache_write?: number;
  // Anthropic extended-thinking: thinking tokens are a subset of output_tokens,
  // returned under usage.output_tokens_details.thinking_tokens.
  // Other providers may use a top-level thinking_tokens field.
  output_tokens_details?: { thinking_tokens?: number };
  thinking_tokens?: number;
};

export type NormalizedUsage = {
  input?: number;
  output?: number;
  cacheRead?: number;
  cacheWrite?: number;
  total?: number;
  /** Thinking/reasoning tokens — a subset of output, tracked separately for
   *  per-type cost calculation. Always ≤ output. Non-thinking output =
   *  output - thinking. Undefined when the model does not use extended thinking. */
  thinking?: number;
};

export type AssistantUsageSnapshot = {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  totalTokens: number;
  cost: {
    input: number;
    output: number;
    cacheRead: number;
    cacheWrite: number;
    total: number;
  };
};

export function makeZeroUsageSnapshot(): AssistantUsageSnapshot {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: {
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      total: 0,
    },
  };
}

const asFiniteNumber = (value: unknown): number | undefined => {
  if (typeof value !== "number") {
    return undefined;
  }
  if (!Number.isFinite(value)) {
    return undefined;
  }
  return value;
};

export function hasNonzeroUsage(usage?: NormalizedUsage | null): usage is NormalizedUsage {
  if (!usage) {
    return false;
  }
  return [usage.input, usage.output, usage.cacheRead, usage.cacheWrite, usage.total].some(
    (v) => typeof v === "number" && Number.isFinite(v) && v > 0,
  );
}

export function normalizeUsage(raw?: UsageLike | null): NormalizedUsage | undefined {
  if (!raw) {
    return undefined;
  }

  // Some providers (pi-ai OpenAI-format) pre-subtract cached_tokens from
  // prompt_tokens upstream.  When cached_tokens > prompt_tokens the result is
  // negative, which is nonsensical.  Clamp to 0.
  const rawInput = asFiniteNumber(
    raw.input ?? raw.inputTokens ?? raw.input_tokens ?? raw.promptTokens ?? raw.prompt_tokens,
  );
  const input = rawInput !== undefined && rawInput < 0 ? 0 : rawInput;
  const output = asFiniteNumber(
    raw.output ??
      raw.outputTokens ??
      raw.output_tokens ??
      raw.completionTokens ??
      raw.completion_tokens,
  );
  const cacheRead = asFiniteNumber(
    raw.cacheRead ??
      raw.cache_read ??
      raw.cache_read_input_tokens ??
      raw.cached_tokens ??
      raw.prompt_tokens_details?.cached_tokens,
  );
  const cacheWrite = asFiniteNumber(
    raw.cacheWrite ?? raw.cache_write ?? raw.cache_creation_input_tokens,
  );
  const total = asFiniteNumber(raw.total ?? raw.totalTokens ?? raw.total_tokens);
  // Thinking tokens: Anthropic nests them under output_tokens_details.thinking_tokens;
  // future providers may surface a top-level thinking_tokens field.
  const thinking = asFiniteNumber(
    raw.output_tokens_details?.thinking_tokens ?? raw.thinking_tokens,
  );

  if (
    input === undefined &&
    output === undefined &&
    cacheRead === undefined &&
    cacheWrite === undefined &&
    total === undefined
  ) {
    return undefined;
  }

  return {
    input,
    output,
    cacheRead,
    cacheWrite,
    total,
    ...(thinking !== undefined ? { thinking } : {}),
  };
}

export function derivePromptTokens(usage?: {
  input?: number;
  cacheRead?: number;
  cacheWrite?: number;
}): number | undefined {
  if (!usage) {
    return undefined;
  }
  const input = usage.input ?? 0;
  const cacheRead = usage.cacheRead ?? 0;
  const cacheWrite = usage.cacheWrite ?? 0;
  const sum = input + cacheRead + cacheWrite;
  return sum > 0 ? sum : undefined;
}

/**
 * Context-window prompt size: the input portion of context, normalized
 * across providers that disagree on whether `input` includes cached
 * tokens.
 *
 *   max(input, cacheRead) + cacheWrite
 *
 * - Gemini/OpenAI report `input` INCLUDING the cached prefix (cacheRead
 *   is a SUBSET of input). max() picks input, so cacheRead is never
 *   double-counted — the value is stable across cache hits/misses (no
 *   oscillation).
 * - Anthropic reports `input` EXCLUDING cached content; the cached
 *   bootstrap lands in cache_read (reuse) or cache_creation (first
 *   write), DISJOINT from input. max() picks cacheRead on cached turns,
 *   and cacheWrite (only ever nonzero for Anthropic) adds the disjoint
 *   creation tokens. Verified 2026-05-27 on go-tes1a: input=6,
 *   cacheWrite=19499 — input alone dropped the entire 19.5K bootstrap.
 *
 * Distinct from derivePromptTokens (the billing-side sum input +
 * cacheRead + cacheWrite), which oscillates for Gemini and must NOT be
 * used for context-window math.
 */
export function deriveContextPromptTokens(usage?: {
  input?: number;
  cacheRead?: number;
  cacheWrite?: number;
}): number {
  const input = usage?.input ?? 0;
  const cacheRead = usage?.cacheRead ?? 0;
  const cacheWrite = usage?.cacheWrite ?? 0;
  const sum = Math.max(input, cacheRead) + cacheWrite;
  return Number.isFinite(sum) && sum > 0 ? sum : 0;
}

export function deriveSessionTotalTokens(params: {
  usage?: {
    input?: number;
    output?: number;
    total?: number;
    cacheRead?: number;
    cacheWrite?: number;
  };
  contextTokens?: number;
  promptTokens?: number;
}): number | undefined {
  const promptOverride = params.promptTokens;
  const hasPromptOverride =
    typeof promptOverride === "number" && Number.isFinite(promptOverride) && promptOverride > 0;

  const usage = params.usage;
  // The hasPromptOverride branch used to allow returning the override
  // even with no usage. Since the override is now ignored (see below),
  // bail when usage is missing — there's nothing else to compute from.
  if (!usage) {
    return undefined;
  }

  // SessionEntry.totalTokens is the single source of truth for the async
  // compaction trigger, the memory flush gate, sessions.status, and the
  // UI ring. Formula: deriveContextPromptTokens(usage) + output =
  // max(input, cacheRead) + cacheWrite + output. See that helper for why
  // max() is correct for both Gemini (cache ⊆ input, no oscillation) and
  // Anthropic (cache disjoint — bootstrap lives in cacheWrite/cacheRead,
  // which a plain input+output would drop).
  //
  // The promptTokens override param is ignored — callers upstream pass
  // pi's agentMeta.promptTokens (the billing sum input+cacheRead+
  // cacheWrite) which has the Gemini oscillation bug. Dropped silently.
  if (hasPromptOverride) {
    // Keep silent rather than logging — happens on every turn for the
    // pi-embedded-runner path and would flood logs. The override is
    // intentionally dropped.
  }
  const output = usage?.output ?? 0;
  const totalTokens = deriveContextPromptTokens(usage) + output;

  if (!Number.isFinite(totalTokens) || totalTokens <= 0) {
    return undefined;
  }

  // Keep this value unclamped; display layers are responsible for capping
  // percentages for terminal output.
  return totalTokens;
}
