/**
 * cli-spawn.ts — minimal "spawn claude-cli for one turn" used by
 * sessions_spawn(runtime="cli").
 *
 * Each invocation runs `claude -p` once. First call (no resumeSessionId)
 * creates a fresh session; subsequent calls with resumeSessionId continue
 * the same conversation. claude-cli persists session state under
 * ~/.claude/projects/ keyed by the session id.
 *
 * Output format: stream-json. We collect the full output, parse line-by-
 * line, extract the final assistant text + any tool calls + the session
 * id, and return a structured result.
 *
 * This intentionally does NOT use runCliAgent / supervisor / enqueueCliRun.
 * Those are for openclaw-managed CLI IAs that participate in the gateway
 * lifecycle. The SHCA-operates-Claude flow is simpler: one short-lived
 * claude -p invocation per turn, no openclaw session tracking.
 */
import { spawn } from "node:child_process";
import { existsSync, statSync, realpathSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

import { createSubsystemLogger } from "../logging/subsystem.js";
import { loadConfig } from "../config/config.js";
import { resolveAgentWorkspaceDir } from "./agent-scope.js";
import { normalizeAgentId } from "../routing/session-key.js";
import { classifyFailoverReason } from "./pi-embedded-helpers/errors.js";
import { sanitizeEnvVars } from "./sandbox/sanitize-env-vars.js";
import { getControlPanelToken } from "./tools/control-panel-auth.js";

const log = createSubsystemLogger("agent/cli-spawn");

// Kill switch — live claude children keyed by taskId (phase turns) and storyId
// (SHCA planning turns that have no taskId). Both maps give the control panel a
// handle to abort a hung turn via shca.claude.abort. Entries exist only while
// a turn is in flight — added on spawn, removed on close/error.
const _childrenByTask  = new Map<string, ReturnType<typeof spawn>>();
const _childrenByStory = new Map<string, ReturnType<typeof spawn>>();

// C3 per-story turn lock: at most ONE `claude -p --resume` in flight per story
// (or per resume-session id when there is no story). Two concurrent resumes of
// the same session corrupt the transcript → OFF_PROTOCOL. Both the phase
// dispatcher (shca.claude.invoke) and the claude_code_communication tool go
// through spawnCliDirect, so this single set serializes them. Distinct from the
// abort maps above (those are per-task/story handles for the kill switch).
const _turnInFlightByStory = new Set<string>();

function _killChild(child: ReturnType<typeof spawn>, label: string): void {
  log.warn(`abort: SIGTERM claude child for ${label} (pid=${child.pid ?? "?"})`);
  try { child.kill("SIGTERM"); } catch { /* ignore */ }
  setTimeout(() => {
    // Still tracked means it never exited cleanly — escalate to SIGKILL.
    if ([..._childrenByTask.values(), ..._childrenByStory.values()].includes(child)) {
      log.warn(`abort: SIGKILL claude child for ${label} (did not exit after SIGTERM)`);
      try { child.kill("SIGKILL"); } catch { /* ignore */ }
    }
  }, 5000);
}

/**
 * Force-kill the in-flight claude child for a phase task. Returns true if a
 * live child was found and signalled, false if there was nothing to kill.
 */
export function abortCliByTask(taskId: string): boolean {
  const child = taskId ? _childrenByTask.get(taskId) : undefined;
  if (!child) {
    log.info(`abortCliByTask: no live claude child for task=${taskId}`);
    return false;
  }
  _killChild(child, `task=${taskId}`);
  return true;
}

/**
 * Force-kill the in-flight claude child for a story (e.g. a SHCA planning turn
 * that has no taskId). Returns true if a live child was found and signalled.
 */
export function abortCliByStory(storyId: string): boolean {
  const child = storyId ? _childrenByStory.get(storyId) : undefined;
  if (!child) {
    log.info(`abortCliByStory: no live claude child for story=${storyId}`);
    return false;
  }
  _killChild(child, `story=${storyId}`);
  return true;
}

const CLAUDE_CANDIDATES = [
  "/usr/local/bin/claude",
  "/usr/bin/claude",
  "/home/openclaw/.local/bin/claude",
  "/home/openclaw/.npm-global/bin/claude",
];

let _claudeBinCached: string | null | undefined;

function resolveClaudeBin(): string | null {
  if (_claudeBinCached !== undefined) return _claudeBinCached;
  for (const c of CLAUDE_CANDIDATES) {
    try {
      if (existsSync(c) && statSync(c).isFile()) {
        _claudeBinCached = c;
        return c;
      }
    } catch {
      // ignore
    }
  }
  _claudeBinCached = null;
  return null;
}

export interface SpawnCliParams {
  task: string;                     // the prompt / message to send
  cwd: string;                      // fallback working dir; OVERRIDDEN by the derived
                                    // <agent-ws>/stories/<storyId> when agentId+storyId are set
  agentId?: string;                 // with storyId, the cwd is DERIVED (the gateway owns
                                    // the layout) so a stale caller cwd can never misroute
  model?: string;                   // optional model override (e.g., "claude-opus-4-7")
  resumeSessionId?: string;         // continue a prior session
  appendSystemPrompt?: string;      // appended to claude's default system prompt
  timeoutMs?: number;               // hard wall-clock timeout (default 30 min)
  // Progress-hook env vars: when set, the PostToolUse hook posts to
  // <controlPanelUrl>/internal/shca/stories/<storyId>/tool-progress on
  // every tool call so the UI can render live activity cards.
  storyId?: string;
  taskId?: string;
  controlPanelUrl?: string;
  internalApiKey?: string;
  // Goal identity for the scoped-token fix. When goalId (or agentSessionKey,
  // format agent:{agentId}:{goalId}[-{threadId}]) is known, the child's hook
  // token is a per-agent, goal-scoped, short-lived JWT minted by the control
  // panel instead of the shared gateway admin token — so an agent that reads
  // OPENCLAW_GATEWAY_TOKEN via Bash gets a low-value, expiring, goal-bound
  // credential, never the operator.admin token. Falls back to internalApiKey
  // (shared) when scoped minting is unavailable (see control-panel-auth.ts).
  goalId?: string;
  agentSessionKey?: string;
}

export interface CliToolCall {
  id?: string;
  name: string;
  input?: unknown;
}

export interface SpawnCliResult {
  status: "ok" | "error" | "busy";
  sessionId: string;
  text: string;                     // final assistant text
  toolCalls: CliToolCall[];         // tool calls Claude made during the turn
  exitCode: number | null;
  stderrSnippet?: string;
  durationMs: number;
  error?: string;
}

/**
 * Spawn one turn of claude -p, serialized per story (C3).
 *
 * If a turn is already in flight for this story (or resume-session id), returns
 * status "busy" WITHOUT spawning a second concurrent `claude -p --resume` — two
 * concurrent resumes of the same session corrupt the transcript. The lock is
 * released in `finally` on EVERY exit: normal completion, error result, early
 * return, a thrown spawn error, and timeout/abort (which resolve via the child's
 * close handler). The has()->add() pair is synchronous (no await between), so two
 * concurrent callers cannot both acquire.
 */
export async function spawnCliDirect(params: SpawnCliParams): Promise<SpawnCliResult> {
  const lockKey = params.storyId || params.resumeSessionId || "";
  if (lockKey && _turnInFlightByStory.has(lockKey)) {
    return {
      status: "busy",
      sessionId: params.resumeSessionId ?? "",
      text: "",
      toolCalls: [],
      exitCode: null,
      durationMs: 0,
      error: "a claude turn is already in flight for this story",
    };
  }
  if (lockKey) _turnInFlightByStory.add(lockKey);
  try {
    return await _spawnCliDirectInner(params);
  } finally {
    if (lockKey) _turnInFlightByStory.delete(lockKey);
  }
}

/** Spawn one turn of claude -p in stream-json mode. */
async function _spawnCliDirectInner(params: SpawnCliParams): Promise<SpawnCliResult> {
  const started = Date.now();
  const claudeBin = resolveClaudeBin();
  if (!claudeBin) {
    return {
      status: "error",
      sessionId: params.resumeSessionId ?? "",
      text: "",
      toolCalls: [],
      exitCode: null,
      durationMs: 0,
      error: `claude binary not found in ${CLAUDE_CANDIDATES.join(", ")}`,
    };
  }

  // claude -p creates a session uuid if one isn't supplied. We want to
  // know the session id deterministically so SHCA can resume it, so we
  // generate one upfront for fresh sessions.
  const sessionId = params.resumeSessionId || randomUUID();

  const args: string[] = [
    "-p",
    "--output-format", "stream-json",
    "--verbose",
    "--permission-mode", "bypassPermissions",
  ];
  if (params.resumeSessionId) {
    args.push("--resume", params.resumeSessionId);
  } else {
    args.push("--session-id", sessionId);
  }
  if (params.model) {
    args.push("--model", params.model);
  }
  if (params.appendSystemPrompt) {
    args.push("--append-system-prompt", params.appendSystemPrompt);
  }
  // Prompt MUST be last; claude -p reads it as a positional arg.
  args.push(params.task);

  // Derive the authoritative cwd from (agentId, storyId). The gateway OWNS the
  // workspace layout — an operator story always lives at <agent-ws>/stories/<storyId>
  // — so the cwd is a pure function of those two ids. Deriving here, at the single
  // spawn chokepoint, makes it structurally impossible for ANY caller (phase
  // dispatch, prespawn, claude_code_communication, sessions_spawn) to misroute the
  // session by passing a stale/empty/misresolved workspace_path. The caller's `cwd`
  // is a fallback used only when we can't derive (no agentId/storyId, or the config
  // lookup throws). This is the "always writes to the correct paths" guarantee.
  let effectiveCwd = params.cwd;
  if (params.agentId && params.storyId) {
    try {
      const cfg = loadConfig();
      const wsRaw = resolveAgentWorkspaceDir(cfg, normalizeAgentId(params.agentId));
      let agentWs: string;
      try { agentWs = realpathSync(wsRaw); } catch { agentWs = path.resolve(wsRaw); }
      const derived = path.join(agentWs, "stories", params.storyId);
      if (params.cwd && path.resolve(params.cwd) !== derived) {
        log.warn(
          `cli-spawn: caller cwd=${params.cwd} != derived story workspace ${derived} `
          + `(agent=${params.agentId} story=${params.storyId}); using the derived path. `
          + `Investigate stories.workspace_path for this story.`,
        );
      }
      effectiveCwd = derived;
    } catch (err) {
      log.warn(
        `cli-spawn: could not derive story workspace (agent=${params.agentId} `
        + `story=${params.storyId}): ${(err as Error)?.message || err}; `
        + `falling back to caller cwd=${params.cwd}`,
      );
    }
  }

  const cwdResolved = path.resolve(effectiveCwd);

  // Invariant: an operator/story turn MUST run inside the story workspace
  // (<agent-ws>/stories/<storyId>) — never the SHCA agent root, the gateway
  // process cwd (which is what path.resolve("") silently yields), or another
  // story's dir. A misrouted cwd corrupts the workspace invisibly: claude finds
  // no repo/ at the wrong root, clones a stray one there, and writes plans/edits
  // the diff-review gate (which only reads <story>/repo) never sees. Every spawn
  // path (shca.claude.invoke dispatch + prespawn, and the claude_code_communication
  // tool) passes storyId, so this one check fails-safe for all of them. Refuse
  // rather than run — a clear error surfaces the bad workspace_path instead of
  // silently checking out at the wrong root.
  if (params.storyId) {
    const expectedSuffix = `${path.sep}stories${path.sep}${params.storyId}`;
    if (!cwdResolved.endsWith(expectedSuffix)) {
      log.warn(
        `cli-spawn: REFUSING misrouted spawn — cwd=${cwdResolved} is not the `
        + `story workspace for story=${params.storyId} (expected suffix `
        + `${expectedSuffix}). Aborting to avoid a stray checkout at the wrong root.`,
      );
      return {
        status: "error",
        sessionId,
        text: "",
        toolCalls: [],
        exitCode: null,
        durationMs: 0,
        error:
          `refusing to spawn: cwd '${cwdResolved}' is not the story workspace for `
          + `story '${params.storyId}' (expected .../stories/${params.storyId}). `
          + `The story's workspace_path is wrong or the workspace is not provisioned.`,
      };
    }
  }

  if (!existsSync(cwdResolved)) {
    return {
      status: "error",
      sessionId,
      text: "",
      toolCalls: [],
      exitCode: null,
      durationMs: 0,
      error: `cwd does not exist: ${cwdResolved}`,
    };
  }

  // ── Scoped hook credential (shared-token exfiltration fix) ──────────
  // The spawned `claude` child has a Bash tool, so ANYTHING in its env is
  // readable via `echo`. Historically we injected the shared gateway admin
  // token (OPENCLAW_GATEWAY_TOKEN) for the hooks — meaning an agent could
  // exfiltrate operator.admin over the entire gateway. Instead mint a
  // per-agent, goal-scoped, ~1h JWT (the control panel dual-accepts it on
  // /internal/*). getControlPanelToken() FAILS SAFE to the shared token when
  // scoped minting is unavailable, so behaviour is preserved where the flag
  // is off (see control-panel-auth.ts). Only the CHILD gets this token; the
  // gateway's own in-process calls (usage/session below) keep internalApiKey.
  let hookToken = params.internalApiKey || "";
  {
    // getControlPanelToken derives agentId from a session key. Tool callers
    // pass one directly; the RPC path passes agentId+goalId, so synthesize it.
    const sessionKeyForToken = params.agentSessionKey
      || (params.agentId && params.goalId
        ? `agent:${normalizeAgentId(params.agentId)}:${params.goalId}`
        : undefined);
    if (sessionKeyForToken) {
      try {
        const t = await getControlPanelToken(sessionKeyForToken, params.goalId);
        if (t) hookToken = t;
      } catch (err) {
        log.warn(
          `cli-spawn: scoped token mint failed, using shared token: `
          + `${(err as Error)?.message || err}`,
        );
      }
    }
  }

  // ── Sanitized child environment (secret exfiltration fix) ───────────
  // Do NOT spread the gateway's full process.env into the child: it carries
  // OPENCLAW_GATEWAY_TOKEN, GEMINI_API_KEY and other secrets a Bash-capable
  // agent could echo. Strip everything credential-shaped, then re-add ONLY
  // the Anthropic model creds the claude CLI itself needs to authenticate
  // (it reads these from env — see infra .../startup.sh.tpl /etc/openclaw/env).
  // NOTE: ANTHROPIC_API_KEY stays reachable by the agent's Bash; fully closing
  // that requires an LLM auth proxy (ANTHROPIC_BASE_URL) — tracked as a
  // follow-up. The high-value gateway/admin + Gemini tokens are NOT re-added.
  const rawEnv: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (typeof v === "string") rawEnv[k] = v;
  }
  const childBaseEnv = sanitizeEnvVars(rawEnv, { strictMode: false }).allowed;
  for (const k of ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]) {
    const v = process.env[k];
    if (typeof v === "string" && v) childBaseEnv[k] = v;
  }

  return new Promise((resolve) => {
    const timeoutMs = params.timeoutMs ?? 30 * 60 * 1000;
    let stdoutBuf = "";
    let stderrBuf = "";
    let killed = false;

    const hookEnv: Record<string, string> = {};
    if (params.storyId) hookEnv.AGENT_STORY_ID = params.storyId;
    if (params.taskId) hookEnv.AGENT_TASK_ID = params.taskId;
    if (params.controlPanelUrl) hookEnv.CONTROL_PANEL_URL = params.controlPanelUrl;
    // The hooks authenticate to /internal/* as OPENCLAW_GATEWAY_TOKEN. This is
    // the SCOPED per-agent JWT resolved above (not the shared admin token),
    // falling back to the shared token only when minting is unavailable.
    if (hookToken) {
      hookEnv.OPENCLAW_GATEWAY_TOKEN = hookToken;
    }

    const child = spawn(claudeBin, args, {
      cwd: cwdResolved,
      env: {
        ...childBaseEnv,
        // claude reads .claude/settings.json from CLAUDE_PROJECT_DIR (or
        // from cwd if unset). The caller is expected to pass cwd as the
        // dir that contains .claude/ — i.e. the story workspace root.
        CLAUDE_PROJECT_DIR: cwdResolved,
        // The PreToolUse hook (restrict-to-workspace.sh) reads this to
        // bound Edit/Write/Read/Bash paths. Without it, the hook fails
        // open, so set it to the same dir we point claude at.
        CLAUDE_WORKSPACE_ROOT: cwdResolved,
        // PostToolUse progress hook reads these to POST live activity
        // cards back to the control panel.
        ...hookEnv,
      },
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    });

    // Kill switch — register under taskId (phase turns) or storyId (planning turns
    // with no taskId) so shca.claude.abort can reach this child. Both are cleaned
    // up in the close and error handlers below.
    if (params.taskId) _childrenByTask.set(params.taskId, child);
    else if (params.storyId) _childrenByStory.set(params.storyId, child);

    const timer = setTimeout(() => {
      killed = true;
      try { child.kill("SIGTERM"); } catch { /* ignore */ }
      setTimeout(() => {
        try { child.kill("SIGKILL"); } catch { /* ignore */ }
      }, 5000);
    }, timeoutMs);

    // Parse stdout line-by-line so we can POST incremental token usage after
    // each assistant LLM response instead of waiting for the whole turn to finish.
    let _lineBuf = "";
    child.stdout.on("data", (d) => {
      const chunk = d.toString();
      stdoutBuf += chunk;
      _lineBuf  += chunk;
      const lines = _lineBuf.split("\n");
      _lineBuf = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const event = JSON.parse(line) as Record<string, unknown>;
          // stream-json emits an "assistant" event after each LLM API call.
          // Its message.usage field has the per-call token counts.
          if (
            event.type === "assistant" &&
            event.message &&
            typeof event.message === "object" &&
            (event.message as Record<string, unknown>).usage
          ) {
            _postIncrementalUsage(params, (event.message as Record<string, unknown>).usage as Record<string, number>, hookToken).catch(
              (err) => log.debug(`incremental usage POST failed: ${err}`),
            );
          }
        } catch { /* non-JSON line — skip */ }
      }
    });
    child.stderr.on("data", (d) => { stderrBuf += d.toString(); });

    child.on("error", (err) => {
      clearTimeout(timer);
      if (params.taskId) _childrenByTask.delete(params.taskId);
      else if (params.storyId) _childrenByStory.delete(params.storyId);
      log.warn(`claude spawn error: ${err.message}`);
      resolve({
        status: "error",
        sessionId,
        text: "",
        toolCalls: [],
        exitCode: null,
        durationMs: Date.now() - started,
        error: `spawn error: ${err.message}`,
      });
    });

    child.on("close", (code) => {
      clearTimeout(timer);
      if (params.taskId) _childrenByTask.delete(params.taskId);
      else if (params.storyId) _childrenByStory.delete(params.storyId);
      const parsed = parseStreamJson(stdoutBuf);
      const finalSessionId = parsed.sessionId || sessionId;
      const status: "ok" | "error" = killed
        ? "error"
        : code === 0
          ? "ok"
          : "error";

      // C4 safe resume-fallback: a `--resume` that failed because the session is
      // PROVABLY gone (workspace recreated, transcript pruned, id expired —
      // stderr matches the session_expired classifier) transparently forks a
      // FRESH session and retries once in the same turn. Guards, all required:
      //   - !killed              — a timeout/abort is not a stale session.
      //   - params.resumeSessionId — only the resume path can be stale.
      //   - !params.taskId       — PLANNING/tool turns only. A phase turn (taskId
      //                            set) must NEVER fork: it would silently drop
      //                            the plan/prior context the DAG depends on, so
      //                            it surfaces the error and the dispatcher decides.
      //   - storyId + controlPanelUrl + internalApiKey — the persistence path must
      //                            be available, else recordClaudeSession can't
      //                            store the new id and every future turn would
      //                            resume the SAME stale id and fork again (silent
      //                            per-turn context churn). Missing → surface error.
      //   - classifyFailoverReason(stderr) === "session_expired" — POSITIVE match
      //                            only; ambiguous failures return error and leave
      //                            current_claude_session_id untouched (no context loss).
      // The retry calls _spawnCliDirectInner directly (no resumeSessionId), so it
      // runs inside the SAME C3 lock and its own close handler records the fresh
      // id. resumeSessionId is undefined on the retry, so this guard cannot re-fire
      // → no recursion.
      if (
        status === "error"
        && !killed
        && params.resumeSessionId
        && !params.taskId
        && params.storyId
        && params.controlPanelUrl
        && params.internalApiKey
        && classifyFailoverReason(stderrBuf) === "session_expired"
      ) {
        log.warn(
          `CLAUDE_SESSION_FORKED: resume of session=${params.resumeSessionId} for `
          + `story=${params.storyId} failed as session_expired (exit=${code}); `
          + `forking a fresh session in the same turn`,
        );
        _spawnCliDirectInner({ ...params, resumeSessionId: undefined })
          .then(resolve)
          .catch((err) => {
            log.warn(`CLAUDE_SESSION_FORKED: fresh-session retry threw: ${err?.message || err}`);
            resolve({
              status: "error",
              sessionId: finalSessionId,
              text: parsed.text,
              toolCalls: parsed.toolCalls,
              exitCode: code,
              stderrSnippet: stderrBuf.slice(0, 1000) || undefined,
              durationMs: Date.now() - started,
              error: `resume failed (session_expired); fresh-session retry threw: ${err?.message || err}`,
            });
          });
        return;
      }

      // Persist sessionId to the control panel — best-effort, fire-and-
      // forget. So plan-approval doesn't need to scan the disk to
      // recover what we already know.
      if (
        status === "ok"
        && finalSessionId
        && params.storyId
        && params.controlPanelUrl
        && params.internalApiKey
      ) {
        recordClaudeSession({
          controlPanelUrl: params.controlPanelUrl,
          internalApiKey: hookToken,  // P0-A: scoped per-agent JWT (falls back to shared)
          storyId: params.storyId,
          sessionId: finalSessionId,
        }).catch((err) => {
          log.warn(
            `cli-spawn: failed to record session_id=${finalSessionId} for story=${params.storyId}: ${err?.message || err}`,
          );
        });
      }

      resolve({
        status,
        sessionId: finalSessionId,
        text: parsed.text,
        toolCalls: parsed.toolCalls,
        exitCode: code,
        stderrSnippet: stderrBuf.slice(0, 1000) || undefined,
        durationMs: Date.now() - started,
        error: killed
          ? `timed out after ${timeoutMs}ms`
          : code !== 0
            ? `claude exited with code ${code}`
            : undefined,
      });
    });
  });
}

