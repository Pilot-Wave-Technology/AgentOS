import fs from "node:fs/promises";
import path from "node:path";
import {
  listAgentIds,
  resolveAgentDir,
  resolveAgentWorkspaceDir,
} from "../../agents/agent-scope.js";
import {
  DEFAULT_AGENTS_FILENAME,
  DEFAULT_BOOTSTRAP_FILENAME,
  DEFAULT_HEARTBEAT_FILENAME,
  DEFAULT_IDENTITY_FILENAME,
  DEFAULT_MEMORY_ALT_FILENAME,
  DEFAULT_MEMORY_FILENAME,
  DEFAULT_RESOURCES_FILENAME,
  DEFAULT_SOUL_FILENAME,
  DEFAULT_TOOLS_FILENAME,
  DEFAULT_USER_FILENAME,
  ensureAgentWorkspace,
  isWorkspaceOnboardingCompleted,
} from "../../agents/workspace.js";
import { movePathToTrash } from "../../browser/trash.js";
import {
  applyAgentConfig,
  findAgentEntryIndex,
  listAgentEntries,
  pruneAgentConfig,
} from "../../commands/agents.config.js";
import { loadConfig, writeConfigFile } from "../../config/config.js";
import { createSubsystemLogger } from "../../logging/subsystem.js";
import { resolveSessionTranscriptsDirForAgent } from "../../config/sessions/paths.js";
import { sameFileIdentity } from "../../infra/file-identity.js";
import { SafeOpenError, readLocalFileSafely, writeFileWithinRoot } from "../../infra/fs-safe.js";
import { assertNoPathAliasEscape } from "../../infra/path-alias-guards.js";
import { isNotFoundPathError } from "../../infra/path-guards.js";
import { DEFAULT_AGENT_ID, normalizeAgentId } from "../../routing/session-key.js";
import { resolveUserPath } from "../../utils.js";
import { archiveAgentToBucket } from "./agents-archive.js";
import {
  ErrorCodes,
  errorShape,
  formatValidationErrors,
  validateAgentsArchiveAndDeleteParams,
  validateAgentsCreateParams,
  validateAgentsDeleteParams,
  validateAgentsFilesGetParams,
  validateAgentsFilesListParams,
  validateAgentsFilesSetParams,
  validateAgentsFilesDeleteParams,
  validateAgentsSkillsSetParams,
  validateAgentsSkillsDeleteParams,
  validateShcaWorkspaceSetupParams,
  validateShcaWorkspaceDeleteStoryParams,
  validateShcaWorkspaceListFilesParams,
  validateShcaWorkspaceReadFileParams,
  validateShcaWorkspaceWriteFileParams,
  validateShcaWorkspaceDeleteFileParams,
  validateShcaWorkspaceGitCommitParams,
  validateShcaWorkspaceGitDiffParams,
  validateShcaWorkspaceSyncBranchParams,
  validateShcaWorkspacePullBranchParams,
  validateShcaWorkspaceRefreshTokenParams,
  validateShcaWorkspaceFindClaudeSessionParams,
  validateShcaClaudeInvokeParams,
  validateAgentsListParams,
  validateAgentsUpdateParams,
} from "../protocol/index.js";
import { listAgentsForGateway } from "../session-utils.js";
import type { GatewayRequestHandlers, RespondFn } from "./types.js";

// Audit trace for consolidation-prompt re-pushes: logs the prompt through
// agents.update (received -> post-applyAgentConfig -> persisted). Added
// 2026-06-12 while chasing an apparent "re-push doesn't stick" report that
// turned out to be an id-truncation measurement artifact (the gateway stores
// long ids lowercased+truncated, so jq by the full id missed the entry — see
// reference_gateway_agent_id_truncation). Kept because it's low-volume (fires
// only on prompt pushes) and logs the real (normalized/truncated) agent id.
// Grep the VM log: grep '[config-write]' /var/log/openclaw-18789.log
const cfgWriteLog = createSubsystemLogger("config-write");

/** Short marker for a consolidation prompt: v1 / v2 / other / ABSENT + length.
 *  v2 (hardened) contains "There is NO condition under which you skip"; v1
 *  contains "reply [NO_REPLY] immediately without writing". */
function consolidationMarker(value: unknown): string {
  if (typeof value !== "string") return "consolidation=ABSENT";
  const tag = /There is NO condition under which you skip/i.test(value)
    ? "v2"
    : /reply \[NO_REPLY\] immediately without writing/i.test(value)
      ? "v1"
      : "other";
  return `consolidation=${tag}(len=${value.length})`;
}

/** Pull an agent entry's compaction.memoryFlush.consolidationSystemPrompt out of
 *  a config object, by agent id, without assuming a typed shape. */
function entryConsolidationSystemPrompt(cfg: unknown, agentId: string): unknown {
  const list = (cfg as { agents?: { list?: unknown } } | undefined)?.agents?.list;
  if (!Array.isArray(list)) return undefined;
  const entry = list.find((a) => (a as { id?: string } | null)?.id === agentId) as
    | { compaction?: { memoryFlush?: { consolidationSystemPrompt?: unknown } } }
    | undefined;
  return entry?.compaction?.memoryFlush?.consolidationSystemPrompt;
}

const BOOTSTRAP_FILE_NAMES = [
  DEFAULT_AGENTS_FILENAME,
  DEFAULT_SOUL_FILENAME,
  DEFAULT_TOOLS_FILENAME,
  DEFAULT_IDENTITY_FILENAME,
  DEFAULT_USER_FILENAME,
  DEFAULT_HEARTBEAT_FILENAME,
  DEFAULT_BOOTSTRAP_FILENAME,
  DEFAULT_RESOURCES_FILENAME,
] as const;
const BOOTSTRAP_FILE_NAMES_POST_ONBOARDING = BOOTSTRAP_FILE_NAMES.filter(
  (name) => name !== DEFAULT_BOOTSTRAP_FILENAME,
);

const MEMORY_FILE_NAMES = [DEFAULT_MEMORY_FILENAME, DEFAULT_MEMORY_ALT_FILENAME] as const;

const ALLOWED_FILE_NAMES = new Set<string>([...BOOTSTRAP_FILE_NAMES, ...MEMORY_FILE_NAMES]);

// Files under docs/ are goal-resource documents uploaded by humans (via the
// control panel, Slack, or the chat paperclip). Admit any filename with one
// of these extensions — the host control panel enforces what gets uploaded
// (see app/services/resource_upload.py ALLOWED_FILE_TYPES), so we just check
// the extension here and guard against path escapes.
const ALLOWED_DOC_EXTENSIONS = new Set<string>([
  "md", "txt", "json", "yaml", "yml", "csv", "tsv",
  "html", "htm", "xml", "log", "rst", "ini", "toml",
  "sql", "sh", "env", "conf",
  "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt",
  "png", "jpg", "jpeg", "gif", "webp",
]);

function isAllowedDocsPath(name: string): boolean {
  // Must be docs/<filename> — no nested dirs, no path escapes.
  if (!name.startsWith("docs/")) return false;
  const rest = name.slice("docs/".length);
  if (!rest || rest.includes("/") || rest.includes("\\")) return false;
  if (rest.includes("..")) return false;
  const dot = rest.lastIndexOf(".");
  if (dot <= 0) return false;
  const ext = rest.slice(dot + 1).toLowerCase();
  return ALLOWED_DOC_EXTENSIONS.has(ext);
}

// Story-scoped writes: operator stories live at <workspace>/stories/<storyId>/.
// The control panel writes the per-story RESOURCES.md (and pre-downloaded
// documents) here so the coding Claude subprocess (cwd = story dir) and the
// control-panel side panel see the same resource set as the SHCA root —
// from BOTH the setup path and mid-session refreshes (which can't re-run the
// destructive shca.workspace.setup). Accepts:
//   stories/<storyId>/<bootstrap-or-memory-name>   (e.g. RESOURCES.md, IDENTITY.md)
//   stories/<storyId>/docs/<filename.ext>          (allowed doc extension)
// Same path-escape + extension guards as isAllowedDocsPath; storyId is
// constrained to a safe charset and "." path segments are rejected.
function isAllowedStoryFilePath(name: string): boolean {
  if (name.includes("\\") || name.includes("..")) return false;
  const m = /^stories\/([A-Za-z0-9._-]+)\/(.+)$/.exec(name);
  if (!m) return false;
  const sub = m[2];
  if (ALLOWED_FILE_NAMES.has(sub)) return true;
  if (sub.startsWith("docs/")) {
    const rest = sub.slice("docs/".length);
    if (!rest || rest.includes("/")) return false;
    const dot = rest.lastIndexOf(".");
    if (dot <= 0) return false;
    return ALLOWED_DOC_EXTENSIONS.has(rest.slice(dot + 1).toLowerCase());
  }
  return false;
}

function resolveAgentWorkspaceFileOrRespondError(
  params: Record<string, unknown>,
  respond: RespondFn,
): {
  cfg: ReturnType<typeof loadConfig>;
  agentId: string;
  workspaceDir: string;
  name: string;
} | null {
  const cfg = loadConfig();
  const rawAgentId = params.agentId;
  const agentId = resolveAgentIdOrError(
    typeof rawAgentId === "string" || typeof rawAgentId === "number" ? String(rawAgentId) : "",
    cfg,
  );
  if (!agentId) {
    respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "unknown agent id"));
    return null;
  }
  const rawName = params.name;
  const name = (
    typeof rawName === "string" || typeof rawName === "number" ? String(rawName) : ""
  ).trim();
  if (!ALLOWED_FILE_NAMES.has(name) && !isAllowedDocsPath(name) && !isAllowedStoryFilePath(name)) {
    respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, `unsupported file "${name}"`));
    return null;
  }
  const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
  return { cfg, agentId, workspaceDir, name };
}

type FileMeta = {
  size: number;
  updatedAtMs: number;
};

type ResolvedAgentWorkspaceFilePath =
  | {
      kind: "ready";
      requestPath: string;
      ioPath: string;
      workspaceReal: string;
    }
  | {
      kind: "missing";
      requestPath: string;
      ioPath: string;
      workspaceReal: string;
    }
  | {
      kind: "invalid";
      requestPath: string;
      reason: string;
    };

type ResolvedWorkspaceFilePath = Exclude<ResolvedAgentWorkspaceFilePath, { kind: "invalid" }>;

function resolveNotFoundWorkspaceFilePathResult(params: {
  error: unknown;
  allowMissing: boolean;
  requestPath: string;
  ioPath: string;
  workspaceReal: string;
}): Extract<ResolvedAgentWorkspaceFilePath, { kind: "missing" | "invalid" }> | undefined {
  if (!isNotFoundPathError(params.error)) {
    return undefined;
  }
  if (params.allowMissing) {
    return {
      kind: "missing",
      requestPath: params.requestPath,
      ioPath: params.ioPath,
      workspaceReal: params.workspaceReal,
    };
  }
  return { kind: "invalid", requestPath: params.requestPath, reason: "file not found" };
}

function resolveWorkspaceFilePathResultOrThrow(params: {
  error: unknown;
  allowMissing: boolean;
  requestPath: string;
  ioPath: string;
  workspaceReal: string;
}): Extract<ResolvedAgentWorkspaceFilePath, { kind: "missing" | "invalid" }> {
  const notFoundResult = resolveNotFoundWorkspaceFilePathResult(params);
  if (notFoundResult) {
    return notFoundResult;
  }
  throw params.error;
}

async function resolveWorkspaceRealPath(workspaceDir: string): Promise<string> {
  try {
    return await fs.realpath(workspaceDir);
  } catch {
    return path.resolve(workspaceDir);
  }
}

async function resolveAgentWorkspaceFilePath(params: {
  workspaceDir: string;
  name: string;
  allowMissing: boolean;
}): Promise<ResolvedAgentWorkspaceFilePath> {
  const requestPath = path.join(params.workspaceDir, params.name);
  const workspaceReal = await resolveWorkspaceRealPath(params.workspaceDir);
  const candidatePath = path.resolve(workspaceReal, params.name);

  try {
    await assertNoPathAliasEscape({
      absolutePath: candidatePath,
      rootPath: workspaceReal,
      boundaryLabel: "workspace root",
    });
  } catch (error) {
    return {
      kind: "invalid",
      requestPath,
      reason: error instanceof Error ? error.message : "path escapes workspace root",
    };
  }

  const notFoundContext = {
    allowMissing: params.allowMissing,
    requestPath,
    workspaceReal,
  } as const;

  let candidateLstat: Awaited<ReturnType<typeof fs.lstat>>;
  try {
    candidateLstat = await fs.lstat(candidatePath);
  } catch (err) {
    return resolveWorkspaceFilePathResultOrThrow({
      error: err,
      ...notFoundContext,
      ioPath: candidatePath,
    });
  }

  if (candidateLstat.isSymbolicLink()) {
    let targetReal: string;
    try {
      targetReal = await fs.realpath(candidatePath);
    } catch (err) {
      return resolveWorkspaceFilePathResultOrThrow({
        error: err,
        ...notFoundContext,
        ioPath: candidatePath,
      });
    }
    let targetStat: Awaited<ReturnType<typeof fs.stat>>;
    try {
      targetStat = await fs.stat(targetReal);
    } catch (err) {
      return resolveWorkspaceFilePathResultOrThrow({
        error: err,
        ...notFoundContext,
        ioPath: targetReal,
      });
    }
    if (!targetStat.isFile()) {
      return { kind: "invalid", requestPath, reason: "path is not a regular file" };
    }
    if (targetStat.nlink > 1) {
      return { kind: "invalid", requestPath, reason: "hardlinked file path not allowed" };
    }
    return { kind: "ready", requestPath, ioPath: targetReal, workspaceReal };
  }

  if (!candidateLstat.isFile()) {
    return { kind: "invalid", requestPath, reason: "path is not a regular file" };
  }
  if (candidateLstat.nlink > 1) {
    return { kind: "invalid", requestPath, reason: "hardlinked file path not allowed" };
  }

  const targetReal = await fs.realpath(candidatePath).catch(() => candidatePath);
  return { kind: "ready", requestPath, ioPath: targetReal, workspaceReal };
}

async function statFileSafely(filePath: string): Promise<FileMeta | null> {
  try {
    const [stat, lstat] = await Promise.all([fs.stat(filePath), fs.lstat(filePath)]);
    if (lstat.isSymbolicLink() || !stat.isFile()) {
      return null;
    }
    if (stat.nlink > 1) {
      return null;
    }
    if (!sameFileIdentity(stat, lstat)) {
      return null;
    }
    return {
      size: stat.size,
      updatedAtMs: Math.floor(stat.mtimeMs),
    };
  } catch {
    return null;
  }
}

