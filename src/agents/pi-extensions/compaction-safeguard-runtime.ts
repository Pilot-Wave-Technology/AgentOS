import type { Api, Model } from "@mariozechner/pi-ai";
import type { AgentCompactionIdentifierPolicy } from "../../config/types.agent-defaults.js";
import { createSessionManagerRuntimeRegistry } from "./session-manager-runtime-registry.js";

export type CompactionSafeguardRuntimeValue = {
  maxHistoryShare?: number;
  contextWindowTokens?: number;
  identifierPolicy?: AgentCompactionIdentifierPolicy;
  identifierInstructions?: string;
  /**
   * Model to use for compaction summarization.
   * Passed through runtime because `ctx.model` is undefined in the compact.ts workflow
   * (extensionRunner.initialize() is never called in that path).
   */
  model?: Model<Api>;
  recentTurnsPreserve?: number;
  /**
   * If "tokens", the safeguard preserves recent turns up to
   * `keepRecentTokensBudget` (estimated tokens), walking backward from
   * the latest message. Tool-call/result pairing is preserved as in
   * the message-count mode.
   *
   * If "turns" (default), the safeguard preserves the last
   * `recentTurnsPreserve` user turns plus their assistant replies
   * (legacy behavior).
   */
  keepRecentMode?: "tokens" | "turns";
  /**
   * Token budget for keepRecentMode="tokens". Typically derived from
   * `agents.defaults.compaction.keepRecentTokensPct * contextWindow`.
   * Ignored when keepRecentMode is "turns".
   */
  keepRecentTokensBudget?: number;

  /** Cap on a single preserved `toolResult`, in estimated tokens.
   *  From `agents.defaults.compaction.preservedToolResultMaxTokens`. */
  preservedToolResultMaxTokens?: number;
  qualityGuardEnabled?: boolean;
  qualityGuardMaxRetries?: number;
  /**
   * Per-agent override for the safeguard's `customInstructions`. When set,
   * the safeguard prefers this over `event.customInstructions`. Sourced
   * from the agent's `compaction.summaryPrompt` (which the control panel
   * pushes from the `compaction_summary_system_prompt` admin row).
   */
  customInstructionsOverride?: string;
};

const registry = createSessionManagerRuntimeRegistry<CompactionSafeguardRuntimeValue>();

export const setCompactionSafeguardRuntime = registry.set;

export const getCompactionSafeguardRuntime = registry.get;
