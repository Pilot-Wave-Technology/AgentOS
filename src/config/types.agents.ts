import type { ChatType } from "../channels/chat-type.js";
import type { AgentDefaultsConfig } from "./types.agent-defaults.js";
import type { AgentModelConfig, AgentSandboxConfig } from "./types.agents-shared.js";
import type { HumanDelayConfig, IdentityConfig } from "./types.base.js";
import type { GroupChatConfig } from "./types.messages.js";
import type { AgentToolsConfig, MemorySearchConfig } from "./types.tools.js";

export type AgentRuntimeAcpConfig = {
  /** ACP harness adapter id (for example codex, claude). */
  agent?: string;
  /** Optional ACP backend override for this agent runtime. */
  backend?: string;
  /** Optional ACP session mode override. */
  mode?: "persistent" | "oneshot";
  /** Optional runtime working directory override. */
  cwd?: string;
};

export type AgentRuntimeConfig =
  | {
      type: "embedded";
    }
  | {
      type: "acp";
      acp?: AgentRuntimeAcpConfig;
    };

export type AgentBindingMatch = {
  channel: string;
  accountId?: string;
  peer?: { kind: ChatType; id: string };
  guildId?: string;
  teamId?: string;
  /** Discord role IDs used for role-based routing. */
  roles?: string[];
};

export type AgentRouteBinding = {
  /** Missing type is interpreted as route for backward compatibility. */
  type?: "route";
  agentId: string;
  comment?: string;
  match: AgentBindingMatch;
};

export type AgentAcpBinding = {
  type: "acp";
  agentId: string;
  comment?: string;
  match: AgentBindingMatch;
  acp?: {
    mode?: "persistent" | "oneshot";
    label?: string;
    cwd?: string;
    backend?: string;
  };
};

export type AgentBinding = AgentRouteBinding | AgentAcpBinding;

export type AgentConfig = {
  id: string;
  default?: boolean;
  name?: string;
  workspace?: string;
  agentDir?: string;
  model?: AgentModelConfig;
  /**
   * Optional per-agent context-window cap (positive integer).
   * When set, overrides agents.defaults.contextTokens for this
   * agent; the effective window is
   * min(contextTokens, model.contextWindow). When unset, falls
   * back to agents.defaults.contextTokens, then the model's
   * native window.
   */
  contextTokens?: number;
  /** Optional allowlist of skills for this agent (omit = all skills; empty = none). */
  skills?: string[];
  memorySearch?: MemorySearchConfig;
  /** Human-like delay between block replies for this agent. */
  humanDelay?: HumanDelayConfig;
  /** Optional per-agent heartbeat overrides. */
  heartbeat?: AgentDefaultsConfig["heartbeat"];
  identity?: IdentityConfig;
  groupChat?: GroupChatConfig;
  subagents?: {
    /** Allow spawning sub-agents under other agent ids. Use "*" to allow any. */
    allowAgents?: string[];
    /** Per-agent default model for spawned sub-agents (string or {primary,fallbacks}). */
    model?: AgentModelConfig;
  };
  /** Optional per-agent sandbox overrides. */
  sandbox?: AgentSandboxConfig;
  /** Optional per-agent stream params (e.g. cacheRetention, temperature). */
  params?: Record<string, unknown>;
  tools?: AgentToolsConfig;
  /** Optional runtime descriptor for this agent. */
  runtime?: AgentRuntimeConfig;
  /** Dispatch mode set by external control panel: "shca" | "ia" | undefined. */
  dispatchMode?: string;
  /**
   * Per-agent compaction overrides. Resolved by resolveMemoryFlushSettings
   * with fallback to `agents.defaults.compaction.memoryFlush` for any
   * field not specified here.
   *
   * - softThresholdPct: percentage-of-contextWindow trigger (0..1).
   *   When set, effective softThresholdTokens = contextWindow * softThresholdPct.
   *   Overrides absolute softThresholdTokens. Lets the same value work
   *   across models with different context windows (e.g. 1M Gemini vs 200k
   *   Sonnet).
   * - compactionSummary: caps for the agent-generated <COMPACTION_SUMMARY>
   *   block inserted at the head of the post-compaction transcript.
   */
  compaction?: {
    /**
     * Compaction summarization mode. "safeguard" enables the upstream
     * LLM-based summarization extension (uses `summaryPrompt` /
     * `summarizerModel` below). "default" leaves Pi's built-in
     * truncation in place. Defaults to "default" if unset.
     */
    mode?: "default" | "safeguard";
    /**
     * Custom instructions / system prompt passed to the safeguard's
     * summarizer model. Typically pulled from the agent's
     * compaction_summary_system_prompt admin-editable row.
     */
    summaryPrompt?: string;
    /**
     * Model used by the safeguard for compaction summarization.
     * Falls back to the agent's primary model when unset. We default
     * this to "google/gemini-2.5-flash" at provisioning for cost.
     */
    summarizerModel?: string;
    memoryFlush?: {
      prompt?: string;
      systemPrompt?: string;
      softThresholdPct?: number;
      compactionSummary?: {
        maxTokens?: number;
        maxChars?: number;
      };
      memoryFileMaxChars?: number;
      memoryFileTargetChars?: number;
      consolidationPrompt?: string;
      consolidationSystemPrompt?: string;
    };
  };
};

export type AgentsConfig = {
  defaults?: AgentDefaultsConfig;
  list?: AgentConfig[];
};
