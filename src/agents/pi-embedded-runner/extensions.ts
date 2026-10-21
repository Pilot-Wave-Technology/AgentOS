import type { Api, Model } from "@mariozechner/pi-ai";
import type { ExtensionFactory, SessionManager } from "@mariozechner/pi-coding-agent";
import type { OpenClawConfig } from "../../config/config.js";
import { createSubsystemLogger } from "../../logging/subsystem.js";
import { getAgentContextTokens, resolveContextWindowInfo } from "../context-window-guard.js";

const log = createSubsystemLogger("async-compaction");
import { DEFAULT_CONTEXT_TOKENS } from "../defaults.js";
import { setCompactionSafeguardRuntime } from "../pi-extensions/compaction-safeguard-runtime.js";
import compactionSafeguardExtension from "../pi-extensions/compaction-safeguard.js";
import contextPruningExtension from "../pi-extensions/context-pruning.js";
import { setContextPruningRuntime } from "../pi-extensions/context-pruning/runtime.js";
import { computeEffectiveSettings } from "../pi-extensions/context-pruning/settings.js";
import { makeToolPrunablePredicate } from "../pi-extensions/context-pruning/tools.js";
import { ensurePiCompactionReserveTokens } from "../pi-settings.js";
import { isCacheTtlEligibleProvider, readLastCacheTtlTimestamp } from "./cache-ttl.js";

function resolveContextWindowTokens(params: {
  cfg: OpenClawConfig | undefined;
  provider: string;
  modelId: string;
  model: Model<Api> | undefined;
  agentId?: string;
}): number {
  return resolveContextWindowInfo({
    cfg: params.cfg,
    provider: params.provider,
    modelId: params.modelId,
    modelContextWindow: params.model?.contextWindow,
    defaultTokens: DEFAULT_CONTEXT_TOKENS,
    agentContextTokens: getAgentContextTokens(params.cfg, params.agentId),
  }).tokens;
}

function buildContextPruningFactory(params: {
  cfg: OpenClawConfig | undefined;
  sessionManager: SessionManager;
  provider: string;
  modelId: string;
  model: Model<Api> | undefined;
  agentId?: string;
}): ExtensionFactory | undefined {
  const raw = params.cfg?.agents?.defaults?.contextPruning;
  if (raw?.mode !== "cache-ttl") {
    return undefined;
  }
  if (!isCacheTtlEligibleProvider(params.provider, params.modelId)) {
    return undefined;
  }

  const settings = computeEffectiveSettings(raw);
  if (!settings) {
    return undefined;
  }

  setContextPruningRuntime(params.sessionManager, {
    settings,
    contextWindowTokens: resolveContextWindowTokens(params),
    isToolPrunable: makeToolPrunablePredicate(settings.tools),
    lastCacheTouchAt: readLastCacheTtlTimestamp(params.sessionManager),
  });

  return contextPruningExtension;
}

/**
 * Look up the per-agent compaction override from `agents.list[i].compaction`
 * matching agentId. Returns undefined when no entry matches.
 */
function resolvePerAgentCompaction(
  cfg: OpenClawConfig | undefined,
  agentId: string | undefined,
):
  | NonNullable<NonNullable<NonNullable<OpenClawConfig["agents"]>["list"]>[number]["compaction"]>
  | undefined {
  if (!cfg || !agentId) return undefined;
  const normalized = agentId.toLowerCase();
  const entry = cfg.agents?.list?.find((a) => a.id?.toLowerCase() === normalized);
  return entry?.compaction;
}

/**
 * Effective compaction mode for a specific agent. Per-agent value wins;
 * otherwise falls back to global `agents.defaults.compaction.mode`.
 */
function resolveCompactionMode(
  cfg: OpenClawConfig | undefined,
  agentId: string | undefined,
): "default" | "safeguard" {
  const perAgent = resolvePerAgentCompaction(cfg, agentId)?.mode;
  if (perAgent === "safeguard") return "safeguard";
  if (perAgent === "default") return "default";
  return cfg?.agents?.defaults?.compaction?.mode === "safeguard" ? "safeguard" : "default";
}

