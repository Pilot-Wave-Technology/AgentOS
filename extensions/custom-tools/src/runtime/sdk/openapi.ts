/**
 * openapi SDK — operation dispatcher for OpenAPI-imported aggregated tools.
 *
 * Each wizard-generated OpenAPI tool emits source like:
 *
 *   const SERVER = "https://api.example.com";
 *   const OPS = {
 *     list_pets:  { method: "GET",  path: "/pets" },
 *     get_pet:    { method: "GET",  path: "/pets/{petId}" },
 *     create_pet: { method: "POST", path: "/pets" },
 *   };
 *
 *   async function execute(args) {
 *     return await openapi.call({ serverUrl: SERVER, operations: OPS }, args.operation, args.arguments);
 *   }
 *
 * Routing rules (matching the previous Python executor):
 *   - Path params (segments like {petId}) are pulled from `arguments`,
 *     URL-encoded, and substituted into the path
 *   - Remaining args go to:
 *       GET / DELETE / HEAD → query string
 *       POST / PUT / PATCH  → JSON body
 *   - Auth headers are NOT added here — wizard-generated source pulls
 *     credentials via secrets.get() and passes them as headers in opts
 *
 * Returns the same shape as http.call: { status, ok, headers, data }.
 */

import type { NetCapability } from "../capabilities/net.js";

export interface OpenApiOperation {
  method: string;
  path: string;
}

export interface OpenApiConfig {
  serverUrl: string;
  operations: Record<string, OpenApiOperation>;
  /** Headers added to every operation call (e.g. auth headers). */
  headers?: Record<string, string>;
}

export interface OpenApiResult {
  status: number;
  ok: boolean;
  headers: Record<string, string>;
  data: unknown;
}

export interface OpenApiSdk {
  call: (
    cfg: OpenApiConfig,
    operationId: string,
    args: Record<string, unknown>,
  ) => Promise<OpenApiResult>;
}

export function makeOpenApiSdk(net: NetCapability): OpenApiSdk {
  return Object.freeze({
    call: async (cfg, operationId, args = {}) => {
      const op = cfg.operations[operationId];
      if (!op) {
        const available = Object.keys(cfg.operations).join(", ");
        throw new Error(
          `openapi.call: unknown operation '${operationId}'. Available: ${available || "(none)"}`,
        );
      }

      const method = op.method.toUpperCase();
      const pathParams = extractPathParams(op.path);

      // Substitute path params (URL-encoded), keep the rest for query/body.
      let renderedPath = op.path;
      const remaining: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(args)) {
        if (pathParams.has(k)) {
          renderedPath = renderedPath.replace(`{${k}}`, encodeURIComponent(String(v)));
        } else {
          remaining[k] = v;
        }
      }

      let url = cfg.serverUrl.replace(/\/$/, "") + renderedPath;
      const headers: Record<string, string> = { ...(cfg.headers ?? {}) };
      const init: RequestInit = { method, headers };

      const bodyAllowed = method === "POST" || method === "PUT" || method === "PATCH";
      if (bodyAllowed && Object.keys(remaining).length > 0) {
        init.body = JSON.stringify(remaining);
        if (!Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) {
          headers["Content-Type"] = "application/json";
        }
      } else if (!bodyAllowed && Object.keys(remaining).length > 0) {
        const qs = new URLSearchParams();
        for (const [k, v] of Object.entries(remaining)) {
          qs.append(k, String(v));
        }
        url += (url.includes("?") ? "&" : "?") + qs.toString();
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

function extractPathParams(template: string): Set<string> {
  const out = new Set<string>();
  const re = /\{([^}]+)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(template)) !== null) {
    out.add(m[1]);
  }
  return out;
}
