/**
 * Security Council tool.
 *
 * submit_security_review — a Security Council agent (an aspect reviewer like
 * 'secrets'/'sast', or the convener) submits its completed review + structured
 * findings. The control panel validates `review_details` per `aspect` and
 * normalizes `findings`; if invalid it returns `{ ok: false, errors: [...] }` so
 * the agent can correct and resubmit. A successful (`{ ok: true }`) call is how
 * the agent's run ends.
 *
 * Mirrors submit_review (review-tools.ts): reads CONTROL_PANEL_URL +
 * OPENCLAW_GATEWAY_TOKEN from env and POSTs to the control panel's internal API,
 * returning the response so the validate→correct loop works in-turn.
 */
import { Type } from "@sinclair/typebox";
import type { OpenClawConfig } from "../../config/config.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult, readStringParam } from "./common.js";

import { getControlPanelAuth } from "./control-panel-auth.js";

const FindingSchema = Type.Object({
  severity: Type.Optional(Type.String({
    description: "critical | high | medium | low | info",
  })),
  file: Type.Optional(Type.String({ description: "path of the file in ./repo" })),
  line: Type.Optional(Type.Integer({ description: "1-based line number, if known" })),
  rule: Type.Optional(Type.String({ description: "scanner rule/check id or pattern" })),
  tool: Type.Optional(Type.String({ description: "the tool that found it (gitleaks, semgrep, …)" })),
  title: Type.Optional(Type.String({ description: "short label for the finding" })),
  description: Type.Optional(Type.String({ description: "what it is + why it matters" })),
});

const SubmitSecurityReviewSchema = Type.Object({
  run_id: Type.String({
    description: "The security run id. Copy it exactly from docs/SECURITY_CONTEXT.json.",
  }),
  agent_id: Type.String({
    description: "This agent instance's id, from docs/SECURITY_CONTEXT.json.",
  }),
  aspect: Type.String({
    description: "Your aspect (e.g. 'secrets', 'sast', or 'convener'), from docs/SECURITY_CONTEXT.json.",
  }),
  review_id: Type.String({
    description: "The review/stage id this submission fills, from docs/SECURITY_CONTEXT.json.",
  }),
  status: Type.String({
    description:
      "'success' if you completed the review, or 'failed' if you could not (toolchain " +
      "missing, no checkout, unable to judge). You MUST submit one or the other.",
  }),
  review_details: Type.Optional(Type.String({
    description:
      "Your review as MARKDOWN TEXT — REQUIRED when status='success'. Use the EXACT section " +
      "headings your AGENTS.md specifies.",
  })),
  findings: Type.Optional(Type.Array(FindingSchema, {
    description:
      "Structured findings, one object per confirmed issue (empty for the convener, or when " +
      "nothing real was found). Aspect agents: include every confirmed finding.",
  })),
  reason: Type.Optional(Type.String({
    description: "REQUIRED when status='failed' — a short explanation of what went wrong.",
  })),
});

export function createSubmitSecurityReviewTool(opts?: {
  config?: OpenClawConfig;
  goalId?: string;
  agentSessionKey?: string;
}): AnyAgentTool {
  return {
    label: "Submit Security Review",
    name: "submit_security_review",
    description:
      "Finish your security review by submitting it to the Security Council. On success, pass " +
      "status='success' with a complete review_details (the server validates it for your aspect — " +
      "if it returns ok:false with errors, fix EXACTLY those and call again) and a findings array " +
      "(empty for the convener). If you cannot complete the review, you MUST STILL call this with " +
      "status='failed' and a reason. Only an ok:true response ends your run. Pass run_id, agent_id, " +
      "aspect and review_id exactly as given in docs/SECURITY_CONTEXT.json.",
    parameters: SubmitSecurityReviewSchema,
    execute: async (_toolCallId, args, _signal) => {
      const params = args as Record<string, unknown>;
      const runId = readStringParam(params, "run_id", { required: true, trim: true });
      const agentId = readStringParam(params, "agent_id", { required: true, trim: true });
      const aspect = readStringParam(params, "aspect", { required: true, trim: true });
      const reviewId = readStringParam(params, "review_id", { required: true, trim: true });
      const status = (readStringParam(params, "status", { trim: true }) || "success").toLowerCase();
      const reason = readStringParam(params, "reason") || "";
      const reviewDetails = readStringParam(params, "review_details") || "";
      const findings = Array.isArray((params as any).findings) ? (params as any).findings : [];

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
        const res = await fetch(`${url}/internal/security/review`, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            run_id: runId,
            agent_id: agentId,
            aspect,
            review_id: reviewId,
            status,
            reason,
            review_details: reviewDetails,
            findings,
          }),
        });
        let json: unknown;
        try {
          json = await res.json();
        } catch {
          json = { ok: false, errors: [`Security endpoint returned a non-JSON response (HTTP ${res.status}).`] };
        }
        return jsonResult(json);
      } catch (err) {
        return jsonResult({
          ok: false,
          errors: [`Failed to reach the security endpoint: ${(err as Error).message}`],
        });
      }
    },
  };
}
