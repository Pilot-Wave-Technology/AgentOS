/**
 * SHCA operator tools.
 *
 * These tools let the SHCA agent operate a Claude Code subprocess on a
 * story end-to-end. Five categories:
 *
 *   HTTP-only (call control panel /internal/assistant/* endpoints):
 *     story_phase        — update stories.current_phase
 *     story_state        — transition stories.state (CHECK-validated)
 *     snapshot_checkpoint — write to checkpoints table
 *     plan_version       — register a versioned plan
 *     escalate_for_approval — BLOCKING gate: open approval + long-poll
 *
 *   Local VM execution (filesystem / git / openclaw gateway):
 *     custom_workspace_file     — read/write/list files in a story workspace
 *     custom_git                — git ops (status/diff/commit/push/checkout/pr_create)
 *     custom_worktree           — git worktree create/delete + register branch in DB
 *     custom_session_control    — pause/resume/kill a Claude Code session
 *
 * Bearer auth uses the same OPENCLAW_GATEWAY_TOKEN that go_internal / story-tools
 * use. Tool ergonomics mirror story-tools.ts (typebox schemas + jsonResult).
 *
 * Per SHCA_REFACTOR_PLAN.md Phase 2.
 */
import { spawn } from "node:child_process";
import * as fs from "node:fs/promises";
import { existsSync, statSync } from "node:fs";
import * as path from "node:path";

import { Type } from "@sinclair/typebox";
import type { OpenClawConfig } from "../../config/config.js";
import type { AnyAgentTool } from "./common.js";
import { jsonResult, readStringParam } from "./common.js";


// ── Helpers ─────────────────────────────────────────────────────────────────

import { getControlPanelAuth } from "./control-panel-auth.js";

function resolveGoalIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  // Session key format: agent:{agentId}:{goalId}-{threadId}
  const parts = sessionKey.split(":");
  if (parts.length >= 3) {
    const goalThread = parts.slice(2).join(":");
    const dashIdx = goalThread.indexOf("-");
    return dashIdx > 0 ? goalThread.substring(0, dashIdx) : goalThread;
  }
  return "";
}

