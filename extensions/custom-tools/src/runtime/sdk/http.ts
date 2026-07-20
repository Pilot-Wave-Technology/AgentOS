/**
 * http SDK — thin convenience wrapper over the net capability.
 *
 * Tool authors get this as the `http` global. Wizard-generated tools (and
 * hand-written ones that just need one HTTP call) use this instead of
 * speaking to `net.fetch` directly.
 *
 * Auto-features over net.fetch:
 *   - Defaults Content-Type to application/json when body is an object
 *   - Auto-stringifies object bodies
 *   - Tries to JSON-parse the response; falls back to text on parse error
 *   - Returns a flat result shape: { status, ok, headers, data }
 *
 * Net's host allowlist still applies — http.call passes URLs straight
 * through to net.fetch. This SDK adds NO new privilege; it's pure ergonomics.
 */

import type { NetCapability } from "../capabilities/net.js";

export interface HttpRequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD";
  body?: unknown;
  headers?: Record<string, string>;
}

export interface HttpResult {
  status: number;
  ok: boolean;
  headers: Record<string, string>;
  /** Parsed JSON when possible; otherwise the raw text body. */
  data: unknown;
}

export interface HttpSdk {
  call: (url: string, opts?: HttpRequestOptions) => Promise<HttpResult>;
}

export function makeHttpSdk(net: NetCapability): HttpSdk {
  return Object.freeze({
    call: async (url: string, opts: HttpRequestOptions = {}): Promise<HttpResult> => {
      const method = opts.method ?? "GET";
      const headers: Record<string, string> = { ...(opts.headers ?? {}) };
      const init: RequestInit = { method, headers };

      const bodyAllowed = method !== "GET" && method !== "HEAD";
      if (opts.body !== undefined && bodyAllowed) {
        if (typeof opts.body === "string") {
          init.body = opts.body;
        } else {
          init.body = JSON.stringify(opts.body);
          if (!Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) {
            headers["Content-Type"] = "application/json";
          }
        }
      }

      const resp = await net.fetch(url, init);
      let data: unknown;
      try {
        data = resp.json();
      } catch {
        data = resp.text();
      }
      return { status: resp.status, ok: resp.ok, headers: resp.headers, data };
    },
  });
}
