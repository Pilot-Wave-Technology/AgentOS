/**
 * @openclaw/insights — Dashboard observability gateway methods.
 *
 * Exposes read-only RPC methods for the AgentOS control-panel dashboard.
 * Zero changes to openclaw core. Enable on the gateway VM with:
 *
 *   node openclaw.mjs config set plugins.entries.insights.enabled true
 *
 * Dependency rule: this plugin depends on openclaw internal APIs (fine — same
 * repo). It NEVER imports from the control panel, and openclaw core never
 * imports from this plugin. All coupling flows through the stable RPC
 * interface (method name + JSON payload shape).
 *
 * Methods exposed:
 *   sandbox.status  — Container runtime availability + active sandbox count.
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { loadConfig } from "../../src/config/config.js";
import { resolveSandboxConfigForAgent } from "../../src/agents/sandbox/config.js";
import {
  DEFAULT_SANDBOX_CONTAINER_PREFIX,
  DEFAULT_SANDBOX_IMAGE,
} from "../../src/agents/sandbox/constants.js";
import type { OpenClawPluginApi } from "../../src/plugins/types.js";

const execFileAsync = promisify(execFile);

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Try the given command; return its stdout or null on any error. */
async function tryExec(cmd: string, args: string[]): Promise<string | null> {
  try {
    const { stdout } = await execFileAsync(cmd, args, { timeout: 5000 });
    return stdout;
  } catch {
    return null;
  }
}

/** Detect which container runtime is reachable ("podman" | "docker" | null). */
async function detectRuntime(): Promise<string | null> {
  if ((await tryExec("podman", ["--version"])) !== null) return "podman";
  if ((await tryExec("docker", ["--version"])) !== null) return "docker";
  return null;
}

/** Count active sandbox containers by the well-known name prefix. */
async function countActiveSandboxes(
  prefix: string,
  runtime: string,
): Promise<number> {
  // docker/podman ps --filter name=<prefix> --format '{{.Names}}'
  const out = await tryExec(runtime, [
    "ps",
    `--filter=name=${prefix}`,
    "--format={{.Names}}",
  ]);
  if (out === null) return 0;
  return out.split("\n").filter((l) => l.trim().length > 0).length;
}

/** Check whether the default sandbox image is present locally. */
async function isImageAvailable(image: string, runtime: string): Promise<boolean> {
  const out = await tryExec(runtime, ["image", "inspect", image, "--format={{.Id}}"]);
  return out !== null && out.trim().length > 0;
}

// ── Plugin ────────────────────────────────────────────────────────────────────

const plugin = {
  id: "insights",
  name: "Platform Insights",
  description:
    "Read-only gateway RPCs for the AgentOS dashboard (sandbox health, platform metrics).",

  register(api: OpenClawPluginApi): void {
    api.logger.info("insights plugin registered");

    /**
     * sandbox.status — Container runtime + sandbox health.
     *
     * Response shape (always ok=true; individual fields may be null):
     * {
     *   mode:            "off" | "all" | "non-main" | string,
     *   enabled:         boolean,   // mode !== "off"
     *   runtime:         "podman" | "docker" | null,
     *   runtime_ok:      boolean,   // runtime CLI reachable
     *   active:          number,    // running sandbox containers
     *   image:           string,    // configured sandbox image name
     *   image_available: boolean,   // image present locally
     * }
     */
    api.registerGatewayMethod("sandbox.status", async ({ respond }) => {
      try {
        const cfg = loadConfig();
        const sandboxCfg = resolveSandboxConfigForAgent(cfg, undefined);
        const mode: string = (sandboxCfg as { mode?: string }).mode ?? "off";
        const image: string =
          (sandboxCfg as { docker?: { image?: string } }).docker?.image ??
          DEFAULT_SANDBOX_IMAGE;
        const containerPrefix: string =
          (sandboxCfg as { docker?: { containerPrefix?: string } }).docker?.containerPrefix ??
          DEFAULT_SANDBOX_CONTAINER_PREFIX;

        if (mode === "off") {
          respond(true, {
            mode,
            enabled: false,
            runtime: null,
            runtime_ok: false,
            active: 0,
            image,
            image_available: false,
          });
          return;
        }

        const runtime = await detectRuntime();
        const runtime_ok = runtime !== null;
        const active = runtime_ok
          ? await countActiveSandboxes(containerPrefix, runtime!)
          : 0;
        const image_available = runtime_ok
          ? await isImageAvailable(image, runtime!)
          : false;

        respond(true, {
          mode,
          enabled: true,
          runtime,
          runtime_ok,
          active,
          image,
          image_available,
        });
      } catch (err) {
        // Degrade gracefully — never crash the gateway.
        api.logger.warn("insights sandbox.status error:", String(err));
        respond(true, {
          mode: "unknown",
          enabled: null,
          runtime: null,
          runtime_ok: false,
          active: 0,
          image: DEFAULT_SANDBOX_IMAGE,
          image_available: false,
          error: String(err),
        });
      }
    });
  },
};

export default plugin;