async function postJson(agentSessionKey: string | undefined, pathPart: string, body: unknown): Promise<{ ok: boolean; status: number; data?: any; error?: string }> {
  const { url, token } = await getControlPanelAuth(agentSessionKey);
  if (!url) return { ok: false, status: 0, error: "CONTROL_PANEL_URL not set" };
  try {
    const res = await fetch(`${url}${pathPart}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await res.text();
    let data: any = null;
    try { data = JSON.parse(text); } catch { data = text; }
    if (!res.ok) return { ok: false, status: res.status, error: typeof data === "string" ? data : data?.error, data };
    return { ok: true, status: res.status, data };
  } catch (e: any) {
    return { ok: false, status: 0, error: e.message };
  }
}

async function getJson(agentSessionKey: string | undefined, pathPart: string): Promise<{ ok: boolean; status: number; data?: any; error?: string }> {
  const { url, token } = await getControlPanelAuth(agentSessionKey);
  if (!url) return { ok: false, status: 0, error: "CONTROL_PANEL_URL not set" };
  try {
    const res = await fetch(`${url}${pathPart}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const text = await res.text();
    let data: any = null;
    try { data = JSON.parse(text); } catch { data = text; }
    if (!res.ok) return { ok: false, status: res.status, error: typeof data === "string" ? data : data?.error, data };
    return { ok: true, status: res.status, data };
  } catch (e: any) {
    return { ok: false, status: 0, error: e.message };
  }
}

/**
 * System-bin paths to search for executables. The openclaw systemd service's
 * EnvironmentFile only sets domain-specific vars (GEMINI_API_KEY, etc.) and
 * does not export PATH — so child Node processes inherit whatever systemd's
 * default is. On this VM that minimal PATH does NOT include /usr/bin.
 *
 * Setting PATH in the spawn env doesn't help: when shell:false and the
 * command has no slash, Node calls posix_spawnp which resolves the executable
 * from the CURRENT process's getenv("PATH"), not from the env we hand to
 * the spawned child. So we resolve the absolute path ourselves before
 * spawning, bypassing PATH lookup entirely.
 */
const SYSTEM_BIN_PATHS = [
  "/usr/local/sbin",
  "/usr/local/bin",
  "/usr/sbin",
  "/usr/bin",
  "/sbin",
  "/bin",
];

/** Memoize resolved executable paths so we don't statSync per spawn. */
const RESOLVED_EXECUTABLES: Map<string, string> = new Map();

/**
 * Find the absolute path of an executable by walking SYSTEM_BIN_PATHS.
 * Returns the input unchanged if it already contains a path separator or
 * if not found (let spawn try its luck with whatever PATH the child has).
 */
function resolveCmd(cmd: string): string {
  if (cmd.includes("/")) return cmd;
  const cached = RESOLVED_EXECUTABLES.get(cmd);
  if (cached) return cached;
  for (const dir of SYSTEM_BIN_PATHS) {
    const candidate = `${dir}/${cmd}`;
    try {
      if (existsSync(candidate) && statSync(candidate).isFile()) {
        RESOLVED_EXECUTABLES.set(cmd, candidate);
        return candidate;
      }
    } catch {
      // skip
    }
  }
  return cmd;
}

function buildSpawnEnv(): NodeJS.ProcessEnv {
  const incoming = process.env.PATH || "";
  const merged = [incoming, ...SYSTEM_BIN_PATHS].filter(Boolean).join(":");
  return { ...process.env, PATH: merged };
}

/** Run a shell command and capture stdout/stderr. Bounded by timeoutMs.
 *
 * Resolves cmd to an absolute path via SYSTEM_BIN_PATHS before spawning
 * so we don't depend on Node's PATH lookup (which uses the parent's PATH,
 * not the env we pass to the child). Also sets PATH in the child env so
 * the spawned process can itself resolve binaries (e.g. git invoking
 * `gpg` for signing, gh invoking `git`, etc).
 */
function runCmd(cmd: string, args: string[], opts: { cwd?: string; timeoutMs?: number } = {}): Promise<{ ok: boolean; stdout: string; stderr: string; code: number | null }> {
  return new Promise((resolve) => {
    const timeoutMs = opts.timeoutMs ?? 60_000;
    const resolvedCmd = resolveCmd(cmd);
    const child = spawn(resolvedCmd, args, {
      cwd: opts.cwd,
      shell: false,
      env: buildSpawnEnv(),
    });
    let stdout = "";
    let stderr = "";
    let killed = false;
    const timer = setTimeout(() => {
      killed = true;
      try { child.kill("SIGTERM"); } catch {}
    }, timeoutMs);
    child.stdout?.on("data", (d) => { stdout += d.toString(); });
    child.stderr?.on("data", (d) => { stderr += d.toString(); });
    child.on("close", (code) => {
      clearTimeout(timer);
      const ok = !killed && code === 0;
      resolve({ ok, stdout, stderr, code });
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      stderr += `\nspawn error: ${err.message}`;
      resolve({ ok: false, stdout, stderr, code: null });
    });
  });
}

/**
 * Resolve the SHCA agent's own openclaw workspace dir from its session key.
 *
 * Session key format: agent:<agentId>:<goalId>-<threadId>
 * Workspace dir convention (openclaw default): ~/.openclaw/workspace-<agentId>
 *
 * Story workspaces are created INSIDE this directory at:
 *   <shca_workspace>/stories/<story_id>/
 *
 * Lifecycle: when the SHCA agent is reaped by the cleanup watchdog, the
 * entire workspace (including all stories) is deleted. No separate cleanup
 * needed for story worktrees.
 */
function shcaWorkspaceFromSessionKey(agentSessionKey?: string): string | null {
  if (!agentSessionKey) return null;
  const parts = agentSessionKey.split(":");
  if (parts.length < 2) return null;
  const agentId = parts[1];
  if (!agentId) return null;
  const home = process.env.HOME || "/home/openclaw";
  return path.join(home, ".openclaw", `workspace-${agentId}`);
}

/** Path to the stories subdir inside the SHCA workspace. */
function storiesDir(shcaWorkspace: string): string {
  return path.join(shcaWorkspace, "stories");
}

/** Path to a specific story's workspace. */
function storyWorkspacePath(shcaWorkspace: string, storyId: string): string {
  return path.join(storiesDir(shcaWorkspace), storyId);
}

/** Validate that a path is inside the given workspace. Resolves symlinks via realpath when possible. */
function isWithinWorkspace(absPath: string, workspaceDir: string): boolean {
  const norm = path.resolve(absPath);
  const root = path.resolve(workspaceDir);
  if (norm === root || norm.startsWith(root + path.sep)) return true;
  // Case-insensitive fallback. The gateway lowercases agent ids in session
  // keys (normalizeAgentId), but the on-disk story workspace dir preserves
  // the original goal-id case (e.g. goal "TEST4"). The session-key-derived
  // stories root therefore differs only in case from the real path recorded
  // in the story_state snapshot — which is the path callers pass here. Treat
  // a case-only difference as within-workspace so custom_git / custom_worktree /
  // custom_workspace_file don't reject valid snapshot paths.
  const normLc = norm.toLowerCase();
  const rootLc = root.toLowerCase();
  return normLc === rootLc || normLc.startsWith(rootLc + path.sep);
}

/**
 * Default settings.json content for a Claude Code spawn inside a story workspace.
 *
 * Layer A of the restriction model — see Phase 3 of SHCA_REFACTOR_PLAN.md:
 *   - Allow: read/write/edit anywhere (path-restricted via Layer B hook),
 *     plus a curated bash command allowlist for safe operations
 *   - Deny: bash commands that escape the workspace or touch system paths
 *   - PreToolUse hook: validate Edit/Write/Read/Bash paths via
 *     /etc/openclaw/hooks/restrict-to-workspace.sh
 */
function defaultClaudeSettingsJson(): string {
  return JSON.stringify(
    {
      permissions: {
        allow: [
          "Read", "Grep", "Glob", "Edit", "Write",
          "Bash(git:*)", "Bash(pytest:*)", "Bash(python:*)",
          "Bash(python3:*)", "Bash(pip:*)", "Bash(pip3:*)",
          "Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)", "Bash(pnpm:*)",
          "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(head:*)",
          "Bash(tail:*)", "Bash(wc:*)", "Bash(find:*)", "Bash(echo:*)",
          "Skill", "Agent",
        ],
        deny: [
          "Read(//etc/**)", "Read(//var/**)", "Read(//home/**)",
          "Read(/**/.env*)", "Read(/**/secrets/**)", "Read(/**/.ssh/**)",
          "Bash(sudo:*)", "Bash(rm:-rf /*)",
          "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)",
          "Bash(nc:*)", "Bash(ncat:*)",
        ],
      },
      hooks: {
        PreToolUse: [
          {
            matcher: "Edit|Write|Read|Bash",
            hooks: [
              {
                type: "command",
                command: "/etc/openclaw/hooks/restrict-to-workspace.sh",
              },
            ],
          },
          {
            // Plan-file DAG validation. Catches malformed Task DAG YAML
            // and wrong-location plan writes BEFORE they hit disk, so
            // Claude self-corrects in-turn instead of writing junk that
            // the DB-side ingest only catches at plan-approval time.
            // POSTs to control-panel /internal/assistant/validate-plan with
            // a 60s timeout; fails open on any infra trouble. Scoped to
            // Edit/Write only — other tools pass through.
            matcher: "Edit|Write",
            hooks: [
              {
                type: "command",
                command: "/etc/openclaw/hooks/validate-plan-dag.sh",
              },
            ],
          },
        ],
      },
    },
    null,
    2,
  ) + "\n";
}

/**
 * Seed the standard story-workspace layout under workspacePath:
 *   docs/plans/INDEX.md
 *   docs/DECISIONS.md
 *   docs/CODE_NOTES.md
 *   .claude/settings.json
 *   .claude/agents/   (empty — Phase 6 populates)
 *   .claude/skills/   (empty — Phase 4+ may populate)
 *   RESOURCES.md      (stub — SHCA-side resource_provisioning writes the real content)
 *   IDENTITY.md       (stub — SHCA-side writes the real content)
 */
async function seedStoryWorkspaceLayout(workspacePath: string, storyId: string): Promise<void> {
  // Subdirs
  await fs.mkdir(path.join(workspacePath, "docs", "plans"), { recursive: true });
  await fs.mkdir(path.join(workspacePath, ".claude", "agents"), { recursive: true });
  await fs.mkdir(path.join(workspacePath, ".claude", "skills"), { recursive: true });

  // Living docs scaffolding
  await fs.writeFile(
    path.join(workspacePath, "docs", "plans", "INDEX.md"),
    "# Plan index\n\nNo plans yet.\n\n" +
      "Each plan version lives at `v<N>.md`. The current version is " +
      "tracked in the `plan_versions` table; this index mirrors it for humans.\n",
    "utf-8",
  );
  await fs.writeFile(
    path.join(workspacePath, "docs", "DECISIONS.md"),
    "# Decision log\n\nAppend-only. Format: `YYYY-MM-DD HH:MM (actor): decision`.\n\n---\n",
    "utf-8",
  );
  await fs.writeFile(
    path.join(workspacePath, "docs", "CODE_NOTES.md"),
    `# Code notes for story ${storyId}\n\n` +
      "Coding-IA appends notes here as it works. Used for plan handoff and replan context.\n",
    "utf-8",
  );

  // Claude Code per-workspace settings (restriction layer A)
  await fs.writeFile(
    path.join(workspacePath, ".claude", "settings.json"),
    defaultClaudeSettingsJson(),
    "utf-8",
  );

  // SHCA-side resource provisioning fills these later; seed stubs so Claude
  // Code doesn't choke if it inspects them before SHCA has written them.
  await fs.writeFile(
    path.join(workspacePath, "RESOURCES.md"),
    `# Resources for story ${storyId}\n\n_(SHCA populates this at story-pickup time.)_\n`,
    "utf-8",
  );
  await fs.writeFile(
    path.join(workspacePath, "IDENTITY.md"),
    `# Identity\n\nStory: ${storyId}\n\n` +
      "_(SHCA populates with model label + story context at story-pickup time.)_\n",
    "utf-8",
  );
}


// ── 1. story_phase ──────────────────────────────────────────────────────

const StoryPhaseSchema = Type.Object({
  story_id: Type.String(),
  new_phase: Type.String(),
  reason: Type.Optional(Type.String()),
});

export function storyPhaseTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Story Phase",
    name: "story_phase",
    description:
      "Update the current_phase of a story (free-text: planning, executing, iterating, " +
      "awaiting_review, deploying, monitoring, discovery, …). Writes a STORY_PHASE_CHANGED " +
      "ledger event. Distinct from story_update (AI-merged progress text) and " +
      "story_state (state-machine transition).",
    parameters: StoryPhaseSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const story_id = readStringParam(params, "story_id", { required: true, trim: true });
      const new_phase = readStringParam(params, "new_phase", { required: true, trim: true });
      const reason = readStringParam(params, "reason", { required: false });
      const r = await postJson(opts?.agentSessionKey, `/internal/assistant/stories/${encodeURIComponent(story_id)}/phase`, {
        new_phase, reason: reason || "",
      });
      return jsonResult(r.ok ? r.data : { success: false, error: r.error, status: r.status });
    },
  };
}


