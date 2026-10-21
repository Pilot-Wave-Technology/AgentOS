import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import type { OpenClawConfig } from "../config/config.js";
import {
  MODELS_CONFIG_IMPLICIT_ENV_VARS,
  installModelsConfigTestHooks,
  unsetEnv,
  withModelsTempHome as withTempHome,
  withTempEnv,
} from "./models-config.e2e-harness.js";
import {
  planOpenClawModelsJson,
  type ResolveProvidersRunner,
} from "./models-config.plan.js";
import type { ProviderConfig } from "./models-config.providers.js";

installModelsConfigTestHooks();

type PlannedProviders = {
  providers: Record<string, ProviderConfig & { apiKey?: string; baseUrl?: string }>;
};

function parsePlanContents(contents: string): PlannedProviders {
  return JSON.parse(contents) as PlannedProviders;
}

function makeOverride(
  providers: Record<string, ProviderConfig>,
  collisions: Array<{ pluginId: string; key: string }> = [],
): ResolveProvidersRunner {
  return vi.fn(async () => ({ providers, collisions }));
}

async function isolatedPlan(
  cfg: OpenClawConfig,
  opts: {
    env?: NodeJS.ProcessEnv;
    override?: ResolveProvidersRunner;
    sourceConfigForSecrets?: OpenClawConfig;
    envVarsToSet?: Record<string, string>;
  } = {},
): Promise<Awaited<ReturnType<typeof planOpenClawModelsJson>>> {
  return withTempHome(async (home) => {
    const agentDir = path.join(home, "agent-under-test");
    return withTempEnv(
      [...MODELS_CONFIG_IMPLICIT_ENV_VARS, "OPENCLAW_AGENT_DIR", "PI_CODING_AGENT_DIR"],
      async () => {
        unsetEnv([...MODELS_CONFIG_IMPLICIT_ENV_VARS, "OPENCLAW_AGENT_DIR", "PI_CODING_AGENT_DIR"]);
        // Point the auth-profile store at the fresh temp dir so
        // resolveImplicitProviders has no profiles to discover.
        process.env.OPENCLAW_AGENT_DIR = agentDir;
        process.env.PI_CODING_AGENT_DIR = agentDir;

        const env: NodeJS.ProcessEnv = { ...opts.env };
        for (const [name, value] of Object.entries(opts.envVarsToSet ?? {})) {
          env[name] = value;
        }

        return planOpenClawModelsJson({
          cfg,
          sourceConfigForSecrets: opts.sourceConfigForSecrets,
          agentDir,
          env,
          existingRaw: "",
          existingParsed: null,
          runResolveProvidersOverride: opts.override,
        });
      },
    );
  });
}

