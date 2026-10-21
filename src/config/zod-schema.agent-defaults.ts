import { z } from "zod";
import { isValidNonNegativeByteSizeString } from "./byte-size.js";
import {
  HeartbeatSchema,
  AgentSandboxSchema,
  AgentModelSchema,
  MemorySearchSchema,
} from "./zod-schema.agent-runtime.js";
import {
  BlockStreamingChunkSchema,
  BlockStreamingCoalesceSchema,
  CliBackendSchema,
  HumanDelaySchema,
  TypingModeSchema,
} from "./zod-schema.core.js";

export const AgentDefaultsSchema = z
  .object({
    model: AgentModelSchema.optional(),
    imageModel: AgentModelSchema.optional(),
    pdfModel: AgentModelSchema.optional(),
    pdfMaxBytesMb: z.number().positive().optional(),
    pdfMaxPages: z.number().int().positive().optional(),
    models: z
      .record(
        z.string(),
        z
          .object({
            alias: z.string().optional(),
            /** Provider-specific API parameters (e.g., GLM-4.7 thinking mode). */
            params: z.record(z.string(), z.unknown()).optional(),
            /** Enable streaming for this model (default: true, false for Ollama to avoid SDK issue #1205). */
            streaming: z.boolean().optional(),
          })
          .strict(),
      )
      .optional(),
    workspace: z.string().optional(),
    repoRoot: z.string().optional(),
    skipBootstrap: z.boolean().optional(),
    bootstrapMaxChars: z.number().int().positive().optional(),
    bootstrapTotalMaxChars: z.number().int().positive().optional(),
    bootstrapPromptTruncationWarning: z
      .union([z.literal("off"), z.literal("once"), z.literal("always")])
      .optional(),
    userTimezone: z.string().optional(),
    timeFormat: z.union([z.literal("auto"), z.literal("12"), z.literal("24")]).optional(),
    envelopeTimezone: z.string().optional(),
    envelopeTimestamp: z.union([z.literal("on"), z.literal("off")]).optional(),
    envelopeElapsed: z.union([z.literal("on"), z.literal("off")]).optional(),
    contextTokens: z.number().int().positive().optional(),
    cliBackends: z.record(z.string(), CliBackendSchema).optional(),
    memorySearch: MemorySearchSchema,
    contextPruning: z
      .object({
        mode: z.union([z.literal("off"), z.literal("cache-ttl")]).optional(),
        ttl: z.string().optional(),
        keepLastAssistants: z.number().int().nonnegative().optional(),
        softTrimRatio: z.number().min(0).max(1).optional(),
        hardClearRatio: z.number().min(0).max(1).optional(),
        minPrunableToolChars: z.number().int().nonnegative().optional(),
        tools: z
          .object({
            allow: z.array(z.string()).optional(),
            deny: z.array(z.string()).optional(),
          })
          .strict()
          .optional(),
        softTrim: z
          .object({
            maxChars: z.number().int().nonnegative().optional(),
            headChars: z.number().int().nonnegative().optional(),
            tailChars: z.number().int().nonnegative().optional(),
          })
          .strict()
          .optional(),
        hardClear: z
          .object({
            enabled: z.boolean().optional(),
            placeholder: z.string().optional(),
          })
          .strict()
          .optional(),
      })
      .strict()
      .optional(),
    compaction: z
      .object({
        mode: z.union([z.literal("default"), z.literal("safeguard")]).optional(),
        reserveTokens: z.number().int().nonnegative().optional(),
        keepRecentTokens: z.number().int().positive().optional(),
        keepRecentTokensPct: z.number().min(0).max(1).optional(),
        // Cap on a single PRESERVED toolResult, in estimated tokens.
        // Lives in defaults (not per-agent) on purpose: per-agent
        // compaction config is pushed once at agent creation, so a
        // per-agent key would never reach the agents already running.
        preservedToolResultMaxTokens: z.number().int().nonnegative().optional(),
        reserveTokensFloor: z.number().int().nonnegative().optional(),
        // Percentage-of-contextWindow reserve. When set, supersedes
        // reserveTokens/reserveTokensFloor — every model size gets the
        // same relative cushion (e.g. 0.1 = 10% of window reserved).
        reserveTokensPct: z.number().min(0).max(1).optional(),
        maxHistoryShare: z.number().min(0.1).max(0.9).optional(),
        identifierPolicy: z
          .union([z.literal("strict"), z.literal("off"), z.literal("custom")])
          .optional(),
        identifierInstructions: z.string().optional(),
        recentTurnsPreserve: z.number().int().min(0).max(12).optional(),
        keepRecentMode: z.union([z.literal("turns"), z.literal("tokens")]).optional(),
        // ── Async compaction ────────────────────────────────────────────
        asyncEnabled: z.boolean().optional(),
        asyncSoftTriggerPct: z.number().min(0).max(1).optional(),
        asyncDebounceMs: z.number().int().nonnegative().optional(),
        asyncMaxConcurrent: z.number().int().positive().optional(),
        asyncMaxFailedAttempts: z.number().int().positive().optional(),
        qualityGuard: z
          .object({
            enabled: z.boolean().optional(),
            maxRetries: z.number().int().nonnegative().optional(),
          })
          .strict()
          .optional(),
        postCompactionSections: z.array(z.string()).optional(),
        model: z.string().optional(),
        aggregateTimeoutMs: z.number().int().positive().optional(),
        memoryFlush: z
          .object({
            enabled: z.boolean().optional(),
            softThresholdTokens: z.number().int().nonnegative().optional(),
            // Percentage-of-contextWindow trigger (0..1). When set, overrides
            // softThresholdTokens. Lets the same setting scale across model
            // window sizes. @deprecated 2026-05-26 — superseded by
            // flushThresholdPct below. Kept for back-compat with old configs.
            softThresholdPct: z.number().min(0).max(1).optional(),
            // 2026-05-26 simplified flush trigger: flush fires at
            // flushThresholdPct of contextWindow. Default 0.7. Replaces the
            // older softThresholdPct + reserveTokens math entirely.
            flushThresholdPct: z.number().min(0).max(1).optional(),
            forceFlushTranscriptBytes: z
              .union([
                z.number().int().nonnegative(),
                z
                  .string()
                  .refine(isValidNonNegativeByteSizeString, "Expected byte size string like 2mb"),
              ])
              .optional(),
            prompt: z.string().optional(),
            systemPrompt: z.string().optional(),
            // Caps for the <COMPACTION_SUMMARY> block extracted from the
            // flush turn and prepended to post-compaction context.
            compactionSummary: z
              .object({
                maxTokens: z.number().int().nonnegative().optional(),
                maxChars: z.number().int().nonnegative().optional(),
              })
              .strict()
              .optional(),
            // MEMORY.md auto-consolidation. When MEMORY.md exceeds
            // memoryFileMaxChars, the next eligible flush turn runs the
            // consolidation prompt instead of the regular flush prompt and
            // compresses MEMORY.md down to memoryFileTargetChars.
            memoryFileMaxChars: z.number().int().nonnegative().optional(),
            memoryFileTargetChars: z.number().int().nonnegative().optional(),
            consolidationPrompt: z.string().optional(),
            consolidationSystemPrompt: z.string().optional(),
          })
          .strict()
          .optional(),
      })
      .strict()
      .optional(),
    embeddedPi: z
      .object({
        projectSettingsPolicy: z
          .union([z.literal("trusted"), z.literal("sanitize"), z.literal("ignore")])
          .optional(),
      })
      .strict()
      .optional(),
    thinkingDefault: z
      .union([
        z.literal("off"),
        z.literal("minimal"),
        z.literal("low"),
        z.literal("medium"),
        z.literal("high"),
        z.literal("xhigh"),
        z.literal("adaptive"),
      ])
      .optional(),
    verboseDefault: z.union([z.literal("off"), z.literal("on"), z.literal("full")]).optional(),
    elevatedDefault: z
      .union([z.literal("off"), z.literal("on"), z.literal("ask"), z.literal("full")])
      .optional(),
    blockStreamingDefault: z.union([z.literal("off"), z.literal("on")]).optional(),
    blockStreamingBreak: z.union([z.literal("text_end"), z.literal("message_end")]).optional(),
    blockStreamingChunk: BlockStreamingChunkSchema.optional(),
    blockStreamingCoalesce: BlockStreamingCoalesceSchema.optional(),
    humanDelay: HumanDelaySchema.optional(),
    timeoutSeconds: z.number().int().positive().optional(),
    mediaMaxMb: z.number().positive().optional(),
    imageMaxDimensionPx: z.number().int().positive().optional(),
    typingIntervalSeconds: z.number().int().positive().optional(),
    typingMode: TypingModeSchema.optional(),
    heartbeat: HeartbeatSchema,
    maxConcurrent: z.number().int().positive().optional(),
    subagents: z
      .object({
        maxConcurrent: z.number().int().positive().optional(),
        maxSpawnDepth: z
          .number()
          .int()
          .min(1)
          .max(5)
          .optional()
          .describe(
            "Maximum nesting depth for sub-agent spawning. 1 = no nesting (default), 2 = sub-agents can spawn sub-sub-agents.",
          ),
        maxChildrenPerAgent: z
          .number()
          .int()
          .min(1)
          .max(20)
          .optional()
          .describe(
            "Maximum number of active children a single agent session can spawn (default: 5).",
          ),
        archiveAfterMinutes: z.number().int().positive().optional(),
        model: AgentModelSchema.optional(),
        thinking: z.string().optional(),
        runTimeoutSeconds: z.number().int().min(0).optional(),
        announceTimeoutMs: z.number().int().positive().optional(),
      })
      .strict()
      .optional(),
    sandbox: AgentSandboxSchema,
  })
  .strict()
  .optional();
