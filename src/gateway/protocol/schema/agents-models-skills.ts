import { Type } from "@sinclair/typebox";
import { NonEmptyString } from "./primitives.js";

export const ModelChoiceSchema = Type.Object(
  {
    id: NonEmptyString,
    name: NonEmptyString,
    provider: NonEmptyString,
    contextWindow: Type.Optional(Type.Integer({ minimum: 1 })),
    reasoning: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);

export const AgentSummarySchema = Type.Object(
  {
    id: NonEmptyString,
    name: Type.Optional(NonEmptyString),
    identity: Type.Optional(
      Type.Object(
        {
          name: Type.Optional(NonEmptyString),
          theme: Type.Optional(NonEmptyString),
          emoji: Type.Optional(NonEmptyString),
          avatar: Type.Optional(NonEmptyString),
          avatarUrl: Type.Optional(NonEmptyString),
        },
        { additionalProperties: false },
      ),
    ),
  },
  { additionalProperties: false },
);

export const AgentsListParamsSchema = Type.Object({}, { additionalProperties: false });

export const AgentsListResultSchema = Type.Object(
  {
    defaultId: NonEmptyString,
    mainKey: NonEmptyString,
    scope: Type.Union([Type.Literal("per-sender"), Type.Literal("global")]),
    agents: Type.Array(AgentSummarySchema),
  },
  { additionalProperties: false },
);

export const AgentsCreateParamsSchema = Type.Object(
  {
    name: NonEmptyString,
    workspace: NonEmptyString,
    emoji: Type.Optional(Type.String()),
    avatar: Type.Optional(Type.String()),
    model: Type.Optional(NonEmptyString),
    tools: Type.Optional(
      Type.Object({
        allow: Type.Optional(Type.Array(Type.String())),
        deny: Type.Optional(Type.Array(Type.String())),
        profile: Type.Optional(Type.String()),
      }),
    ),
    /** Dispatch mode: "shca" = SHCA with memory tools, "ia" = IA worker, unset = regular agent. */
    dispatchMode: Type.Optional(Type.String()),
  },
  { additionalProperties: false },
);

export const AgentsCreateResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    agentId: NonEmptyString,
    name: NonEmptyString,
    workspace: NonEmptyString,
  },
  { additionalProperties: false },
);

export const AgentsUpdateParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    name: Type.Optional(NonEmptyString),
    workspace: Type.Optional(NonEmptyString),
    model: Type.Optional(NonEmptyString),
    avatar: Type.Optional(Type.String()),
    tools: Type.Optional(
      Type.Object({
        allow: Type.Optional(Type.Array(Type.String())),
        deny: Type.Optional(Type.Array(Type.String())),
        profile: Type.Optional(Type.String()),
      }),
    ),
    /**
     * Per-agent compaction overrides (memory_v2 add-on). Same shape as
     * the agents.list[i].compaction field declared in
     * zod-schema.agent-runtime.ts. Set to apply per-agent flush prompts
     * that override agents.defaults.compaction.memoryFlush.
     */
    compaction: Type.Optional(
      Type.Object({
        mode: Type.Optional(
          Type.Union([Type.Literal("default"), Type.Literal("safeguard")]),
        ),
        summaryPrompt: Type.Optional(Type.String()),
        summarizerModel: Type.Optional(Type.String()),
        memoryFlush: Type.Optional(
          Type.Object({
            prompt: Type.Optional(Type.String()),
            systemPrompt: Type.Optional(Type.String()),
            // Percentage of contextWindow at which to fire pre-compaction
            // flush. Overrides absolute softThresholdTokens when set.
            softThresholdPct: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
            // Caps for the agent-generated <COMPACTION_SUMMARY> block.
            compactionSummary: Type.Optional(
              Type.Object({
                maxTokens: Type.Optional(Type.Integer({ minimum: 0 })),
                maxChars: Type.Optional(Type.Integer({ minimum: 0 })),
              }),
            ),
            // MEMORY.md auto-consolidation: thresholds + prompts.
            memoryFileMaxChars: Type.Optional(Type.Integer({ minimum: 0 })),
            memoryFileTargetChars: Type.Optional(Type.Integer({ minimum: 0 })),
            consolidationPrompt: Type.Optional(Type.String()),
            consolidationSystemPrompt: Type.Optional(Type.String()),
          }),
        ),
      }),
    ),
  },
  { additionalProperties: false },
);