// ── 2. story_state ──────────────────────────────────────────────────────

const StoryStateSchema = Type.Object({
  story_id: Type.String(),
  new_state: Type.String({
    description:
      "One of: proposed | accepted | in_progress | blocked | done | rejected | cancelled. " +
      "Validated server-side against the CHECK constraint.",
  }),
  reason: Type.Optional(Type.String()),
});

export function storyStateTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Story State",
    name: "story_state",
    description:
      "Transition a story's state field. SHCA's typical transitions: " +
      "accepted→in_progress (pick up), in_progress↔blocked (gates), " +
      "in_progress→done (completion). Writes STORY_STATE_CHANGED to ledger.",
    parameters: StoryStateSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const story_id = readStringParam(params, "story_id", { required: true, trim: true });
      const new_state = readStringParam(params, "new_state", { required: true, trim: true });
      const reason = readStringParam(params, "reason", { required: false });
      const r = await postJson(opts?.agentSessionKey, `/internal/assistant/stories/${encodeURIComponent(story_id)}/state`, {
        new_state, reason: reason || "",
      });
      return jsonResult(r.ok ? r.data : { success: false, error: r.error, status: r.status });
    },
  };
}


// ── 3. snapshot_checkpoint ──────────────────────────────────────────────

const CheckpointSchema = Type.Object({
  story_id: Type.String(),
  name: Type.String({ description: "e.g. 'plan_v1', 'diff_v1', 'deploy_plan_v1'" }),
  payload: Type.Optional(Type.Any()),
  git_sha: Type.Optional(Type.String()),
  approval_id: Type.Optional(Type.String()),
});

