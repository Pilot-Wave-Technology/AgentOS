import type { ToolManifest } from "./runtime/types.js";

/**
 * Shape of a custom tool definition returned by /internal/custom-tools.
 *
 * Tools that have both `source_code` and `manifest` are executed via the
 * capability runtime (runtime/isolate.ts). Tools without either are legacy
 * and currently produce no execution — phase 4 deletes the legacy path.
 */
export interface CustomToolDef {
  name: string;
  label: string;
  description: string;
  tier: "http_action" | "openapi" | "typescript" | "mcp_server";
  parameters_schema: Record<string, unknown>;
  /** TypeScript source — defines `async function execute(args)`. Runtime-only. */
  source_code?: string;
  /** Declared capabilities + limits. Required for runtime execution. */
  manifest?: ToolManifest;
}