export const AgentsUpdateResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    agentId: NonEmptyString,
  },
  { additionalProperties: false },
);

export const AgentsDeleteParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    deleteFiles: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);

export const AgentsDeleteResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    agentId: NonEmptyString,
    removedBindings: Type.Integer({ minimum: 0 }),
  },
  { additionalProperties: false },
);

/** `agents.archive-and-delete` — uploads MEMORY.md, memory/, and sessions/
 *  to a GCS bucket under <goalId>/<normalisedAgentId>/, then runs the same
 *  removal flow as `agents.delete`. Delete is skipped if any file fails
 *  to upload, so callers can safely retry. */
export const AgentsArchiveAndDeleteParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    goalId: NonEmptyString,
    /** GCS bucket name (no `gs://` prefix). Overrides the gateway's
     *  AGENT_ARCHIVE_BUCKET env default; one of the two must be set. */
    bucket: Type.Optional(NonEmptyString),
    /** Mirror of `agents.delete.deleteFiles`. Defaults to true: archive
     *  then trash the local copies. Set false to archive only (useful
     *  for verifying the bucket layout without losing the local state). */
    deleteFiles: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);

export const AgentsArchiveAndDeleteResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    agentId: NonEmptyString,
    goalId: NonEmptyString,
    bucket: NonEmptyString,
    uploaded: Type.Array(
      Type.Object(
        {
          path: NonEmptyString,
          gsUri: NonEmptyString,
          bytes: Type.Integer({ minimum: 0 }),
        },
        { additionalProperties: false },
      ),
    ),
    totalBytes: Type.Integer({ minimum: 0 }),
    /** `removedBindings` is 0 when `deleteFiles=false` (archive-only mode). */
    removedBindings: Type.Integer({ minimum: 0 }),
  },
  { additionalProperties: false },
);

export const AgentsFileEntrySchema = Type.Object(
  {
    name: NonEmptyString,
    path: NonEmptyString,
    missing: Type.Boolean(),
    size: Type.Optional(Type.Integer({ minimum: 0 })),
    updatedAtMs: Type.Optional(Type.Integer({ minimum: 0 })),
    content: Type.Optional(Type.String()),
  },
  { additionalProperties: false },
);

export const AgentsFilesListParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
  },
  { additionalProperties: false },
);

export const AgentsFilesListResultSchema = Type.Object(
  {
    agentId: NonEmptyString,
    workspace: NonEmptyString,
    files: Type.Array(AgentsFileEntrySchema),
  },
  { additionalProperties: false },
);

export const AgentsFilesGetParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    name: NonEmptyString,
  },
  { additionalProperties: false },
);

export const AgentsFilesGetResultSchema = Type.Object(
  {
    agentId: NonEmptyString,
    workspace: NonEmptyString,
    file: AgentsFileEntrySchema,
  },
  { additionalProperties: false },
);

export const AgentsFilesSetParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    name: NonEmptyString,
    content: Type.String(),
    // Optional: "utf8" (default) writes content as-is; "base64" decodes
    // content from base64 before writing, so the caller can push binary
    // payloads (PDFs, DOCX, XLSX, images, …) through this same method.
    encoding: Type.Optional(
      Type.Union([Type.Literal("utf8"), Type.Literal("base64")]),
    ),
  },
  { additionalProperties: false },
);

export const AgentsFilesSetResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    agentId: NonEmptyString,
    workspace: NonEmptyString,
    file: AgentsFileEntrySchema,
  },
  { additionalProperties: false },
);

export const AgentsFilesDeleteParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    name: NonEmptyString,
  },
  { additionalProperties: false },
);

// ── Per-agent custom skills ────────────────────────────────────────────────
// Writes / removes SKILL.md files under <workspace>/skills/<skillName>/.
// The control panel uses these RPCs to push skill content from the `skills`
// table into a live agent's workspace; openclaw's workspace skill scanner
// (loadWorkspaceSkillEntries) discovers them on the next run.
//
// skillName is a slug — lowercase letters / digits / underscores, starting
// with a letter, 2–64 chars. The handler enforces this to prevent path
// traversal via `..`, slashes, or weird casing.