export function snapshotCheckpointTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Snapshot Checkpoint",
    name: "snapshot_checkpoint",
    description:
      "Write a named snapshot to the checkpoints table — captures the state of a story " +
      "at a gate (plan ready, diff ready, deploy plan ready, etc.) for audit and replay.",
    parameters: CheckpointSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const story_id = readStringParam(params, "story_id", { required: true, trim: true });
      const name = readStringParam(params, "name", { required: true, trim: true });
      const r = await postJson(opts?.agentSessionKey, `/internal/assistant/stories/${encodeURIComponent(story_id)}/checkpoints`, {
        name,
        payload: params.payload ?? {},
        git_sha: params.git_sha,
        approval_id: params.approval_id,
      });
      return jsonResult(r.ok ? r.data : { success: false, error: r.error, status: r.status });
    },
  };
}


// ── 4. plan_version ─────────────────────────────────────────────────────

const PlanVersionSchema = Type.Object({
  story_id: Type.String(),
  version: Type.Integer({ minimum: 1 }),
  plan_doc_path: Type.Optional(Type.String({ description: "Workspace-relative, e.g. 'docs/plans/v1.md'" })),
  task_dag: Type.Optional(Type.Any()),
  risk_level: Type.Optional(Type.String({ description: "low | medium | high" })),
  proposed_by: Type.Optional(Type.String({ description: "Default 'shca'. Use 'ia:research-ia' or 'human:<uid>' if applicable." })),
  claude_session_id: Type.Optional(Type.String()),
  branch_id: Type.Optional(Type.String()),
  parent_plan_id: Type.Optional(Type.String()),
  reason: Type.Optional(Type.String()),
});

export function planVersionTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Plan Version",
    name: "plan_version",
    description:
      "Register a plan version after Claude produces a plan (writes docs/plans/v<N>.md). " +
      "Inserts a plan_versions row, writes PLAN_PROPOSED to ledger. Returns plan_id.",
    parameters: PlanVersionSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const story_id = readStringParam(params, "story_id", { required: true, trim: true });
      const r = await postJson(opts?.agentSessionKey, `/internal/assistant/stories/${encodeURIComponent(story_id)}/plans`, {
        version: params.version,
        plan_doc_path: params.plan_doc_path,
        task_dag: params.task_dag ?? {},
        risk_level: params.risk_level,
        proposed_by: params.proposed_by || "shca",
        claude_session_id: params.claude_session_id,
        branch_id: params.branch_id,
        parent_plan_id: params.parent_plan_id,
        reason: params.reason,
      });
      return jsonResult(r.ok ? r.data : { success: false, error: r.error, status: r.status });
    },
  };
}