export function buildEmbeddedExtensionFactories(params: {
  cfg: OpenClawConfig | undefined;
  sessionManager: SessionManager;
  provider: string;
  modelId: string;
  model: Model<Api> | undefined;
  agentId?: string;
}): ExtensionFactory[] {
  const factories: ExtensionFactory[] = [];
  const asyncEnabled = params.cfg?.agents?.defaults?.compaction?.asyncEnabled === true;
  if (asyncEnabled) {
    // When async compaction is on, we OWN the trigger via the
    // AsyncCompactionManager (see attempt.ts onTurnComplete). Don't also
    // install the sync safeguard extension — they would race on the same
    // threshold and double-fire compactions. Manager logs prefix:
    // '[async-compaction]'. Sync safeguard logs prefix:
    // '[compaction-safeguard]'. You should NEVER see both for the same
    // session while asyncEnabled=true.
    log.info(
      `[async-compaction] sync safeguard DISABLED for ${params.agentId ?? "?"} — async manager owns compaction`,
    );
  } else if (resolveCompactionMode(params.cfg, params.agentId) === "safeguard") {
    const defaultsCfg = params.cfg?.agents?.defaults?.compaction;
    const perAgent = resolvePerAgentCompaction(params.cfg, params.agentId);
    const qualityGuardCfg = defaultsCfg?.qualityGuard;
    const contextWindowInfo = resolveContextWindowInfo({
      cfg: params.cfg,
      provider: params.provider,
      modelId: params.modelId,
      modelContextWindow: params.model?.contextWindow,
      defaultTokens: DEFAULT_CONTEXT_TOKENS,
      agentContextTokens: getAgentContextTokens(params.cfg, params.agentId),
    });
    setCompactionSafeguardRuntime(params.sessionManager, {
      maxHistoryShare: defaultsCfg?.maxHistoryShare,
      contextWindowTokens: contextWindowInfo.tokens,
      identifierPolicy: defaultsCfg?.identifierPolicy,
      identifierInstructions: defaultsCfg?.identifierInstructions,
      qualityGuardEnabled: qualityGuardCfg?.enabled ?? false,
      qualityGuardMaxRetries: qualityGuardCfg?.maxRetries,
      // Per-agent summary prompt wins; carried into the safeguard event
      // handler via the runtime registry (handler prefers it over
      // event.customInstructions). Source: admin-editable
      // compaction_summary_system_prompt, pushed at provisioning.
      customInstructionsOverride: perAgent?.summaryPrompt,
      // Model: per-agent summarizerModel falls back to defaults.compaction.model
      // falls back to the agent's primary model. We pass the agent's
      // primary model here as-is; resolving a different model id to a
      // Model<Api> requires the model registry which isn't available
      // in this synchronous path — admin-side override of the
      // summarizer model is a follow-up. Per-agent prompt swap is the
      // load-bearing piece.
      model: params.model,
      recentTurnsPreserve: defaultsCfg?.recentTurnsPreserve,
      // keepRecentMode controls how the safeguard chooses which recent
      // turns to preserve verbatim. "tokens" walks backward accumulating
      // ~estimated tokens until keepRecentTokensBudget is exhausted.
      // "turns" (default) keeps the last N user turns + replies.
      keepRecentMode: defaultsCfg?.keepRecentMode,
      // Token budget for "tokens" mode. Computed from keepRecentTokensPct
      // applied to the effective context window so larger windows
      // automatically preserve more turns.
      keepRecentTokensBudget:
        typeof defaultsCfg?.keepRecentTokensPct === "number" &&
        defaultsCfg.keepRecentTokensPct > 0 &&
        defaultsCfg.keepRecentTokensPct <= 1 &&
        contextWindowInfo.tokens > 0
          ? Math.max(1, Math.floor(contextWindowInfo.tokens * defaultsCfg.keepRecentTokensPct))
          : undefined,
      // Bounds a single preserved toolResult. The budget above does not:
      // the pairing pass adds results without counting them.
      preservedToolResultMaxTokens: defaultsCfg?.preservedToolResultMaxTokens,
    });
    factories.push(compactionSafeguardExtension);
  }
  const pruningFactory = buildContextPruningFactory(params);
  if (pruningFactory) {
    factories.push(pruningFactory);
  }
  return factories;
}

export { ensurePiCompactionReserveTokens };