async function listAgentFiles(workspaceDir: string, options?: { hideBootstrap?: boolean }) {
  const files: Array<{
    name: string;
    path: string;
    missing: boolean;
    size?: number;
    updatedAtMs?: number;
  }> = [];

  const bootstrapFileNames = options?.hideBootstrap
    ? BOOTSTRAP_FILE_NAMES_POST_ONBOARDING
    : BOOTSTRAP_FILE_NAMES;
  for (const name of bootstrapFileNames) {
    const resolved = await resolveAgentWorkspaceFilePath({
      workspaceDir,
      name,
      allowMissing: true,
    });
    const filePath = resolved.requestPath;
    const meta =
      resolved.kind === "ready"
        ? await statFileSafely(resolved.ioPath)
        : resolved.kind === "missing"
          ? null
          : null;
    if (meta) {
      files.push({
        name,
        path: filePath,
        missing: false,
        size: meta.size,
        updatedAtMs: meta.updatedAtMs,
      });
    } else {
      files.push({ name, path: filePath, missing: true });
    }
  }

  const primaryResolved = await resolveAgentWorkspaceFilePath({
    workspaceDir,
    name: DEFAULT_MEMORY_FILENAME,
    allowMissing: true,
  });
  const primaryMeta =
    primaryResolved.kind === "ready" ? await statFileSafely(primaryResolved.ioPath) : null;
  if (primaryMeta) {
    files.push({
      name: DEFAULT_MEMORY_FILENAME,
      path: primaryResolved.requestPath,
      missing: false,
      size: primaryMeta.size,
      updatedAtMs: primaryMeta.updatedAtMs,
    });
  } else {
    const altMemoryResolved = await resolveAgentWorkspaceFilePath({
      workspaceDir,
      name: DEFAULT_MEMORY_ALT_FILENAME,
      allowMissing: true,
    });
    const altMeta =
      altMemoryResolved.kind === "ready" ? await statFileSafely(altMemoryResolved.ioPath) : null;
    if (altMeta) {
      files.push({
        name: DEFAULT_MEMORY_ALT_FILENAME,
        path: altMemoryResolved.requestPath,
        missing: false,
        size: altMeta.size,
        updatedAtMs: altMeta.updatedAtMs,
      });
    } else {
      files.push({
        name: DEFAULT_MEMORY_FILENAME,
        path: primaryResolved.requestPath,
        missing: true,
      });
    }
  }

  return files;
}

function resolveAgentIdOrError(agentIdRaw: string, cfg: ReturnType<typeof loadConfig>) {
  const agentId = normalizeAgentId(agentIdRaw);
  const allowed = new Set(listAgentIds(cfg));
  if (!allowed.has(agentId)) {
    return null;
  }
  return agentId;
}

function sanitizeIdentityLine(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function resolveOptionalStringParam(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function respondInvalidMethodParams(
  respond: RespondFn,
  method: string,
  errors: Parameters<typeof formatValidationErrors>[0],
): void {
  respond(
    false,
    undefined,
    errorShape(
      ErrorCodes.INVALID_REQUEST,
      `invalid ${method} params: ${formatValidationErrors(errors)}`,
    ),
  );
}

function isConfiguredAgent(cfg: ReturnType<typeof loadConfig>, agentId: string): boolean {
  return findAgentEntryIndex(listAgentEntries(cfg), agentId) >= 0;
}

function respondAgentNotFound(respond: RespondFn, agentId: string): void {
  respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, `agent "${agentId}" not found`));
}

async function moveToTrashBestEffort(pathname: string): Promise<void> {
  if (!pathname) {
    return;
  }
  try {
    await fs.access(pathname);
  } catch {
    return;
  }
  try {
    await movePathToTrash(pathname);
  } catch {
    // Best-effort: path may already be gone or trash unavailable.
  }
}

function respondWorkspaceFileInvalid(respond: RespondFn, name: string, reason: string): void {
  respond(
    false,
    undefined,
    errorShape(ErrorCodes.INVALID_REQUEST, `unsafe workspace file "${name}" (${reason})`),
  );
}

async function resolveWorkspaceFilePathOrRespond(params: {
  respond: RespondFn;
  workspaceDir: string;
  name: string;
}): Promise<ResolvedWorkspaceFilePath | undefined> {
  const resolvedPath = await resolveAgentWorkspaceFilePath({
    workspaceDir: params.workspaceDir,
    name: params.name,
    allowMissing: true,
  });
  if (resolvedPath.kind === "invalid") {
    respondWorkspaceFileInvalid(params.respond, params.name, resolvedPath.reason);
    return undefined;
  }
  return resolvedPath;
}

function respondWorkspaceFileUnsafe(respond: RespondFn, name: string): void {
  respond(
    false,
    undefined,
    errorShape(ErrorCodes.FILE_UNSAFE_PATH, `unsafe workspace file "${name}"`, {
      details: { file: name, reason: "path_traversal_or_symlink_escape" },
    }),
  );
}

function respondWorkspaceFileWriteError(respond: RespondFn, name: string, reason?: string): void {
  respond(
    false,
    undefined,
    errorShape(ErrorCodes.FILE_WRITE_ERROR, `failed to write workspace file "${name}"${reason ? `: ${reason}` : ""}`, {
      details: { file: name, reason: reason ?? "io_error" },
      retryable: true,
      retryAfterMs: 1000,
    }),
  );
}

function respondWorkspaceFileMissing(params: {
  respond: RespondFn;
  agentId: string;
  workspaceDir: string;
  name: string;
  filePath: string;
}): void {
  params.respond(
    true,
    {
      agentId: params.agentId,
      workspace: params.workspaceDir,
      file: { name: params.name, path: params.filePath, missing: true },
    },
    undefined,
  );
}

