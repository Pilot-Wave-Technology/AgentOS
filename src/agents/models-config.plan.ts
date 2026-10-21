import type { OpenClawConfig } from "../config/config.js";
import { createSubsystemLogger } from "../logging/subsystem.js";
import { createHookRunner, type HookRunner } from "../plugins/hooks.js";
import { getActivePluginRegistry } from "../plugins/runtime.js";
import type {
  PluginHookAgentContext,
  PluginHookResolveProvidersEvent,
} from "../plugins/types.js";
import { isRecord } from "../utils.js";
import {
  mergeProviders,
  mergeWithExistingProviderSecrets,
  type ExistingProviderConfig,
} from "./models-config.merge.js";
import {
  enforceSourceManagedProviderSecrets,
  normalizeProviders,
  resolveImplicitProviders,
  type ProviderConfig,
} from "./models-config.providers.js";

type ModelsConfig = NonNullable<OpenClawConfig["models"]>;

const pluginsLog = createSubsystemLogger("plugins");

/**
 * Injection point for unit tests. Matches the shape of
 * `HookRunner["runResolveProviders"]` so tests can supply a fake without
 * touching the process-global plugin registry.
 */
export type ResolveProvidersRunner = HookRunner["runResolveProviders"];

export type ModelsJsonPlan =
  | {
      action: "skip";
    }
  | {
      action: "noop";
    }
  | {
      action: "write";
      contents: string;
    };

/**
 * Invoke plugin `resolve_providers` handlers and drain collision reports
 * into the active registry's diagnostics.
 *
 * - When `override` is supplied, that runner is used (tests inject their
 *   own fake). Otherwise the runner is built from
 *   {@link getActivePluginRegistry}; if no registry is active the pass is a
 *   no-op that returns `{}`.
 * - When an active registry exists, every collision returned by the runner
 *   is pushed as a `warn`-level diagnostic — this is how operators discover
 *   two plugins fighting over the same provider key.
 */
async function runPluginResolveProviders(params: {
  override?: ResolveProvidersRunner;
  event: PluginHookResolveProvidersEvent;
}): Promise<Record<string, ProviderConfig>> {
  const registry = getActivePluginRegistry();
  const runner: ResolveProvidersRunner | null =
    params.override ??
    (registry
      ? createHookRunner(registry, {
          catchErrors: true,
          logger: pluginsLog,
        }).runResolveProviders
      : null);
  if (!runner) {
    return {};
  }
  const ctx: PluginHookAgentContext = {};
  const outcome = await runner({ event: params.event, ctx });
  if (registry) {
    for (const { pluginId, key } of outcome.collisions) {
      registry.diagnostics.push({
        level: "warn",
        pluginId,
        source: pluginId,
        message: `resolve_providers collision on key '${key}' — higher-priority handler won`,
      });
    }
  }
  return outcome.providers;
}

async function resolveProvidersForModelsJson(params: {
  cfg: OpenClawConfig;
  agentDir: string;
  env: NodeJS.ProcessEnv;
  runResolveProvidersOverride?: ResolveProvidersRunner;
}): Promise<Record<string, ProviderConfig>> {
  const { cfg, agentDir, env } = params;
  const explicitProviders = cfg.models?.providers ?? {};
  const implicitProviders = await resolveImplicitProviders({
    agentDir,
    config: cfg,
    env,
    explicitProviders,
  });

  const pluginProviders = await runPluginResolveProviders({
    override: params.runResolveProvidersOverride,
    event: {
      agentDir,
      implicit: implicitProviders ?? {},
      explicit: explicitProviders,
    },
  });

  // Precedence: explicit > plugin > implicit. Compose two two-way merges
  // rather than extending `mergeProviders` — smaller diff and preserves
  // the existing two-arg contract for every other caller.
  const withPlugin = mergeProviders({
    implicit: implicitProviders,
    explicit: pluginProviders,
  });
  return mergeProviders({
    implicit: withPlugin,
    explicit: explicitProviders,
  });
}

function resolveExplicitBaseUrlProviders(
  providers: OpenClawConfig["models"] | undefined,
): ReadonlySet<string> {
  return new Set(
    Object.entries(providers?.providers ?? {})
      .map(([key, provider]) => [key.trim(), provider] as const)
      .filter(
        ([key, provider]) =>
          Boolean(key) && typeof provider?.baseUrl === "string" && provider.baseUrl.trim(),
      )
      .map(([key]) => key),
  );
}

async function resolveProvidersForMode(params: {
  mode: NonNullable<ModelsConfig["mode"]>;
  existingParsed: unknown;
  providers: Record<string, ProviderConfig>;
  secretRefManagedProviders: ReadonlySet<string>;
  explicitBaseUrlProviders: ReadonlySet<string>;
}): Promise<Record<string, ProviderConfig>> {
  if (params.mode !== "merge") {
    return params.providers;
  }
  const existing = params.existingParsed;
  if (!isRecord(existing) || !isRecord(existing.providers)) {
    return params.providers;
  }
  const existingProviders = existing.providers as Record<
    string,
    NonNullable<ModelsConfig["providers"]>[string]
  >;
  return mergeWithExistingProviderSecrets({
    nextProviders: params.providers,
    existingProviders: existingProviders as Record<string, ExistingProviderConfig>,
    secretRefManagedProviders: params.secretRefManagedProviders,
    explicitBaseUrlProviders: params.explicitBaseUrlProviders,
  });
}

export async function planOpenClawModelsJson(params: {
  cfg: OpenClawConfig;
  sourceConfigForSecrets?: OpenClawConfig;
  agentDir: string;
  env: NodeJS.ProcessEnv;
  existingRaw: string;
  existingParsed: unknown;
  /**
   * Test-only injection: supply a fake `resolve_providers` runner without
   * touching the process-global plugin registry. Production callers omit
   * this and the runner is built from `getActivePluginRegistry()`.
   */
  runResolveProvidersOverride?: ResolveProvidersRunner;
}): Promise<ModelsJsonPlan> {
  const { cfg, agentDir, env } = params;
  const providers = await resolveProvidersForModelsJson({
    cfg,
    agentDir,
    env,
    runResolveProvidersOverride: params.runResolveProvidersOverride,
  });

  if (Object.keys(providers).length === 0) {
    return { action: "skip" };
  }

  const mode = cfg.models?.mode ?? "merge";
  const secretRefManagedProviders = new Set<string>();
  const normalizedProviders =
    normalizeProviders({
      providers,
      agentDir,
      env,
      secretDefaults: cfg.secrets?.defaults,
      sourceProviders: params.sourceConfigForSecrets?.models?.providers,
      sourceSecretDefaults: params.sourceConfigForSecrets?.secrets?.defaults,
      secretRefManagedProviders,
    }) ?? providers;
  const mergedProviders = await resolveProvidersForMode({
    mode,
    existingParsed: params.existingParsed,
    providers: normalizedProviders,
    secretRefManagedProviders,
    explicitBaseUrlProviders: resolveExplicitBaseUrlProviders(cfg.models),
  });
  const secretEnforcedProviders =
    enforceSourceManagedProviderSecrets({
      providers: mergedProviders,
      sourceProviders: params.sourceConfigForSecrets?.models?.providers,
      sourceSecretDefaults: params.sourceConfigForSecrets?.secrets?.defaults,
      secretRefManagedProviders,
    }) ?? mergedProviders;
  const nextContents = `${JSON.stringify({ providers: secretEnforcedProviders }, null, 2)}\n`;

  if (params.existingRaw === nextContents) {
    return { action: "noop" };
  }

  return {
    action: "write",
    contents: nextContents,
  };
}
