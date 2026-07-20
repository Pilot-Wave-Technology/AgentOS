/**
 * task_report tool — allows agents to report structured task results
 * to the multi-agent control panel.
 *
 * Agents call this when they finish work, need human input, or hit an error.
 * The control panel processes the structured report to advance the task DAG.
 */
import { Type } from "@sinclair/typebox";
import { getControlPanelToken } from "./control-panel-auth.js";
import { stringEnum } from "../schema/typebox.js";
import type { OpenClawConfig } from "../../config/config.js";
import { loadConfig } from "../../config/config.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult, readStringParam } from "./common.js";

function resolveAgentIdFromSession(sessionKey?: string): string {
  // Session key format: agent:{agentId}:{goalId}-{threadId}
  if (!sessionKey) return "";
  const parts = sessionKey.split(":");
  return parts.length >= 2 ? parts[1] : "";
}

const TaskReportSchema = Type.Object({
  task_id: Type.String({
    description: "The task ID assigned to this agent. Provided in the dispatch message.",
  }),
  status: stringEnum(["completed", "failed", "blocked"], {
    description:
      "Task status: 'completed' = work done successfully, 'failed' = unrecoverable error, 'blocked' = need human input.",
  }),
  summary: Type.String({
    description: "Brief one-line summary of what was done, what failed, or what question you have.",
  }),
  branch: Type.Optional(Type.String({
    description: "Git branch name where changes were pushed.",
  })),
  files_changed: Type.Optional(Type.Array(Type.String(), {
    description: "List of files created or modified.",
  })),
  commits: Type.Optional(Type.Array(Type.String(), {
    description: "List of commit messages.",
  })),
  details: Type.Optional(Type.String({
    description: "Detailed description of work done, error details, or full question context. For planner agents, include the json:subtasks block here.",
  })),
  test_results: Type.Optional(Type.Object({
    passed: Type.Optional(Type.Number({ description: "Number of tests passed" })),
    failed: Type.Optional(Type.Number({ description: "Number of tests failed" })),
    coverage: Type.Optional(Type.String({ description: "Coverage percentage" })),
    issues: Type.Optional(Type.Array(Type.String(), { description: "List of test issues found" })),
  }, { additionalProperties: true, description: "Test results (for test agents)." })),
  review: Type.Optional(Type.Object({
    verdict: Type.Optional(stringEnum(["approve", "request_changes", "block"], {
      description: "Review verdict",
    })),
    critical_issues: Type.Optional(Type.Array(Type.String(), { description: "Critical issues that must be fixed" })),
    suggestions: Type.Optional(Type.Array(Type.String(), { description: "Non-blocking suggestions" })),
  }, { additionalProperties: true, description: "Code review results (for review agents)." })),
});

export function createTaskReportTool(opts?: {
  config?: OpenClawConfig;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Task Report",
    name: "task_report",
    description:
      "Report structured task results to the control panel. " +
      "Call this when you finish your task, encounter an error, or need human input. " +
      "Include as much structured data as possible (branch, files, test results, review verdict) " +
      "so the task controller can process your report automatically.",
    parameters: TaskReportSchema,
    execute: async (_toolCallId, args, signal) => {
      const params = args as Record<string, unknown>;
      const taskId = readStringParam(params, "task_id", { required: true, trim: true });
      const status = readStringParam(params, "status", { required: true, trim: true });
      const summary = readStringParam(params, "summary", { required: true });

      // Collect all structured fields
      const report: Record<string, unknown> = {
        status,
        summary,
        branch: params.branch ?? null,
        files_changed: params.files_changed ?? null,
        commits: params.commits ?? null,
        details: params.details ?? null,
        test_results: params.test_results ?? null,
        review: params.review ?? null,
      };

      // Build result string for backward compat (stored in tasks.result)
      let resultText = summary;
      if (params.details) {
        resultText += "\n\n" + String(params.details);
      }

      const controlPanelUrl =
        process.env.CONTROL_PANEL_URL || process.env.OPENCLAW_CONTROL_PANEL_URL || "";
      const token = await getControlPanelToken(opts?.agentSessionKey);

      if (!controlPanelUrl) {
        return jsonResult({
          success: false,
          error: "CONTROL_PANEL_URL environment variable not set.",
        });
      }

      if (!token) {
        return jsonResult({
          success: false,
          error: "OPENCLAW_GATEWAY_TOKEN not set.",
        });
      }

      const url = `${controlPanelUrl.replace(/\/$/, "")}/internal/tasks/${encodeURIComponent(taskId)}/complete`;

      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            status,
            result: resultText,
            report,
            gateway_agent_id: resolveAgentIdFromSession(opts?.agentSessionKey),
          }),
          signal,
        });

        if (!res.ok) {
          const errorText = await res.text().catch(() => "unknown error");
          return jsonResult({
            success: false,
            error: `Control panel returned ${res.status}: ${errorText}`,
          });
        }

        const payload = await res.json().catch(() => ({}));
        return jsonResult({
          success: true,
          task_id: taskId,
          status,
          message: "Task report submitted successfully.",
          ...payload,
        });
      } catch (err) {
        return jsonResult({
          success: false,
          error: `Failed to submit task report: ${String(err)}`,
        });
      }
    },
  };
}
