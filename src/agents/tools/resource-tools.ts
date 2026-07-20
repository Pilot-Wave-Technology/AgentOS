/**
 * Resource management tools for GO (Goal Orchestrator) agents.
 *
 * resource_summary — get stored summary for a resource (fast, from DB)
 * resource_scan    — trigger fresh scan + summarization (slow, GitHub API + LLM)
 * resource_read    — read a specific file from a repo resource (real-time)
 * resource_list    — list all resources in the goal with summary status
 *
 * These tools call the control panel's internal API.
 * Requires CONTROL_PANEL_URL and OPENCLAW_GATEWAY_TOKEN in env.
 */
import { Type } from "@sinclair/typebox";
import type { OpenClawConfig } from "../../config/config.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult, readStringParam } from "./common.js";

import { getControlPanelAuth } from "./control-panel-auth.js";

function resolveGoalIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  const parts = sessionKey.split(":");
  if (parts.length < 3) return "";
  const rest = parts.slice(2).join(":");
  const dashIndex = rest.indexOf("-");
  return dashIndex > 0 ? rest.substring(0, dashIndex) : rest;
}

// ── resource_summary ────────────────────────────────────────────────────

const ResourceSummarySchema = Type.Object({
  resource_id: Type.String({
    description:
      "The resource ID to get the summary for. " +
      "Use resource_list first to find resource IDs.",
  }),
});

export function createResourceSummaryTool(opts?: {
  config?: OpenClawConfig;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Resource Summary",
    name: "resource_summary",
    description:
      "Get the stored summary for a goal resource (repo, document, file). " +
      "Returns the analysis/summary from the last scan. Fast — reads from database. " +
      "If the summary is stale or missing, use resource_scan to generate a fresh one.",
    parameters: ResourceSummarySchema,
    execute: async (_toolCallId, args, signal) => {
      const params = args as Record<string, unknown>;
      const resourceId = readStringParam(params, "resource_id", { required: true, trim: true });

      const goalId = resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) {
        return jsonResult({ success: false, error: "Could not determine goal_id from session." });
      }

      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });

      try {
        const res = await fetch(
          `${url}/internal/go/${encodeURIComponent(goalId)}/resources/${encodeURIComponent(resourceId)}/summary`,
          {
            headers: { Authorization: `Bearer ${token}` },
            signal,
          },
        );

        if (!res.ok) {
          const err = await res.text().catch(() => "unknown");
          return jsonResult({ success: false, error: `HTTP ${res.status}: ${err}` });
        }

        return jsonResult(await res.json());
      } catch (err) {
        return jsonResult({ success: false, error: String(err) });
      }
    },
  };
}

// ── resource_scan ───────────────────────────────────────────────────────

const ResourceScanSchema = Type.Object({
  resource_id: Type.String({
    description: "The resource ID to scan/analyze.",
  }),
  focus: Type.Optional(Type.String({
    description:
      "Optional: focus the scan on a specific area. " +
      "For repos: a directory path like 'src/auth/'. " +
      "For documents: a section or topic to focus on.",
  })),
});

export function createResourceScanTool(opts?: {
  config?: OpenClawConfig;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Resource Scan",
    name: "resource_scan",
    description:
      "Trigger a fresh scan and analysis of a goal resource. " +
      "For repos: fetches file tree, README, key configs, recent commits, then summarizes with AI. " +
      "For documents (PDF, Excel, Word): extracts text and generates summary. " +
      "Slower than resource_summary — use only when you need fresh data or no summary exists.",
    parameters: ResourceScanSchema,
    execute: async (_toolCallId, args, signal) => {
      const params = args as Record<string, unknown>;
      const resourceId = readStringParam(params, "resource_id", { required: true, trim: true });
      const focus = readStringParam(params, "focus") || "";

      const goalId = resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) {
        return jsonResult({ success: false, error: "Could not determine goal_id from session." });
      }

      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });

      try {
        const body: Record<string, unknown> = {};
        if (focus) body.focus = focus;

        const res = await fetch(
          `${url}/internal/go/${encodeURIComponent(goalId)}/resources/${encodeURIComponent(resourceId)}/scan`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${token}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify(body),
            signal,
          },
        );

        if (!res.ok) {
          const err = await res.text().catch(() => "unknown");
          return jsonResult({ success: false, error: `HTTP ${res.status}: ${err}` });
        }

        return jsonResult(await res.json());
      } catch (err) {
        return jsonResult({ success: false, error: String(err) });
      }
    },
  };
}