interface ParsedStreamOutput {
  text: string;
  toolCalls: CliToolCall[];
  sessionId: string;
}

/**
 * Parse claude -p --output-format stream-json output.
 *
 * stream-json emits one JSON object per line. Shape varies — typical
 * events include `system`, `assistant`, `user`, `result`. We accumulate
 * assistant text and tool_use blocks; capture session_id from the
 * `system` init event when present.
 */
function parseStreamJson(raw: string): ParsedStreamOutput {
  let text = "";
  const toolCalls: CliToolCall[] = [];
  let sessionId = "";

  const lines = raw.split("\n");
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let event: any;
    try {
      event = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (!event || typeof event !== "object") continue;

    // Session id is typically emitted in a "system" init event.
    if (typeof event.session_id === "string" && !sessionId) {
      sessionId = event.session_id;
    }

    // Assistant text + tool_use blocks live inside message.content[].
    const message = event.message;
    if (message && Array.isArray(message.content)) {
      for (const block of message.content) {
        if (!block || typeof block !== "object") continue;
        if (block.type === "text" && typeof block.text === "string") {
          text += block.text;
        } else if (block.type === "tool_use" && typeof block.name === "string") {
          toolCalls.push({
            id: typeof block.id === "string" ? block.id : undefined,
            name: block.name,
            input: block.input,
          });
        }
      }
    }

    // Some stream-json variants emit a final `result` event with the
    // full assistant text at top level.
    if (event.type === "result" && typeof event.result === "string" && !text) {
      text = event.result;
    }
  }

  return { text: text.trim(), toolCalls, sessionId };
}


