import type { OpenClawConfig } from "../config/config.js";
import type { ContextEngineInfo } from "../context-engine/types.js";
import { lookupContextTokens } from "./context.js";

export const DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR = 20_000;

/**
 * Resolve the effective context window in tokens used for percentage-based
 * compaction settings (e.g. keepRecentTokensPct). Honors the
 * `agents.defaults.contextTokens` cap when present so dev profiles with a
 * low cap (e.g. 50k) get a sensible recent-preservation budget instead of
 * the model's raw 1M native window.
 */
export function resolveCompactionEffectiveContextWindow(params: {
  cfg?: OpenClawConfig;
  modelId?: string;
}): number | undefined {
  const native = lookupContextTokens(params.modelId);
  const cap = params.cfg?.agents?.defaults?.contextTokens;
  const hasCap = typeof cap === "number" && Number.isFinite(cap) && cap > 0;
  if (typeof native === "number" && native > 0) {
    return hasCap ? Math.min(Math.floor(cap as number), native) : native;
  }
  return hasCap ? Math.floor(cap as number) : undefined;
}

type PiSettingsManagerLike = {
  getCompactionReserveTokens: () => number;
  getCompactionKeepRecentTokens: () => number;
  applyOverrides: (overrides: {
    compaction: {
      reserveTokens?: number;
      keepRecentTokens?: number;
    };
  }) => void;
  setCompactionEnabled?: (enabled: boolean) => void;
};

export function ensurePiCompactionReserveTokens(params: {
  settingsManager: PiSettingsManagerLike;
  minReserveTokens?: number;
}): { didOverride: boolean; reserveTokens: number } {
  const minReserveTokens = params.minReserveTokens ?? DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR;
  const current = params.settingsManager.getCompactionReserveTokens();

  if (current >= minReserveTokens) {
    return { didOverride: false, reserveTokens: current };
  }

  params.settingsManager.applyOverrides({
    compaction: { reserveTokens: minReserveTokens },
  });

  return { didOverride: true, reserveTokens: minReserveTokens };
}

export function resolveCompactionReserveTokensFloor(
  cfg?: OpenClawConfig,
  modelId?: string,
): number {
  // Percentage-based reserve wins when set + window resolvable.
  const pct = cfg?.agents?.defaults?.compaction?.reserveTokensPct;
  if (typeof pct === "number" && pct > 0 && pct <= 1) {
    const ctxWindow = resolveCompactionEffectiveContextWindow({ cfg, modelId });
    if (typeof ctxWindow === "number" && ctxWindow > 0) {
      return Math.max(1, Math.floor(ctxWindow * pct));
    }
  }
  const raw = cfg?.agents?.defaults?.compaction?.reserveTokensFloor;
  if (typeof raw === "number" && Number.isFinite(raw) && raw >= 0) {
    return Math.floor(raw);
  }
  return DEFAULT_PI_COMPACTION_RESERVE_TOKENS_FLOOR;
}

function toNonNegativeInt(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return undefined;
  }
  return Math.floor(value);
}

function toPositiveInt(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return undefined;
  }
  return Math.floor(value);
}

export function applyPiCompactionSettingsFromConfig(params: {
  settingsManager: PiSettingsManagerLike;
  cfg?: OpenClawConfig;
  /** Model id used to resolve effective context window for percentage-based settings (e.g. keepRecentTokensPct). */
  modelId?: string;
}): {
  didOverride: boolean;
  compaction: { reserveTokens: number; keepRecentTokens: number };
} {
  const currentReserveTokens = params.settingsManager.getCompactionReserveTokens();
  const currentKeepRecentTokens = params.settingsManager.getCompactionKeepRecentTokens();
  const compactionCfg = params.cfg?.agents?.defaults?.compaction;

  const configuredReserveTokens = toNonNegativeInt(compactionCfg?.reserveTokens);
  const configuredKeepRecentTokens = toPositiveInt(compactionCfg?.keepRecentTokens);
  const reserveTokensFloor = resolveCompactionReserveTokensFloor(params.cfg, params.modelId);

  // Resolve keepRecentTokensPct → absolute against effective context window.
  // Pct wins over the absolute keepRecentTokens when both are set.
  const pct = compactionCfg?.keepRecentTokensPct;
  let pctResolvedKeepRecentTokens: number | undefined;
  if (typeof pct === "number" && pct > 0 && pct <= 1) {
    const ctxWindow = resolveCompactionEffectiveContextWindow({
      cfg: params.cfg,
      modelId: params.modelId,
    });
    if (typeof ctxWindow === "number" && ctxWindow > 0) {
      pctResolvedKeepRecentTokens = Math.max(1, Math.floor(ctxWindow * pct));
    }
  }

  const targetReserveTokens = Math.max(
    configuredReserveTokens ?? currentReserveTokens,
    reserveTokensFloor,
  );
  const targetKeepRecentTokens =
    pctResolvedKeepRecentTokens ?? configuredKeepRecentTokens ?? currentKeepRecentTokens;

  const overrides: { reserveTokens?: number; keepRecentTokens?: number } = {};
  if (targetReserveTokens !== currentReserveTokens) {
    overrides.reserveTokens = targetReserveTokens;
  }
  if (targetKeepRecentTokens !== currentKeepRecentTokens) {
    overrides.keepRecentTokens = targetKeepRecentTokens;
  }

  const didOverride = Object.keys(overrides).length > 0;
  if (didOverride) {
    params.settingsManager.applyOverrides({ compaction: overrides });
  }

  return {
    didOverride,
    compaction: {
      reserveTokens: targetReserveTokens,
      keepRecentTokens: targetKeepRecentTokens,
    },
  };
}

/** Decide whether Pi's internal auto-compaction should be disabled for this run. */
export function shouldDisablePiAutoCompaction(params: {
  contextEngineInfo?: ContextEngineInfo;
  asyncCompactionEnabled?: boolean;
}): boolean {
  if (params.asyncCompactionEnabled === true) return true;
  return params.contextEngineInfo?.ownsCompaction === true;
}

/** Disable Pi auto-compaction via settings when a context engine owns compaction
 * OR when openclaw's AsyncCompactionManager owns the trigger
 * (agents.defaults.compaction.asyncEnabled=true). */
export function applyPiAutoCompactionGuard(params: {
  settingsManager: PiSettingsManagerLike;
  contextEngineInfo?: ContextEngineInfo;
  asyncCompactionEnabled?: boolean;
}): { supported: boolean; disabled: boolean } {
  const disable = shouldDisablePiAutoCompaction({
    contextEngineInfo: params.contextEngineInfo,
    asyncCompactionEnabled: params.asyncCompactionEnabled,
  });
  const hasMethod = typeof params.settingsManager.setCompactionEnabled === "function";
  if (!disable || !hasMethod) {
    return { supported: hasMethod, disabled: false };
  }
  params.settingsManager.setCompactionEnabled!(false);
  return { supported: true, disabled: true };
}
