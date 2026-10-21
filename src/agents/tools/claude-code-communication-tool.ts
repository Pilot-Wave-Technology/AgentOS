/**
 * claude-code-communication-tool.ts — the ONE sanctioned way for an SHCA to
 * talk to its story's Claude Code session.
 *
 * Contract (deliberately parameter-free except the message):
 *   input:  { message: string }
 *   1. instanceId  ← agentSessionKey  (agent:{instanceId}:{goalKey})
 *   2. resolve active story  ← control panel /internal/shca/instances/{id}/active-story
 *      → { story_id, workspace_path, claude_session_id, current_phase,
 *          execution_paused, prespawn_in_flight, phase_in_flight, ambiguous }
 *   3. C2 fail-closed ALLOW-LIST gate (see below) → error | busy | proceed
 *   4. spawnCliDirect({ cwd, resumeSessionId, task, storyId })  — C3 lock + C4 fallback apply
 *   5. return { status:"ok", reply } | { status:"busy", reason } | { status:"error", error }
 *
 * Why parameter-free: the SHCA must never pass a session id / cwd / runtime.
 * Session ids are resolved server-side from durable state (C1) so a stale or
 * hallucinated id can't reach `claude -p`, and the phase gate (C2) can't be
 * bypassed by hand-crafting a sessions_spawn call.
 *
 * The C2 gate is a fail-closed allow-list (NOT a block-list): the phase space
 * is larger than {executing, awaiting_review} — integrating, resolving_conflicts,
 * deploying, iterating, monitoring, awaiting_mr_approval are all execution-family
 * (the merge orchestrator even drives coder turns during resolving_conflicts). A
 * block-list would let the tool inject a turn during those and corrupt the phase
 * session. So we proceed ONLY for planning-family phases (or a paused-idle
 * session); every other phase value — known or future — refuses by default.
 */
import { Type } from "@sinclair/typebox";
import type { GatewayMessageChannel } from "../../utils/message-channel.js";
import { spawnCliDirect } from "../cli-spawn.js";
import { getControlPanelToken } from "./control-panel-auth.js";
import type { SpawnedToolContext } from "../spawned-context.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult, readStringParam } from "./common.js";

// Planning-family phases where the SHCA may drive the Claude Code session
// directly (the DAG is not executing, so the session is idle and safe to
// resume). Any other value refuses. awaiting_next = post-DAG idle.
const PLANNING_PHASES = new Set<string>([
  "discovery",
  "planning",
  "awaiting_plan_approval",
  "awaiting_next",
]);

const ClaudeCodeCommunicationSchema = Type.Object({
  message: Type.String({
    description:
      "The message to send to this story's Claude Code session. The session, working directory, and phase gating are all resolved server-side from the active story — do not pass ids, cwd, or runtime.",
  }),
});

interface ActiveStory {
  ok?: boolean;
  story_id?: string | null;
  workspace_path?: string;
  claude_session_id?: string;
  current_phase?: string;
  execution_paused?: boolean;
  prespawn_in_flight?: boolean;
  phase_in_flight?: boolean;
  ambiguous?: boolean;
}