export const agentsHandlers: GatewayRequestHandlers = {
  "agents.list": ({ params, respond }) => {
    if (!validateAgentsListParams(params)) {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          `invalid agents.list params: ${formatValidationErrors(validateAgentsListParams.errors)}`,
        ),
      );
      return;
    }

    const cfg = loadConfig();
    const result = listAgentsForGateway(cfg);
    respond(true, result, undefined);
  },
  "agents.create": async ({ params, respond }) => {
    if (!validateAgentsCreateParams(params)) {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          `invalid agents.create params: ${formatValidationErrors(
            validateAgentsCreateParams.errors,
          )}`,
        ),
      );
      return;
    }

    const cfg = loadConfig();
    const rawName = String(params.name ?? "").trim();
    const agentId = normalizeAgentId(rawName);
    if (agentId === DEFAULT_AGENT_ID) {
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, `"${DEFAULT_AGENT_ID}" is reserved`),
      );
      return;
    }

    if (findAgentEntryIndex(listAgentEntries(cfg), agentId) >= 0) {
      // Self-heal: writeConfigFile has no mutual exclusion, so an unrelated
      // concurrent config write (e.g. a different agent's own routine write)
      // can silently resurrect this agent's config entry from a stale snapshot
      // after its workspace directory was genuinely deleted (e.g. by a
      // recreate-on-build agents.delete) -- and since this early-return never
      // used to call ensureAgentWorkspace, the directory stayed missing forever,
      // failing every subsequent write with ENOENT. Idempotent (only creates
      // what's missing), so this repairs that stuck state on the very next
      // create attempt instead of failing the same way indefinitely.
      try {
        const existingWorkspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
        const skipBootstrapExisting = Boolean(cfg.agents?.defaults?.skipBootstrap);
        await ensureAgentWorkspace({
          dir: existingWorkspaceDir,
          ensureBootstrapFiles: !skipBootstrapExisting,
        });
      } catch {
        // Best-effort -- still respond with the original error below either way.
      }
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, `agent "${agentId}" already exists`),
      );
      return;
    }

    const workspaceDir = resolveUserPath(String(params.workspace ?? "").trim());

    // Resolve agentDir against the config we're about to persist (vs the pre-write config),
    // so subsequent resolutions can't disagree about the agent's directory.
    // Apply all config in a single write — name, workspace, model, tools.
    // This avoids multiple config reloads that slow down agent readiness.
    const model = typeof params.model === "string" && params.model.trim() ? params.model.trim() : undefined;
    const tools = params.tools && typeof params.tools === "object" ? params.tools as { allow?: string[]; deny?: string[]; profile?: string } : undefined;
    const dispatchMode = typeof params.dispatchMode === "string" && params.dispatchMode.trim() ? params.dispatchMode.trim() : undefined;

    let nextConfig = applyAgentConfig(cfg, {
      agentId,
      name: rawName,
      workspace: workspaceDir,
      ...(model ? { model } : {}),
      ...(tools ? { tools } : {}),
      ...(dispatchMode ? { dispatchMode } : {}),
    });
    const agentDir = resolveAgentDir(nextConfig, agentId);
    nextConfig = applyAgentConfig(nextConfig, { agentId, agentDir });

    // Ensure workspace & transcripts exist BEFORE writing config so a failure
    // here does not leave a broken config entry behind.
    const skipBootstrap = Boolean(nextConfig.agents?.defaults?.skipBootstrap);
    await ensureAgentWorkspace({ dir: workspaceDir, ensureBootstrapFiles: !skipBootstrap });
    await fs.mkdir(resolveSessionTranscriptsDirForAgent(agentId), { recursive: true });

    await writeConfigFile(nextConfig);

    // Always write Name to IDENTITY.md; optionally include emoji/avatar.
    const safeName = sanitizeIdentityLine(rawName);
    const emoji = resolveOptionalStringParam(params.emoji);
    const avatar = resolveOptionalStringParam(params.avatar);
    const identityPath = path.join(workspaceDir, DEFAULT_IDENTITY_FILENAME);
    const lines = [
      "",
      `- Name: ${safeName}`,
      ...(emoji ? [`- Emoji: ${sanitizeIdentityLine(emoji)}`] : []),
      ...(avatar ? [`- Avatar: ${sanitizeIdentityLine(avatar)}`] : []),
      "",
    ];
    await fs.appendFile(identityPath, lines.join("\n"), "utf-8");

    respond(true, { ok: true, agentId, name: rawName, workspace: workspaceDir }, undefined);
  },
  "agents.update": async ({ params, respond }) => {
    if (!validateAgentsUpdateParams(params)) {
      respondInvalidMethodParams(respond, "agents.update", validateAgentsUpdateParams.errors);
      return;
    }

    const cfg = loadConfig();
    const agentId = normalizeAgentId(String(params.agentId ?? ""));
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }

    const workspaceDir =
      typeof params.workspace === "string" && params.workspace.trim()
        ? resolveUserPath(params.workspace.trim())
        : undefined;

    const model = resolveOptionalStringParam(params.model);
    const avatar = resolveOptionalStringParam(params.avatar);
    const tools = params.tools && typeof params.tools === "object" ? params.tools as Record<string, unknown> : undefined;
    const compaction =
      params.compaction && typeof params.compaction === "object"
        ? (params.compaction as {
            memoryFlush?: { prompt?: string; systemPrompt?: string };
          })
        : undefined;

    // 2026-06-12 trace: only when a consolidation prompt is in the payload
    // (i.e. a prompt re-push), follow it through the three stages so we can see
    // on prod exactly where a v2 push degrades to v1.
    const incomingConsolidation = (compaction as
      | { memoryFlush?: { consolidationSystemPrompt?: unknown } }
      | undefined)?.memoryFlush?.consolidationSystemPrompt;
    const traceConsolidation = typeof incomingConsolidation === "string";
    if (traceConsolidation) {
      cfgWriteLog.info(
        `[config-write] agents.update RECEIVED agent=${agentId} ${consolidationMarker(incomingConsolidation)}`,
      );
    }

    const nextConfig = applyAgentConfig(cfg, {
      agentId,
      ...(typeof params.name === "string" && params.name.trim()
        ? { name: params.name.trim() }
        : {}),
      ...(workspaceDir ? { workspace: workspaceDir } : {}),
      ...(model ? { model } : {}),
      ...(tools ? { tools: tools as { allow?: string[]; deny?: string[]; profile?: string } } : {}),
      ...(compaction ? { compaction } : {}),
    });

    if (traceConsolidation) {
      cfgWriteLog.info(
        `[config-write] agents.update POST-APPLY agent=${agentId} ` +
          `${consolidationMarker(entryConsolidationSystemPrompt(nextConfig, agentId))}`,
      );
    }

    await writeConfigFile(nextConfig);

    if (traceConsolidation) {
      // What loadConfig() now returns is what the gateway will actually use
      // (runtime snapshot on the secret path, or the file otherwise). If this
      // disagrees with the on-disk file (operator jq), snapshot/file diverged.
      cfgWriteLog.info(
        `[config-write] agents.update PERSISTED(loadConfig) agent=${agentId} ` +
          `${consolidationMarker(entryConsolidationSystemPrompt(loadConfig(), agentId))}`,
      );
    }

    if (workspaceDir) {
      const skipBootstrap = Boolean(nextConfig.agents?.defaults?.skipBootstrap);
      await ensureAgentWorkspace({ dir: workspaceDir, ensureBootstrapFiles: !skipBootstrap });
    }

    if (avatar) {
      const workspace = workspaceDir ?? resolveAgentWorkspaceDir(nextConfig, agentId);
      await fs.mkdir(workspace, { recursive: true });
      const identityPath = path.join(workspace, DEFAULT_IDENTITY_FILENAME);
      await fs.appendFile(identityPath, `\n- Avatar: ${sanitizeIdentityLine(avatar)}\n`, "utf-8");
    }

    respond(true, { ok: true, agentId }, undefined);
  },
  "agents.delete": async ({ params, respond }) => {
    if (!validateAgentsDeleteParams(params)) {
      respondInvalidMethodParams(respond, "agents.delete", validateAgentsDeleteParams.errors);
      return;
    }

    const cfg = loadConfig();
    const agentId = normalizeAgentId(String(params.agentId ?? ""));
    if (agentId === DEFAULT_AGENT_ID) {
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, `"${DEFAULT_AGENT_ID}" cannot be deleted`),
      );
      return;
    }
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }

    const deleteFiles = typeof params.deleteFiles === "boolean" ? params.deleteFiles : true;
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
    const agentDir = resolveAgentDir(cfg, agentId);
    const sessionsDir = resolveSessionTranscriptsDirForAgent(agentId);

    const result = pruneAgentConfig(cfg, agentId);
    await writeConfigFile(result.config);

    if (deleteFiles) {
      await Promise.all([
        moveToTrashBestEffort(workspaceDir),
        moveToTrashBestEffort(agentDir),
        moveToTrashBestEffort(sessionsDir),
      ]);
    }

    respond(true, { ok: true, agentId, removedBindings: result.removedBindings }, undefined);
  },
  "agents.archive-and-delete": async ({ params, respond }) => {
    if (!validateAgentsArchiveAndDeleteParams(params)) {
      respondInvalidMethodParams(
        respond,
        "agents.archive-and-delete",
        validateAgentsArchiveAndDeleteParams.errors,
      );
      return;
    }

    const cfg = loadConfig();
    const agentId = normalizeAgentId(String(params.agentId ?? ""));
    if (agentId === DEFAULT_AGENT_ID) {
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, `"${DEFAULT_AGENT_ID}" cannot be deleted`),
      );
      return;
    }
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }

    const goalId = String(params.goalId ?? "").trim();
    if (!goalId) {
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, "goalId is required for archive-and-delete"),
      );
      return;
    }

    const bucket = (
      typeof params.bucket === "string" && params.bucket.trim()
        ? params.bucket.trim()
        : process.env.AGENT_ARCHIVE_BUCKET
    );
    if (!bucket) {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          "archive bucket not configured: pass `bucket` in params or set AGENT_ARCHIVE_BUCKET on the gateway",
        ),
      );
      return;
    }

    const deleteFiles = typeof params.deleteFiles === "boolean" ? params.deleteFiles : true;
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
    const agentDir = resolveAgentDir(cfg, agentId);
    const sessionsDir = resolveSessionTranscriptsDirForAgent(agentId);

    // Phase 1 — archive. Refuses to proceed to the delete if ANY upload
    // fails. Idempotent overwrite of GCS objects means a partial failure
    // can be retried by the caller without producing duplicate uploads.
    let archive;
    try {
      archive = await archiveAgentToBucket({
        agentId,
        goalId,
        bucket,
        workspaceDir,
        sessionsDir,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, `archive failed before delete: ${msg}`),
      );
      return;
    }

    if (archive.failed.length > 0) {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          `archive incomplete (${archive.failed.length} file(s) failed); refusing to delete. ` +
            `First failure: ${archive.failed[0].path} — ${archive.failed[0].error}`,
        ),
      );
      return;
    }

    // Phase 2 — same removal flow as agents.delete. Only reached when
    // archive.failed is empty.
    const result = pruneAgentConfig(cfg, agentId);
    await writeConfigFile(result.config);

    if (deleteFiles) {
      await Promise.all([
        moveToTrashBestEffort(workspaceDir),
        moveToTrashBestEffort(agentDir),
        moveToTrashBestEffort(sessionsDir),
      ]);
    }

    respond(
      true,
      {
        ok: true,
        agentId,
        goalId,
        bucket,
        uploaded: archive.uploaded,
        totalBytes: archive.totalBytes,
        removedBindings: result.removedBindings,
      },
      undefined,
    );
  },
  "agents.files.list": async ({ params, respond }) => {
    if (!validateAgentsFilesListParams(params)) {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          `invalid agents.files.list params: ${formatValidationErrors(
            validateAgentsFilesListParams.errors,
          )}`,
        ),
      );
      return;
    }
    const cfg = loadConfig();
    const agentId = resolveAgentIdOrError(String(params.agentId ?? ""), cfg);
    if (!agentId) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "unknown agent id"));
      return;
    }
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
    let hideBootstrap = false;
    try {
      hideBootstrap = await isWorkspaceOnboardingCompleted(workspaceDir);
    } catch {
      // Fall back to showing BOOTSTRAP if workspace state cannot be read.
    }
    const files = await listAgentFiles(workspaceDir, { hideBootstrap });
    respond(true, { agentId, workspace: workspaceDir, files }, undefined);
  },
  "agents.files.get": async ({ params, respond }) => {
    if (!validateAgentsFilesGetParams(params)) {
      respondInvalidMethodParams(respond, "agents.files.get", validateAgentsFilesGetParams.errors);
      return;
    }
    const resolved = resolveAgentWorkspaceFileOrRespondError(params, respond);
    if (!resolved) {
      return;
    }
    const { agentId, workspaceDir, name } = resolved;
    const filePath = path.join(workspaceDir, name);
    const resolvedPath = await resolveWorkspaceFilePathOrRespond({
      respond,
      workspaceDir,
      name,
    });
    if (!resolvedPath) {
      return;
    }
    if (resolvedPath.kind === "missing") {
      respondWorkspaceFileMissing({ respond, agentId, workspaceDir, name, filePath });
      return;
    }
    let safeRead: Awaited<ReturnType<typeof readLocalFileSafely>>;
    try {
      safeRead = await readLocalFileSafely({ filePath: resolvedPath.ioPath });
    } catch (err) {
      if (err instanceof SafeOpenError && err.code === "not-found") {
        respondWorkspaceFileMissing({ respond, agentId, workspaceDir, name, filePath });
        return;
      }
      respondWorkspaceFileUnsafe(respond, name);
      return;
    }
    respond(
      true,
      {
        agentId,
        workspace: workspaceDir,
        file: {
          name,
          path: filePath,
          missing: false,
          size: safeRead.stat.size,
          updatedAtMs: Math.floor(safeRead.stat.mtimeMs),
          content: safeRead.buffer.toString("utf-8"),
        },
      },
      undefined,
    );
  },
  "agents.memoryLogs": async ({ params, respond }) => {
    // Return the agent's daily memory logs (memory/*.md) in chronological order.
    // The agents.files.get allowlist blocks `memory/` paths and listAgentFiles
    // ignores the folder, so this dedicated read traverses + reads it
    // server-side. Used by the council goal_health RA to see what GO actually
    // did/decided over time. Bounded: when the logs exceed the cap we keep the
    // NEWEST ones (drop oldest) and still return them in chronological order.
    const cfg = loadConfig();
    const agentId = resolveAgentIdOrError(String((params as any).agentId ?? ""), cfg);
    if (!agentId) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "unknown agent id"));
      return;
    }
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
    const memoryDir = path.join(workspaceDir, "memory");
    let names: string[] = [];
    try {
      const dirents = await fs.readdir(memoryDir, { withFileTypes: true });
      names = dirents
        .filter((d) => d.isFile() && d.name.endsWith(".md")
          && !d.name.includes("/") && !d.name.includes("\\") && !d.name.includes(".."))
        .map((d) => d.name)
        .sort();  // dated YYYY-MM-DD.md -> chronological ascending
    } catch (err) {
      const code = (err as NodeJS.ErrnoException | undefined)?.code;
      if (code === "ENOENT") {
        respond(true, { agentId, workspace: workspaceDir, files: [], truncated: false }, undefined);
        return;
      }
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `memory dir read failed: ${(err as Error).message}`));
      return;
    }
    const MAX_TOTAL = 120 * 1024;  // ~30k tokens — fits goal_health's 200k budget alongside other inputs
    const kept: Array<{ name: string; content: string }> = [];
    let total = 0;
    let truncated = false;
    for (let i = names.length - 1; i >= 0; i--) {  // newest first, so a cap drops the oldest
      let content = "";
      try {
        content = await fs.readFile(path.join(memoryDir, names[i]), "utf-8");
      } catch {
        continue;
      }
      if (total + content.length > MAX_TOTAL) { truncated = true; break; }
      total += content.length;
      kept.push({ name: `memory/${names[i]}`, content });
    }
    kept.reverse();  // back to chronological order
    respond(true, { agentId, workspace: workspaceDir, files: kept, truncated }, undefined);
  },
  "agents.files.set": async ({ params, respond }) => {
    if (!validateAgentsFilesSetParams(params)) {
      respondInvalidMethodParams(respond, "agents.files.set", validateAgentsFilesSetParams.errors);
      return;
    }
    const resolved = resolveAgentWorkspaceFileOrRespondError(params, respond);
    if (!resolved) {
      return;
    }
    const { agentId, workspaceDir, name } = resolved;
    await fs.mkdir(workspaceDir, { recursive: true });
    const filePath = path.join(workspaceDir, name);
    const resolvedPath = await resolveWorkspaceFilePathOrRespond({
      respond,
      workspaceDir,
      name,
    });
    if (!resolvedPath) {
      return;
    }
    const content = String(params.content ?? "");
    const requestedEncoding =
      typeof params.encoding === "string" ? params.encoding : "utf8";
    const relativeWritePath = path.relative(resolvedPath.workspaceReal, resolvedPath.ioPath);
    if (
      !relativeWritePath ||
      relativeWritePath.startsWith("..") ||
      path.isAbsolute(relativeWritePath)
    ) {
      respondWorkspaceFileUnsafe(respond, name);
      return;
    }
    try {
      if (requestedEncoding === "base64") {
        // Caller passed base64-encoded bytes (binary file: PDF/DOCX/XLSX/...).
        // Decode into a Buffer and write the raw bytes — do not pass an
        // encoding to writeFileWithinRoot so it writes the buffer as-is.
        const bytes = Buffer.from(content, "base64");
        await writeFileWithinRoot({
          rootDir: resolvedPath.workspaceReal,
          relativePath: relativeWritePath,
          data: bytes,
        });
      } else {
        await writeFileWithinRoot({
          rootDir: resolvedPath.workspaceReal,
          relativePath: relativeWritePath,
          data: content,
          encoding: "utf8",
        });
      }
    } catch (err) {
      const reason = err instanceof Error ? err.message : "unknown write error";
      respondWorkspaceFileWriteError(respond, name, reason);
      return;
    }
    const meta = await statFileSafely(resolvedPath.ioPath);
    // For binary writes, echoing back the original base64 would be huge and
    // useless — return an empty string instead so the result stays small.
    const echoContent = requestedEncoding === "base64" ? "" : content;
    respond(
      true,
      {
        ok: true,
        agentId,
        workspace: workspaceDir,
        file: {
          name,
          path: filePath,
          missing: false,
          size: meta?.size,
          updatedAtMs: meta?.updatedAtMs,
          content: echoContent,
        },
      },
      undefined,
    );
  },
  "agents.files.delete": async ({ params, respond }) => {
    if (!validateAgentsFilesDeleteParams(params)) {
      respondInvalidMethodParams(
        respond, "agents.files.delete", validateAgentsFilesDeleteParams.errors,
      );
      return;
    }
    const resolved = resolveAgentWorkspaceFileOrRespondError(params, respond);
    if (!resolved) {
      return;
    }
    const { agentId, workspaceDir, name } = resolved;
    const resolvedPath = await resolveWorkspaceFilePathOrRespond({
      respond,
      workspaceDir,
      name,
    });
    if (!resolvedPath) {
      return;
    }
    const relativeDeletePath = path.relative(
      resolvedPath.workspaceReal, resolvedPath.ioPath,
    );
    if (
      !relativeDeletePath ||
      relativeDeletePath.startsWith("..") ||
      path.isAbsolute(relativeDeletePath)
    ) {
      respondWorkspaceFileUnsafe(respond, name);
      return;
    }
    let existed = true;
    try {
      await fs.unlink(resolvedPath.ioPath);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException | undefined)?.code;
      if (code === "ENOENT") {
        // Already gone — treat as idempotent success.
        existed = false;
      } else {
        const reason = err instanceof Error ? err.message : "unknown delete error";
        respondWorkspaceFileWriteError(respond, name, reason);
        return;
      }
    }
    respond(
      true,
      {
        ok: true,
        agentId,
        workspace: workspaceDir,
        name,
        existed,
      },
      undefined,
    );
  },

  // ── Per-agent custom skills ──────────────────────────────────────────────
  // Writes a SKILL.md under <workspace>/skills/<skillName>/. The workspace
  // skill scanner (loadWorkspaceSkillEntries) discovers it on the next run.
  // Used by the control panel to push skills from the `skills` table to a
  // live agent without going through the ALLOWED_FILE_NAMES allowlist.
  "agents.skills.set": async ({ params, respond }) => {
    if (!validateAgentsSkillsSetParams(params)) {
      respondInvalidMethodParams(
        respond, "agents.skills.set", validateAgentsSkillsSetParams.errors,
      );
      return;
    }
    const cfg = loadConfig();
    const agentId = resolveAgentIdOrError(
      typeof params.agentId === "string" ? params.agentId : String(params.agentId ?? ""),
      cfg,
    );
    if (!agentId) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "unknown agent id"));
      return;
    }
    const skillName = String(params.skillName ?? "").trim();
    if (!/^[a-z][a-z0-9_]{1,63}$/.test(skillName)) {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          "skillName must be lowercase letters, digits, underscores; 2-64 chars; starts with a letter",
        ),
      );
      return;
    }
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
    const skillDir = path.join(workspaceDir, "skills", skillName);
    const relativePath = path.join("skills", skillName, "SKILL.md");
    const content = String(params.content ?? "");
    try {
      await fs.mkdir(skillDir, { recursive: true });
      await writeFileWithinRoot({
        rootDir: workspaceDir,
        relativePath,
        data: content,
        encoding: "utf8",
      });
    } catch (err) {
      const reason = err instanceof Error ? err.message : "unknown write error";
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INTERNAL_ERROR, `skill write failed: ${reason}`),
      );
      return;
    }
    respond(
      true,
      {
        ok: true,
        agentId,
        workspace: workspaceDir,
        skillName,
        path: relativePath,
      },
      undefined,
    );
  },

  "agents.skills.delete": async ({ params, respond }) => {
    if (!validateAgentsSkillsDeleteParams(params)) {
      respondInvalidMethodParams(
        respond, "agents.skills.delete", validateAgentsSkillsDeleteParams.errors,
      );
      return;
    }
    const cfg = loadConfig();
    const agentId = resolveAgentIdOrError(
      typeof params.agentId === "string" ? params.agentId : String(params.agentId ?? ""),
      cfg,
    );
    if (!agentId) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "unknown agent id"));
      return;
    }
    const skillName = String(params.skillName ?? "").trim();
    if (!/^[a-z][a-z0-9_]{1,63}$/.test(skillName)) {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          "skillName must be lowercase letters, digits, underscores; 2-64 chars; starts with a letter",
        ),
      );
      return;
    }
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
    const skillDir = path.join(workspaceDir, "skills", skillName);

    // Path safety: ensure the skill directory is strictly inside the workspace.
    const wsReal = path.resolve(workspaceDir);
    const skReal = path.resolve(skillDir);
    const rel = path.relative(wsReal, skReal);
    if (!rel || rel.startsWith("..") || path.isAbsolute(rel)) {
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, "skill path escapes workspace"),
      );
      return;
    }

    let existed = true;
    try {
      await fs.rm(skillDir, { recursive: true, force: false });
    } catch (err) {
      const code = (err as NodeJS.ErrnoException | undefined)?.code;
      if (code === "ENOENT") {
        existed = false;
      } else {
        const reason = err instanceof Error ? err.message : "unknown delete error";
        respond(
          false,
          undefined,
          errorShape(ErrorCodes.INTERNAL_ERROR, `skill delete failed: ${reason}`),
        );
        return;
      }
    }
    respond(
      true,
      {
        ok: true,
        agentId,
        workspace: workspaceDir,
        skillName,
        existed,
      },
      undefined,
    );
  },

  // ── SHCA operator-mode workspace provisioning (Phase 4 of SHCA refactor) ──
  //
  // The control panel calls this BEFORE sending the operator-mode kickoff to
  // SHCA, gating SHCA's "start working" on successful provisioning.
  //
  // The workspace lives inside the SHCA agent's own workspace dir
  // (<agentWorkspace>/stories/<storyId>/) so it is automatically cleaned up
  // when the SHCA is reaped via agents.delete.

  "shca.workspace.setup": async ({ params, respond }) => {
    if (!validateShcaWorkspaceSetupParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.setup", validateShcaWorkspaceSetupParams.errors,
      );
      return;
    }

    const { spawn } = await import("node:child_process");

    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    // Accept BOTH shapes (plan Phase G, backward-compatible): the legacy single
    // `repo` object, or the multi-repo `repos` array. Normalize to one list so the
    // rest of the handler has a single code path. The legacy form keeps cloning into
    // `repo/` (agent docs, _resolveStoryRepoDir and every existing story on disk
    // depend on that exact dir); the multi-repo form uses each entry's `subdir`.
    const legacyRepoCfg = (params as any).repo as {
      cloneUrl: string; branch: string; userName?: string; userEmail?: string;
    } | undefined;
    const multiRepoCfg = (params as any).repos as Array<{
      subdir: string; cloneUrl: string; branch: string; write?: boolean;
      userName?: string; userEmail?: string;
    }> | undefined;

    // Three shapes now, not two. `repo` (legacy single), `repos` (multi), and
    // NEITHER — a story workspace that holds documents and plans and no
    // checkout at all, because the work happens in an executor sandbox that
    // has its own. Sending both is still a contradiction and still refused.
    //
    // An empty `repos: []` is NOT the way to ask for that. It is truthy in JS,
    // so it would slip past a `!a === !b` check and crash on an undefined
    // primary target further down; the schema's minItems:1 rejects it before
    // this handler runs, and the explicit length check below keeps that
    // invariant local rather than depending on a schema in another file. Omit
    // the field to get a docs-only workspace.
    const docsOnly = !legacyRepoCfg && !multiRepoCfg;
    if (legacyRepoCfg && multiRepoCfg) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        "provide `repo` (single) or `repos` (multi), not both — or neither, " +
        "for a story workspace with no checkout",
      ));
      return;
    }
    if (multiRepoCfg && multiRepoCfg.length === 0) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        "`repos` must list at least one repository — omit the field entirely " +
        "for a workspace with no checkout",
      ));
      return;
    }

    const isMultiRepo = Boolean(multiRepoCfg);
    const repoList = multiRepoCfg ?? (legacyRepoCfg ? [{
      subdir: "repo",
      cloneUrl: legacyRepoCfg.cloneUrl,
      branch: legacyRepoCfg.branch,
      write: true,
      userName: legacyRepoCfg.userName,
      userEmail: legacyRepoCfg.userEmail,
    }] : []);

    // Each subdir must stay inside the story workspace and must not collide.
    const seenSubdirs = new Set<string>();
    for (const r of repoList) {
      const sub = (r.subdir || "").trim();
      if (!sub || sub.startsWith("/") || sub.includes("..") || path.isAbsolute(sub)) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `repo subdir must be a relative path without '..', got '${sub}'`,
        ));
        return;
      }
      if (seenSubdirs.has(sub)) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST, `duplicate repo subdir '${sub}'`,
        ));
        return;
      }
      seenSubdirs.add(sub);
    }

    const files = (params as any).files as Record<string, string>;
    const githubToken = (params as any).githubToken as string | undefined;
    // When present, install a callback credential helper pointing at this URL instead
    // of embedding the token in .git/config — see the credential-helper block below.
    const credentialBrokerUrl =
      String((params as any).credentialBrokerUrl ?? "").trim();
    // Validated ONCE, here, because it is interpolated into a shell command that git
    // executes. `new URL` both rejects malformed input and normalises/percent-encodes
    // most of it — but a legal URL may still contain `$` or a backtick, which the shell
    // would expand inside the helper string. Reject those outright rather than trying to
    // quote around them. Doing this up front also stops a malformed URL throwing from
    // inside the clone loop and failing the RPC with an unhandled error.
    if (credentialBrokerUrl) {
      let parsed: URL;
      try {
        parsed = new URL(credentialBrokerUrl);
      } catch {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `credentialBrokerUrl is not a valid URL`,
        ));
        return;
      }
      if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `credentialBrokerUrl must be http(s), got ${parsed.protocol}`,
        ));
        return;
      }
      if (/[$`"'\\;|&<>\s]/.test(credentialBrokerUrl)) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `credentialBrokerUrl contains characters unsafe to embed in a git credential helper`,
        ));
        return;
      }
    }
    // Story dirs the control panel says must NOT be wiped (e.g. a 'done' story
    // whose final council review hasn't run yet — it reads the live workspace).
    const protectStoryIds = new Set<string>(
      Array.isArray((params as any).protectStoryIds)
        ? ((params as any).protectStoryIds as unknown[]).map((s) => String(s))
        : [],
    );

    if (!/^[A-Za-z0-9._-]+$/.test(storyId)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `storyId must match [A-Za-z0-9._-]+, got ${storyId}`,
      ));
      return;
    }

    // Caller (control panel) must register the SHCA via agents.create
    // BEFORE invoking this RPC — the workspace path comes from the agent's
    // config so the agent has to exist.
    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }

    const agentWorkspaceRaw = resolveAgentWorkspaceDir(cfg, agentId);
    let agentWorkspaceDir: string;
    try {
      agentWorkspaceDir = await fs.realpath(agentWorkspaceRaw);
    } catch {
      agentWorkspaceDir = path.resolve(agentWorkspaceRaw);
    }

    const storiesParentDir = path.join(agentWorkspaceDir, "stories");
    const wsReal = path.join(storiesParentDir, storyId);
    // Absolute clone target per repo, in the caller's order (write repos first).
    // `owner/name` parsed from the clone URL. Used to tell the credential broker
    // WHICH repo a request is for — derived rather than passed so the legacy
    // single-repo form gets it too, without a params change.
    function repoNameFromCloneUrl(cloneUrl: string): string {
      try {
        const segments = new URL(cloneUrl).pathname
          .replace(/\.git$/, "").split("/").filter(Boolean);
        return segments.length >= 2
          ? `${segments[segments.length - 2]}/${segments[segments.length - 1]}`
          : (segments[0] ?? "");
      } catch {
        return "";
      }
    }

    const repoTargets = repoList.map((r) => ({
      ...r,
      write: r.write !== false,
      absPath: path.join(wsReal, r.subdir),
      repoName: repoNameFromCloneUrl(r.cloneUrl),
    }));
    // Primary = the first WRITE repo (the single repo in the legacy form). Its path
    // is what the result's `repoPath` reports for un-updated callers. Empty when
    // nothing is being cloned — there is no primary repo to name, and reporting
    // the story dir as one would tell the caller a checkout exists.
    const primaryTarget = repoTargets.find((r) => r.write) ?? repoTargets[0];
    const repoReal = primaryTarget ? primaryTarget.absPath : "";

    // Defense-in-depth: every clone target must resolve inside the story workspace.
    for (const t of repoTargets) {
      const resolved = path.resolve(t.absPath);
      if (!resolved.startsWith(wsReal + path.sep)) {
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR,
          `repo subdir '${t.subdir}' escaped the story workspace`,
        ));
        return;
      }
    }

    // Defense-in-depth: ensure computed path is still under the agent workspace.
    if (!wsReal.startsWith(agentWorkspaceDir + path.sep)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `resolved workspace path escaped agent workspace: ${wsReal}`,
      ));
      return;
    }

    // Remove the story dirs the control panel did NOT list in protectStoryIds.
    //
    // This began as a single-story-at-a-time invariant: one assistant held one
    // story, so every other dir was by definition abandoned, and the protect
    // list was a narrow exception for a 'done' story whose council review had
    // not run yet. That reading is no longer safe — an assistant can now hold
    // several stories at once, and this loop cannot tell a finished one from a
    // live one. The control panel can, so the list is now the full set of
    // stories it is still working on, and the rule here is simply: keep what
    // was named, remove the rest. A live story omitted from the list loses its
    // documents silently, which is why the caller sends state, not intent.
    // The new story is still imported + created regardless.
    try {
      const existing = await fs.readdir(storiesParentDir, { withFileTypes: true });
      for (const ent of existing) {
        if (!ent.isDirectory() || ent.name === storyId) continue;
        if (protectStoryIds.has(ent.name)) {
          continue;  // unreviewed 'done' story — preserve until reviewed
        }
        const stalePath = path.join(storiesParentDir, ent.name);
        await fs.rm(stalePath, { recursive: true, force: true });
      }
    } catch (err) {
      const code = (err as NodeJS.ErrnoException | undefined)?.code;
      if (code !== "ENOENT") {
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR,
          `cleanup of prior story workspaces failed: ${(err as Error).message}`,
        ));
        return;
      }
    }

    // Resolve git absolute path — the systemd-rooted gateway may not have
    // /usr/bin on its inherited PATH. Mirrors shca-tools.ts pattern. Shared
    // resolver (_resolveGitBin) also covers Windows local-dev paths.
    const gitBin = _resolveGitBin();
    if (!gitBin) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git binary not found in ${_GIT_CANDIDATES.join(", ")}`,
      ));
      return;
    }

    function runGit(
      args: string[],
      opts: { cwd?: string; env?: Record<string, string>; timeoutMs?: number } = {},
    ): Promise<{ ok: boolean; stdout: string; stderr: string; code: number | null }> {
      return new Promise((resolve) => {
        const timeoutMs = opts.timeoutMs ?? 120_000;
        const env = { ...process.env, ...(opts.env || {}) };
        const child = spawn(gitBin as string, args, { cwd: opts.cwd, env, shell: false });
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
          resolve({ ok: !killed && code === 0, stdout, stderr, code });
        });
        child.on("error", (err) => {
          clearTimeout(timer);
          stderr += `\nspawn error: ${err.message}`;
          resolve({ ok: false, stdout, stderr, code: null });
        });
      });
    }

    // Build the clone URL: embed credentials inline for the clone, then
    // overwrite the remote URL afterwards so the token isn't persisted in
    // .git/config. The local credential.helper handles subsequent push/pull.
    function buildAuthedCloneUrl(rawUrl: string, token: string): string {
      try {
        const u = new URL(rawUrl);
        u.username = "x-access-token";
        u.password = token;
        return u.toString();
      } catch {
        // Fall back: caller passed a malformed URL — return as-is and let git error.
        return rawUrl;
      }
    }

    // 1. Create workspace dir (and its parent 'stories' dir).
    try {
      await fs.mkdir(wsReal, { recursive: true });
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `mkdir workspace failed: ${(err as Error).message}`,
      ));
      return;
    }

    // Make re-provisioning idempotent: nuke any pre-existing repo subdir
    // so the clone doesn't conflict. The other scaffolding files at the
    // workspace root (RESOURCES.md, IDENTITY.md, docs/, .claude/) get
    // overwritten by the writeFile loop below — no special handling
    // needed for those.
    // In the multi-repo form the clones live under a shared `repos/` parent, so
    // wiping that whole parent (plus any legacy `repo/` left by an earlier
    // single-repo provisioning of the same story) is what makes re-provisioning
    // idempotent. Collect the distinct dirs to remove rather than removing each
    // clone target individually.
    const staleDirs = new Set<string>([path.join(wsReal, "repo")]);
    for (const t of repoTargets) {
      const firstSegment = t.subdir.split(/[\\/]/)[0];
      staleDirs.add(path.join(wsReal, firstSegment));
    }
    for (const dir of staleDirs) {
      try {
        await fs.rm(dir, { recursive: true, force: true });
      } catch (err) {
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR,
          `rm stale repo dir '${dir}' failed: ${(err as Error).message}`,
        ));
        return;
      }
    }

    // 2-6. Clone + configure each repo. Sequential on purpose: these are network
    // clones on a shared VM, and a failure must abort the whole provisioning rather
    // than leave a half-built workspace the assistant would start working in.
    const clonedRepos: Array<{
      subdir: string; repoPath: string; branch: string; headSha: string; write: boolean;
    }> = [];

    for (const target of repoTargets) {
      // 2. Clone the branch.
      const cloneUrlForGit = githubToken
        ? buildAuthedCloneUrl(target.cloneUrl, githubToken)
        : target.cloneUrl;

      await fs.mkdir(path.dirname(target.absPath), { recursive: true });

      const cloneResult = await runGit(
        ["clone", "--single-branch", "--branch", target.branch, "--no-tags",
         cloneUrlForGit, target.absPath],
        { env: { GIT_TERMINAL_PROMPT: "0" } },
      );

      if (!cloneResult.ok) {
        // Redact the token from any error spillage just in case.
        const stderr = (githubToken
          ? cloneResult.stderr.replaceAll(githubToken, "<redacted>")
          : cloneResult.stderr
        ).slice(0, 1000);
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR,
          `git clone failed for '${target.subdir}' (branch ${target.branch}): ${stderr}`,
        ));
        return;
      }

      // 3. Overwrite remote URL to drop the inline credentials from .git/config.
      if (githubToken) {
        await runGit(["-C", target.absPath, "remote", "set-url", "origin", target.cloneUrl]);
      }

      // 4. Configure git user.name + user.email in the clone
      const userName = target.userName || "openclaw-shca";
      const userEmail = target.userEmail || "openclaw+shca@noreply.agents";
      await runGit(["-C", target.absPath, "config", "user.name", userName]);
      await runGit(["-C", target.absPath, "config", "user.email", userEmail]);

      // 5. Configure the local credential helper. Installed per clone because the
      // operator path deliberately does not inherit a global helper.
      //
      // TWO MODES (plan M5/G.6):
      //   * BROKERED (preferred) — the helper calls back to the control panel and
      //     gets a freshly minted token per git operation. NOTHING is stored: no
      //     token in .git/config, no 50-minute rewrite loop. The control panel
      //     re-checks on every call that this story may touch this repo.
      //   * INLINE (legacy) — the token is embedded in .git/config and refreshed by
      //     later setup calls.
      //
      // The control panel chooses, by sending `credentialBrokerUrl`. It only does so
      // once /internal/git-credential is live, so an older control panel (or a
      // rollback) transparently keeps the inline helper. Both are per-clone, so a
      // workspace can even be mixed without harm.
      // READ-ONLY CLONES GET NO CREDENTIALS AT ALL, and lose their remote.
      //
      // Installing a write-capable helper in a `write: false` clone left the read-only
      // boundary enforced by nothing but instructions in a markdown file: the clone had
      // a working push credential and an `origin` pointing at the real remote, so one
      // stray `git push` from the assistant would land commits in a repository the story
      // was only ever granted READ access to. Mirrors what
      // agents.workspace.prepareRepo already does for Security Council clones.
      if (!target.write) {
        await runGit(["-C", target.absPath, "remote", "remove", "origin"]);
        // Also strip any inherited helper so a push cannot pick one up implicitly.
        await runGit([
          "-C", target.absPath, "config", "--local", "credential.helper", "",
        ]);
      } else if (credentialBrokerUrl) {
        // $OPENCLAW_GATEWAY_TOKEN is already injected into the story workspace env
        // (cli-spawn.ts) and is what the existing assistant hooks authenticate with.
        // -f makes curl fail loudly rather than feeding git an HTML error page.
        //
        // The bearer is passed to curl on STDIN via --config, never in argv: an
        // argument-vector secret is readable from /proc/<pid>/cmdline by any co-tenant
        // process for the lifetime of every git operation. `--config -` makes curl read
        // the header from stdin instead.
        const brokerUrl = new URL(credentialBrokerUrl);
        brokerUrl.searchParams.set("story_id", storyId);
        brokerUrl.searchParams.set("repo", target.repoName || "");
        const safeUrl = brokerUrl.toString();
        const helperCmd =
          `!f() { printf 'header = "Authorization: Bearer %s"\\n' ` +
          `"$OPENCLAW_GATEWAY_TOKEN" | curl -sf --config - ` +
          `${JSON.stringify(safeUrl)}; }; f`;
        await runGit(["-C", target.absPath, "config", "credential.helper", helperCmd]);
      } else if (githubToken) {
        const helperCmd =
          `!f() { echo "username=x-access-token"; echo "password=${githubToken}"; }; f`;
        await runGit(["-C", target.absPath, "config", "credential.helper", helperCmd]);
      }

      // 6. Capture HEAD sha
      const shaResult = await runGit(["-C", target.absPath, "rev-parse", "HEAD"]);
      clonedRepos.push({
        subdir: target.subdir,
        repoPath: target.absPath,
        branch: target.branch,
        headSha: shaResult.stdout.trim() || "unknown",
        write: target.write,
      });
    }

    const primaryClone =
      clonedRepos.find((r) => r.repoPath === repoReal) ?? clonedRepos[0];
    const headSha = primaryClone?.headSha;

    // 6b. Write the repo manifest.
    //
    // WHY THIS FILE EXISTS: write-vs-read-only is a control-plane fact (it comes from
    // story_resources.relation) that is invisible on disk — every clone looks the
    // same. Without it, _resolveStoryRepoDirs can only order clones alphabetically,
    // so gitDiff/gitCommit could pick a READ-ONLY context repo and commit into a repo
    // the story was never granted write access to. The manifest is the gateway's only
    // durable record of the distinction between provisioning and a later RPC.
    //
    // Dotfile at the workspace root, outside every clone, so it can never be
    // committed. Best-effort: a manifest failure must not fail an otherwise-good
    // workspace — the resolver falls back to alphabetical order.
    if (isMultiRepo) {
      try {
        await fs.writeFile(
          path.join(wsReal, ".openclaw-story-repos.json"),
          JSON.stringify({ version: 1, repos: clonedRepos }, null, 2),
          "utf-8",
        );
      } catch (err) {
        // NOT non-fatal. This file is the only record of which clones are writable;
        // without it _resolveStoryRepoDirs treats EVERY clone as writable, so a
        // read-only context repo would become a commit target. Fail provisioning
        // rather than hand back a workspace whose write boundary is unknown.
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR,
          `failed to write the repo manifest, so the read-only boundary cannot be ` +
          `enforced: ${(err as Error).message}`,
        ));
        return;
      }
    }

    // 7. Write scaffolding files. Path safety: relPath must be relative,
    // no `..`, no absolute paths.
    let filesWritten = 0;
    for (const [relPath, content] of Object.entries(files)) {
      if (typeof content !== "string") continue;
      if (!relPath || relPath.startsWith("/") || relPath.includes("..")) continue;
      const absPath = path.join(wsReal, relPath);
      if (!path.resolve(absPath).startsWith(wsReal + path.sep) &&
          path.resolve(absPath) !== wsReal) continue;
      try {
        await fs.mkdir(path.dirname(absPath), { recursive: true });
        await fs.writeFile(absPath, content, "utf-8");
        filesWritten++;
      } catch (err) {
        // best-effort
      }
    }

    respond(true, {
      ok: true,
      workspacePath: wsReal,
      // Primary-repo fields stay populated in BOTH cloning forms so a control
      // panel that predates the multi-repo result shape keeps working (see the
      // schema comment). Omitted entirely when nothing was cloned: an empty
      // string would read as a path, and a made-up one as a checkout.
      ...(primaryClone ? {
        repoPath: repoReal,
        branch: primaryClone.branch,
        headSha,
      } : {}),
      // Per-repo detail only in the multi-repo form — omitted for legacy callers so
      // the response is byte-identical to what they got before this change.
      ...(isMultiRepo ? { repos: clonedRepos } : {}),
      filesWritten,
    }, undefined);
  },

  // Clone a branch read-only into an agent's workspace for the Security Council.
  // Unlike shca.workspace.setup, this installs NO credential helper and REMOVES
  // the remote after cloning, so the aspect agents CANNOT push or pull. The full
  // branch history (.git) is kept on purpose — the Secrets aspect scans it for
  // committed-then-removed secrets. The token is used only for this server-side
  // clone and is never written into the workspace.
  "agents.workspace.prepareRepo": async ({ params, respond }) => {
    const { spawn } = await import("node:child_process");

    const agentId = normalizeAgentId(String((params as any).agentId ?? ""));
    const cloneUrl = String((params as any).cloneUrl ?? "").trim();
    const branch = String((params as any).branch ?? "").trim();
    const token = String((params as any).token ?? "").trim();
    const destRaw = (String((params as any).dest ?? "repo").trim()) || "repo";

    if (!agentId) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "agentId is required"));
      return;
    }
    if (!cloneUrl) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "cloneUrl is required"));
      return;
    }
    if (!branch) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "branch is required"));
      return;
    }
    if (!/^[A-Za-z0-9._-]+$/.test(destRaw)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST, `dest must match [A-Za-z0-9._-]+, got ${destRaw}`,
      ));
      return;
    }

    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }

    const wsRaw = resolveAgentWorkspaceDir(cfg, agentId);
    let wsDir: string;
    try { wsDir = await fs.realpath(wsRaw); } catch { wsDir = path.resolve(wsRaw); }
    const repoReal = path.join(wsDir, destRaw);
    if (!repoReal.startsWith(wsDir + path.sep)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `resolved dest escaped workspace: ${repoReal}`,
      ));
      return;
    }

    const gitBin = _resolveGitBin();
    if (!gitBin) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `git binary not found in ${_GIT_CANDIDATES.join(", ")}`,
      ));
      return;
    }

    function runGit(
      args: string[],
      opts: { cwd?: string; env?: Record<string, string>; timeoutMs?: number } = {},
    ): Promise<{ ok: boolean; stdout: string; stderr: string; code: number | null }> {
      return new Promise((resolve) => {
        const timeoutMs = opts.timeoutMs ?? 240_000;
        const env = { ...process.env, ...(opts.env || {}) };
        const child = spawn(gitBin as string, args, { cwd: opts.cwd, env, shell: false });
        let stdout = ""; let stderr = ""; let killed = false;
        const timer = setTimeout(() => {
          killed = true;
          try { child.kill("SIGTERM"); } catch {}
        }, timeoutMs);
        child.stdout?.on("data", (d) => { stdout += d.toString(); });
        child.stderr?.on("data", (d) => { stderr += d.toString(); });
        child.on("close", (code) => {
          clearTimeout(timer);
          resolve({ ok: !killed && code === 0, stdout, stderr, code });
        });
        child.on("error", (err) => {
          clearTimeout(timer);
          stderr += `\nspawn error: ${err.message}`;
          resolve({ ok: false, stdout, stderr, code: null });
        });
      });
    }

    function buildAuthedCloneUrl(rawUrl: string, t: string): string {
      try {
        const u = new URL(rawUrl);
        u.username = "x-access-token";
        u.password = t;
        return u.toString();
      } catch {
        return rawUrl;
      }
    }

    // Ensure the workspace exists, then nuke any stale checkout so the clone is idempotent.
    try {
      await fs.mkdir(wsDir, { recursive: true });
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `mkdir workspace failed: ${(err as Error).message}`,
      ));
      return;
    }
    try {
      await fs.rm(repoReal, { recursive: true, force: true });
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `rm stale checkout failed: ${(err as Error).message}`,
      ));
      return;
    }

    // Clone the single branch WITH its history (no --depth) — gitleaks needs the
    // commit history to catch committed-then-removed secrets.
    const cloneUrlForGit = token ? buildAuthedCloneUrl(cloneUrl, token) : cloneUrl;
    const cloneResult = await runGit(
      ["clone", "--single-branch", "--branch", branch, "--no-tags", cloneUrlForGit, repoReal],
      { env: { GIT_TERMINAL_PROMPT: "0" } },
    );
    if (!cloneResult.ok) {
      const stderr = (token ? cloneResult.stderr.replaceAll(token, "<redacted>") : cloneResult.stderr).slice(0, 1000);
      respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR, `git clone failed: ${stderr}`));
      return;
    }

    // No-push/no-pull guarantee: drop the remote entirely and unset any cached
    // credential helper so nothing in the workspace can reach the origin.
    await runGit(["-C", repoReal, "remote", "remove", "origin"]);
    await runGit(["-C", repoReal, "config", "--unset-all", "credential.helper"]);

    const shaResult = await runGit(["-C", repoReal, "rev-parse", "HEAD"]);
    const headSha = shaResult.stdout.trim() || "unknown";

    respond(true, {
      ok: true,
      repoPath: repoReal,
      dest: destRaw,
      branch,
      headSha,
    }, undefined);
  },

  // Remove a specific story workspace under the SHCA agent. Called from
  // the control panel when a story transitions to done/cancelled/rejected.
  // Idempotent — succeeds if the dir is already gone.
  "shca.workspace.deleteStory": async ({ params, respond }) => {
    if (!validateShcaWorkspaceDeleteStoryParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.deleteStory",
        validateShcaWorkspaceDeleteStoryParams.errors,
      );
      return;
    }

    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);

    if (!/^[A-Za-z0-9._-]+$/.test(storyId)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `storyId must match [A-Za-z0-9._-]+, got ${storyId}`,
      ));
      return;
    }

    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      // Agent already gone (probably reaped) — story workspace went with it.
      respond(true, { ok: true, existed: false, storyId, agentId }, undefined);
      return;
    }

    const agentWorkspaceRaw = resolveAgentWorkspaceDir(cfg, agentId);
    let agentWorkspaceDir: string;
    try {
      agentWorkspaceDir = await fs.realpath(agentWorkspaceRaw);
    } catch {
      agentWorkspaceDir = path.resolve(agentWorkspaceRaw);
    }

    const wsReal = path.join(agentWorkspaceDir, "stories", storyId);
    if (!wsReal.startsWith(agentWorkspaceDir + path.sep)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `resolved workspace path escaped agent workspace: ${wsReal}`,
      ));
      return;
    }

    let existed = true;
    try {
      await fs.rm(wsReal, { recursive: true, force: false });
    } catch (err) {
      const code = (err as NodeJS.ErrnoException | undefined)?.code;
      if (code === "ENOENT") existed = false;
      else {
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR,
          `delete story workspace failed: ${(err as Error).message}`,
        ));
        return;
      }
    }

    respond(true, { ok: true, existed, storyId, agentId }, undefined);
  },

  // List files under <agentWorkspace>/stories/<storyId>/ — used by the
  // SHCA chat's right-rail file viewer. Returns a flat list of entries
  // (paths relative to the story workspace root). Excludes noise dirs
  // (.git, __pycache__, node_modules, .pytest_cache). Cap = 10000 entries.
  "shca.workspace.listFiles": async ({ params, respond }) => {
    if (!validateShcaWorkspaceListFilesParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.listFiles",
        validateShcaWorkspaceListFilesParams.errors,
      );
      return;
    }

    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    if (!/^[A-Za-z0-9._-]+$/.test(storyId)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `storyId must match [A-Za-z0-9._-]+, got ${storyId}`,
      ));
      return;
    }

    const cfg = loadConfig();

    // Resolve the story workspace. Prefer an explicit, caller-provided path (the
    // control panel passes the story's real provisioned dir); fall back to
    // re-deriving from the agent id. The explicit path is confined to the
    // openclaw state dir and must be the story's own dir (basename === storyId).
    // Re-derivation is unreliable for long / uppercase agent ids because
    // normalizeAgentId lowercases+truncates, so the derived dir can miss the
    // actual worktree (e.g. goal ids like MARL).
    const explicitPath = typeof (params as any).workspacePath === "string"
      ? String((params as any).workspacePath).trim() : "";
    let storyRoot: string;
    if (explicitPath) {
      const stateDirRaw = path.dirname(resolveAgentWorkspaceDir(cfg, agentId));
      let stateDir: string;
      try { stateDir = await fs.realpath(stateDirRaw); } catch { stateDir = path.resolve(stateDirRaw); }
      let abs: string;
      try { abs = await fs.realpath(explicitPath); } catch { abs = path.resolve(explicitPath); }
      if ((abs !== stateDir && !abs.startsWith(stateDir + path.sep)) || path.basename(abs) !== storyId) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `workspacePath must be the story dir within the openclaw state dir`,
        ));
        return;
      }
      storyRoot = abs;
    } else {
      if (!isConfiguredAgent(cfg, agentId)) {
        respondAgentNotFound(respond, agentId);
        return;
      }
      const agentWorkspaceRaw = resolveAgentWorkspaceDir(cfg, agentId);
      let agentWorkspaceDir: string;
      try {
        agentWorkspaceDir = await fs.realpath(agentWorkspaceRaw);
      } catch {
        agentWorkspaceDir = path.resolve(agentWorkspaceRaw);
      }
      storyRoot = path.join(agentWorkspaceDir, "stories", storyId);
    }

    const EXCLUDE_DIRS = new Set([
      ".git", "__pycache__", "node_modules", ".pytest_cache",
      ".venv", "venv", ".mypy_cache", ".ruff_cache", "dist", "build",
    ]);
    const MAX_ENTRIES = 10000;

    const entries: Array<{ path: string; type: "file" | "dir"; size: number; mtimeMs: number }> = [];
    let truncated = false;

    async function walk(absDir: string, relDir: string): Promise<void> {
      if (truncated) return;
      let items: import("node:fs").Dirent[];
      try {
        items = await fs.readdir(absDir, { withFileTypes: true });
      } catch {
        return; // ENOENT or perm — skip silently
      }
      items.sort((a, b) => a.name.localeCompare(b.name));
      for (const ent of items) {
        if (truncated) return;
        if (EXCLUDE_DIRS.has(ent.name)) continue;
        const rel = relDir ? `${relDir}/${ent.name}` : ent.name;
        const abs = path.join(absDir, ent.name);
        let st: import("node:fs").Stats;
        try {
          st = await fs.stat(abs);
        } catch {
          continue;
        }
        if (ent.isDirectory()) {
          entries.push({ path: rel, type: "dir", size: 0, mtimeMs: st.mtimeMs });
          if (entries.length >= MAX_ENTRIES) { truncated = true; return; }
          await walk(abs, rel);
        } else if (ent.isFile()) {
          entries.push({ path: rel, type: "file", size: st.size, mtimeMs: st.mtimeMs });
          if (entries.length >= MAX_ENTRIES) { truncated = true; return; }
        }
      }
    }

    try {
      await walk(storyRoot, "");
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `walk failed: ${(err as Error).message}`,
      ));
      return;
    }

    respond(true, { ok: true, storyId, entries, truncated }, undefined);
  },

  // Read a single text file under <agentWorkspace>/stories/<storyId>/<path>.
  // Caps at 1 MB; returns mime hint based on extension. Binary files return
  // an octet-stream mime with empty content (frontend should show "preview
  // not available").
  "shca.workspace.readFile": async ({ params, respond }) => {
    if (!validateShcaWorkspaceReadFileParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.readFile",
        validateShcaWorkspaceReadFileParams.errors,
      );
      return;
    }

    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    const relPath = String((params as any).path);

    if (!/^[A-Za-z0-9._-]+$/.test(storyId)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `storyId must match [A-Za-z0-9._-]+, got ${storyId}`,
      ));
      return;
    }
    if (relPath.startsWith("/") || relPath.includes("..") || relPath.includes("\0")) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `path must be relative, no '..', no absolute prefix`,
      ));
      return;
    }

    const cfg = loadConfig();

    // Prefer the caller's explicit workspacePath (the control panel passes the
    // story's real provisioned dir), confined to the openclaw state dir and
    // required to be the story's own dir; else re-derive from the agent id.
    // Re-derivation is unreliable for long / uppercase agent ids because
    // normalizeAgentId lowercases+truncates (e.g. goal ids like MARL).
    const explicitPath = typeof (params as any).workspacePath === "string"
      ? String((params as any).workspacePath).trim() : "";
    let storyRoot: string;
    if (explicitPath) {
      const stateDirRaw = path.dirname(resolveAgentWorkspaceDir(cfg, agentId));
      let stateDir: string;
      try { stateDir = await fs.realpath(stateDirRaw); } catch { stateDir = path.resolve(stateDirRaw); }
      let abs: string;
      try { abs = await fs.realpath(explicitPath); } catch { abs = path.resolve(explicitPath); }
      if ((abs !== stateDir && !abs.startsWith(stateDir + path.sep)) || path.basename(abs) !== storyId) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `workspacePath must be the story dir within the openclaw state dir`,
        ));
        return;
      }
      storyRoot = abs;
    } else {
      if (!isConfiguredAgent(cfg, agentId)) {
        respondAgentNotFound(respond, agentId);
        return;
      }
      const agentWorkspaceRaw = resolveAgentWorkspaceDir(cfg, agentId);
      let agentWorkspaceDir: string;
      try {
        agentWorkspaceDir = await fs.realpath(agentWorkspaceRaw);
      } catch {
        agentWorkspaceDir = path.resolve(agentWorkspaceRaw);
      }
      storyRoot = path.join(agentWorkspaceDir, "stories", storyId);
    }
    const absPath = path.resolve(storyRoot, relPath);

    // Belt + suspenders: ensure resolved path stays inside the story root.
    if (!absPath.startsWith(storyRoot + path.sep) && absPath !== storyRoot) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `resolved path escapes story workspace`,
      ));
      return;
    }

    let st: import("node:fs").Stats;
    try {
      st = await fs.stat(absPath);
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `file not found: ${relPath}`,
      ));
      return;
    }
    if (!st.isFile()) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `not a file: ${relPath}`,
      ));
      return;
    }

    const MAX_READ_BYTES = 1024 * 1024; // 1 MB
    const size = st.size;
    const ext = path.extname(relPath).toLowerCase();
    const mime = mimeForExtension(ext);
    const isBinary = mime === "application/octet-stream";

    if (isBinary) {
      // Don't try to UTF-8 decode binaries. Empty content + flag.
      respond(true, { ok: true, path: relPath, content: "", mime, size, truncated: false }, undefined);
      return;
    }

    let content = "";
    let truncated = false;
    try {
      if (size > MAX_READ_BYTES) {
        const fh = await fs.open(absPath, "r");
        try {
          const buf = Buffer.alloc(MAX_READ_BYTES);
          await fh.read(buf, 0, MAX_READ_BYTES, 0);
          content = buf.toString("utf-8");
          truncated = true;
        } finally {
          await fh.close();
        }
      } else {
        content = await fs.readFile(absPath, "utf-8");
      }
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `read failed: ${(err as Error).message}`,
      ));
      return;
    }

    respond(true, { ok: true, path: relPath, content, mime, size, truncated }, undefined);
  },

  // Write a small file under the story workspace. Admin-only (operator.admin
  // scope) — agents on the runner cannot call this; only the control panel
  // can. Used by the credential broker to drop per-task SA key JSON into
  // <agent-ws>/stories/<storyId>/<path> before dispatch.
  //
  // Path is restricted to the story root with the same validation as
  // readFile (no `..`, no leading `/`, no null bytes, symlink-escape check).
  // Size capped at MAX_WRITE_BYTES so a runaway caller can't fill the disk.
  // Mode defaults to 0o644; callers should pass 0o600 for secrets.
  "shca.workspace.writeFile": async ({ params, respond }) => {
    if (!validateShcaWorkspaceWriteFileParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.writeFile",
        validateShcaWorkspaceWriteFileParams.errors,
      );
      return;
    }

    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    const relPath = String((params as any).path);
    const content = String((params as any).content ?? "");
    const requestedMode = typeof (params as any).mode === "number"
      ? (params as any).mode
      : 0o644;

    if (!/^[A-Za-z0-9._-]+$/.test(storyId)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `storyId must match [A-Za-z0-9._-]+, got ${storyId}`,
      ));
      return;
    }
    if (relPath.startsWith("/") || relPath.includes("..") || relPath.includes("\0")) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `path must be relative, no '..', no absolute prefix`,
      ));
      return;
    }

    const MAX_WRITE_BYTES = 256 * 1024; // 256 KiB — credentials are small
    const byteLength = Buffer.byteLength(content, "utf8");
    if (byteLength > MAX_WRITE_BYTES) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `content too large (${byteLength} bytes, max ${MAX_WRITE_BYTES})`,
      ));
      return;
    }

    const cfg = loadConfig();

    // Prefer the caller's explicit workspacePath (the control panel passes the
    // story's real provisioned dir), confined to the openclaw state dir and
    // required to be the story's own dir; else re-derive from the agent id.
    // Re-derivation is unreliable for long / uppercase agent ids because
    // normalizeAgentId lowercases+truncates (e.g. goal ids like MARL).
    const explicitPath = typeof (params as any).workspacePath === "string"
      ? String((params as any).workspacePath).trim() : "";
    let storyRoot: string;
    if (explicitPath) {
      const stateDirRaw = path.dirname(resolveAgentWorkspaceDir(cfg, agentId));
      let stateDir: string;
      try { stateDir = await fs.realpath(stateDirRaw); } catch { stateDir = path.resolve(stateDirRaw); }
      let abs: string;
      try { abs = await fs.realpath(explicitPath); } catch { abs = path.resolve(explicitPath); }
      if ((abs !== stateDir && !abs.startsWith(stateDir + path.sep)) || path.basename(abs) !== storyId) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `workspacePath must be the story dir within the openclaw state dir`,
        ));
        return;
      }
      storyRoot = abs;
    } else {
      if (!isConfiguredAgent(cfg, agentId)) {
        respondAgentNotFound(respond, agentId);
        return;
      }
      const agentWorkspaceRaw = resolveAgentWorkspaceDir(cfg, agentId);
      let agentWorkspaceDir: string;
      try {
        agentWorkspaceDir = await fs.realpath(agentWorkspaceRaw);
      } catch {
        agentWorkspaceDir = path.resolve(agentWorkspaceRaw);
      }
      storyRoot = path.join(agentWorkspaceDir, "stories", storyId);
    }
    const absPath = path.resolve(storyRoot, relPath);

    // Belt + suspenders: ensure resolved path stays inside the story root.
    if (!absPath.startsWith(storyRoot + path.sep) && absPath !== storyRoot) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `resolved path escapes story workspace`,
      ));
      return;
    }

    // Ensure the story root exists; reject if not — caller must run
    // shca.workspace.setup first.
    try {
      const rootStat = await fs.stat(storyRoot);
      if (!rootStat.isDirectory()) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `story root is not a directory: ${storyId}`,
        ));
        return;
      }
    } catch {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `story root does not exist: ${storyId} (run shca.workspace.setup first)`,
      ));
      return;
    }

    // Atomic write: write to temp file in the same directory, fsync, then
    // rename. Avoids a half-written credentials file racing the agent read.
    const parentDir = path.dirname(absPath);
    try {
      await fs.mkdir(parentDir, { recursive: true });
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `mkdir failed: ${(err as Error).message}`,
      ));
      return;
    }

    const tmpPath = `${absPath}.tmp-${process.pid}-${Date.now()}`;
    try {
      await fs.writeFile(tmpPath, content, { encoding: "utf8", mode: requestedMode });
      await fs.rename(tmpPath, absPath);
      // Re-chmod after rename — some platforms drop mode on rename.
      await fs.chmod(absPath, requestedMode);
    } catch (err) {
      // Best-effort cleanup of the temp file if rename failed.
      try { await fs.unlink(tmpPath); } catch { /* ignore */ }
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `write failed: ${(err as Error).message}`,
      ));
      return;
    }

    respond(true, { ok: true, path: relPath, size: byteLength }, undefined);
  },

  // Delete a previously-written file under the story workspace. Admin-only.
  // Idempotent: missing file returns ok:true with existed:false. Used by the
  // credential broker's revoke() at task end.
  "shca.workspace.deleteFile": async ({ params, respond }) => {
    if (!validateShcaWorkspaceDeleteFileParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.deleteFile",
        validateShcaWorkspaceDeleteFileParams.errors,
      );
      return;
    }

    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    const relPath = String((params as any).path);

    if (!/^[A-Za-z0-9._-]+$/.test(storyId)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `storyId must match [A-Za-z0-9._-]+, got ${storyId}`,
      ));
      return;
    }
    if (relPath.startsWith("/") || relPath.includes("..") || relPath.includes("\0")) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `path must be relative, no '..', no absolute prefix`,
      ));
      return;
    }

    const cfg = loadConfig();

    // Prefer the caller's explicit workspacePath (the control panel passes the
    // story's real provisioned dir), confined to the openclaw state dir and
    // required to be the story's own dir; else re-derive from the agent id.
    // Re-derivation is unreliable for long / uppercase agent ids because
    // normalizeAgentId lowercases+truncates (e.g. goal ids like MARL).
    const explicitPath = typeof (params as any).workspacePath === "string"
      ? String((params as any).workspacePath).trim() : "";
    let storyRoot: string;
    if (explicitPath) {
      const stateDirRaw = path.dirname(resolveAgentWorkspaceDir(cfg, agentId));
      let stateDir: string;
      try { stateDir = await fs.realpath(stateDirRaw); } catch { stateDir = path.resolve(stateDirRaw); }
      let abs: string;
      try { abs = await fs.realpath(explicitPath); } catch { abs = path.resolve(explicitPath); }
      if ((abs !== stateDir && !abs.startsWith(stateDir + path.sep)) || path.basename(abs) !== storyId) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `workspacePath must be the story dir within the openclaw state dir`,
        ));
        return;
      }
      storyRoot = abs;
    } else {
      if (!isConfiguredAgent(cfg, agentId)) {
        respondAgentNotFound(respond, agentId);
        return;
      }
      const agentWorkspaceRaw = resolveAgentWorkspaceDir(cfg, agentId);
      let agentWorkspaceDir: string;
      try {
        agentWorkspaceDir = await fs.realpath(agentWorkspaceRaw);
      } catch {
        agentWorkspaceDir = path.resolve(agentWorkspaceRaw);
      }
      storyRoot = path.join(agentWorkspaceDir, "stories", storyId);
    }
    const absPath = path.resolve(storyRoot, relPath);

    if (!absPath.startsWith(storyRoot + path.sep) && absPath !== storyRoot) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `resolved path escapes story workspace`,
      ));
      return;
    }

    let existed = true;
    try {
      await fs.unlink(absPath);
    } catch (err: any) {
      if (err && err.code === "ENOENT") {
        existed = false;
      } else {
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR,
          `delete failed: ${(err as Error).message}`,
        ));
        return;
      }
    }

    respond(true, { ok: true, path: relPath, existed }, undefined);
  },

  // Commit + push specific paths from the story repo. Returns the new
  // commit sha. If push=true (default) and the push fails, still returns
  // ok:true with push_error set — the commit landed locally, the caller
  // can decide how to surface the failure.
  "shca.workspace.gitCommit": async ({ params, respond }) => {
    if (!validateShcaWorkspaceGitCommitParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.gitCommit",
        validateShcaWorkspaceGitCommitParams.errors,
      );
      return;
    }
    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    const paths = (params as any).paths as string[];
    const message = String((params as any).message);
    const push = (params as any).push !== false;
    // Multi-repo (plan G.5): commit + push EVERY write repo. Opt-in, so existing
    // callers keep the single-repo response shape they parse today.
    const allRepos = (params as any).allRepos === true;

    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }
    const allDirs = await _resolveStoryRepoDirs(cfg, agentId, storyId);
    // Never commit in a read-only context clone — the story has no write grant there
    // and the push would be rejected (or worse, accepted for a repo nobody reviewed).
    const commitTargets = allRepos ? allDirs.filter((d) => d.write) : allDirs.slice(0, 1);
    if (commitTargets.length === 0) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `story repo not found at <agent-ws>/stories/${storyId}/repo`,
      ));
      return;
    }
    const repoDir = commitTargets[0].repoPath;
    const gitBin = _resolveGitBin();
    if (!gitBin) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git binary not found`,
      ));
      return;
    }

    // MULTI-REPO PATH. Each repo is committed and pushed independently and the
    // per-repo outcome is reported, because there is no way to make N pushes atomic:
    // repo A can land while repo B's push is rejected. Reporting each outcome lets the
    // control panel show a partial-land state instead of a single misleading ok/fail.
    if (allRepos) {
      const results: Array<{
        subdir: string; commit_sha: string; pushed: boolean;
        nothing_to_commit?: boolean; push_error?: string; error?: string;
      }> = [];
      for (const target of commitTargets) {
        const res = await _commitOneRepo(
          gitBin, target.repoPath, paths, message, push,
        );
        results.push({ subdir: target.subdir, ...res });
      }
      const failed = results.filter((r) => r.error);
      const primaryRes = results[0];
      respond(true, {
        ok: failed.length === 0,
        // Legacy top-level fields describe the primary repo.
        commit_sha: primaryRes.commit_sha,
        pushed: results.every((r) => r.pushed),
        ...(results.every((r) => r.nothing_to_commit) ? { nothing_to_commit: true } : {}),
        ...(failed.length ? { error: failed.map((f) => `${f.subdir}: ${f.error}`).join("; ") } : {}),
        repos: results,
      }, undefined);
      return;
    }

    // Add paths. "all" → add -A; otherwise add each path individually.
    if (paths.length === 1 && paths[0] === "all") {
      const r = await _runGit(gitBin, ["-C", repoDir, "add", "-A"], {});
      if (!r.ok) {
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR,
          `git add -A failed: ${r.stderr.slice(0, 600)}`,
        ));
        return;
      }
    } else {
      for (const p of paths) {
        const r = await _runGit(gitBin, ["-C", repoDir, "add", "--", p], {});
        if (!r.ok) {
          respond(false, undefined, errorShape(
            ErrorCodes.INTERNAL_ERROR,
            `git add ${p} failed: ${r.stderr.slice(0, 400)}`,
          ));
          return;
        }
      }
    }

    // Check for staged changes. If none, there is nothing NEW to commit — but the
    // branch may still have UNPUSHED commits that must reach the remote: the
    // assistant may have already committed its work, or (common for engine sync/diff
    // stories) a `git pull <trunk>` created a merge commit. Bailing here without
    // pushing strands those commits locally, so the remote — which the git engine
    // compares against trunk — never reflects the work and the merge never lands.
    // So on nothing-to-commit we STILL push when requested (a no-op "Everything
    // up-to-date" if the branch is already in sync with its remote).
    const statusR = await _runGit(gitBin, ["-C", repoDir, "diff", "--staged", "--quiet"], {});
    if (statusR.code === 0) {
      if (!push) {
        respond(true, {
          ok: true, commit_sha: "", pushed: false, nothing_to_commit: true,
        }, undefined);
        return;
      }
      const branchR0 = await _runGit(gitBin,
        ["-C", repoDir, "symbolic-ref", "--short", "HEAD"], {});
      const branch0 = branchR0.stdout.trim() || "HEAD";
      const headR0 = await _runGit(gitBin, ["-C", repoDir, "rev-parse", "HEAD"], {});
      const head0 = headR0.stdout.trim();
      const pushR0 = await _runGit(gitBin,
        ["-C", repoDir, "push", "origin", branch0],
        { timeoutMs: 120_000 });
      if (!pushR0.ok) {
        respond(true, {
          ok: true, commit_sha: head0, pushed: false,
          nothing_to_commit: true, push_error: pushR0.stderr.slice(0, 600),
        }, undefined);
        return;
      }
      respond(true, {
        ok: true, commit_sha: head0, pushed: true, nothing_to_commit: true,
      }, undefined);
      return;
    }

    // Commit.
    const commitR = await _runGit(gitBin,
      ["-C", repoDir, "commit", "-m", message],
      { timeoutMs: 60_000 },
    );
    if (!commitR.ok) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git commit failed: ${commitR.stderr.slice(0, 600)}`,
      ));
      return;
    }
    const shaR = await _runGit(gitBin, ["-C", repoDir, "rev-parse", "HEAD"], {});
    const commit_sha = shaR.stdout.trim();

    if (!push) {
      respond(true, { ok: true, commit_sha, pushed: false }, undefined);
      return;
    }

    // Push. Failure is non-fatal — commit landed locally.
    const branchR = await _runGit(gitBin,
      ["-C", repoDir, "symbolic-ref", "--short", "HEAD"], {});
    const branch = branchR.stdout.trim() || "HEAD";

    const pushR = await _runGit(gitBin,
      ["-C", repoDir, "push", "origin", branch],
      { timeoutMs: 120_000 },
    );
    if (!pushR.ok) {
      respond(true, {
        ok: true,
        commit_sha,
        pushed: false,
        push_error: pushR.stderr.slice(0, 600),
      }, undefined);
      return;
    }

    respond(true, { ok: true, commit_sha, pushed: true }, undefined);
  },

  // Capture the current diff. Optionally also writes it to a file in
  // the workspace so the file viewer can render it for human review.
  "shca.workspace.gitDiff": async ({ params, respond }) => {
    if (!validateShcaWorkspaceGitDiffParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.gitDiff",
        validateShcaWorkspaceGitDiffParams.errors,
      );
      return;
    }
    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    const scope = (params as any).scope || "all";
    const writeToPath = (params as any).writeToPath as string | undefined;
    // Multi-repo (plan G.4): diff EVERY write repo instead of just the primary.
    // Opt-in so existing callers keep the exact single-repo response they parse.
    const allRepos = (params as any).allRepos === true;

    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }
    const allDirs = await _resolveStoryRepoDirs(cfg, agentId, storyId);
    // Read-only context clones are excluded even under allRepos: the story cannot
    // commit there, so any diff in one is not part of the change under review.
    const targets = allRepos ? allDirs.filter((d) => d.write) : allDirs.slice(0, 1);
    if (targets.length === 0) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `story repo not found`,
      ));
      return;
    }
    const gitBin = _resolveGitBin();
    if (!gitBin) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `git binary not found`,
      ));
      return;
    }

    const MAX_DIFF_BYTES = 1024 * 1024; // 1 MB, per repo

    async function diffOneRepo(repoDir: string): Promise<
      { ok: true; diff: string; truncated: boolean; statSummary: string }
      | { ok: false; error: string }
    > {
      // Pick the right diff args.
      let diffArgs: string[];
      if (scope === "staged") {
        diffArgs = ["-C", repoDir, "diff", "--staged"];
      } else if (scope === "unstaged") {
        diffArgs = ["-C", repoDir, "diff"];
      } else if (scope === "vs_origin") {
        const branchR = await _runGit(gitBin as string,
          ["-C", repoDir, "symbolic-ref", "--short", "HEAD"], {});
        const branch = branchR.stdout.trim();
        diffArgs = ["-C", repoDir, "diff", `origin/${branch}...HEAD`];
      } else {
        // "all" = staged + unstaged + untracked. `git diff HEAD` alone
        // skips untracked files; mark them intent-to-add (-N) so they
        // surface as additions in the diff. This adds zero-blob index
        // entries — no content is staged, `git commit` won't commit them,
        // and a later `git add -A` cleanly replaces them.
        const intentR = await _runGit(gitBin as string,
          ["-C", repoDir, "add", "--intent-to-add", "-A"],
          { timeoutMs: 30_000 },
        );
        if (!intentR.ok) {
          return { ok: false, error: `git add --intent-to-add failed: ${intentR.stderr.slice(0, 400)}` };
        }
        diffArgs = ["-C", repoDir, "diff", "HEAD"];
      }

      const r = await _runGit(gitBin as string, diffArgs, { timeoutMs: 60_000 });
      if (!r.ok) {
        return { ok: false, error: `git diff failed: ${r.stderr.slice(0, 600)}` };
      }

      let diff = r.stdout;
      let truncated = false;
      if (diff.length > MAX_DIFF_BYTES) {
        diff = diff.slice(0, MAX_DIFF_BYTES);
        truncated = true;
      }

      // Stat summary (e.g. "5 files changed, 100 insertions(+), 20 deletions(-)")
      const statR = await _runGit(gitBin as string, [...diffArgs, "--shortstat"],
        { timeoutMs: 30_000 });
      return { ok: true, diff, truncated, statSummary: statR.stdout.trim() };
    }

    const perRepo: Array<{
      subdir: string; diff: string; truncated: boolean; stat_summary: string;
    }> = [];
    for (const t of targets) {
      const res = await diffOneRepo(t.repoPath);
      if (!res.ok) {
        respond(false, undefined, errorShape(
          ErrorCodes.INTERNAL_ERROR, `${t.subdir}: ${res.error}`,
        ));
        return;
      }
      perRepo.push({
        subdir: t.subdir, diff: res.diff, truncated: res.truncated,
        stat_summary: res.statSummary,
      });
    }

    // Legacy top-level fields describe the PRIMARY repo. Under allRepos the diffs are
    // concatenated with a per-repo banner so a caller that only reads `diff` still
    // sees every change rather than silently reviewing one repo out of N.
    const primary = perRepo[0];
    const combinedDiff = allRepos && perRepo.length > 1
      ? perRepo.map((p) => `=== ${p.subdir} ===\n${p.diff}`).join("\n")
      : primary.diff;
    const combinedStat = allRepos && perRepo.length > 1
      ? perRepo.map((p) => `${p.subdir}: ${p.stat_summary || "no changes"}`).join("; ")
      : primary.stat_summary;

    // Optional: write diff to a file in the story workspace.
    let writtenTo: string | undefined;
    if (writeToPath) {
      if (writeToPath.startsWith("/") || writeToPath.includes("..")) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `writeToPath must be relative, no '..'`,
        ));
        return;
      }
      // The story workspace root — NOT path.dirname(repoDir). That shortcut was only
      // correct for the legacy `<ws>/repo` layout; for `<ws>/repos/<name>` it resolves
      // to `<ws>/repos` and would write the diff inside the clones.
      const storyWs = await _resolveStoryWorkspaceDir(cfg, agentId, storyId);
      const abs = path.resolve(storyWs, writeToPath);
      if (!abs.startsWith(storyWs + path.sep)) {
        respond(false, undefined, errorShape(
          ErrorCodes.INVALID_REQUEST,
          `writeToPath resolved outside story workspace`,
        ));
        return;
      }
      await fs.mkdir(path.dirname(abs), { recursive: true });
      await fs.writeFile(abs, combinedDiff, "utf-8");
      writtenTo = writeToPath;
    }

    respond(true, {
      ok: true,
      diff: combinedDiff,
      scope,
      written_to: writtenTo,
      truncated: perRepo.some((p) => p.truncated),
      stat_summary: combinedStat,
      ...(allRepos ? { repos: perRepo } : {}),
    }, undefined);
  },

  // Update the local repo's credential.helper with a fresh GitHub token.
  // Called by the control panel's 50-min refresh loop because GitHub
  // installation tokens have a 1h hard cap.
  "shca.workspace.refreshToken": async ({ params, respond }) => {
    if (!validateShcaWorkspaceRefreshTokenParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.refreshToken",
        validateShcaWorkspaceRefreshTokenParams.errors,
      );
      return;
    }
    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    const githubToken = String((params as any).githubToken);

    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }
    // EVERY write clone, not just the first. This RPC was not migrated with
    // gitDiff/gitCommit, so on a multi-write story it refreshed only the primary clone
    // and the others kept the ~1h setup token — then started failing pushes with no
    // obvious cause. Read-only clones are skipped: they deliberately have no remote and
    // no credential helper.
    const refreshDirs = (await _resolveStoryRepoDirs(cfg, agentId, storyId))
      .filter((d) => d.write);
    if (refreshDirs.length === 0) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `story repo not found`,
      ));
      return;
    }
    const gitBin = _resolveGitBin();
    if (!gitBin) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `git binary not found`,
      ));
      return;
    }

    const helperCmd =
      `!f() { echo "username=x-access-token"; echo "password=${githubToken}"; }; f`;

    const refreshed: string[] = [];
    for (const target of refreshDirs) {
      const repoDir = target.repoPath;

    // Wipe any prior LOCAL credential.helper entries. Multiple values
    // accumulate over refreshes because `git config key value` only
    // replaces when there's exactly one prior entry; with multiple it
    // errors out, and with append-style writes you end up with stacks
    // of stale tokens. Ignore the exit code — 5 means the key didn't
    // exist, which is fine.
    await _runGit(gitBin,
      ["-C", repoDir, "config", "--unset-all", "credential.helper"], {});

    // Disable INHERITED credential.helper from global / system config.
    // RESOURCES.md provisioning sets a global helper with the initial
    // GitHub App token, which expires 1h later. Per git-config(1):
    // "To disable a helper that was configured in a system or global
    //  configuration, set credential.helper to an empty string in the
    //  more specific configuration." So we add an empty entry first;
    // then any inherited helpers are ignored.
    const blankR = await _runGit(gitBin,
      ["-C", repoDir, "config", "--add", "credential.helper", ""], {});
    if (!blankR.ok) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git config (disable inherited) failed: ${blankR.stderr.slice(0, 400)}`,
      ));
      return;
    }

    // Add our fresh inline helper.
    const r = await _runGit(gitBin,
      ["-C", repoDir, "config", "--add", "credential.helper", helperCmd], {});
    if (!r.ok) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git config credential.helper failed for ${target.subdir}: ` +
        `${r.stderr.slice(0, 400)}`,
      ));
      return;
    }
      refreshed.push(target.subdir);
    }

    respond(true, { ok: true, ...(refreshed.length > 1 ? { repos: refreshed } : {}) }, undefined);
  },

  // Fetch the trunk and merge it into the story's branch LOCALLY, returning any
  // conflict paths for the assistant to resolve (plan §5.5.5). The clean-path
  // trunk→branch sync is performed remotely by the engine via the GitHub API;
  // this is the LOCAL conflict-resolution primitive used in a merge story. On
  // conflict the merge is left in progress (markers in the worktree) so the
  // assistant edits + commits (shca.workspace.gitCommit) to complete it.
  "shca.workspace.syncBranch": async ({ params, respond }) => {
    if (!validateShcaWorkspaceSyncBranchParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.syncBranch",
        validateShcaWorkspaceSyncBranchParams.errors,
      );
      return;
    }
    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    const trunkBranch = String((params as any).trunkBranch);
    const githubToken = String((params as any).githubToken);

    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }
    const repoDir = await _resolveStoryRepoDir(cfg, agentId, storyId);
    if (!repoDir) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST, `story repo not found`,
      ));
      return;
    }
    const gitBin = _resolveGitBin();
    if (!gitBin) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `git binary not found`,
      ));
      return;
    }

    // Refresh the local credential.helper with the fresh token so the fetch is
    // authed (same approach as refreshToken; GitHub App tokens expire after 1h).
    const helperCmd =
      `!f() { echo "username=x-access-token"; echo "password=${githubToken}"; }; f`;
    await _runGit(gitBin, ["-C", repoDir, "config", "--unset-all", "credential.helper"], {});
    await _runGit(gitBin, ["-C", repoDir, "config", "--add", "credential.helper", ""], {});
    const credR = await _runGit(gitBin,
      ["-C", repoDir, "config", "--add", "credential.helper", helperCmd], {});
    if (!credR.ok) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git config credential.helper failed: ${credR.stderr.slice(0, 400)}`,
      ));
      return;
    }

    // Fetch the trunk branch into FETCH_HEAD.
    const fetchR = await _runGit(gitBin,
      ["-C", repoDir, "fetch", "origin", trunkBranch],
      { timeoutMs: 120_000 });
    if (!fetchR.ok) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git fetch origin ${trunkBranch} failed: ${fetchR.stderr.slice(0, 600)}`,
      ));
      return;
    }

    // Merge trunk into the current (story) branch.
    const mergeR = await _runGit(gitBin,
      ["-C", repoDir, "merge", "--no-edit", "FETCH_HEAD"],
      { timeoutMs: 120_000 });

    const headR = await _runGit(gitBin, ["-C", repoDir, "rev-parse", "HEAD"], {});
    const head_sha = headR.stdout.trim();

    if (mergeR.ok) {
      const already = /already up to date/i.test(mergeR.stdout);
      respond(true, {
        ok: true,
        merged: !already,
        already_up_to_date: already,
        conflicted: false,
        conflict_paths: [],
        head_sha,
      }, undefined);
      return;
    }

    // Non-zero merge: detect conflicts (left in progress for the assistant to
    // resolve). A merge that fails with NO conflicting paths is a different
    // problem (e.g. a dirty work tree) — surface it as an error.
    const confR = await _runGit(gitBin,
      ["-C", repoDir, "diff", "--name-only", "--diff-filter=U"], {});
    const conflict_paths = confR.stdout
      .split("\n").map((s) => s.trim()).filter(Boolean);
    if (conflict_paths.length === 0) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git merge failed (no conflicts detected): ${mergeR.stderr.slice(0, 600)}`,
      ));
      return;
    }
    respond(true, {
      ok: true,
      merged: false,
      already_up_to_date: false,
      conflicted: true,
      conflict_paths,
      head_sha,
    }, undefined);
  },

  // Fast-forward the story's LOCAL branch to its remote tip (cooperative pull,
  // plan §5.5.2): fetch origin <branch> then `git merge --ff-only`. Used at the
  // start of a merge story and by continue-mode provisioning to pick up commits
  // the engine restacked or a handoff pushed. Never merges trunk (that's
  // syncBranch), never force-updates; a diverged local reports ff_failed and
  // leaves the worktree untouched.
  "shca.workspace.pullBranch": async ({ params, respond }) => {
    if (!validateShcaWorkspacePullBranchParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.pullBranch",
        validateShcaWorkspacePullBranchParams.errors,
      );
      return;
    }
    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    const branch = String((params as any).branch);
    const githubToken = String((params as any).githubToken);

    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }
    const repoDir = await _resolveStoryRepoDir(cfg, agentId, storyId);
    if (!repoDir) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST, `story repo not found`,
      ));
      return;
    }
    const gitBin = _resolveGitBin();
    if (!gitBin) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR, `git binary not found`,
      ));
      return;
    }

    // Fresh credential.helper so the fetch is authed (same as refreshToken).
    const helperCmd =
      `!f() { echo "username=x-access-token"; echo "password=${githubToken}"; }; f`;
    await _runGit(gitBin, ["-C", repoDir, "config", "--unset-all", "credential.helper"], {});
    await _runGit(gitBin, ["-C", repoDir, "config", "--add", "credential.helper", ""], {});
    const credR = await _runGit(gitBin,
      ["-C", repoDir, "config", "--add", "credential.helper", helperCmd], {});
    if (!credR.ok) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git config credential.helper failed: ${credR.stderr.slice(0, 400)}`,
      ));
      return;
    }

    const fetchR = await _runGit(gitBin,
      ["-C", repoDir, "fetch", "origin", branch],
      { timeoutMs: 120_000 });
    if (!fetchR.ok) {
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `git fetch origin ${branch} failed: ${fetchR.stderr.slice(0, 600)}`,
      ));
      return;
    }

    // Fast-forward only. A diverged local (own commits + remote moved) fails ff;
    // we report ff_failed and leave the worktree untouched (caller decides).
    const mergeR = await _runGit(gitBin,
      ["-C", repoDir, "merge", "--ff-only", "FETCH_HEAD"],
      { timeoutMs: 60_000 });
    const headR = await _runGit(gitBin, ["-C", repoDir, "rev-parse", "HEAD"], {});
    const head_sha = headR.stdout.trim();

    if (mergeR.ok) {
      const already = /already up to date/i.test(mergeR.stdout);
      respond(true, {
        ok: true,
        updated: !already,
        already_current: already,
        ff_failed: false,
        head_sha,
      }, undefined);
      return;
    }
    respond(true, {
      ok: true,
      updated: false,
      already_current: false,
      ff_failed: true,
      head_sha,
    }, undefined);
  },

  // Spawn or resume a claude-cli session directly from the control panel.
  // Wraps spawnCliDirect() — same underlying machinery as the
  // sessions_spawn(runtime="cli") agent tool, but exposed as a gateway
  // RPC so the control panel's task dispatcher can call it without
  // routing through an agent's tool-use loop.
  "shca.claude.invoke": async ({ params, respond }) => {
    if (!validateShcaClaudeInvokeParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.claude.invoke",
        validateShcaClaudeInvokeParams.errors,
      );
      return;
    }

    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);
    // goalId lets spawnCliDirect mint a goal-scoped hook token instead of
    // handing the child the shared gateway admin token (shared-token fix).
    const goalId = ((params as any).goalId || "").toString() || undefined;
    const cwd = String((params as any).cwd);
    const task = String((params as any).task);
    const resumeSessionId = ((params as any).resumeSessionId || "").toString() || undefined;
    const taskId = ((params as any).taskId || "").toString() || undefined;
    const model = ((params as any).model || "").toString() || undefined;
    const appendSystemPrompt = ((params as any).appendSystemPrompt || "").toString() || undefined;
    const timeoutMs = typeof (params as any).timeoutMs === "number"
      ? (params as any).timeoutMs
      : undefined;

    // Existence check on the agent for clean error messages.
    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }

    // The gateway OWNS the workspace layout — pass agentId so spawnCliDirect
    // DERIVES the cwd (<agent-ws>/stories/<storyId>) rather than trusting the
    // caller-supplied `cwd` (stories.workspace_path). A stale/empty value there
    // can therefore never run claude at the SHCA agent root. `cwd` is kept only
    // as a fallback for when derivation can't run.
    const { spawnCliDirect } = await import("../../agents/cli-spawn.js");
    const result = await spawnCliDirect({
      task,
      agentId,
      cwd,
      resumeSessionId,
      model,
      appendSystemPrompt,
      timeoutMs,
      storyId,
      goalId,
      taskId,
      controlPanelUrl: process.env.CONTROL_PANEL_URL,
      internalApiKey: process.env.OPENCLAW_GATEWAY_TOKEN,
    });

    respond(true, {
      ok: true,
      status: result.status,
      sessionId: result.sessionId,
      text: result.text,
      exitCode: result.exitCode,
      durationMs: result.durationMs,
      ...(result.error ? { error: result.error } : {}),
      ...(result.stderrSnippet ? { stderrSnippet: result.stderrSnippet } : {}),
    }, undefined);
  },

  // Kill switch — force-kill a hung claude-code-phase turn (by taskId) or a
  // SHCA planning turn (by storyId). The control panel calls this when an
  // operator force-kills a stuck agent. spawnCliDirect deliberately bypasses
  // the run-registry, so chat.abort can't reach these children — abortCliByTask
  // / abortCliByStory look the child up in the respective map and SIGTERM/SIGKILL
  // it. Idempotent: aborting an unknown/already-finished id returns { aborted: false }.
  // Accepts taskId OR storyId; taskId takes precedence when both are provided.
  "shca.claude.abort": async ({ params, respond }) => {
    const taskId  = ((params as any)?.taskId  || "").toString();
    const storyId = ((params as any)?.storyId || "").toString();
    if (!taskId && !storyId) {
      respond(true, { ok: false, aborted: false, error: "taskId or storyId is required" }, undefined);
      return;
    }
    const { abortCliByTask, abortCliByStory } = await import("../../agents/cli-spawn.js");
    const aborted = taskId ? abortCliByTask(taskId) : abortCliByStory(storyId);
    respond(true, { ok: true, aborted }, undefined);
  },

  // Kill switch — abort in-flight embedded PI runs (IA/GO/SHCA non-CLI agents
  // managed by pi-embedded-runner). Accepts an optional sessionId to abort a
  // single run, or no params to abort all active embedded runs (system-wide
  // pause). Idempotent: aborting an unknown session returns { aborted: false }.
  "shca.embedded.abort": async ({ params, respond }) => {
    const sessionId = ((params as any)?.sessionId || "").toString();
    const { abortEmbeddedPiRun } = await import("../../agents/pi-embedded-runner/runs.js");
    const aborted = sessionId
      ? abortEmbeddedPiRun(sessionId)
      : abortEmbeddedPiRun(undefined, { mode: "all" });
    respond(true, { ok: true, aborted }, undefined);
  },

  // Discover the claude-cli session id for a story by listing
  // ~/.claude/projects/<encoded-workspace>/*.jsonl. claude-cli encodes
  // the cwd by replacing `/` with `-` and prepending another `-`.
  // Returns the most recently modified session file's name (minus
  // .jsonl), or empty strings if none found.
  "shca.workspace.findClaudeSession": async ({ params, respond }) => {
    if (!validateShcaWorkspaceFindClaudeSessionParams(params)) {
      respondInvalidMethodParams(
        respond, "shca.workspace.findClaudeSession",
        validateShcaWorkspaceFindClaudeSessionParams.errors,
      );
      return;
    }
    const agentId = normalizeAgentId(String((params as any).agentId));
    const storyId = String((params as any).storyId);

    const cfg = loadConfig();
    if (!isConfiguredAgent(cfg, agentId)) {
      respondAgentNotFound(respond, agentId);
      return;
    }

    const agentWorkspaceRaw = resolveAgentWorkspaceDir(cfg, agentId);
    let agentWorkspaceDir: string;
    try {
      agentWorkspaceDir = await fs.realpath(agentWorkspaceRaw);
    } catch {
      agentWorkspaceDir = path.resolve(agentWorkspaceRaw);
    }
    const workspaceCwd = path.join(agentWorkspaceDir, "stories", storyId);
    // claude-cli's project-dir naming convention: replace BOTH '/' and '.'
    // with '-' (so /home/openclaw/.openclaw/ becomes -home-openclaw--openclaw-
    // with the double-dash from the dot+slash collision around .openclaw).
    const encoded = "-" + workspaceCwd.replace(/[\/.]/g, "-");
    const home = process.env.HOME || "/home/openclaw";
    const projectDir = path.join(home, ".claude", "projects", encoded);

    let entries: import("node:fs").Dirent[] = [];
    try {
      entries = await fs.readdir(projectDir, { withFileTypes: true });
    } catch {
      respond(true, { ok: true, sessionId: "", sessionFile: "", candidateCount: 0 }, undefined);
      return;
    }

    const sessions: Array<{ name: string; path: string; mtimeMs: number }> = [];
    for (const ent of entries) {
      if (!ent.isFile() || !ent.name.endsWith(".jsonl")) continue;
      const abs = path.join(projectDir, ent.name);
      try {
        const st = await fs.stat(abs);
        sessions.push({
          name: ent.name.replace(/\.jsonl$/, ""),
          path: abs,
          mtimeMs: st.mtimeMs,
        });
      } catch { /* skip */ }
    }
    if (sessions.length === 0) {
      respond(true, { ok: true, sessionId: "", sessionFile: "", candidateCount: 0 }, undefined);
      return;
    }
    sessions.sort((a, b) => b.mtimeMs - a.mtimeMs);
    const latest = sessions[0];
    respond(true, {
      ok: true,
      sessionId: latest.name,
      sessionFile: latest.path,
      candidateCount: sessions.length,
    }, undefined);
  },
};

