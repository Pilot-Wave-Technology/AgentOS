import { Type } from "@sinclair/typebox";
import type { GatewayMessageChannel } from "../../utils/message-channel.js";
import { ACP_SPAWN_MODES, ACP_SPAWN_STREAM_TARGETS, spawnAcpDirect } from "../acp-spawn.js";
import { spawnCliDirect } from "../cli-spawn.js";
import { optionalStringEnum } from "../schema/typebox.js";
import type { SpawnedToolContext } from "../spawned-context.js";
import { SUBAGENT_SPAWN_MODES, spawnSubagentDirect } from "../subagent-spawn.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult, readStringParam, ToolInputError } from "./common.js";
import { getControlPanelToken } from "./control-panel-auth.js";

const SESSIONS_SPAWN_RUNTIMES = ["subagent", "acp", "cli"] as const;
const SESSIONS_SPAWN_SANDBOX_MODES = ["inherit", "require"] as const;
const UNSUPPORTED_SESSIONS_SPAWN_PARAM_KEYS = [
  "target",
  "transport",
  "channel",
  "to",
  "threadId",
  "thread_id",
  "replyTo",
  "reply_to",
] as const;

const SessionsSpawnToolSchema = Type.Object({
  task: Type.String(),
  label: Type.Optional(Type.String()),
  runtime: optionalStringEnum(SESSIONS_SPAWN_RUNTIMES),
  agentId: Type.Optional(Type.String()),
  resumeSessionId: Type.Optional(
    Type.String({
      description:
        'Resume an existing session by its ID. For runtime="acp": a Codex/ACP session UUID. For runtime="cli": the sessionId returned from a prior runtime="cli" spawn — claude -p --resume <id> continues that conversation. Not supported for runtime="subagent".',
    }),
  ),
  model: Type.Optional(Type.String()),
  thinking: Type.Optional(Type.String()),
  cwd: Type.Optional(Type.String()),
  runTimeoutSeconds: Type.Optional(Type.Number({ minimum: 0 })),
  // Back-compat: older callers used timeoutSeconds for this tool.
  timeoutSeconds: Type.Optional(Type.Number({ minimum: 0 })),
  thread: Type.Optional(Type.Boolean()),
  mode: optionalStringEnum(SUBAGENT_SPAWN_MODES),
  cleanup: optionalStringEnum(["delete", "keep"] as const),
  sandbox: optionalStringEnum(SESSIONS_SPAWN_SANDBOX_MODES),
  streamTo: optionalStringEnum(ACP_SPAWN_STREAM_TARGETS),

  // Inline attachments (snapshot-by-value).
  // NOTE: Attachment contents are redacted from transcript persistence by sanitizeToolCallInputs.
  attachments: Type.Optional(
    Type.Array(
      Type.Object({
        name: Type.String(),
        content: Type.String(),
        encoding: Type.Optional(optionalStringEnum(["utf8", "base64"] as const)),
        mimeType: Type.Optional(Type.String()),
      }),
      { maxItems: 50 },
    ),
  ),
  attachAs: Type.Optional(
    Type.Object({
      // Where the spawned agent should look for attachments.
      // Kept as a hint; implementation materializes into the child workspace.
      mountPath: Type.Optional(Type.String()),
    }),
  ),

  // Optional progress-hook plumbing for runtime="cli". When set, the
  // story/task ids are exposed to claude-cli's PostToolUse hook via
  // env vars so the hook can post live activity to the control panel.
  storyId: Type.Optional(Type.String()),
  taskId: Type.Optional(Type.String()),
  controlPanelUrl: Type.Optional(Type.String()),
  internalApiKey: Type.Optional(Type.String()),
});