export function createClaudeCodeCommunicationTool(
  opts?: {
    agentSessionKey?: string;
    agentChannel?: GatewayMessageChannel;
  } & SpawnedToolContext,
): AnyAgentTool {
  return {
    label: "ClaudeCode",
    name: "claude_code_communication",
    description:
      "Send a message to the Claude Code session for the story you are currently working on, and get its reply. This is the ONLY supported way to talk to Claude Code — never use sessions_spawn / sessions_send or session ids. The session and working directory are resolved automatically. Returns { status: 'ok', reply } on success; { status: 'busy', reason } if Claude Code is mid-turn or a phase is running (do NOT retry — tell the human to resend later); { status: 'error', error } if there is no single active story or the current phase does not allow direct messaging.",
    parameters: ClaudeCodeCommunicationSchema,
    execute: async (_toolCallId, args) => {
      const params = args as Record<string, unknown>;
      const message = readStringParam(params, "message", { required: true });

      // 1. instanceId from the agent session key: agent:{instanceId}:{goalKey}
      const sessionKey = opts?.agentSessionKey || "";
      const parts = sessionKey.split(":");
      if (parts.length < 2 || parts[0] !== "agent" || !parts[1]) {
        return jsonResult({
          status: "error",
          error:
            "could not determine the assistant instance from the session; claude_code_communication is only available to a story-scoped assistant.",
        });
      }
      const instanceId = parts[1];

      const cpUrl = (process.env.CONTROL_PANEL_URL || "").replace(/\/$/, "");
      const apiKey = process.env.OPENCLAW_GATEWAY_TOKEN || "";
      if (!cpUrl || !apiKey) {
        return jsonResult({
          status: "error",
          error: "control panel URL / token not configured on the gateway; cannot resolve the active story.",
        });
      }

      // 2. Resolve the active story (durable server-side state — C1).
      // P0-A: authenticate with the scoped per-agent JWT (falls back to the
      // shared token when minting is unavailable — see control-panel-auth.ts).
      const authToken = await getControlPanelToken(sessionKey);
      let data: ActiveStory;
      try {
        const resp = await fetch(
          `${cpUrl}/internal/shca/instances/${encodeURIComponent(instanceId)}/active-story`,
          { headers: { Authorization: `Bearer ${authToken}` } },
        );
        if (!resp.ok) {
          return jsonResult({
            status: "error",
            error: `could not resolve the active story (control panel returned ${resp.status}).`,
          });
        }
        data = (await resp.json()) as ActiveStory;
      } catch (err) {
        return jsonResult({
          status: "error",
          error: `could not reach the control panel to resolve the active story: ${(err as Error)?.message || err}`,
        });
      }

      // 3. C2 fail-closed ALLOW-LIST gate.
      if (!data.ok || !data.story_id || data.ambiguous) {
        return jsonResult({
          status: "error",
          error: data.ambiguous
            ? "you have more than one active story and none is marked the current focus — open the specific story first, then message its Claude Code session."
            : "no active story is assigned to you right now, so there is no Claude Code session to message.",
        });
      }
      if (!data.workspace_path) {
        return jsonResult({
          status: "error",
          error: "the story workspace is not provisioned yet; try again once it is ready.",
        });
      }
      if (data.prespawn_in_flight) {
        return jsonResult({
          status: "busy",
          reason: "the Claude Code session is still being provisioned for this story. Do not retry automatically — ask the human to resend in a moment.",
        });
      }
      // A phase task (or system gate) in flight owns the session — both the
      // planning-phase and the paused-idle allow paths require it to be clear.
      if (data.phase_in_flight) {
        return jsonResult({
          status: "busy",
          reason: "Claude Code is busy running an automated phase for this story. Do not retry automatically — tell the human it is busy and to resend later.",
        });
      }
      // Paused-idle carve-out: when execution is paused, phase dispatch is frozen
      // so the session is idle — allow the sanctioned brainstorm-while-paused
      // relay REGARDLESS of the recorded phase. Otherwise require a planning-family
      // phase; every other phase value refuses by default (fail closed).
      const phase = (data.current_phase || "").trim();
      const allowed = data.execution_paused === true || PLANNING_PHASES.has(phase);
      if (!allowed) {
        return jsonResult({
          status: "error",
          error: `Claude Code is in the "${phase || "unknown"}" phase, which runs autonomously — you cannot message it directly now. Recovery from a stalled phase is a phase retry from the task board, not a direct message.`,
        });
      }

      // 4. Spawn one turn. No taskId → this is a planning/tool turn, so C4's
      // safe resume-fallback may fork a fresh session if the recorded one is
      // provably gone. C3's per-story lock serializes against any concurrent turn.
      const result = await spawnCliDirect({
        task: message,
        // Pass agentId so spawnCliDirect DERIVES cwd = <agent-ws>/stories/<storyId>.
        // workspace_path (from the control panel) is only a fallback — its presence
        // was already checked above as the "is the workspace provisioned?" signal.
        agentId: instanceId,
        cwd: data.workspace_path,
        resumeSessionId: data.claude_session_id || undefined,
        storyId: data.story_id,
        // Session key (agent:{instanceId}:{goalId}[-{threadId}]) lets
        // spawnCliDirect mint a goal-scoped hook token for the child instead
        // of injecting the shared gateway admin token (shared-token fix).
        agentSessionKey: sessionKey,
        controlPanelUrl: cpUrl,
        internalApiKey: apiKey,
      });

      // 5. Map the spawn result onto the tool contract.
      if (result.status === "busy") {
        return jsonResult({
          status: "busy",
          reason: "Claude Code is already handling another turn for this story. Do not retry automatically — tell the human it is busy and to resend later.",
        });
      }
      if (result.status === "error") {
        // result.stderrSnippet was previously dropped here — the SHCA (and anyone
        // reading its report) only ever saw the bare "claude exited with code N",
        // with the actual crash reason nowhere to be found (not even logged; see
        // cli-spawn.ts's close handler). Include it so a real diagnosis is
        // possible without gateway/runtime log access.
        return jsonResult({
          status: "error",
          error: result.error || "Claude Code returned an error.",
          ...(result.stderrSnippet ? { stderr: result.stderrSnippet } : {}),
        });
      }
      return jsonResult({
        status: "ok",
        reply: result.text,
      });
    },
  };
}