// ── Git helpers shared by the workspace RPCs above ──────────────────────────

// Ubuntu (prod VM) paths -- the only ones that are ever real in a deployed
// environment; the systemd-rooted gateway may not have /usr/bin on its
// inherited PATH, hence resolving an absolute path instead of trusting PATH.
const _GIT_CANDIDATES_PROD = ["/usr/bin/git", "/usr/local/bin/git", "/bin/git"];
// Standard Windows Git-for-Windows install locations. NOT a production
// concern (the gateway VM is always Ubuntu) -- purely a local-dev
// accommodation, so it only applies when OPENCLAW_LOCAL_DEV=1, a flag our
// own local-development/scripts/run/gateway.ps1 sets (never present on the
// deployed VM). Keeps this list from silently growing prod's search surface
// for something prod can never use.
const _GIT_CANDIDATES_LOCAL_DEV_WIN32 = [
  "C:\\Program Files\\Git\\cmd\\git.exe",
  "C:\\Program Files\\Git\\bin\\git.exe",
  "C:\\Program Files (x86)\\Git\\cmd\\git.exe",
  "C:\\Program Files (x86)\\Git\\bin\\git.exe",
];
const _GIT_CANDIDATES = process.env.OPENCLAW_LOCAL_DEV === "1"
  ? [..._GIT_CANDIDATES_PROD, ..._GIT_CANDIDATES_LOCAL_DEV_WIN32]
  : _GIT_CANDIDATES_PROD;
