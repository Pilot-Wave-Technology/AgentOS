import { lookupContextTokens } from "../../agents/context.js";
import { resolveCronStyleNow } from "../../agents/current-time.js";
import { DEFAULT_CONTEXT_TOKENS } from "../../agents/defaults.js";
import { DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR } from "../../agents/pi-settings.js";
import { parseNonNegativeByteSize } from "../../config/byte-size.js";
import type { OpenClawConfig } from "../../config/config.js";
import { resolveFreshSessionTotalTokens, type SessionEntry } from "../../config/sessions.js";
import { SILENT_REPLY_TOKEN } from "../tokens.js";

export const DEFAULT_MEMORY_FLUSH_SOFT_TOKENS = 4000;
export const DEFAULT_MEMORY_FLUSH_FORCE_TRANSCRIPT_BYTES = 2 * 1024 * 1024;
/**
 * Default percentage-of-contextWindow at which to fire pre-compaction flush.
 * `undefined` here means: "fall back to absolute softThresholdTokens." The
 * control panel pushes a non-undefined value (typically 0.2) per agent at
 * provisioning so SHCAs get 20% headroom regardless of model.
 */
export const DEFAULT_MEMORY_FLUSH_SOFT_PCT: number | undefined = undefined;
/** Caps for the agent-generated <COMPACTION_SUMMARY> block. */
export const DEFAULT_COMPACTION_SUMMARY_MAX_TOKENS = 5000;
export const DEFAULT_COMPACTION_SUMMARY_MAX_CHARS = 20000;
/** Default cap before MEMORY.md auto-consolidation fires. */
export const DEFAULT_MEMORY_FILE_MAX_CHARS = 30000;
/** Default consolidation target — 80% of the cap. */
export const DEFAULT_MEMORY_FILE_TARGET_CHARS = 24000;

const MEMORY_FLUSH_TARGET_HINT =
  "Append session narrative to memory/YYYY-MM-DD.md (create memory/ if needed). Also distill ≤500 chars of key insights into the appropriate section(s) of MEMORY.md — both files are append-only during this flush.";
const MEMORY_FLUSH_APPEND_ONLY_HINT =
  "If memory/YYYY-MM-DD.md or MEMORY.md already have content, APPEND only — never overwrite existing entries.";
const MEMORY_FLUSH_READ_ONLY_HINT =
  "Treat workspace bootstrap/reference files such as SOUL.md, TOOLS.md, AGENTS.md, IDENTITY.md, RESOURCES.md, USER.md, and BOOTSTRAP.md as read-only during this flush; never overwrite, replace, or edit them. (MEMORY.md is writable — append-only — for distilled insights.)";
const MEMORY_FLUSH_REQUIRED_HINTS = [
  MEMORY_FLUSH_TARGET_HINT,
  MEMORY_FLUSH_APPEND_ONLY_HINT,
  MEMORY_FLUSH_READ_ONLY_HINT,
];

export const DEFAULT_MEMORY_FLUSH_PROMPT = [
  "Pre-compaction memory flush.",
  MEMORY_FLUSH_TARGET_HINT,
  MEMORY_FLUSH_READ_ONLY_HINT,
  MEMORY_FLUSH_APPEND_ONLY_HINT,
  "Do NOT create timestamped variant files (e.g., YYYY-MM-DD-HHMM.md); always use the canonical YYYY-MM-DD.md filename.",
  `If nothing to store, reply with ${SILENT_REPLY_TOKEN}.`,
].join(" ");

export const DEFAULT_MEMORY_FLUSH_SYSTEM_PROMPT = [
  "Pre-compaction memory flush turn.",
  "The session is near auto-compaction; capture durable memories to disk.",
  MEMORY_FLUSH_TARGET_HINT,
  MEMORY_FLUSH_READ_ONLY_HINT,
  MEMORY_FLUSH_APPEND_ONLY_HINT,
  `You may reply, but usually ${SILENT_REPLY_TOKEN} is correct.`,
].join(" ");

