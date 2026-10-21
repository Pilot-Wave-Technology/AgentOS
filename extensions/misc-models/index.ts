import { ErrorCodes, errorShape } from "../../src/gateway/protocol/index.js";
import type { OpenClawPluginApi } from "../../src/plugins/types.js";
import { clearMiscModelCache, getCachedOverrides, refreshMiscModelCache } from "./src/cache.js";

// Self-heal interval — same shape as extensions/model-registry, so an
// admin's edit takes effect within this window even if a models.reload
// signal is dropped.
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

function getControlPanelConfig(): { url: string; token: string } {
  const url = (process.env.CONTROL_PANEL_URL ?? "").replace(/\/$/, "");
  const token = process.env.OPENCLAW_GATEWAY_TOKEN ?? "";
  return { url, token };
}

const plugin = {
  id: "misc-models",
  name: "Utility Models",
  description:
    "Fetches utility-purpose Gemini model overrides from the AgentOS control " +
    "panel's /internal/misc_model_settings endpoint and exposes them via " +
    "getMiscModel() for direct-SDK-call utility sites (e.g. web-search " +
    "grounding) — a separate, Gemini-only config from the agent-facing " +
    "model registry. Refreshes on boot, every 5 minutes, and on " +
    "models.reload RPC.",

  register(api: OpenClawPluginApi) {
    let refreshTimer: ReturnType<typeof setInterval> | null = null;

    api.on("gateway_start", async () => {
      const { url, token } = getControlPanelConfig();
      if (!url) {
        api.logger.warn("misc-models: CONTROL_PANEL_URL not set — utility model overrides disabled");
        return;
      }

      try {
        await refreshMiscModelCache(url, token);
        api.logger.info(
          `misc-models: loaded ${Object.keys(getCachedOverrides()).length} override(s) from control panel`,
        );
      } catch (err) {
        // Non-fatal: every consumer falls back to its own hardcoded
        // default until the next successful refresh.
        api.logger.warn(`misc-models: initial cache load failed — ${String(err)}`);
      }

      refreshTimer = setInterval(async () => {
        try {
          await refreshMiscModelCache(url, token);
          api.logger.debug?.(
            `misc-models: periodic refresh ok — ${Object.keys(getCachedOverrides()).length} override(s)`,
          );
        } catch (err) {
          api.logger.debug?.(`misc-models: periodic refresh failed (will retry) — ${String(err)}`);
        }
      }, REFRESH_INTERVAL_MS);
    });

    api.on("gateway_stop", () => {
      if (refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
      }
      clearMiscModelCache();
    });

    // Reuses the same models.reload RPC extensions/model-registry registers
    // for the same "admin just changed a model setting" trigger — refresh
    // this cache too so both plugins pick up the change on one signal.
    api.registerGatewayMethod("misc-models.reload", async ({ respond }) => {
      const { url, token } = getControlPanelConfig();
      if (!url) {
        respond(false, undefined, errorShape(ErrorCodes.UNAVAILABLE, "CONTROL_PANEL_URL not set"));
        return;
      }
      try {
        await refreshMiscModelCache(url, token);
        const count = Object.keys(getCachedOverrides()).length;
        respond(true, { ok: true, count });
        api.logger.info(`misc-models: cache refreshed — ${count} override(s) active`);
      } catch (err) {
        respond(false, undefined, errorShape(ErrorCodes.UNAVAILABLE, String(err)));
      }
    });
  },
};

export default plugin;