let _gitBinCached: string | null | undefined;

function _resolveGitBin(): string | null {
  if (_gitBinCached !== undefined) return _gitBinCached;
  const { existsSync } = require("node:fs");
  for (const c of _GIT_CANDIDATES) {
    if (existsSync(c)) { _gitBinCached = c; return c; }
  }
  _gitBinCached = null;
  return null;
}

/**
 * Stage → commit → push ONE repo. Shared by the multi-repo branch of
 * `shca.workspace.gitCommit` (plan G.5).
 *
 * Mirrors the single-repo path's semantics exactly, including the subtle one: when
 * there is nothing staged we STILL push if asked, because the branch may hold
 * unpushed commits (the assistant already committed, or a trunk pull created a merge
 * commit) and leaving those local means the git engine — which compares the REMOTE
 * against trunk — never sees the work.
 *
 * Never throws for a git-level failure; returns `error` so the caller can report a
 * per-repo outcome rather than failing the whole multi-repo commit.
 */
async function _commitOneRepo(
  gitBin: string, repoDir: string, paths: string[], message: string, push: boolean,
): Promise<{
  commit_sha: string; pushed: boolean; nothing_to_commit?: boolean;
  push_error?: string; error?: string;
}> {
  if (paths.length === 1 && paths[0] === "all") {
    const r = await _runGit(gitBin, ["-C", repoDir, "add", "-A"], {});
    if (!r.ok) {
      return { commit_sha: "", pushed: false, error: `git add -A failed: ${r.stderr.slice(0, 400)}` };
    }
  } else {
    for (const p of paths) {
      const r = await _runGit(gitBin, ["-C", repoDir, "add", "--", p], {});
      if (!r.ok) {
        return { commit_sha: "", pushed: false, error: `git add ${p} failed: ${r.stderr.slice(0, 300)}` };
      }
    }
  }

  async function pushCurrentBranch(sha: string, nothingToCommit: boolean) {
    const branchR = await _runGit(gitBin,
      ["-C", repoDir, "symbolic-ref", "--short", "HEAD"], {});
    const branch = branchR.stdout.trim() || "HEAD";
    const pushR = await _runGit(gitBin, ["-C", repoDir, "push", "origin", branch],
      { timeoutMs: 120_000 });
    return {
      commit_sha: sha,
      pushed: pushR.ok,
      ...(nothingToCommit ? { nothing_to_commit: true } : {}),
      ...(pushR.ok ? {} : { push_error: pushR.stderr.slice(0, 600) }),
    };
  }

  const staged = await _runGit(gitBin, ["-C", repoDir, "diff", "--staged", "--quiet"], {});
  if (staged.code === 0) {
    if (!push) return { commit_sha: "", pushed: false, nothing_to_commit: true };
    const headR = await _runGit(gitBin, ["-C", repoDir, "rev-parse", "HEAD"], {});
    return await pushCurrentBranch(headR.stdout.trim(), true);
  }

  const commitR = await _runGit(gitBin, ["-C", repoDir, "commit", "-m", message],
    { timeoutMs: 60_000 });
  if (!commitR.ok) {
    return { commit_sha: "", pushed: false, error: `git commit failed: ${commitR.stderr.slice(0, 400)}` };
  }
  const shaR = await _runGit(gitBin, ["-C", repoDir, "rev-parse", "HEAD"], {});
  const commit_sha = shaR.stdout.trim();
  if (!push) return { commit_sha, pushed: false };
  return await pushCurrentBranch(commit_sha, false);
}