export const AgentsSkillsSetParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    skillName: NonEmptyString,
    content: Type.String(),
  },
  { additionalProperties: false },
);

export const AgentsSkillsSetResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    agentId: NonEmptyString,
    workspace: NonEmptyString,
    skillName: NonEmptyString,
    path: NonEmptyString,
  },
  { additionalProperties: false },
);

export const AgentsSkillsDeleteParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    skillName: NonEmptyString,
  },
  { additionalProperties: false },
);

export const AgentsSkillsDeleteResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    agentId: NonEmptyString,
    workspace: NonEmptyString,
    skillName: NonEmptyString,
    existed: Type.Boolean(),
  },
  { additionalProperties: false },
);

// ── SHCA operator-mode workspace RPC (Phase 4 of SHCA refactor) ───────────
//
// The control panel calls this BEFORE sending the operator-mode kickoff to
// SHCA, gating SHCA's "start working" on successful provisioning.
//
// shca.workspace.setup
//   - Resolves the SHCA agent's workspace dir from agentId
//   - Creates <agentWorkspace>/stories/<storyId>/ on the VM
//   - Clones the repo branch into <ws>/repo (single-branch, full history)
//   - Configures git credential helper using the inline-token model that
//     matches existing IA provisioning (resource_provisioning._git_setup_section)
//   - Writes the seeded scaffolding (docs/, .claude/settings.json, RESOURCES.md,
//     IDENTITY.md) the control panel hands over inline
//
// Cleanup happens automatically when the SHCA is reaped via agents.delete
// (which recursively removes the agent's whole workspace dir). No separate
// shca.workspace.delete RPC is needed.

export const ShcaWorkspaceSetupParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,            // SHCA instance id (e.g. user-shca-GOAL-<uuid>)
    storyId: NonEmptyString,            // story id (e.g. abcd1234)
    repo: Type.Object({
      cloneUrl: NonEmptyString,         // https://github.com/foo/bar.git
      branch: NonEmptyString,           // story/<sid>-<slug>
      userName: Type.Optional(NonEmptyString),    // for git user.name
      userEmail: Type.Optional(NonEmptyString),   // for git user.email
    }),
    githubToken: Type.Optional(NonEmptyString),
    // ↑ Optional so callers can pre-write RESOURCES.md with the embedded
    //   credential helper (existing pattern). If provided, this RPC will
    //   ALSO configure a local credential helper in the repo's .git/config
    //   that reads from the inline token.
    // Files to write under the story workspace. The first six are
    // canonical scaffolding paths required for any operator-mode story.
    // The map is open-ended (additionalProperties: string) so callers
    // can attach extra files like `.claude/agents/<phase>.md` from the
    // Claude phase registry — the gateway-side path validator enforces
    // relative + no `..` + must resolve inside the story workspace.
    files: Type.Object({
      "RESOURCES.md": Type.String(),
      "IDENTITY.md": Type.String(),
      "docs/plans/INDEX.md": Type.Optional(Type.String()),
      "docs/DECISIONS.md": Type.Optional(Type.String()),
      "docs/CODE_NOTES.md": Type.Optional(Type.String()),
      ".claude/settings.json": Type.String(),
    }, { additionalProperties: Type.String() }),
    // Sibling stories/<id>/ dirs that must NOT be wiped by the single-story
    // cleanup (e.g. a 'done' story whose final council review hasn't run yet —
    // it reads the live workspace). The new story is still created; protected
    // dirs are cleaned on a later import once they drop off this list.
    protectStoryIds: Type.Optional(Type.Array(Type.String())),
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceSetupResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    workspacePath: NonEmptyString,
    repoPath: NonEmptyString,
    branch: NonEmptyString,
    headSha: NonEmptyString,
    filesWritten: Type.Number(),
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceDeleteStoryParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceDeleteStoryResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    existed: Type.Boolean(),
    agentId: NonEmptyString,
    storyId: NonEmptyString,
  },
  { additionalProperties: false },
);

// shca.workspace.listFiles — return a flat list of files (and dirs) under the
// story workspace, excluding noise (.git, __pycache__, node_modules, etc.).
// Used by the SHCA chat's right-rail file viewer.

export const ShcaWorkspaceListFilesParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    // Optional: the caller's known, provisioned story workspace dir. When
    // supplied the handler walks it directly (confined to the openclaw state
    // dir) instead of re-deriving from the agent id — which is unreliable for
    // long / uppercase agent ids (normalizeAgentId lowercases+truncates).
    workspacePath: Type.Optional(Type.String()),
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceFileEntrySchema = Type.Object(
  {
    path: NonEmptyString,      // relative path under <workspace>/stories/<sid>/
    type: Type.Union([Type.Literal("file"), Type.Literal("dir")]),
    size: Type.Number(),       // bytes for files; 0 for dirs
    mtimeMs: Type.Number(),    // last-modified epoch ms (for client cache invalidation)
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceListFilesResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    storyId: NonEmptyString,
    entries: Type.Array(ShcaWorkspaceFileEntrySchema),
    truncated: Type.Boolean(),
  },
  { additionalProperties: false },
);

// shca.workspace.readFile — return the contents of a single file under the
// story workspace. UTF-8 only for MVP; binary files return a "binary" mime
// marker and empty content. Capped at MAX_READ_BYTES (set in the handler).

export const ShcaWorkspaceReadFileParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    path: NonEmptyString,      // relative path, no `..`, no leading `/`
    // Optional: see ShcaWorkspaceListFilesParamsSchema.workspacePath.
    workspacePath: Type.Optional(Type.String()),
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceReadFileResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    path: NonEmptyString,
    content: Type.String(),
    mime: NonEmptyString,      // text/markdown, text/x-python, text/plain, application/octet-stream, ...
    size: Type.Number(),
    truncated: Type.Boolean(),
  },
  { additionalProperties: false },
);

// shca.workspace.writeFile — write a small file under the story workspace.
// Admin-only (operator.admin scope). Used by the control panel to drop
// per-task credential files (ephemeral SA key JSON, etc.) into the
// workspace before dispatch, then delete them at task end.
//
// Security:
//   • Admin scope required (no agent-side caller can hit this).
//   • Path is relative to <story-root>; symlink escape is blocked.
//   • No null bytes, no `..`, no leading `/`.
//   • Size capped at MAX_WRITE_BYTES (set in the handler).
//   • Mode defaults to 0o644; callers should request 0o600 for secrets.

export const ShcaWorkspaceWriteFileParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    path: NonEmptyString,          // relative path under story root, no `..`, no leading `/`
    content: Type.String(),        // UTF-8 only for now
    mode: Type.Optional(Type.Number()),  // POSIX mode, e.g. 0o600 for secrets. Default 0o644.
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceWriteFileResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    path: NonEmptyString,
    size: Type.Number(),
  },
  { additionalProperties: false },
);

// shca.workspace.deleteFile — remove a previously-written file under the
// story workspace. Admin-only. Idempotent: missing file is not an error.

export const ShcaWorkspaceDeleteFileParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    path: NonEmptyString,          // same constraints as writeFile
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceDeleteFileResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    path: NonEmptyString,
    existed: Type.Boolean(),       // true if file was present before delete; false if no-op
  },
  { additionalProperties: false },
);

// shca.workspace.gitCommit — commit + push specific paths from the story
// repo. Used for plan-approval push (only docs/plans/* committed) and
// final implementation push (everything staged).
export const ShcaWorkspaceGitCommitParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    paths: Type.Array(NonEmptyString),      // workspace-relative paths to git-add, e.g. ["repo/docs/plans/v1.md"]
                                            // OR "all" for everything currently modified
    message: NonEmptyString,                // commit message
    push: Type.Optional(Type.Boolean()),    // default true
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceGitCommitResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    commit_sha: Type.String(),
    pushed: Type.Boolean(),
    push_error: Type.Optional(Type.String()),  // present if push=true and push failed
    nothing_to_commit: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);