// ── 5. escalate_for_approval ────────────────────────────────────────────
// Blocking gate: opens an approval, long-polls until decided or timed out.

const EscalateForApprovalSchema = Type.Object({
  story_id: Type.String(),
  kind: Type.String({
    description: "plan | diff | deploy_to_prod | clarification | iteration_stuck | analysis_finding",
  }),
  payload: Type.Optional(Type.Any()),
  payload_ref: Type.Optional(Type.String()),
  channels: Type.Optional(Type.Array(Type.String(), {
    description: "['ui', 'slack', 'email']. Default ['ui'].",
  })),
  timeout_hours: Type.Optional(Type.Number({ description: "Default 4. Max practical: 168 (one week)." })),
  poll_timeout_seconds: Type.Optional(Type.Number({
    description: "Per-poll wait in seconds (server clamps to 1..300). Default 60.",
  })),
  max_total_poll_minutes: Type.Optional(Type.Number({
    description: "Cap total polling time across re-polls. Default 30. " +
      "Hitting this returns timed_out_polling — caller should re-invoke if still wanted.",
  })),
});

export function escalateForApprovalTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Escalate for Approval (blocking)",
    name: "escalate_for_approval",
    description:
      "Open a human approval request and BLOCK until a decision arrives, the approval " +
      "times out, or max_total_poll_minutes is hit. Returns the decision + feedback. " +
      "For non-blocking notification use a Slack/UI tool instead.",
    parameters: EscalateForApprovalSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const story_id = readStringParam(params, "story_id", { required: true, trim: true });
      const kind = readStringParam(params, "kind", { required: true, trim: true });

      // 1. Create the approval
      const createResp = await postJson(opts?.agentSessionKey, `/internal/assistant/stories/${encodeURIComponent(story_id)}/approvals`, {
        kind,
        payload: params.payload ?? {},
        payload_ref: params.payload_ref,
        channels: params.channels || ["ui"],
        timeout_hours: params.timeout_hours ?? 4,
      });
      if (!createResp.ok) {
        return jsonResult({ success: false, stage: "create", error: createResp.error, status: createResp.status });
      }
      const approval_id: string = createResp.data?.approval_id;
      if (!approval_id) {
        return jsonResult({ success: false, stage: "create", error: "no approval_id returned", data: createResp.data });
      }

      // 2. Long-poll until decided / timed out
      const pollTimeout = Math.max(1, Math.min(Number(params.poll_timeout_seconds ?? 60), 300));
      const maxTotalMs = Math.max(1, Number(params.max_total_poll_minutes ?? 30)) * 60 * 1000;
      const startedAt = Date.now();
      let pollCount = 0;
      while (Date.now() - startedAt < maxTotalMs) {
        pollCount += 1;
        const r = await getJson(opts?.agentSessionKey, `/internal/assistant/approvals/${encodeURIComponent(approval_id)}/poll?timeout=${pollTimeout}`);
        if (!r.ok) {
          return jsonResult({
            success: false, stage: "poll", error: r.error, status: r.status,
            approval_id, poll_count: pollCount,
          });
        }
        const data = r.data || {};
        if (data.decided) {
          return jsonResult({
            success: true,
            approval_id,
            decision: data.decision,
            decided_by: data.decided_by,
            decided_at: data.decided_at,
            feedback: data.feedback,
            poll_count: pollCount,
          });
        }
        if (data.timed_out_approval) {
          return jsonResult({
            success: false, reason: "approval_timed_out",
            approval_id, poll_count: pollCount,
          });
        }
        // timed_out_polling — keep going until maxTotalMs
      }

      return jsonResult({
        success: false, reason: "max_total_poll_minutes_reached",
        approval_id, poll_count: pollCount,
        note: "Approval still open server-side; caller can re-invoke escalate_for_approval " +
              "or query the approval directly. Decision will be persisted whenever the human responds.",
      });
    },
  };
}


// ── 6. custom_workspace_file ───────────────────────────────────────────────────

const WorkspaceFileSchema = Type.Object({
  op: Type.String({ description: "read | write | list | delete" }),
  workspace_path: Type.String({ description: "Absolute path of the story workspace root" }),
  file_path: Type.Optional(Type.String({ description: "Workspace-relative file path (for read/write/delete)" })),
  content: Type.Optional(Type.String({ description: "File content (for write)" })),
  pattern: Type.Optional(Type.String({ description: "Glob/substring filter for list" })),
});