async function _resolveStoryWorkspaceDir(
  cfg: ReturnType<typeof loadConfig>,
  agentId: string,
  storyId: string,
): Promise<string> {
  const raw = resolveAgentWorkspaceDir(cfg, agentId);
  let agentWs: string;
  try {
    agentWs = await fs.realpath(raw);
  } catch {
    agentWs = path.resolve(raw);
  }
  return path.join(agentWs, "stories", storyId);
}

/**
 * Every git clone in a story workspace, in a stable order.
 *
 * Two layouts coexist by design (plan Phase G): the legacy single-repo layout has
 * one clone at `<workspace>/repo`, the multi-repo layout has N clones at
 * `<workspace>/repos/<subdir>`. A story provisioned before the multi-repo rollout
 * keeps the legacy layout for its whole life — nothing migrates it — so callers must
 * handle both indefinitely, not just during the transition.
 *
 * A directory counts only if it actually contains `.git`, which keeps a stray
 * `repos/` scaffolding dir from being mistaken for a clone.
 */
async function _resolveStoryRepoDirs(
  cfg: ReturnType<typeof loadConfig>,
  agentId: string,
  storyId: string,
): Promise<Array<{ subdir: string; repoPath: string; write: boolean }>> {
  const wsDir = await _resolveStoryWorkspaceDir(cfg, agentId, storyId);

  async function isGitClone(dir: string): Promise<boolean> {
    try {
      const st = await fs.stat(path.join(dir, ".git"));
      return st.isDirectory() || st.isFile();  // .git is a file in worktrees
    } catch {
      return false;
    }
  }

  // The manifest written by shca.workspace.setup is the ONLY source of the
  // write/read-only flag — it cannot be inferred from the filesystem. Absent (legacy
  // workspace, or a best-effort write that failed) every clone is treated as
  // writable, which is exactly the pre-multi-repo behaviour.
  const writeBySubdir = new Map<string, boolean>();
  try {
    const raw = await fs.readFile(
      path.join(wsDir, ".openclaw-story-repos.json"), "utf-8",
    );
    const parsed = JSON.parse(raw) as { repos?: Array<{ subdir?: string; write?: boolean }> };
    for (const entry of parsed.repos ?? []) {
      if (entry.subdir) writeBySubdir.set(entry.subdir, entry.write !== false);
    }
  } catch { /* no manifest — every clone counts as writable */ }

  const found: Array<{ subdir: string; repoPath: string; write: boolean }> = [];

  const legacyDir = path.join(wsDir, "repo");
  if (await isGitClone(legacyDir)) {
    found.push({ subdir: "repo", repoPath: legacyDir, write: true });
  }

  const reposParent = path.join(wsDir, "repos");
  try {
    const entries = await fs.readdir(reposParent, { withFileTypes: true });
    // Sort by name for a deterministic base order, then hoist write repos below.
    for (const ent of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      if (!ent.isDirectory()) continue;
      const abs = path.join(reposParent, ent.name);
      if (await isGitClone(abs)) {
        const subdir = `repos/${ent.name}`;
        found.push({ subdir, repoPath: abs, write: writeBySubdir.get(subdir) ?? true });
      }
    }
  } catch { /* no repos/ dir — legacy layout only */ }

  // WRITE repos first. Callers that take index 0 as "the" repo (diff, commit) must
  // never land on a read-only context clone. Stable within each group.
  const writes = found.filter((r) => r.write);
  const reads = found.filter((r) => !r.write);
  return [...writes, ...reads];
}