// ── resource_read ───────────────────────────────────────────────────────

const ResourceReadSchema = Type.Object({
  resource_id: Type.String({
    description: "The resource ID to read.",
  }),
  path: Type.Optional(Type.String({
    description:
      "File path within the repository to read (required for repo/branch resources). " +
      "Example: 'src/routes/auth.ts', 'package.json'. " +
      "Not needed for uploaded documents — leave empty to read the full document.",
  })),
  lines: Type.Optional(Type.Number({
    description: "Maximum number of lines to return. Default: 500.",
  })),
});

export function createResourceReadTool(opts?: {
  config?: OpenClawConfig;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Resource Read File",
    name: "resource_read",
    description:
      "Read the content of a resource. " +
      "For repos/branches: reads a specific file via GitHub API (path required). " +
      "For uploaded documents (PDF, Excel, Word, Markdown): reads the full extracted text (no path needed). " +
      "Use this when you need to see actual content to make planning decisions.",
    parameters: ResourceReadSchema,
    execute: async (_toolCallId, args, signal) => {
      const params = args as Record<string, unknown>;
      const resourceId = readStringParam(params, "resource_id", { required: true, trim: true });
      const path = readStringParam(params, "path") || "";
      const lines = typeof params.lines === "number" ? params.lines : 500;

      const goalId = resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) {
        return jsonResult({ success: false, error: "Could not determine goal_id from session." });
      }

      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });

      try {
        const queryParams = new URLSearchParams({
          path,
          lines: String(lines),
        });

        const res = await fetch(
          `${url}/internal/go/${encodeURIComponent(goalId)}/resources/${encodeURIComponent(resourceId)}/read?${queryParams}`,
          {
            headers: { Authorization: `Bearer ${token}` },
            signal,
          },
        );

        if (!res.ok) {
          const err = await res.text().catch(() => "unknown");
          return jsonResult({ success: false, error: `HTTP ${res.status}: ${err}` });
        }

        return jsonResult(await res.json());
      } catch (err) {
        return jsonResult({ success: false, error: String(err) });
      }
    },
  };
}

// ── resource_list ───────────────────────────────────────────────────────

const ResourceListSchema = Type.Object({});

export function createResourceListTool(opts?: {
  config?: OpenClawConfig;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Resource List",
    name: "resource_list",
    description:
      "List all resources in the current goal with their types, status, and summary availability. " +
      "Use this to discover what resources exist before calling resource_summary or resource_read.",
    parameters: ResourceListSchema,
    execute: async (_toolCallId, _args, signal) => {
      const goalId = resolveGoalIdFromSession(opts?.agentSessionKey) || "";
      if (!goalId) {
        return jsonResult({ success: false, error: "Could not determine goal_id from session." });
      }

      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) return jsonResult({ success: false, error: "CONTROL_PANEL_URL not set." });

      try {
        const res = await fetch(
          `${url}/internal/go/${encodeURIComponent(goalId)}/resources`,
          {
            headers: { Authorization: `Bearer ${token}` },
            signal,
          },
        );

        if (!res.ok) {
          const err = await res.text().catch(() => "unknown");
          return jsonResult({ success: false, error: `HTTP ${res.status}: ${err}` });
        }

        return jsonResult(await res.json());
      } catch (err) {
        return jsonResult({ success: false, error: String(err) });
      }
    },
  };
}