/**
 * Default consolidation prompt — fires when MEMORY.md grows past its cap.
 * Distinct from regular flush: focus is on DROPPING stale entries and
 * MERGING duplicates, not adding new ones.
 */
export const DEFAULT_MEMORY_CONSOLIDATION_PROMPT = [
  "MEMORY.md is over its size budget. This turn is invisible to the user.",
  "Audit the current MEMORY.md, drop stale or resolved entries, merge",
  "duplicates, compress verbose sections. Target ≤80% of the cap.",
  "Do not add new facts in this turn — focus is consolidation, not capture.",
  `Reply with ${SILENT_REPLY_TOKEN} after Write/Edit tool calls complete.`,
].join(" ");

export const DEFAULT_MEMORY_CONSOLIDATION_SYSTEM_PROMPT = [
  "MEMORY.md consolidation turn. Invisible to the user.",
  "Read the current MEMORY.md. Identify entries that are: stale (resolved threads),",
  "duplicated (same fact in two sections), or verbose (can be tightened).",
  "Write a smaller, surgically-edited MEMORY.md back.",
  "Do not introduce new content — strictly consolidation.",
  `End with ${SILENT_REPLY_TOKEN}.`,
].join(" ");

function formatDateStampInTimezone(nowMs: number, timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(nowMs));
  const year = parts.find((part) => part.type === "year")?.value;
  const month = parts.find((part) => part.type === "month")?.value;
  const day = parts.find((part) => part.type === "day")?.value;
  if (year && month && day) {
    return `${year}-${month}-${day}`;
  }
  return new Date(nowMs).toISOString().slice(0, 10);
}

export function resolveMemoryFlushRelativePathForRun(params: {
  cfg?: OpenClawConfig;
  nowMs?: number;
}): string {
  const nowMs = Number.isFinite(params.nowMs) ? (params.nowMs as number) : Date.now();
  const { userTimezone } = resolveCronStyleNow(params.cfg ?? {}, nowMs);
  const dateStamp = formatDateStampInTimezone(nowMs, userTimezone);
  return `memory/${dateStamp}.md`;
}

