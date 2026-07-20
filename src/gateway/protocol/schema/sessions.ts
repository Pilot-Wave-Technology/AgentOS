import { Type } from "@sinclair/typebox";
import { NonEmptyString, SessionLabelString } from "./primitives.js";

export const SessionsListParamsSchema = Type.Object(
  {
    limit: Type.Optional(Type.Integer({ minimum: 1 })),
    activeMinutes: Type.Optional(Type.Integer({ minimum: 1 })),
    includeGlobal: Type.Optional(Type.Boolean()),
    includeUnknown: Type.Optional(Type.Boolean()),
    /**
     * Read first 8KB of each session transcript to derive title from first user message.
     * Performs a file read per session - use `limit` to bound result set on large stores.
     */
    includeDerivedTitles: Type.Optional(Type.Boolean()),
    /**
     * Read last 16KB of each session transcript to extract most recent message preview.
     * Performs a file read per session - use `limit` to bound result set on large stores.
     */
    includeLastMessage: Type.Optional(Type.Boolean()),
    label: Type.Optional(SessionLabelString),
    spawnedBy: Type.Optional(NonEmptyString),
    agentId: Type.Optional(NonEmptyString),
    search: Type.Optional(Type.String()),
  },
  { additionalProperties: false },
);

export const SessionsPreviewParamsSchema = Type.Object(
  {
    keys: Type.Array(NonEmptyString, { minItems: 1 }),
    limit: Type.Optional(Type.Integer({ minimum: 1 })),
    maxChars: Type.Optional(Type.Integer({ minimum: 20 })),
  },
  { additionalProperties: false },
);

export const SessionsResolveParamsSchema = Type.Object(
  {
    key: Type.Optional(NonEmptyString),
    sessionId: Type.Optional(NonEmptyString),
    label: Type.Optional(SessionLabelString),
    agentId: Type.Optional(NonEmptyString),
    spawnedBy: Type.Optional(NonEmptyString),
    includeGlobal: Type.Optional(Type.Boolean()),
    includeUnknown: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);

export const SessionsPatchParamsSchema = Type.Object(
  {
    key: NonEmptyString,
    label: Type.Optional(Type.Union([SessionLabelString, Type.Null()])),
    thinkingLevel: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    verboseLevel: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    reasoningLevel: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    responseUsage: Type.Optional(
      Type.Union([
        Type.Literal("off"),
        Type.Literal("tokens"),
        Type.Literal("full"),
        // Backward compat with older clients/stores.
        Type.Literal("on"),
        Type.Null(),
      ]),
    ),
    elevatedLevel: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    execHost: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    execSecurity: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    execAsk: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    execNode: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    model: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    spawnedBy: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    spawnedWorkspaceDir: Type.Optional(Type.Union([NonEmptyString, Type.Null()])),
    spawnDepth: Type.Optional(Type.Union([Type.Integer({ minimum: 0 }), Type.Null()])),
    subagentRole: Type.Optional(
      Type.Union([Type.Literal("orchestrator"), Type.Literal("leaf"), Type.Null()]),
    ),
    subagentControlScope: Type.Optional(
      Type.Union([Type.Literal("children"), Type.Literal("none"), Type.Null()]),
    ),
    sendPolicy: Type.Optional(
      Type.Union([Type.Literal("allow"), Type.Literal("deny"), Type.Null()]),
    ),
    groupActivation: Type.Optional(
      Type.Union([Type.Literal("mention"), Type.Literal("always"), Type.Null()]),
    ),
  },
  { additionalProperties: false },
);

export const SessionsResetParamsSchema = Type.Object(
  {
    key: NonEmptyString,
    reason: Type.Optional(Type.Union([Type.Literal("new"), Type.Literal("reset")])),
  },
  { additionalProperties: false },
);

export const SessionsDeleteParamsSchema = Type.Object(
  {
    key: NonEmptyString,
    deleteTranscript: Type.Optional(Type.Boolean()),
    // Internal control: when false, still unbind thread bindings but skip hook emission.
    emitLifecycleHooks: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);

export const SessionsCompactParamsSchema = Type.Object(
  {
    key: NonEmptyString,
    /**
     * Legacy line-count budget. Still honored when `keptBudgetTokens` is
     * unset; otherwise the token-budget walk takes precedence.
     */
    maxLines: Type.Optional(Type.Integer({ minimum: 1 })),
    /**
     * Token-budgeted kept-transcript size. Newer setting that replaces
     * the crude maxLines counter — walks the JSONL transcript newest
     * first and keeps lines whose cumulative estimated tokens fit in the
     * budget.
     */
    keptBudgetTokens: Type.Optional(Type.Integer({ minimum: 1 })),
    /**
     * Per-line token cap. Any single transcript entry larger than this
     * has its content body truncated in-place during compaction so a
     * single 500k-token tool-result blob can't blow the budget by itself.
     */
    maxSingleLineTokens: Type.Optional(Type.Integer({ minimum: 1 })),
    /**
     * Force the compaction to run even when the transcript is already
     * within budget. Useful for manual-button testing on fresh sessions
     * — bumps compactionCount and resets token counters regardless of
     * size.
     */
    force: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);

/**
 * Lightweight per-session snapshot for context-usage UIs.
 *
 * Returns the running token counters the gateway already maintains on
 * `SessionEntry`, plus the model context window and the per-agent memory-
 * flush soft threshold, so a UI can render a context-usage ring without
 * loading the heavy `sessions.usage` aggregate.
 */
export const SessionsStatusParamsSchema = Type.Object(
  {
    key: NonEmptyString,
  },
  { additionalProperties: false },
);

/**
 * Invoke Pi's compaction directly for a session. Replaces the legacy
 * line-truncation `sessions.compact` path for manual button presses.
 *
 * The handler resolves sessionId / sessionFile / workspaceDir / provider
 * / model from the existing SessionEntry, sets trigger="manual" +
 * force=true, and calls `compactEmbeddedPiSessionDirect`. The safeguard
 * fires (when mode=safeguard) using the per-agent `summaryPrompt` we
 * push at provisioning.
 */
export const SessionsPiCompactParamsSchema = Type.Object(
  {
    key: NonEmptyString,
  },
  { additionalProperties: false },
);

export const SessionsUsageParamsSchema = Type.Object(
  {
    /** Specific session key to analyze; if omitted returns all sessions. */
    key: Type.Optional(NonEmptyString),
    /** Start date for range filter (YYYY-MM-DD). */
    startDate: Type.Optional(Type.String({ pattern: "^\\d{4}-\\d{2}-\\d{2}$" })),
    /** End date for range filter (YYYY-MM-DD). */
    endDate: Type.Optional(Type.String({ pattern: "^\\d{4}-\\d{2}-\\d{2}$" })),
    /** How start/end dates should be interpreted. Defaults to UTC when omitted. */
    mode: Type.Optional(
      Type.Union([Type.Literal("utc"), Type.Literal("gateway"), Type.Literal("specific")]),
    ),
    /** UTC offset to use when mode is `specific` (for example, UTC-4 or UTC+5:30). */
    utcOffset: Type.Optional(Type.String({ pattern: "^UTC[+-]\\d{1,2}(?::[0-5]\\d)?$" })),
    /** Maximum sessions to return (default 50). */
    limit: Type.Optional(Type.Integer({ minimum: 1 })),
    /** Include context weight breakdown (systemPromptReport). */
    includeContextWeight: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);