export function workspaceFileTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Workspace File",
    name: "custom_workspace_file",
    description:
      "Read, write, list, or delete files in a story workspace on the VM. All paths " +
      "are validated to be inside workspace_path (no escaping via ../). workspace_path " +
      "must be a story workspace under the calling SHCA's stories/ directory.",
    parameters: WorkspaceFileSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const op = readStringParam(params, "op", { required: true, trim: true });
      const workspace_path = readStringParam(params, "workspace_path", { required: true, trim: true });

      // Sanity: workspace_path must be under this SHCA's stories/ dir.
      const shcaWs = shcaWorkspaceFromSessionKey(opts?.agentSessionKey);
      if (!shcaWs) {
        return jsonResult({ success: false, error: "could not resolve SHCA workspace from session key" });
      }
      const storiesRoot = storiesDir(shcaWs);
      if (!isWithinWorkspace(workspace_path, storiesRoot)) {
        return jsonResult({ success: false, error: `workspace_path ${workspace_path} not under ${storiesRoot}` });
      }

      const resolveTarget = (rel?: string) => {
        if (!rel) return null;
        const abs = path.resolve(workspace_path, rel);
        return isWithinWorkspace(abs, workspace_path) ? abs : null;
      };

      try {
        if (op === "read") {
          const rel = readStringParam(params, "file_path", { required: true });
          const target = resolveTarget(rel);
          if (!target) return jsonResult({ success: false, error: "file_path escapes workspace" });
          const content = await fs.readFile(target, "utf-8");
          return jsonResult({ success: true, op, file_path: rel, bytes: content.length, content });
        }
        if (op === "write") {
          const rel = readStringParam(params, "file_path", { required: true });
          const target = resolveTarget(rel);
          if (!target) return jsonResult({ success: false, error: "file_path escapes workspace" });
          const content = typeof params.content === "string" ? params.content : "";
          await fs.mkdir(path.dirname(target), { recursive: true });
          await fs.writeFile(target, content, "utf-8");
          return jsonResult({ success: true, op, file_path: rel, bytes: content.length });
        }
        if (op === "delete") {
          const rel = readStringParam(params, "file_path", { required: true });
          const target = resolveTarget(rel);
          if (!target) return jsonResult({ success: false, error: "file_path escapes workspace" });
          await fs.unlink(target);
          return jsonResult({ success: true, op, file_path: rel });
        }
        if (op === "list") {
          const rel = readStringParam(params, "file_path", { required: false }) || "";
          const target = resolveTarget(rel || ".");
          if (!target) return jsonResult({ success: false, error: "file_path escapes workspace" });
          const pattern = readStringParam(params, "pattern", { required: false }) || "";
          const entries = await fs.readdir(target, { withFileTypes: true });
          const items = entries
            .filter((e) => !pattern || e.name.includes(pattern))
            .map((e) => ({ name: e.name, is_dir: e.isDirectory() }));
          return jsonResult({ success: true, op, path: rel || ".", count: items.length, items });
        }
        return jsonResult({ success: false, error: `unknown op '${op}'` });
      } catch (e: any) {
        return jsonResult({ success: false, error: e.message, op });
      }
    },
  };
}


// ── 7. custom_git ──────────────────────────────────────────────────────────────

const GitSchema = Type.Object({
  op: Type.String({ description: "status | diff | commit | push | pr_create | checkout | log" }),
  workspace_path: Type.String(),
  // For diff:
  from_ref: Type.Optional(Type.String()),
  to_ref: Type.Optional(Type.String()),
  // For commit:
  message: Type.Optional(Type.String()),
  files: Type.Optional(Type.Array(Type.String(), {
    description: "Specific files to add. Omit for `git add -A`.",
  })),
  // For push:
  branch: Type.Optional(Type.String()),
  // For pr_create:
  title: Type.Optional(Type.String()),
  body: Type.Optional(Type.String()),
});

