/**
 * Goal Review Council tool.
 *
 * submit_review — a reviewer RA (goal_health, convener, …) submits its
 * completed review. The control panel validates `review_details` per
 * `agent_type`; if invalid it returns `{ ok: false, errors: [...] }` so the
 * agent can correct and resubmit. A successful (`{ ok: true }`) call is how the
 * reviewer's run ends.
 *
 * Mirrors the pwt_* task tools: reads CONTROL_PANEL_URL + OPENCLAW_GATEWAY_TOKEN
 * from env and POSTs to the control panel's internal API, returning the
 * response to the agent so the validate→correct loop works in-turn.
 */
import { Type } from "@sinclair/typebox";
import type { OpenClawConfig } from "../../config/config.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult, readStringParam } from "./common.js";

import { getControlPanelAuth } from "./control-panel-auth.js";

const SubmitReviewSchema = Type.Object({
  goal_id: Type.String({
    description: "The goal id. Copy it exactly from docs/REVIEW_CONTEXT.json.",
  }),
  agent_id: Type.String({
    description: "This reviewer instance's agent id, from docs/REVIEW_CONTEXT.json.",
  }),
  agent_type: Type.String({
    description: "Your reviewer type (e.g. 'goal_health' or 'convener'), from docs/REVIEW_CONTEXT.json.",
  }),
  review_id: Type.String({
    description: "The review id this submission fills, from docs/REVIEW_CONTEXT.json.",
  }),
  status: Type.String({
    description:
      "'success' if you completed the review, or 'failed' if you could not (missing " +
      "critical inputs, unable to judge, or an error). You MUST submit one or the other.",
  }),
  review_details: Type.Optional(Type.String({
    description:
      "Your review as MARKDOWN TEXT — REQUIRED when status='success'. Use the exact section " +
      "headings your AGENTS.md specifies, and keep it under ~5000 tokens.",
  })),
  reason: Type.Optional(Type.String({
    description: "REQUIRED when status='failed' — a short explanation of what went wrong.",
  })),
});

export function createSubmitReviewTool(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Submit Review",
    name: "submit_review",
    description:
      "Finish your review by submitting it to the Goal Review Council. On success, pass " +
      "status='success' with a complete review_details (the server validates it for your " +
      "agent_type — if it returns ok:false with errors, fix EXACTLY those and call again). " +
      "If you cannot complete the review, you MUST STILL call this with status='failed' and " +
      "a reason. Only an ok:true response ends your run. Pass goal_id, agent_id, agent_type " +
      "and review_id exactly as given in docs/REVIEW_CONTEXT.json.",
    parameters: SubmitReviewSchema,
    execute: async (_toolCallId, args, _signal) => {
      const params = args as Record<string, unknown>;
      const goalId = readStringParam(params, "goal_id", { required: true, trim: true });
      const agentId = readStringParam(params, "agent_id", { required: true, trim: true });
      const agentType = readStringParam(params, "agent_type", { required: true, trim: true });
      const reviewId = readStringParam(params, "review_id", { required: true, trim: true });
      const status = (readStringParam(params, "status", { trim: true }) || "success").toLowerCase();
      const reason = readStringParam(params, "reason") || "";
      const reviewDetails = readStringParam(params, "review_details") || "";

      if (status === "success" && !reviewDetails.trim()) {
        return jsonResult({ ok: false, errors: ["review_details (markdown text) is required when status='success'."] });
      }
      if (status === "failed" && !reason.trim()) {
        return jsonResult({ ok: false, errors: ["reason is required when status='failed'."] });
      }

      const { url, token } = await getControlPanelAuth(opts?.agentSessionKey);
      if (!url) {
        return jsonResult({ ok: false, errors: ["CONTROL_PANEL_URL is not set on the gateway."] });
      }

      try {
        const res = await fetch(`${url}/internal/council/review`, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            goal_id: goalId,
            agent_id: agentId,
            agent_type: agentType,
            review_id: reviewId,
            status,
            reason,
            review_details: reviewDetails,
          }),
        });
        let json: unknown;
        try {
          json = await res.json();
        } catch {
          json = { ok: false, errors: [`Council endpoint returned a non-JSON response (HTTP ${res.status}).`] };
        }
        return jsonResult(json);
      } catch (err) {
        return jsonResult({
          ok: false,
          errors: [`Failed to reach the council endpoint: ${(err as Error).message}`],
        });
      }
    },
  };
}
