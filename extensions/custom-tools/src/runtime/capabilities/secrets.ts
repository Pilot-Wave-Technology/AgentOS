/**
 * secrets.get capability — name-allowlisted plaintext credential fetch.
 *
 * The plaintext lives only inside the capability closure for the duration
 * of one tool call. It's fetched on demand from the control panel and
 * never stored on the VM:
 *
 *   plugin (here) ──HTTPS──▶ AgentOS /internal/custom-tools/credentials/by-name/{n}
 *                            (Fernet-decrypted server-side, returned over Bearer)
 *
 * The Fernet key never leaves Cloud Run. Worst case if the VM is
 * compromised: an attacker can request any credential we've granted to
 * any tool — same blast radius as today, where the AgentOS executor
 * has access to all credentials anyway.
 *
 * Resolved values are memoised PER CALL so a tool that does
 *   const k = await secrets.get('x'); const k2 = await secrets.get('x');
 * only triggers one round-trip. Memoisation is per-AgentCallContext
 * (i.e. per tool invocation) — never shared across calls.
 */

import { emit } from "../audit.js";
import type { AgentCallContext, SecretsGrant } from "../types.js";

export interface SecretsCapability {
  get: (name: string) => Promise<string>;
}

export function makeSecretsCapability(
  grant: SecretsGrant | undefined,
  ctx: AgentCallContext,
): SecretsCapability | undefined {
  if (!grant) return undefined;

  const allowed = new Set(grant.names);
  const cache = new Map<string, string>();

  return Object.freeze({
    get: async (name: string): Promise<string> => {
      if (typeof name !== "string") {
        throw new Error("secrets.get: name must be a string");
      }
      if (!allowed.has(name)) {
        emit(ctx, {
          event: "secrets.get",
          outcome: "denied",
          reason: "not_granted",
          detail: { name, granted: [...allowed] },
        });
        throw new Error(`secrets.get: '${name}' not in tool manifest`);
      }

      const cached = cache.get(name);
      if (cached !== undefined) {
        emit(ctx, { event: "secrets.get", outcome: "ok", detail: { name, cached: true } });
        return cached;
      }

      const base = ctx.control_panel_url.replace(/\/$/, "");
      // Pass the calling tool's name so the control panel can re-verify the grant
      // server-side (the client-side allowlist above is advisory; a compromised
      // plugin could skip it). See audit #3.
      const toolQuery = ctx.tool_name ? `?tool=${encodeURIComponent(ctx.tool_name)}` : "";
      const url = `${base}/internal/custom-tools/credentials/by-name/${encodeURIComponent(name)}${toolQuery}`;
      const resp = await globalThis.fetch(url, {
        headers: { Authorization: `Bearer ${ctx.control_panel_token}` },
      });
      if (!resp.ok) {
        emit(ctx, {
          event: "secrets.get",
          outcome: "denied",
          reason: "fetch_failed",
          detail: { name, status: resp.status },
        });
        throw new Error(`secrets.get: '${name}' fetch failed (HTTP ${resp.status})`);
      }
      const body = (await resp.json()) as { value?: unknown };
      if (typeof body.value !== "string") {
        throw new Error(`secrets.get: '${name}' response missing 'value'`);
      }

      cache.set(name, body.value);
      emit(ctx, { event: "secrets.get", outcome: "ok", detail: { name, cached: false } });
      return body.value;
    },
  });
}