export function resolveMemoryFlushPromptForRun(params: {
  prompt: string;
  cfg?: OpenClawConfig;
  nowMs?: number;
}): string {
  const nowMs = Number.isFinite(params.nowMs) ? (params.nowMs as number) : Date.now();
  const { timeLine } = resolveCronStyleNow(params.cfg ?? {}, nowMs);
  const dateStamp = resolveMemoryFlushRelativePathForRun({
    cfg: params.cfg,
    nowMs,
  })
    .replace(/^memory\//, "")
    .replace(/\.md$/, "");
  const withDate = params.prompt.replaceAll("YYYY-MM-DD", dateStamp).trimEnd();
  if (!withDate) {
    return timeLine;
  }
  if (withDate.includes("Current time:")) {
    return withDate;
  }
  return `${withDate}\n${timeLine}`;
}

export type MemoryFlushSettings = {
  enabled: boolean;
  /** @deprecated 2026-05-26 — replaced by flushThresholdPct. */
  softThresholdTokens: number;
  /** @deprecated 2026-05-26 — replaced by flushThresholdPct. */
  softThresholdPct?: number;
  /**
   * 2026-05-26 simplified trigger: flush fires when totalTokens >=
   * contextWindow * flushThresholdPct. Default 0.3. Replaces the old
   * softThresholdTokens / softThresholdPct + reserveTokens math.
   */
  flushThresholdPct?: number;
  /**
   * Force a pre-compaction memory flush when the session transcript reaches this
   * size. Set to 0 to disable byte-size based triggering.
   */
  forceFlushTranscriptBytes: number;
  prompt: string;
  systemPrompt: string;
  reserveTokensFloor: number;
  /** Caps applied to the <COMPACTION_SUMMARY> block extracted from the flush turn. */
  compactionSummary: {
    maxTokens: number;
    maxChars: number;
  };
  /**
   * MEMORY.md auto-consolidation: when MEMORY.md exceeds `memoryFileMaxChars`,
   * the gateway fires a silent turn using consolidation prompts (not the
   * regular flush prompts) targeting `memoryFileTargetChars`.
   */
  memoryFileMaxChars: number;
  memoryFileTargetChars: number;
  consolidationPrompt: string;
  consolidationSystemPrompt: string;
};

const normalizeNonNegativeInt = (value: unknown): number | null => {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  const int = Math.floor(value);
  return int >= 0 ? int : null;
};

export function resolveMemoryFlushSettings(
  cfg?: OpenClawConfig,
  agentId?: string,
  modelId?: string,
): MemoryFlushSettings | null {
  const defaults = cfg?.agents?.defaults?.compaction?.memoryFlush;
  const enabled = defaults?.enabled ?? true;
  if (!enabled) {
    return null;
  }

  // Per-agent override lookup. Falls back to defaults per-field so an
  // agent can override just `prompt` and inherit `softThresholdTokens`.
  let agentOverride:
    | {
        prompt?: string;
        systemPrompt?: string;
        softThresholdPct?: number;
        compactionSummary?: { maxTokens?: number; maxChars?: number };
        memoryFileMaxChars?: number;
        memoryFileTargetChars?: number;
        consolidationPrompt?: string;
        consolidationSystemPrompt?: string;
      }
    | undefined;
  if (cfg && agentId) {
    // Lazy import to avoid a circular dependency between agents/* and
    // auto-reply/*; agent-scope.ts depends only on the config types.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { resolveAgentConfig } = require("../../agents/agent-scope.js");
    const resolved = resolveAgentConfig(cfg, agentId);
    agentOverride = resolved?.compaction?.memoryFlush;
  }

  const softThresholdTokens =
    normalizeNonNegativeInt(defaults?.softThresholdTokens) ?? DEFAULT_MEMORY_FLUSH_SOFT_TOKENS;
  // Pct overrides absolute when set on either the per-agent override or the
  // global defaults. Per-agent wins, default falls through.
  const softThresholdPct = normalizePct(
    agentOverride?.softThresholdPct ?? defaults?.softThresholdPct,
  );
  const forceFlushTranscriptBytes =
    parseNonNegativeByteSize(defaults?.forceFlushTranscriptBytes) ??
    DEFAULT_MEMORY_FLUSH_FORCE_TRANSCRIPT_BYTES;

  const promptSource =
    agentOverride?.prompt?.trim() ||
    defaults?.prompt?.trim() ||
    DEFAULT_MEMORY_FLUSH_PROMPT;
  const systemPromptSource =
    agentOverride?.systemPrompt?.trim() ||
    defaults?.systemPrompt?.trim() ||
    DEFAULT_MEMORY_FLUSH_SYSTEM_PROMPT;

  const prompt = ensureMemoryFlushSafetyHints(promptSource);
  const systemPrompt = ensureMemoryFlushSafetyHints(systemPromptSource);
  // Use the central reserve resolver so reserveTokensPct (percentage) is
  // honored. Falls through to reserveTokensFloor (absolute) and finally
  // the package default if pct isn't set or no modelId is known.
  const reserveTokensFloor = (() => {
    try {
      // Dynamic import-style require to avoid a circular type-only import
      // between memory-flush.ts and pi-settings.ts.
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const { resolveCompactionReserveTokensFloor } = require("../../agents/pi-settings.js");
      return resolveCompactionReserveTokensFloor(cfg, modelId);
    } catch {
      return (
        normalizeNonNegativeInt(cfg?.agents?.defaults?.compaction?.reserveTokensFloor) ??
        DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR
      );
    }
  })();

  const compactionSummary = {
    maxTokens:
      normalizeNonNegativeInt(
        agentOverride?.compactionSummary?.maxTokens ??
          defaults?.compactionSummary?.maxTokens,
      ) ?? DEFAULT_COMPACTION_SUMMARY_MAX_TOKENS,
    maxChars:
      normalizeNonNegativeInt(
        agentOverride?.compactionSummary?.maxChars ??
          defaults?.compactionSummary?.maxChars,
      ) ?? DEFAULT_COMPACTION_SUMMARY_MAX_CHARS,
  };

  const memoryFileMaxChars =
    normalizeNonNegativeInt(
      agentOverride?.memoryFileMaxChars ?? defaults?.memoryFileMaxChars,
    ) ?? DEFAULT_MEMORY_FILE_MAX_CHARS;
  const memoryFileTargetChars =
    normalizeNonNegativeInt(
      agentOverride?.memoryFileTargetChars ?? defaults?.memoryFileTargetChars,
    ) ?? Math.floor(memoryFileMaxChars * 0.8);

  const consolidationPromptSource =
    agentOverride?.consolidationPrompt?.trim() ||
    defaults?.consolidationPrompt?.trim() ||
    DEFAULT_MEMORY_CONSOLIDATION_PROMPT;
  const consolidationSystemPromptSource =
    agentOverride?.consolidationSystemPrompt?.trim() ||
    defaults?.consolidationSystemPrompt?.trim() ||
    DEFAULT_MEMORY_CONSOLIDATION_SYSTEM_PROMPT;

  const flushThresholdPct = normalizePct(
    (agentOverride as { flushThresholdPct?: number } | undefined)?.flushThresholdPct ??
      (defaults as { flushThresholdPct?: number } | undefined)?.flushThresholdPct,
  );

  return {
    enabled,
    softThresholdTokens,
    softThresholdPct,
    flushThresholdPct,
    forceFlushTranscriptBytes,
    prompt: ensureNoReplyHint(prompt),
    systemPrompt: ensureNoReplyHint(systemPrompt),
    reserveTokensFloor,
    compactionSummary,
    memoryFileMaxChars,
    memoryFileTargetChars,
    consolidationPrompt: ensureNoReplyHint(consolidationPromptSource),
    consolidationSystemPrompt: ensureNoReplyHint(consolidationSystemPromptSource),
  };
}

function normalizePct(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
  if (value <= 0 || value > 1) return undefined;
  return value;
}

function ensureNoReplyHint(text: string): string {
  if (text.includes(SILENT_REPLY_TOKEN)) {
    return text;
  }
  return `${text}\n\nIf no user-visible reply is needed, start with ${SILENT_REPLY_TOKEN}.`;
}

function ensureMemoryFlushSafetyHints(text: string): string {
  let next = text.trim();
  for (const hint of MEMORY_FLUSH_REQUIRED_HINTS) {
    if (!next.includes(hint)) {
      next = next ? `${next}\n\n${hint}` : hint;
    }
  }
  return next;
}

export function resolveMemoryFlushContextWindowTokens(params: {
  modelId?: string;
  agentCfgContextTokens?: number;
  cfg?: OpenClawConfig;
}): number {
  const modelTokens = lookupContextTokens(params.modelId);
  const base =
    modelTokens ?? params.agentCfgContextTokens ?? DEFAULT_CONTEXT_TOKENS;
  // Honor the global `agents.defaults.contextTokens` cap when set and
  // smaller than the resolved base. Without this, the flush threshold
  // computes against the model's native window (e.g. 1M for Gemini
  // Flash) and never fires on agents that should see a smaller window
  // via the cap. Matches the precedence in resolveContextWindowInfo's
  // "Priority 3" cap check in context-window-guard.ts.
  const cap = params.cfg?.agents?.defaults?.contextTokens;
  if (
    typeof cap === "number" &&
    Number.isFinite(cap) &&
    cap > 0 &&
    cap < base
  ) {
    return Math.floor(cap);
  }
  return base;
}

export function shouldRunMemoryFlush(params: {
  entry?: Pick<
    SessionEntry,
    | "totalTokens"
    | "totalTokensFresh"
    | "compactionCount"
    | "memoryFlushCount"
    | "memoryFlushLastFiredAtTokens"
  >;
  /**
   * Optional token count override for flush gating. When provided, this value is
   * treated as a fresh context snapshot and used instead of the cached
   * SessionEntry.totalTokens (which may be stale/unknown).
   */
  tokenCount?: number;
  contextWindowTokens: number;
  /**
   * 2026-05-26 simplified design: the only threshold knob. Flush fires
   * when totalTokens >= contextWindow * flushThresholdPct. Default 0.7.
   * The older softThresholdTokens / reserveTokens math has been removed
   * — passing those params now has no effect (signature kept for
   * compatibility with existing callers; will be cleaned up in a
   * follow-up sweep).
   */
  flushThresholdPct?: number;
  /** @deprecated 2026-05-26 — no longer used. */
  reserveTokensFloor?: number;
  /** @deprecated 2026-05-26 — no longer used. */
  softThresholdTokens?: number;
  /** @deprecated 2026-05-26 — no longer used. */
  softThresholdPct?: number;
}): boolean {
  if (!params.entry) {
    return false;
  }

  const override = params.tokenCount;
  const overrideTokens =
    typeof override === "number" && Number.isFinite(override) && override > 0
      ? Math.floor(override)
      : undefined;

  const totalTokens = overrideTokens ?? resolveFreshSessionTotalTokens(params.entry);
  if (!totalTokens || totalTokens <= 0) {
    return false;
  }
  const contextWindow = Math.max(1, Math.floor(params.contextWindowTokens));
  const pct =
    typeof params.flushThresholdPct === "number" &&
    params.flushThresholdPct > 0 &&
    params.flushThresholdPct <= 1
      ? params.flushThresholdPct
      : 0.7;
  const threshold = Math.max(1, Math.floor(contextWindow * pct));
  if (totalTokens < threshold) {
    return false;
  }

  if (hasAlreadyFlushedForCurrentCompaction(params.entry)) {
    return false;
  }

  return true;
}

/**
 * Returns true when a memory flush has already been performed for the
 * current compaction cycle. Simplified 2026-05-26 to use explicit
 * counters:
 *
 *   block iff memoryFlushCount > compactionCount
 *
 * Meaning: "we've already flushed since the last compaction; wait for
 * the next compaction to tick its counter before re-arming." This is
 * exactly the user's stated design: flush at most once per cycle, the
 * gate re-arms naturally when compactionCount catches up.
 *
 * Both counters tick independently:
 *   - memoryFlushCount: bumped in commands/agent/session-store.ts when
 *     an agent run completes with trigger="memory_flush"
 *   - compactionCount: bumped via incrementCompactionCount for sync
 *     safeguard / overflow recovery, and via the async manager's
 *     post-SPLICED updateSessionStoreEntry call for async splices
 *
 * Backward-compat note: existing sessions.json files may carry
 * memoryFlushLastFiredAtTokens (the previous gate primitive, removed
 * 2026-05-26). It's ignored now — the counter pair is the only signal.
 */
export function hasAlreadyFlushedForCurrentCompaction(
  entry: Pick<SessionEntry, "compactionCount" | "memoryFlushCount">,
): boolean {
  const flushCount = entry.memoryFlushCount ?? 0;
  const compactionCount = entry.compactionCount ?? 0;
  return flushCount > compactionCount;
}

/**
 * Reads MEMORY.md size from disk and returns true if it exceeds the
 * configured cap. Best-effort — returns false on any FS error so
 * consolidation never blocks normal turn flow.
 */
export function shouldRunMemoryConsolidation(params: {
  workspaceDir?: string;
  memoryFileMaxChars: number;
}): { trigger: boolean; sizeChars: number } {
  if (!params.workspaceDir || params.memoryFileMaxChars <= 0) {
    return { trigger: false, sizeChars: 0 };
  }
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const path = require("node:path") as typeof import("node:path");
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const fs = require("node:fs") as typeof import("node:fs");
    const memoryPath = path.join(params.workspaceDir, "MEMORY.md");
    if (!fs.existsSync(memoryPath)) return { trigger: false, sizeChars: 0 };
    const content = fs.readFileSync(memoryPath, "utf-8");
    return {
      trigger: content.length > params.memoryFileMaxChars,
      sizeChars: content.length,
    };
  } catch {
    return { trigger: false, sizeChars: 0 };
  }
}