export function createSessionsSpawnTool(
  opts?: {
    agentSessionKey?: string;
    agentChannel?: GatewayMessageChannel;
    agentAccountId?: string;
    agentTo?: string;
    agentThreadId?: string | number;
    sandboxed?: boolean;
    /** Explicit agent ID override for cron/hook sessions where session key parsing may not work. */
    requesterAgentIdOverride?: string;
  } & SpawnedToolContext,
): AnyAgentTool {
  return {
    label: "Sessions",
    name: "sessions_spawn",
    description:
      'Spawn an isolated session. runtime="subagent" (default) for an openclaw subagent, runtime="acp" for an ACP backend, or runtime="cli" to invoke claude-cli (`claude -p`) one-shot in a workspace dir. For runtime="cli", pass `cwd` (the dir to run in) and optionally `resumeSessionId` to continue a prior conversation; the result includes the session id you can resume next turn.',
    parameters: SessionsSpawnToolSchema,
    execute: async (_toolCallId, args) => {
      const params = args as Record<string, unknown>;
      const unsupportedParam = UNSUPPORTED_SESSIONS_SPAWN_PARAM_KEYS.find((key) =>
        Object.hasOwn(params, key),
      );
      if (unsupportedParam) {
        throw new ToolInputError(
          `sessions_spawn does not support "${unsupportedParam}". Use "message" or "sessions_send" for channel delivery.`,
        );
      }
      const task = readStringParam(params, "task", { required: true });
      const label = typeof params.label === "string" ? params.label.trim() : "";
      const runtime =
        params.runtime === "acp"
          ? "acp"
          : params.runtime === "cli"
            ? "cli"
            : "subagent";
      const requestedAgentId = readStringParam(params, "agentId");
      const resumeSessionId = readStringParam(params, "resumeSessionId");
      const modelOverride = readStringParam(params, "model");
      const thinkingOverrideRaw = readStringParam(params, "thinking");
      const cwd = readStringParam(params, "cwd");
      const mode = params.mode === "run" || params.mode === "session" ? params.mode : undefined;
      const cleanup =
        params.cleanup === "keep" || params.cleanup === "delete" ? params.cleanup : "keep";
      const sandbox = params.sandbox === "require" ? "require" : "inherit";
      const streamTo = params.streamTo === "parent" ? "parent" : undefined;
      // Back-compat: older callers used timeoutSeconds for this tool.
      const timeoutSecondsCandidate =
        typeof params.runTimeoutSeconds === "number"
          ? params.runTimeoutSeconds
          : typeof params.timeoutSeconds === "number"
            ? params.timeoutSeconds
            : undefined;
      const runTimeoutSeconds =
        typeof timeoutSecondsCandidate === "number" && Number.isFinite(timeoutSecondsCandidate)
          ? Math.max(0, Math.floor(timeoutSecondsCandidate))
          : undefined;
      const thread = params.thread === true;
      const attachments = Array.isArray(params.attachments)
        ? (params.attachments as Array<{
            name: string;
            content: string;
            encoding?: "utf8" | "base64";
            mimeType?: string;
          }>)
        : undefined;

      if (streamTo && runtime !== "acp") {
        return jsonResult({
          status: "error",
          error: `streamTo is only supported for runtime=acp; got runtime=${runtime}`,
        });
      }

      if (resumeSessionId && runtime !== "acp" && runtime !== "cli") {
        return jsonResult({
          status: "error",
          error: `resumeSessionId is only supported for runtime=acp or runtime=cli; got runtime=${runtime}`,
        });
      }

      if (runtime === "cli") {
        // Phase 4 Stage 4 Option B: if cwd / storyId / resumeSessionId
        // are missing, resolve them from the control panel using the
        // SHCA agent's identity. SHCA's skills now call sessions_spawn
        // with just `{ runtime: "cli", task: "..." }` — the gateway
        // fills in the rest from active-story state in the DB.
        let resolvedCwd = cwd;
        let resolvedStoryId = readStringParam(params, "storyId");
        let resolvedResumeSessionId = resumeSessionId;
        const forceNewSession = (params as Record<string, unknown>)["forceNewSession"] === true;
        const sessionKey = opts?.agentSessionKey;
        const needsResolution = !resolvedCwd || !resolvedStoryId
          || (!resolvedResumeSessionId && !forceNewSession);
        if (needsResolution && sessionKey) {
          // agentSessionKey format: agent:{agentId}:{goalKey}
          const parts = sessionKey.split(":");
          if (parts.length >= 2 && parts[0] === "agent") {
            const instanceId = parts[1];
            const cpUrl = (readStringParam(params, "controlPanelUrl")
              || process.env.CONTROL_PANEL_URL
              || "").replace(/\/$/, "");
            const apiKey = readStringParam(params, "internalApiKey")
              || process.env.OPENCLAW_GATEWAY_TOKEN
              || "";
            if (cpUrl && apiKey) {
              try {
                // P0-A: scoped per-agent JWT (falls back to shared token).
                const authToken = await getControlPanelToken(sessionKey);
                const resp = await fetch(
                  `${cpUrl}/internal/shca/instances/${encodeURIComponent(instanceId)}/active-story`,
                  { headers: { Authorization: `Bearer ${authToken}` } },
                );
                if (resp.ok) {
                  const data: any = await resp.json();
                  if (!resolvedCwd && data.workspace_path) resolvedCwd = data.workspace_path;
                  if (!resolvedStoryId && data.story_id) resolvedStoryId = data.story_id;
                  if (!resolvedResumeSessionId && !forceNewSession && data.claude_session_id) {
                    resolvedResumeSessionId = data.claude_session_id;
                  }
                }
              } catch {
                // Fall through; original check below will surface a
                // helpful error if cwd is still missing.
              }
            }
          }
        }

        if (!resolvedCwd) {
          return jsonResult({
            status: "error",
            error: 'runtime="cli" requires cwd (the directory to run claude in). Could not auto-resolve from active story.',
          });
        }
        if (Array.isArray(attachments) && attachments.length > 0) {
          return jsonResult({
            status: "error",
            error: 'attachments are not supported for runtime="cli"',
          });
        }
        const taskIdHint = readStringParam(params, "taskId");
        // Gateway env fallbacks so callers (especially SHCA's planning-phase
        // spawns) don't have to know these — the gateway process already
        // has both injected by the systemd unit + control panel deploy.
        const controlPanelUrl = readStringParam(params, "controlPanelUrl")
          || process.env.CONTROL_PANEL_URL
          || "";
        const internalApiKey = readStringParam(params, "internalApiKey")
          || process.env.OPENCLAW_GATEWAY_TOKEN
          || "";
        // Agent id from the session key (agent:{agentId}:{goalKey}) so
        // spawnCliDirect can DERIVE cwd = <agent-ws>/stories/<storyId> instead of
        // trusting the resolved workspace_path — keeps this path consistent with
        // shca.claude.invoke and the claude_code_communication tool.
        const spawnAgentId = (() => {
          const p = (opts?.agentSessionKey || "").split(":");
          return p.length >= 2 && p[0] === "agent" ? p[1] : undefined;
        })();
        const result = await spawnCliDirect({
          task,
          agentId: spawnAgentId,
          cwd: resolvedCwd,
          model: modelOverride || undefined,
          resumeSessionId: resolvedResumeSessionId || undefined,
          storyId: resolvedStoryId || undefined,
          taskId: taskIdHint || undefined,
          // Session key (agent:{agentId}:{goalId}[-{threadId}]) lets
          // spawnCliDirect mint a goal-scoped hook token for the child instead
          // of injecting the shared gateway admin token (shared-token fix).
          agentSessionKey: opts?.agentSessionKey || undefined,
          controlPanelUrl: controlPanelUrl || undefined,
          internalApiKey: internalApiKey || undefined,
        });
        return jsonResult(result);
      }

      if (runtime === "acp") {
        if (Array.isArray(attachments) && attachments.length > 0) {
          return jsonResult({
            status: "error",
            error:
              "attachments are currently unsupported for runtime=acp; use runtime=subagent or remove attachments",
          });
        }
        const result = await spawnAcpDirect(
          {
            task,
            label: label || undefined,
            agentId: requestedAgentId,
            resumeSessionId,
            cwd,
            mode: mode && ACP_SPAWN_MODES.includes(mode) ? mode : undefined,
            thread,
            sandbox,
            streamTo,
          },
          {
            agentSessionKey: opts?.agentSessionKey,
            agentChannel: opts?.agentChannel,
            agentAccountId: opts?.agentAccountId,
            agentTo: opts?.agentTo,
            agentThreadId: opts?.agentThreadId,
            sandboxed: opts?.sandboxed,
          },
        );
        return jsonResult(result);
      }

      const result = await spawnSubagentDirect(
        {
          task,
          label: label || undefined,
          agentId: requestedAgentId,
          model: modelOverride,
          thinking: thinkingOverrideRaw,
          runTimeoutSeconds,
          thread,
          mode,
          cleanup,
          sandbox,
          expectsCompletionMessage: true,
          attachments,
          attachMountPath:
            params.attachAs && typeof params.attachAs === "object"
              ? readStringParam(params.attachAs as Record<string, unknown>, "mountPath")
              : undefined,
        },
        {
          agentSessionKey: opts?.agentSessionKey,
          agentChannel: opts?.agentChannel,
          agentAccountId: opts?.agentAccountId,
          agentTo: opts?.agentTo,
          agentThreadId: opts?.agentThreadId,
          agentGroupId: opts?.agentGroupId,
          agentGroupChannel: opts?.agentGroupChannel,
          agentGroupSpace: opts?.agentGroupSpace,
          requesterAgentIdOverride: opts?.requesterAgentIdOverride,
          workspaceDir: opts?.workspaceDir,
        },
      );

      return jsonResult(result);
    },
  };
}
