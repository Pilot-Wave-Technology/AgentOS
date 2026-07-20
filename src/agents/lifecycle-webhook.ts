/**
 * Agent lifecycle webhook — notifies the control panel when CLI agents
 * start, complete, fail, or time out.
 *
 * Events are fire-and-forget (non-blocking). If the control panel is
 * unreachable, events are logged and dropped.
 *
 * Environment:
 *   CONTROL_PANEL_URL — base URL of the control panel
 *   OPENCLAW_GATEWAY_TOKEN — bearer token for auth
 */

import { createSubsystemLogger } from "../logging/subsystem.js";
import { getControlPanelToken } from "./tools/control-panel-auth.js";

const log = createSubsystemLogger("lifecycle-webhook");

interface LifecycleEvent {
  event: "agent.started" | "agent.completed" | "agent.failed" | "agent.timeout" | "session.ended" | "api.call";
  session_key: string;
  agent_id: string;
  run_id: string;
  // provider and model are per-call fields on agent.completed; on session.ended
  // they come from SessionEntry (last model used in the session).
  provider?: string;
  model?: string;
  model_provider?: string;
  timestamp: string;
  duration_ms?: number;
  exit_code?: number;
  error?: string;
  reason?: string;
  outcome?: string;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    /** Thinking/reasoning tokens — subset of output_tokens. Undefined when
     *  extended thinking was not used. Always ≤ output_tokens. */
    thinking_tokens?: number;
    cost_usd?: number;
    num_turns?: number;
  };
  // session.ended only — tool call counts across the whole session.
  tool_usage?: {
    total_calls: number;
    tools: Array<{ name: string; count: number }>;
  };
  // session.ended only — message breakdown across the whole session.
  message_counts?: {
    total: number;
    user: number;
    assistant: number;
    tool_calls: number;
    errors: number;
  };
  // session.ended only — LLM response latency stats across the whole session.
  latency?: {
    avg_ms: number;
    p95_ms: number;
    count: number;
    min_ms: number;
    max_ms: number;
  };
  session_id?: string;
}

const WEBHOOK_TIMEOUT_MS = 5000;

export function fireLifecycleWebhook(event: LifecycleEvent): void {
  const url = process.env.CONTROL_PANEL_URL;
  if (!url) return;

  const endpoint = `${url.replace(/\/$/, "")}/internal/agents/lifecycle`;

  // Fire and forget — don't await, don't block the agent. P0-A: resolve the
  // scoped per-agent JWT from the event's session_key (agent:{id}:{goal}...),
  // falling back to the shared token when minting is unavailable.
  getControlPanelToken(event.session_key)
    .then((token) => {
      if (!token) return undefined;
      return fetch(endpoint, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(event),
        signal: AbortSignal.timeout(WEBHOOK_TIMEOUT_MS),
      }).then((res) => {
        if (!res.ok) {
          log.warn(`lifecycle webhook ${event.event} returned ${res.status}`);
        }
      });
    })
    .catch((err) => {
      log.debug(`lifecycle webhook ${event.event} failed: ${err}`);
    });
}