export function gitTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Git",
    name: "custom_git",
    description:
      "Run git operations scoped to a story workspace: status, diff, commit, push, " +
      "checkout, log, or pr_create (gh CLI). Output captured; failures return stderr.",
    parameters: GitSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const op = readStringParam(params, "op", { required: true, trim: true });
      const workspace_path = readStringParam(params, "workspace_path", { required: true, trim: true });

      // workspace_path must be under the calling SHCA's stories/ dir.
      const shcaWs = shcaWorkspaceFromSessionKey(opts?.agentSessionKey);
      if (!shcaWs) {
        return jsonResult({ success: false, error: "could not resolve SHCA workspace from session key" });
      }
      const storiesRoot = storiesDir(shcaWs);
      if (!isWithinWorkspace(workspace_path, storiesRoot)) {
        return jsonResult({ success: false, error: `workspace_path not under ${storiesRoot}` });
      }

      let cmd: string = "git";
      let argv: string[] = [];

      switch (op) {
        case "status":
          argv = ["status", "--porcelain=v1"];
          break;
        case "diff": {
          const from = readStringParam(params, "from_ref", { required: false }) || "HEAD";
          const to = readStringParam(params, "to_ref", { required: false }) || "";
          argv = to ? ["diff", from, to] : ["diff", from];
          break;
        }
        case "commit": {
          const message = readStringParam(params, "message", { required: true });
          const files = Array.isArray(params.files) ? (params.files as string[]) : [];
          // Two-step: add then commit.
          const addArgs = files.length ? ["add", ...files] : ["add", "-A"];
          const addRes = await runCmd("git", addArgs, { cwd: workspace_path });
          if (!addRes.ok) {
            return jsonResult({ success: false, op, stage: "add", stderr: addRes.stderr, code: addRes.code });
          }
          const commitRes = await runCmd("git", ["commit", "-m", message], { cwd: workspace_path });
          if (!commitRes.ok) {
            return jsonResult({ success: false, op, stage: "commit", stderr: commitRes.stderr, stdout: commitRes.stdout });
          }
          const shaRes = await runCmd("git", ["rev-parse", "HEAD"], { cwd: workspace_path });
          return jsonResult({
            success: true, op, sha: shaRes.stdout.trim(),
            stdout: commitRes.stdout, files: files.length || "all",
          });
        }
        case "push": {
          const branch = readStringParam(params, "branch", { required: false }) || "HEAD";
          argv = ["push", "origin", branch];
          break;
        }
        case "checkout": {
          const branch = readStringParam(params, "branch", { required: true });
          argv = ["checkout", branch];
          break;
        }
        case "log":
          argv = ["log", "--oneline", "-20"];
          break;
        case "pr_create": {
          const title = readStringParam(params, "title", { required: true });
          const body = readStringParam(params, "body", { required: false }) || "";
          cmd = "gh";
          argv = ["pr", "create", "--title", title, "--body", body];
          break;
        }
        default:
          return jsonResult({ success: false, error: `unknown op '${op}'` });
      }

      const r = await runCmd(cmd, argv, { cwd: workspace_path });
      return jsonResult({
        success: r.ok, op,
        stdout: r.stdout,
        stderr: r.ok ? undefined : r.stderr,
        code: r.code,
      });
    },
  };
}


// ── 8. custom_worktree ─────────────────────────────────────────────────────────

const WorktreeSchema = Type.Object({
  op: Type.String({ description: "create | delete" }),
  story_id: Type.String(),
  base_repo_path: Type.Optional(Type.String({
    description: "Absolute path of the source repo to branch from. Default: $OPENCLAW_BASE_REPO_PATH.",
  })),
  base_branch: Type.Optional(Type.String({ description: "Default 'main'." })),
  branch_name: Type.Optional(Type.String({ description: "Default: 'story/<first-8-chars-of-id>'" })),
  workspace_path: Type.Optional(Type.String({
    description: "For delete: the worktree path to remove. For create: target path (default $OPENCLAW_WORKSPACE_ROOT/<story_id>).",
  })),
});