describe("planOpenClawModelsJson × resolve_providers", () => {
  const customProviderShape = (baseUrl: string, apiKey: string): ProviderConfig =>
    ({ baseUrl, apiKey, models: [] }) as ProviderConfig;

  it("includes a brand-new plugin-contributed provider in the emitted contents", async () => {
    const override = makeOverride({
      "plugin-lab": customProviderShape("https://plugin.example", "PLUGIN_LAB_KEY"),
    });

    const plan = await isolatedPlan(
      {
        models: {
          providers: {
            "explicit-only": customProviderShape("https://explicit.example", "EXPLICIT_KEY"),
          },
        },
      },
      { override },
    );

    expect(plan.action).toBe("write");
    if (plan.action !== "write") {
      throw new Error("expected write action");
    }
    const parsed = parsePlanContents(plan.contents);
    expect(Object.keys(parsed.providers).sort()).toEqual(["explicit-only", "plugin-lab"]);
    expect(parsed.providers["plugin-lab"]?.baseUrl).toBe("https://plugin.example");
    expect(parsed.providers["plugin-lab"]?.apiKey).toBe("PLUGIN_LAB_KEY");
    expect(override).toHaveBeenCalledTimes(1);
  });

  it("plugin overrides an implicit provider's baseUrl while implicit apiKey env-ref survives", async () => {
    const override = makeOverride({
      minimax: {
        baseUrl: "https://minimax.override.example",
        models: [],
      } as ProviderConfig,
    });

    const plan = await isolatedPlan(
      { models: { providers: {} } },
      {
        // Implicit `minimax` provider is auto-discovered when MINIMAX_API_KEY
        // is present in the env passed to the plan.
        envVarsToSet: { MINIMAX_API_KEY: "sk-minimax-test" },
        override,
      },
    );

    expect(plan.action).toBe("write");
    if (plan.action !== "write") {
      throw new Error("expected write action");
    }
    const parsed = parsePlanContents(plan.contents);
    const minimax = parsed.providers.minimax;
    expect(minimax?.baseUrl).toBe("https://minimax.override.example");
    // Implicit apiKey env-var reference is preserved via `mergeProviderModels`
    // (plugin omitted `apiKey`, so implicit's env-marker survives).
    expect(minimax?.apiKey).toBe("MINIMAX_API_KEY");
  });

  it("explicit config wins entirely over a colliding plugin contribution", async () => {
    const override = makeOverride({
      "shared-key": customProviderShape("https://plugin.example", "PLUGIN_KEY"),
    });

    const plan = await isolatedPlan(
      {
        models: {
          providers: {
            "shared-key": customProviderShape("https://explicit.example", "EXPLICIT_KEY"),
          },
        },
      },
      { override },
    );

    expect(plan.action).toBe("write");
    if (plan.action !== "write") {
      throw new Error("expected write action");
    }
    const parsed = parsePlanContents(plan.contents);
    const entry = parsed.providers["shared-key"];
    expect(entry?.baseUrl).toBe("https://explicit.example");
    expect(entry?.apiKey).toBe("EXPLICIT_KEY");
  });

  it("collapses plugin-supplied apiKey '${MY_TOKEN}' to the env-var name via normalizeProviders", async () => {
    const override = makeOverride({
      "template-plugin": {
        baseUrl: "https://template.example",
        apiKey: "${MY_TOKEN}",
        models: [],
      } as ProviderConfig,
    });

    const plan = await isolatedPlan({ models: { providers: {} } }, { override });

    expect(plan.action).toBe("write");
    if (plan.action !== "write") {
      throw new Error("expected write action");
    }
    const parsed = parsePlanContents(plan.contents);
    expect(parsed.providers["template-plugin"]?.apiKey).toBe("MY_TOKEN");
    expect(parsed.providers["template-plugin"]?.baseUrl).toBe("https://template.example");
  });

  it("source-managed secret enforcement still rewrites the apiKey on a plugin-colliding key", async () => {
    // Both plugin and explicit try to set apiKey; explicit wins the merge,
    // then source-managed enforcement rewrites the apiKey to the source's
    // env-marker. Verifies the plugin path does not disable the enforcement
    // step.
    const override = makeOverride({
      "managed-provider": customProviderShape("https://plugin.example", "PLUGIN_KEY"),
    });

    const cfg: OpenClawConfig = {
      models: {
        providers: {
          "managed-provider": customProviderShape("https://explicit.example", "PLAINTEXT_LEAK"),
        },
      },
    };

    const sourceConfigForSecrets: OpenClawConfig = {
      models: {
        providers: {
          "managed-provider": {
            baseUrl: "https://explicit.example",
            apiKey: "${SOURCE_MANAGED_KEY}",
            models: [],
          } as ProviderConfig,
        },
      },
    };

    const plan = await isolatedPlan(cfg, { override, sourceConfigForSecrets });

    expect(plan.action).toBe("write");
    if (plan.action !== "write") {
      throw new Error("expected write action");
    }
    const parsed = parsePlanContents(plan.contents);
    const entry = parsed.providers["managed-provider"];
    // enforceSourceManagedProviderSecrets rewrites the plaintext apiKey to
    // the source-config-declared env-var marker.
    expect(entry?.apiKey).toBe("SOURCE_MANAGED_KEY");
    expect(entry?.baseUrl).toBe("https://explicit.example");
  });

  it("emits byte-identical contents whether the plugin path contributes nothing or is bypassed entirely", async () => {
    // Baseline: no override, no plugin registry activity (active registry is
    // an empty PluginRegistry created by runtime.ts, so no typed hooks fire).
    const baseline = await isolatedPlan({
      models: {
        providers: {
          "baseline-provider": customProviderShape("https://baseline.example", "BASELINE_KEY"),
        },
      },
    });

    // Same config, but with an override that contributes nothing.
    const noopOverride = makeOverride({});
    const withNoopOverride = await isolatedPlan(
      {
        models: {
          providers: {
            "baseline-provider": customProviderShape("https://baseline.example", "BASELINE_KEY"),
          },
        },
      },
      { override: noopOverride },
    );

    expect(baseline.action).toBe("write");
    expect(withNoopOverride.action).toBe("write");
    if (baseline.action !== "write" || withNoopOverride.action !== "write") {
      throw new Error("expected write action for both plans");
    }
    expect(withNoopOverride.contents).toBe(baseline.contents);
    expect(noopOverride).toHaveBeenCalledTimes(1);
  });
});