/**
 * The story's PRIMARY repo dir, or null.
 *
 * SINGLE-REPO SHIM: preserves the original behaviour for callers not yet migrated to
 * `_resolveStoryRepoDirs`. On a legacy workspace this returns exactly what it always
 * did (`<workspace>/repo`); on a multi-repo workspace it returns the first WRITE
 * clone — correct for any story that writes to exactly one repo (including the
 * read-context case: N repos referenced, one written).
 *
 * KNOWN LIMITATION: for a genuinely multi-WRITE story this returns only the first
 * write repo, so a caller using it would diff/commit just that one. That is why the
 * plan requires gitDiff/gitCommit to move to `_resolveStoryRepoDirs` (Phase G.4/G.5)
 * before multi-write is enabled — this shim is sufficient for Stage 1 only.
 */
async function _resolveStoryRepoDir(
  cfg: ReturnType<typeof loadConfig>,
  agentId: string,
  storyId: string,
): Promise<string | null> {
  const dirs = await _resolveStoryRepoDirs(cfg, agentId, storyId);
  return dirs.length > 0 ? dirs[0].repoPath : null;
}

async function _runGit(
  bin: string, args: string[],
  opts: { cwd?: string; env?: Record<string, string>; timeoutMs?: number },
): Promise<{ ok: boolean; stdout: string; stderr: string; code: number | null }> {
  const { spawn } = await import("node:child_process");
  return new Promise((resolve) => {
    const timeoutMs = opts.timeoutMs ?? 30_000;
    const env = { ...process.env, ...(opts.env || {}) };
    const child = spawn(bin, args, { cwd: opts.cwd, env, shell: false });
    let stdout = ""; let stderr = ""; let killed = false;
    const timer = setTimeout(() => {
      killed = true;
      try { child.kill("SIGTERM"); } catch {}
    }, timeoutMs);
    child.stdout?.on("data", (d) => { stdout += d.toString(); });
    child.stderr?.on("data", (d) => { stderr += d.toString(); });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ ok: !killed && code === 0, stdout, stderr, code });
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      stderr += `\nspawn error: ${err.message}`;
      resolve({ ok: false, stdout, stderr, code: null });
    });
  });
}

function mimeForExtension(ext: string): string {
  const m: Record<string, string> = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".ts": "text/x-typescript",
    ".tsx": "text/x-typescript",
    ".jsx": "text/javascript",
    ".json": "application/json",
    ".yaml": "text/x-yaml",
    ".yml": "text/x-yaml",
    ".toml": "text/x-toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".zsh": "text/x-shellscript",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".scss": "text/x-scss",
    ".sql": "text/x-sql",
    ".sass": "text/x-scss",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".java": "text/x-java-source",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cpp": "text/x-c++",
    ".hpp": "text/x-c++",
    ".rb": "text/x-ruby",
    ".php": "text/x-php",
    ".xml": "text/xml",
    ".patch": "text/x-diff",
    ".diff": "text/x-diff",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".env": "text/plain",
    ".gitignore": "text/plain",
    ".dockerignore": "text/plain",
    ".lock": "text/plain",
  };
  return m[ext] || "application/octet-stream";
}
