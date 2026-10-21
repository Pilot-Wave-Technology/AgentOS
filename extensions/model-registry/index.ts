import { ErrorCodes, errorShape } from "../../src/gateway/protocol/index.js";
import type { OpenClawPluginApi } from "../../src/plugins/types.js";
import {
  clearCache,
  getCachedCredentialRefs,
  getCachedModelIds,
  getCachedProviders,
  refreshCache,
} from "./src/cache.js";
import { refreshCredentialsAndReachability } from "./src/credential-refresh.js";
import { clearInjectedApiKeysExcept } from "./src/env-injector.js";

// Self-heal interval: if a models.reload signal is dropped (gateway briefly
// unreachable when an admin edited the catalog), the periodic refresh picks up
// the new definitions within REFRESH_INTERVAL_MS without operator action.
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

function getControlPanelConfig(): { url: string; token: string } {
  const url = (process.env.CONTROL_PANEL_URL ?? "").replace(/\/$/, "");
  const token = process.env.OPENCLAW_GATEWAY_TOKEN ?? "";
  return { url, token };
}

/** Resolve every provider's credential health + per-model reachability
 * against the CURRENT cache generation (call refreshCache first). Soft-
 * fail: this is status reporting only, never blocks the caller — a
 * Secret Manager or provider-API hiccup shouldn't take down catalog
 * refresh. Safe to run on every trigger (boot, 5-min self-heal, explicit
 * `models.reload`) since — unlike a real `config.patch` — nothing here
 * writes to gateway config or triggers a restart; see
 * secret-resolver.ts's docstring for why that's deliberately deferred. */
async function refreshCredentialHealth(
  logger: OpenClawPluginApi["logger"],
): Promise<{ credentials: Record<string, { healthy: boolean; error?: string }>; modelReachability: Record<string, boolean> }> {
  const refs = getCachedCredentialRefs();
  if (Object.keys(refs).length === 0) {
    // Still clear stale injections even when there's nothing left to
    // resolve — the case that matters is "every provider just got
    // revoked/removed," which must not leave a previous refresh's env
    // vars sitting around forever. See env-injector.ts's docstring.
    clearInjectedApiKeysExcept([]);
    return { credentials: {}, modelReachability: {} };
  }
  try {
    const report = await refreshCredentialsAndReachability(refs, getCachedModelIds());
    logger.debug?.(
      `model-registry: credential refresh — ${Object.keys(report.credentials).length} provider(s), ` +
        `${Object.keys(report.modelReachability).length} model(s) probed`,
    );
    return report;
  } catch (err) {
    logger.warn(`model-registry: credential refresh failed — ${String(err)}`);
    return { credentials: {}, modelReachability: {} };
  }
}

const plugin = {
  id: "model-registry",
  name: "Model Registry",
  description:
    "Fetches provider + model definitions from the AgentOS control panel's " +
    "/internal/model_catalog endpoint and contributes them to models.json " +
    "via the resolve_providers hook. Also resolves each provider's Secret " +
    "Manager credential reference directly (the value never reaches " +
    "AgentOS), injects it into this process's env for real auth on known " +
    "provider ids, and probes per-model reachability, reporting both back " +
    "for the admin UI's health badges. Refreshes on boot, every 5 minutes, " +
    "and on models.reload RPC.",

  register(api: OpenClawPluginApi) {
    let refreshTimer: ReturnType<typeof setInterval> | null = null;

    // Fetch the catalog on gateway start; warn-and-continue if unavailable
    // (providers just won't be contributed and models.json falls back to
    // implicit + explicit).
    api.on("gateway_start", async () => {
      const { url, token } = getControlPanelConfig();
      if (!url) {
        api.logger.warn(
          "model-registry: CONTROL_PANEL_URL not set — model registry disabled",
        );
        return;
      }

      try {
        await refreshCache(url, token);
        api.logger.info(
          `model-registry: loaded ${Object.keys(getCachedProviders()).length} provider(s) from control panel`,
        );
        // Fetch-on-boot for secret references too — never rely on
        // whatever a previous boot's on-disk state implies; always
        // resolve fresh. Free here since boot is already a restart.
        await refreshCredentialHealth(api.logger);
      } catch (err) {
        // Non-fatal: gateway continues without a catalog; periodic refresh
        // below retries every 5 min, and the resolve_providers handler
        // returns {} until the first successful fetch.
        api.logger.warn(`model-registry: initial cache load failed — ${String(err)}`);
      }

      // Schedule periodic refresh — self-heal if a models.reload RPC was missed.
      refreshTimer = setInterval(async () => {
        try {
          await refreshCache(url, token);
          api.logger.debug?.(
            `model-registry: periodic refresh ok — ${Object.keys(getCachedProviders()).length} provider(s)`,
          );
          await refreshCredentialHealth(api.logger);
        } catch (err) {
          api.logger.debug?.(
            `model-registry: periodic refresh failed (will retry) — ${String(err)}`,
          );
        }
      }, REFRESH_INTERVAL_MS);
    });

    // Stop the periodic refresh + clear the cache when the gateway shuts down,
    // so a fresh boot within the same process always starts from empty state.
    api.on("gateway_stop", () => {
      if (refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
      }
      clearCache();
    });

    // Contribute cached providers to every planOpenClawModelsJson pass. The
    // runner enforces a 500 ms per-handler soft budget — no I/O here; the
    // handler returns the in-memory cache synchronously. Precedence is
    // resolved by the caller as `explicit > plugin > implicit`.
    api.on("resolve_providers", () => ({ providers: getCachedProviders() }));

    // models.reload RPC — called by AgentOS after any provider/model catalog
    // change. Signal-only: no payload, just refreshes the in-memory cache.
    // Newly-loaded providers appear on the next planOpenClawModelsJson pass.
    api.registerGatewayMethod("models.reload", async ({ respond }) => {
      const { url, token } = getControlPanelConfig();
      if (!url) {
        respond(
          false,
          undefined,
          errorShape(ErrorCodes.UNAVAILABLE, "CONTROL_PANEL_URL not set"),
        );
        return;
      }
      try {
        await refreshCache(url, token);
        const count = Object.keys(getCachedProviders()).length;
        const report = await refreshCredentialHealth(api.logger);
        respond(true, {
          ok: true,
          count,
          credentials: report.credentials,
          modelReachability: report.modelReachability,
        });
        api.logger.info(`model-registry: cache refreshed — ${count} provider(s) active`);
      } catch (err) {
        respond(false, undefined, errorShape(ErrorCodes.UNAVAILABLE, String(err)));
      }
    });
  },
};

export default plugin;
