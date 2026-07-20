/**
 * net.fetch capability — host-allowlisted HTTP.
 *
 * Construction:
 *   const net = makeNetCapability(manifest.capabilities.net, ctx);
 * If the grant is undefined, the returned value is undefined — the tool
 * source then sees `net === undefined` and any access throws a TypeError.
 *
 * Enforcement:
 *   - URL must use http: or https: (no file:, no data:, no ftp:)
 *   - URL hostname must exactly match one entry in grant.hosts
 *   - Every call emits an audit event (outcome=ok or outcome=denied)
 *
 * The returned NetResponse is a frozen plain object — not a native
 * Response — so the tool can't reach back into Node's internals via
 * symbol properties on a real Response.
 */

import dns from "node:dns/promises";
import { emit } from "../audit.js";
import type { AgentCallContext, NetGrant, NetResponse } from "../types.js";

export interface NetCapability {
  fetch: (url: string, init?: RequestInit) => Promise<NetResponse>;
}

const ALLOWED_SCHEMES = new Set(["http:", "https:"]);

// Private / link-local / loopback ranges that must not be reached by tool code.
// Resolving ALL A/AAAA records and checking each prevents DNS rebinding attacks
// where a public hostname resolves to a private IP.
const BLOCKED_PREFIXES = [
  "10.",
  "192.168.",
  "172.16.", "172.17.", "172.18.", "172.19.",
  "172.20.", "172.21.", "172.22.", "172.23.",
  "172.24.", "172.25.", "172.26.", "172.27.",
  "172.28.", "172.29.", "172.30.", "172.31.",
  "169.254.",  // link-local / GCP metadata server
  "127.",      // loopback
  "0.",        // 0.0.0.0/8 — "this host"
  "::1",       // IPv6 loopback
  "fc00:",     // IPv6 unique local
  "fd",        // IPv6 unique local (fd00::/8)
  "fe80:",     // IPv6 link-local
];

function isPrivateIp(ip: string): boolean {
  for (const prefix of BLOCKED_PREFIXES) {
    if (ip.startsWith(prefix)) return true;
  }
  return false;
}

/**
 * Resolve all A/AAAA records for `hostname` and reject if ANY resolves to a
 * private / link-local address. This closes the DNS rebinding window that the
 * hostname-only allow-list leaves open.
 */
async function assertNoPrivateResolution(hostname: string): Promise<void> {
  let records: { address: string }[] = [];
  try {
    records = await dns.resolve4(hostname).then((r) => r.map((address) => ({ address }))).catch(() => []);
    const v6 = await dns.resolve6(hostname).then((r) => r.map((address) => ({ address }))).catch(() => []);
    records = [...records, ...v6];
  } catch {
    // Resolution failure — let the subsequent fetch() attempt and fail naturally.
    return;
  }
  for (const { address } of records) {
    if (isPrivateIp(address)) {
      throw new Error(
        `net.fetch: SSRF guard — hostname '${hostname}' resolves to private address '${address}'`,
      );
    }
  }
}

export function makeNetCapability(
  grant: NetGrant | undefined,
  ctx: AgentCallContext,
): NetCapability | undefined {
  if (!grant) return undefined;

  const hosts = new Set(grant.hosts);

  return Object.freeze({
    fetch: async (url: string, init?: RequestInit): Promise<NetResponse> => {
      let parsed: URL;
      try {
        parsed = new URL(url);
      } catch {
        emit(ctx, {
          event: "net.fetch",
          outcome: "denied",
          reason: "invalid_url",
          detail: { url: String(url) },
        });
        throw new Error(`net.fetch: invalid URL '${url}'`);
      }

      if (!ALLOWED_SCHEMES.has(parsed.protocol)) {
        emit(ctx, {
          event: "net.fetch",
          outcome: "denied",
          reason: "unsupported_scheme",
          detail: { url, scheme: parsed.protocol },
        });
        throw new Error(`net.fetch: scheme '${parsed.protocol}' not allowed`);
      }

      if (!hosts.has(parsed.hostname)) {
        emit(ctx, {
          event: "net.fetch",
          outcome: "denied",
          reason: "host_not_granted",
          detail: { url, host: parsed.hostname, granted: [...hosts] },
        });
        throw new Error(`net.fetch: host '${parsed.hostname}' not in tool manifest`);
      }

      // Resolve all A/AAAA records and block if any maps to a private range.
      // This runs after the hostname allow-list check so we only pay the DNS
      // cost for allowed hostnames, and it closes the DNS rebinding window
      // that allow-listing alone cannot prevent.
      try {
        await assertNoPrivateResolution(parsed.hostname);
      } catch (ssrfErr) {
        emit(ctx, {
          event: "net.fetch",
          outcome: "denied",
          reason: "ssrf_blocked",
          detail: { url, host: parsed.hostname },
        });
        throw ssrfErr;
      }

      emit(ctx, {
        event: "net.fetch",
        outcome: "ok",
        detail: { url, method: init?.method ?? "GET" },
      });

      const resp = await globalThis.fetch(url, init);
      // Eagerly read the body so the returned object has no live references
      // back into Node's stream internals.
      const bodyText = await resp.text();

      const headers: Record<string, string> = {};
      resp.headers.forEach((value, key) => {
        headers[key] = value;
      });

      return Object.freeze<NetResponse>({
        status: resp.status,
        ok: resp.ok,
        headers,
        text: () => bodyText,
        json: () => JSON.parse(bodyText),
      });
    },
  });
}