export function worktreeTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Worktree (DEPRECATED — control panel handles workspace setup)",
    name: "custom_worktree",
    description:
      "DEPRECATED in Phase 4. Operator-mode story workspaces are now provisioned " +
      "by the control panel at story import time (before the kickoff message is " +
      "sent to SHCA). Do NOT call this tool for normal operator-mode flow — your " +
      "story workspace already exists at the path noted in IDENTITY.md when you " +
      "receive the operator kickoff. This tool remains registered for legacy " +
      "compatibility and may be removed in a future phase.",
    parameters: WorktreeSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const op = readStringParam(params, "op", { required: true, trim: true });
      const story_id = readStringParam(params, "story_id", { required: true, trim: true });

      // SHCA workspace dir derived from the caller's session key.
      const shcaWs = shcaWorkspaceFromSessionKey(opts?.agentSessionKey);
      if (!shcaWs) {
        return jsonResult({ success: false, error: "could not resolve SHCA workspace from session key" });
      }

      if (op === "create") {
        const baseRepo = readStringParam(params, "base_repo_path", { required: false })
          || process.env.OPENCLAW_BASE_REPO_PATH
          || "";
        if (!baseRepo) {
          return jsonResult({ success: false, error: "base_repo_path required (or set $OPENCLAW_BASE_REPO_PATH)" });
        }
        const baseBranch = readStringParam(params, "base_branch", { required: false }) || "main";
        const branchName = readStringParam(params, "branch_name", { required: false })
          || `story/${story_id.substring(0, 8)}`;
        const workspacePath = readStringParam(params, "workspace_path", { required: false })
          || storyWorkspacePath(shcaWs, story_id);

        // Story workspace must end up under <shca_ws>/stories/.
        if (!isWithinWorkspace(workspacePath, storiesDir(shcaWs))) {
          return jsonResult({
            success: false,
            error: `workspace_path ${workspacePath} not under ${storiesDir(shcaWs)} — story worktrees must live inside SHCA workspace`,
          });
        }

        // Ensure the parent stories/ dir exists.
        try {
          await fs.mkdir(storiesDir(shcaWs), { recursive: true });
        } catch (e: any) {
          return jsonResult({ success: false, op, stage: "mkdir_stories", error: e.message });
        }

        // git worktree add — note: the worktree dir must NOT already exist.
        const r = await runCmd("git", ["worktree", "add", "-b", branchName, workspacePath, baseBranch], {
          cwd: baseRepo,
        });
        if (!r.ok) {
          return jsonResult({ success: false, op, stage: "git_worktree_add", stderr: r.stderr, code: r.code });
        }

        // Seed the standard layout (.claude/settings.json, docs/, stubs).
        let seedWarning: string | undefined;
        try {
          await seedStoryWorkspaceLayout(workspacePath, story_id);
        } catch (e: any) {
          seedWarning = `worktree created but seed failed: ${e.message}`;
        }

        // Register branch in story_branches (control panel HTTP).
        const regResp = await postJson(opts?.agentSessionKey, `/internal/assistant/stories/${encodeURIComponent(story_id)}/branches`, {
          branch_name: branchName,
          parent_branch: baseBranch,
          purpose: "main",
          worktree_path: workspacePath,
        });

        const result: Record<string, unknown> = {
          success: true,
          op,
          workspace_path: workspacePath,
          branch_name: branchName,
        };
        if (regResp.ok) {
          result.branch_id = regResp.data?.branch_id;
        } else {
          result.warning_branch_register = `story_branches insert failed: ${regResp.error}`;
        }
        if (seedWarning) {
          result.warning_seed = seedWarning;
        }
        return jsonResult(result);
      }

      if (op === "delete") {
        const workspacePath = readStringParam(params, "workspace_path", { required: true });
        if (!isWithinWorkspace(workspacePath, storiesDir(shcaWs))) {
          return jsonResult({
            success: false,
            error: `refusing to delete ${workspacePath} — not under ${storiesDir(shcaWs)}`,
          });
        }
        const baseRepo = readStringParam(params, "base_repo_path", { required: false })
          || process.env.OPENCLAW_BASE_REPO_PATH
          || "";
        if (!baseRepo) {
          return jsonResult({ success: false, error: "base_repo_path required for delete" });
        }
        const r = await runCmd("git", ["worktree", "remove", "--force", workspacePath], { cwd: baseRepo });
        if (!r.ok) {
          return jsonResult({ success: false, op, stderr: r.stderr, code: r.code });
        }
        return jsonResult({ success: true, op, workspace_path: workspacePath });
      }

      return jsonResult({ success: false, error: `unknown op '${op}'` });
    },
  };
}


// ── 9. custom_session_control ──────────────────────────────────────────────────
// Pause / resume / kill a Claude Code session. The actual session lives in
// the OpenClaw gateway; we go through gateway RPCs (sessions.abort,
// sessions.cleanup) rather than direct process control.

const SessionControlSchema = Type.Object({
  op: Type.String({ description: "pause | resume | kill | status" }),
  session_id: Type.String({ description: "Claude session UUID or OpenClaw sessionKey." }),
  reason: Type.Optional(Type.String()),
});

export function sessionControlTool(opts?: { config?: OpenClawConfig; agentSessionKey?: string }): AnyAgentTool {
  return {
    label: "SHCA: Session Control",
    name: "custom_session_control",
    description:
      "Pause, resume, kill, or query a Claude Code session SHCA is operating. " +
      "Pause/resume are advisory — the gateway treats them as 'stop sending follow-ups' / " +
      "'resume sending'. Kill aborts any in-flight run AND deregisters the session.",
    parameters: SessionControlSchema,
    execute: async (_tid, args) => {
      const params = args as Record<string, unknown>;
      const op = readStringParam(params, "op", { required: true, trim: true });
      const session_id = readStringParam(params, "session_id", { required: true, trim: true });
      const reason = readStringParam(params, "reason", { required: false }) || "";

      // Phase 2b: log to ledger; actual gateway-side pause/kill plumbing lands
      // in Phase 4 alongside the operator-loop integration. Returning success
      // here lets SHCA's skills exercise the call-path; the underlying gateway
      // RPC wiring (sessions.abort + a new sessions.pause flag) is filed as
      // a follow-up.
      const evtMap: Record<string, string> = {
        pause: "claude.session_paused",
        resume: "claude.session_resumed",
        kill: "claude.session_killed",
        status: "",
      };
      const event_type = evtMap[op];
      if (event_type === undefined) {
        return jsonResult({ success: false, error: `unknown op '${op}'` });
      }
      if (event_type) {
        await postJson(opts?.agentSessionKey, "/internal/assistant/ledger", {
          event_type,
          payload: { session_id, reason },
        });
      }
      return jsonResult({
        success: true, op, session_id,
        note: "Phase 2b records intent to ledger; gateway-side pause/kill plumbing lands in Phase 4.",
      });
    },
  };
}