// shca.workspace.gitDiff — capture diff for review.
// Usually called after all execution tasks complete; writes diff content
// back as a string (caller can also pass writeToPath to persist as a
// patch file in the workspace).
export const ShcaWorkspaceGitDiffParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    scope: Type.Optional(Type.Union([
      Type.Literal("staged"),
      Type.Literal("unstaged"),
      Type.Literal("all"),       // staged + unstaged
      Type.Literal("vs_origin"), // local HEAD vs origin/<branch>
    ])),
    writeToPath: Type.Optional(NonEmptyString),   // workspace-relative; if set, also write diff content here
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceGitDiffResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    diff: Type.String(),
    scope: NonEmptyString,
    written_to: Type.Optional(NonEmptyString),
    truncated: Type.Boolean(),
    stat_summary: Type.String(),    // e.g. "5 files changed, 100 insertions(+), 20 deletions(-)"
  },
  { additionalProperties: false },
);

// shca.workspace.refreshToken — mint a fresh GitHub installation token and
// update the local repo's credential.helper. Used by the 50-min refresh
// loop because GitHub App tokens have a 1h hard cap.
export const ShcaWorkspaceRefreshTokenParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    githubToken: NonEmptyString,        // fresh token minted by control panel before the call
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceRefreshTokenResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
  },
  { additionalProperties: false },
);

// shca.workspace.syncBranch — fetch the trunk and merge it into the story's
// branch LOCALLY, returning any conflict paths for the assistant to resolve
// (plan §5.5.5). The clean-path trunk→branch sync is done remotely by the engine
// via the GitHub API; this is the local conflict-resolution primitive used in a
// merge story. On conflict the merge is left in progress (markers in the
// worktree) so the assistant edits + commits (shca.workspace.gitCommit) to finish.
export const ShcaWorkspaceSyncBranchParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    trunkBranch: NonEmptyString,        // trunk branch to merge in, e.g. "main"
    githubToken: NonEmptyString,        // fresh token to auth the fetch (credential helper)
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceSyncBranchResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    merged: Type.Boolean(),             // a merge commit was created (trunk brought in)
    already_up_to_date: Type.Boolean(),
    conflicted: Type.Boolean(),         // merge left in progress with conflicts to resolve
    conflict_paths: Type.Array(Type.String()),
    head_sha: Type.String(),
  },
  { additionalProperties: false },
);

// shca.workspace.pullBranch — fast-forward the story's LOCAL branch to its remote
// tip (cooperative pull, plan §5.5.2): fetch origin <branch> then merge --ff-only.
// Used at the start of a merge story and by continue-mode provisioning to pick up
// commits the engine restacked or a handoff pushed. Never merges trunk (that's
// syncBranch) and never force-updates; a diverged local reports ff_failed.
export const ShcaWorkspacePullBranchParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
    branch: NonEmptyString,             // the story's branch to fast-forward
    githubToken: NonEmptyString,        // fresh token to auth the fetch (credential helper)
  },
  { additionalProperties: false },
);

export const ShcaWorkspacePullBranchResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    updated: Type.Boolean(),            // local fast-forwarded to a newer remote tip
    already_current: Type.Boolean(),
    ff_failed: Type.Boolean(),          // local diverged from remote — could not fast-forward
    head_sha: Type.String(),
  },
  { additionalProperties: false },
);

// shca.workspace.findClaudeSession — discover the claude-cli session id
// for a story by listing ~/.claude/projects/<encoded-workspace>/*.jsonl
// and returning the most recently modified file's name (minus .jsonl).
// Used as a fallback when stories.current_claude_session_id is unset.
export const ShcaWorkspaceFindClaudeSessionParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,
    storyId: NonEmptyString,
  },
  { additionalProperties: false },
);

export const ShcaWorkspaceFindClaudeSessionResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    sessionId: Type.String(),       // empty string if no session file found
    sessionFile: Type.String(),     // absolute path; empty if not found
    candidateCount: Type.Number(),  // total .jsonl files seen
  },
  { additionalProperties: false },
);

// shca.claude.invoke — gateway-level wrapper around spawnCliDirect.
// Lets the control panel spawn / resume a claude-cli session directly
// (without going through an agent's tool-use loop). Used by the
// claude-code-phase task dispatcher.
export const ShcaClaudeInvokeParamsSchema = Type.Object(
  {
    agentId: NonEmptyString,                       // SHCA instance id (auth boundary)
    storyId: NonEmptyString,
    goalId: Type.Optional(Type.String()),          // mint a goal-scoped hook token (shared-token fix)
    cwd: NonEmptyString,                           // story workspace dir
    task: NonEmptyString,                          // the message to send claude
    resumeSessionId: Type.Optional(Type.String()), // continue a prior session
    taskId: Type.Optional(Type.String()),          // for PostToolUse hook env
    model: Type.Optional(Type.String()),
    appendSystemPrompt: Type.Optional(Type.String()),
    timeoutMs: Type.Optional(Type.Number({ minimum: 1000 })),
  },
  { additionalProperties: false },
);

export const ShcaClaudeInvokeResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    status: Type.Union([Type.Literal("ok"), Type.Literal("error"), Type.Literal("busy")]),
    sessionId: Type.String(),
    text: Type.String(),
    exitCode: Type.Union([Type.Number(), Type.Null()]),
    durationMs: Type.Number(),
    error: Type.Optional(Type.String()),
    stderrSnippet: Type.Optional(Type.String()),
  },
  { additionalProperties: false },
);


export const AgentsFilesDeleteResultSchema = Type.Object(
  {
    ok: Type.Literal(true),
    agentId: NonEmptyString,
    workspace: NonEmptyString,
    name: NonEmptyString,
    existed: Type.Boolean(),
  },
  { additionalProperties: false },
);

export const ModelsListParamsSchema = Type.Object({}, { additionalProperties: false });

export const ModelsListResultSchema = Type.Object(
  {
    models: Type.Array(ModelChoiceSchema),
  },
  { additionalProperties: false },
);

export const SkillsStatusParamsSchema = Type.Object(
  {
    agentId: Type.Optional(NonEmptyString),
  },
  { additionalProperties: false },
);

export const SkillsBinsParamsSchema = Type.Object({}, { additionalProperties: false });

export const SkillsBinsResultSchema = Type.Object(
  {
    bins: Type.Array(NonEmptyString),
  },
  { additionalProperties: false },
);

export const SkillsInstallParamsSchema = Type.Object(
  {
    name: NonEmptyString,
    installId: NonEmptyString,
    timeoutMs: Type.Optional(Type.Integer({ minimum: 1000 })),
  },
  { additionalProperties: false },
);

export const SkillsUpdateParamsSchema = Type.Object(
  {
    skillKey: NonEmptyString,
    enabled: Type.Optional(Type.Boolean()),
    apiKey: Type.Optional(Type.String()),
    env: Type.Optional(Type.Record(NonEmptyString, Type.String())),
  },
  { additionalProperties: false },
);

export const ToolsCatalogParamsSchema = Type.Object(
  {
    agentId: Type.Optional(NonEmptyString),
    includePlugins: Type.Optional(Type.Boolean()),
  },
  { additionalProperties: false },
);

export const ToolCatalogProfileSchema = Type.Object(
  {
    id: Type.Union([
      Type.Literal("minimal"),
      Type.Literal("coding"),
      Type.Literal("messaging"),
      Type.Literal("full"),
    ]),
    label: NonEmptyString,
  },
  { additionalProperties: false },
);

export const ToolCatalogEntrySchema = Type.Object(
  {
    id: NonEmptyString,
    label: NonEmptyString,
    description: Type.String(),
    source: Type.Union([Type.Literal("core"), Type.Literal("plugin")]),
    pluginId: Type.Optional(NonEmptyString),
    optional: Type.Optional(Type.Boolean()),
    defaultProfiles: Type.Array(
      Type.Union([
        Type.Literal("minimal"),
        Type.Literal("coding"),
        Type.Literal("messaging"),
        Type.Literal("full"),
      ]),
    ),
  },
  { additionalProperties: false },
);

export const ToolCatalogGroupSchema = Type.Object(
  {
    id: NonEmptyString,
    label: NonEmptyString,
    source: Type.Union([Type.Literal("core"), Type.Literal("plugin")]),
    pluginId: Type.Optional(NonEmptyString),
    tools: Type.Array(ToolCatalogEntrySchema),
  },
  { additionalProperties: false },
);

export const ToolsCatalogResultSchema = Type.Object(
  {
    agentId: NonEmptyString,
    profiles: Type.Array(ToolCatalogProfileSchema),
    groups: Type.Array(ToolCatalogGroupSchema),
  },
  { additionalProperties: false },
);