/** Fire-and-forget POST of per-call token usage to the control panel.
 * Called after each assistant LLM event in the stream-json output so the
 * dashboard shows cost building up through a session rather than only at end.
 */
async function _postIncrementalUsage(
  params: SpawnCliParams,
  usage: Record<string, number>,
  authToken?: string,
): Promise<void> {
  // P0-A: prefer the scoped per-agent JWT (hookToken) over the shared token.
  const token = authToken || params.internalApiKey;
  if (!params.storyId || !params.controlPanelUrl || !token) return;

  const url = `${params.controlPanelUrl.replace(/\/$/, "")}/internal/assistant/stories/${encodeURIComponent(params.storyId)}/token-usage`;
  const body = JSON.stringify({
    task_id:     params.taskId ?? "",
    session_key: params.resumeSessionId ?? "",
    provider:    "claude-cli",
    model:       params.model ?? "sonnet",
    usage: {
      input_tokens:               usage["input_tokens"]                ?? 0,
      output_tokens:              usage["output_tokens"]               ?? 0,
      thinking_tokens:            usage["thinking_tokens"]             ?? 0,
      cache_read_input_tokens:    usage["cache_read_input_tokens"]     ?? 0,
      cache_creation_input_tokens: usage["cache_creation_input_tokens"] ?? 0,
    },
  });

  if (typeof (globalThis as any).fetch !== "function") return;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    await (globalThis as any).fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type":  "application/json",
      },
      body,
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
  }
}


async function recordClaudeSession(params: {
  controlPanelUrl: string;
  internalApiKey: string;
  storyId: string;
  sessionId: string;
}): Promise<void> {
  const url = `${params.controlPanelUrl.replace(/\/$/, "")}/internal/assistant/stories/${encodeURIComponent(params.storyId)}/claude-session`;
  const body = JSON.stringify({ session_id: params.sessionId });

  // 5s timeout — non-blocking, fire-and-forget caller. Node 20+ has
  // built-in fetch; fall back to no-op if fetch is unavailable.
  if (typeof (globalThis as any).fetch !== "function") {
    return;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const resp = await (globalThis as any).fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${params.internalApiKey}`,
        "Content-Type": "application/json",
      },
      body,
      signal: controller.signal,
    });
    if (!resp.ok) {
      log.warn(`recordClaudeSession: HTTP ${resp.status} for ${params.storyId}`);
    }
  } finally {
    clearTimeout(timer);
  }
}
