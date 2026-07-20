/**
 * workspace-ops — merge preview and net-diff RPCs for the AgentOS integration flow.
 *
 * Provides two admin RPC methods used by the control panel's merge orchestrator:
 *
 *   workspace.mergePreview
 *     Fetches the target branch, runs `git merge --no-commit --no-ff` in the
 *     agent workspace, and returns the result. On success the workspace is LEFT
 *     in the merged preview state so the integration-tester can run against it.
 *     On conflict the merge is aborted and conflict files are returned.
 *
 *   workspace.gitDiff
 *     Returns the diff between two refs in the agent workspace.
 *     Used by the integrate gate to generate the net diff for human review.
 *
 * Both methods accept a `session_key` in the format `agent:<agentId>:<rest>` —
 * the same key format used everywhere in the control panel gateway protocol.
 *
 * See documentation/story-integration/SYSTEM_GATE.md and CODE_INTEGRATION.md in the
 * control panel repo for the full design and expected request/response contracts.
 */

import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import type { OpenClawPluginApi } from "../../src/plugins/types.js";
import { ErrorCodes, errorShape } from "../../src/gateway/protocol/index.js";

// ── session key parsing ────────────────────────────────────────────────────

/** Parse `agent:<agentId>:<rest>` → agentId, or null on bad format. */
function parseSessionKeyAgentId(sessionKey: unknown): string | null {
  if (typeof sessionKey !== "string") return null;
  const parts = sessionKey.trim().split(":").filter(Boolean);
  if (parts.length < 3 || parts[0] !== "agent") return null;
  const agentId = parts[1]?.trim();
  return agentId || null;
}

// ── extensionAPI dynamic loader ────────────────────────────────────────────
// Extensions can't directly import from ../../src because that's the TypeScript
// source; at runtime only the compiled dist/ is available. We load the stable
// extensionAPI surface the same way voice-call and other workspace-aware
// extensions do: by finding the package root and importing dist/extensionAPI.js.

function findPackageRoot(start: string, name: string): string | null {
  let dir = start;
  for (let i = 0; i < 20; i++) {
    try {
      const pkg = path.join(dir, "package.json");
      const content = require("fs").readFileSync(pkg, "utf8");
      const json = JSON.parse(content);
      if (json.name === name) return dir;
    } catch {}
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function resolveOpenClawRoot(): string {
  const fromEnv = process.env.OPENCLAW_ROOT?.trim();
  if (fromEnv) return fromEnv;

  const candidates = new Set<string>();
  if (process.argv[1]) candidates.add(path.dirname(process.argv[1]));
  candidates.add(process.cwd());
  try { candidates.add(path.dirname(fileURLToPath(import.meta.url))); } catch {}

  for (const start of candidates) {
    const found = findPackageRoot(start, "openclaw");
    if (found) return found;
  }
  throw new Error("Cannot resolve openclaw root — set OPENCLAW_ROOT env var.");
}

let _coreAPI: { resolveAgentWorkspaceDir: (cfg: unknown, agentId: string) => string } | null = null;

async function getCoreAPI() {
  if (_coreAPI) return _coreAPI;
  const distPath = path.join(resolveOpenClawRoot(), "dist", "extensionAPI.js");
  _coreAPI = await import(pathToFileURL(distPath).href);
  return _coreAPI!;
}

// ── git helpers ────────────────────────────────────────────────────────────

const GIT_CANDIDATES = ["/usr/bin/git", "/usr/local/bin/git", "git"];

function resolveGitBin(): string | null {
  const { existsSync } = require("fs");
  for (const c of GIT_CANDIDATES) {
    if (c === "git" || existsSync(c)) return c;
  }
  return null;
}

interface GitResult {
  ok: boolean;
  stdout: string;
  stderr: string;
  code: number | null;
}

async function runGit(
  bin: string,
  args: string[],
  opts: { cwd: string; timeoutMs?: number },
): Promise<GitResult> {
  return new Promise((resolve) => {
    const timeoutMs = opts.timeoutMs ?? 30_000;
    // Prepend safe.directory to handle ownership mismatches between the gateway
    // process user and the workspace directory owner (common on shared VMs).
    const safeArgs = ["-c", `safe.directory=${opts.cwd}`, ...args];
    const child = spawn(bin, safeArgs, { cwd: opts.cwd, shell: false });
    let stdout = "";
    let stderr = "";
    let killed = false;

    const timer = setTimeout(() => {
      killed = true;
      try { child.kill("SIGTERM"); } catch {}
    }, timeoutMs);

    child.stdout?.on("data", (d: Buffer) => { stdout += d.toString(); });
    child.stderr?.on("data", (d: Buffer) => { stderr += d.toString(); });
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

/**
 * Find the git repo root within (or equal to) workspaceDir.
 * The coder phase clones the repo into a subdirectory of the SHCA workspace
 * (e.g. workspace/repo-name/), so workspaceDir itself may have a bare `.git`
 * from openclaw's own workspace tracking — without any `origin` remote.
 * Subdirectories are checked first so the cloned repo takes priority over the
 * openclaw workspace root.
 */
async function findGitRepoDir(workspaceDir: string): Promise<string | null> {
  // Check immediate subdirectories first — the cloned repo lives here.
  try {
    const entries = await fs.readdir(workspaceDir, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const candidate = path.join(workspaceDir, entry.name);
      try {
        await fs.access(path.join(candidate, ".git"));
        return candidate;
      } catch {}
    }
  } catch {}
  // Fall back to the workspace root itself.
  try {
    await fs.access(path.join(workspaceDir, ".git"));
    return workspaceDir;
  } catch {}
  return null;
}

/** Return files listed as conflicted by `git status --porcelain`. */
function extractConflictFiles(statusOutput: string): string[] {
  const conflicts: string[] = [];
  for (const line of statusOutput.split("\n")) {
    const xy = line.slice(0, 2);
    // Conflict codes: UU, AA, DD, AU, UA, DU, UD
    if (/^(UU|AA|DD|AU|UA|DU|UD)/.test(xy)) {
      const file = line.slice(3).trim();
      if (file) conflicts.push(file);
    }
  }
  return conflicts;
}

// ── plugin ─────────────────────────────────────────────────────────────────

const plugin = {
  id: "workspace-ops",
  name: "Workspace Operations",
  description:
    "Admin RPC methods for the AgentOS integration flow: merge preview " +
    "(workspace.mergePreview) and net diff (workspace.gitDiff).",

  register(api: OpenClawPluginApi) {
    // ── workspace.mergePreview ─────────────────────────────────────────────
    //
    // Request:  { session_key, source_branch, target_branch, write_report_to? }
    // Response: { success, conflict_files, changed_files, source_sha,
    //             target_sha, preview_merge_sha }
    //
    // On success: workspace is left in merged preview state (--no-commit).
    //             The integration-tester will run against this merged state.
    // On conflict: git merge --abort is called; workspace returned to clean state.
    api.registerGatewayMethod("workspace.mergePreview", async ({ params, respond }) => {
      const p = params as Record<string, unknown>;

      const agentId = parseSessionKeyAgentId(p.session_key);
      if (!agentId) {
        api.logger.error(
          `workspace-ops: mergePreview invalid session_key — expected 'agent:<id>:<rest>', got: ${String(p.session_key)}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST,
          `workspace.mergePreview: session_key must be 'agent:<id>:<rest>', got: ${String(p.session_key)}`));
        return;
      }

      const sourceBranch = typeof p.source_branch === "string" ? p.source_branch.trim() : "";
      const targetBranch = typeof p.target_branch === "string" ? p.target_branch.trim() : "main";
      const writeReportTo = typeof p.write_report_to === "string" ? p.write_report_to.trim() : "";
      // github_token: authenticate git fetch (Claude session credential helper not active server-side).
      const githubToken = typeof p.github_token === "string" ? p.github_token.trim() : "";
      // repo_url: the HTTPS GitHub URL — SHCA workspaces are local git repos with no origin by default.
      const repoUrl = typeof p.repo_url === "string" ? p.repo_url.trim() : "";
      // story_id: used to locate the repo clone at stories/{story_id}/repo/ inside the workspace.
      const storyId = typeof p.story_id === "string" ? p.story_id.trim() : "";

      if (!sourceBranch) {
        api.logger.error(`workspace-ops: mergePreview missing source_branch (agentId=${agentId})`);
        respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST,
          "workspace.mergePreview: source_branch is required"));
        return;
      }

      const gitBin = resolveGitBin();
      if (!gitBin) {
        api.logger.error(
          `workspace-ops: mergePreview git binary not found — tried: ${GIT_CANDIDATES.join(", ")}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR,
          "workspace.mergePreview: git binary not found"));
        return;
      }

      let workspaceDir: string;
      try {
        const core = await getCoreAPI();
        workspaceDir = core.resolveAgentWorkspaceDir(api.config, agentId);
      } catch (err) {
        api.logger.error(
          `workspace-ops: mergePreview workspace resolution failed agentId=${agentId}: ${String(err)}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR,
          `workspace.mergePreview: failed to resolve workspace: ${String(err)}`));
        return;
      }

      // Find the actual git repo dir. The coder clones into stories/{story_id}/repo/
      // inside the SHCA workspace. Use story_id for a direct lookup when available,
      // falling back to findGitRepoDir for resilience.
      let gitDir: string;
      try {
        let resolved: string | null = null;
        if (storyId) {
          const storyRepoPath = path.join(workspaceDir, "stories", storyId, "repo");
          try {
            await fs.access(path.join(storyRepoPath, ".git"));
            resolved = storyRepoPath;
            api.logger.info(
              `workspace-ops: mergePreview found repo at stories/${storyId}/repo agentId=${agentId}`
            );
          } catch {
            api.logger.warn(
              `workspace-ops: mergePreview stories/${storyId}/repo not found — falling back to scan agentId=${agentId}`
            );
          }
        }
        if (!resolved) {
          resolved = await findGitRepoDir(workspaceDir);
        }
        if (!resolved) {
          api.logger.error(
            `workspace-ops: mergePreview no git repo found in workspace agentId=${agentId} workspace=${workspaceDir}`
          );
          respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST,
            `workspace.mergePreview: no git repository found in workspace ${workspaceDir}`));
          return;
        }
        gitDir = resolved;
      } catch (err) {
        api.logger.error(
          `workspace-ops: mergePreview git repo lookup failed agentId=${agentId}: ${String(err)}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR,
          `workspace.mergePreview: git repo lookup failed: ${String(err)}`));
        return;
      }

      api.logger.info(
        `workspace-ops: mergePreview start agentId=${agentId} source=${sourceBranch} target=${targetBranch} workspace=${workspaceDir} gitDir=${gitDir}`,
      );

      try {
        // 1. Ensure the origin remote is set. SHCA workspaces are git-init'd locally
        //    without a remote — repo_url is required for the fetch to work.
        if (repoUrl) {
          const remoteListR = await runGit(gitBin, ["remote"], { cwd: gitDir });
          const existingRemotes = remoteListR.stdout.split("\n").map(r => r.trim()).filter(Boolean);
          if (existingRemotes.includes("origin")) {
            const setUrlR = await runGit(gitBin, ["remote", "set-url", "origin", repoUrl], { cwd: gitDir });
            api.logger.info(
              `workspace-ops: mergePreview origin remote updated to ${repoUrl} ok=${setUrlR.ok} agentId=${agentId}`
            );
          } else {
            const addR = await runGit(gitBin, ["remote", "add", "origin", repoUrl], { cwd: gitDir });
            api.logger.info(
              `workspace-ops: mergePreview origin remote added ${repoUrl} ok=${addR.ok} agentId=${agentId}`
            );
          }
        } else {
          api.logger.warn(
            `workspace-ops: mergePreview no repo_url provided — fetch may fail if origin is not configured agentId=${agentId}`
          );
        }

        // 2. Fetch the target branch from origin.
        // When a github_token is provided, inject it via http.extraheader so git can
        // authenticate without the Claude session credential helper (only active during agent turns).
        let fetchArgs: string[];
        // Embed the token in the remote URL for this fetch using the x-access-token
        // convention (the official GitHub approach for CI/CD). After the fetch we
        // restore the clean URL so the token is not stored in git config.
        if (githubToken) {
          const currentUrlR = await runGit(gitBin, ["remote", "get-url", "origin"], { cwd: gitDir });
          const cleanUrl = currentUrlR.stdout.trim();
          const authedUrl = cleanUrl.replace(/^https:\/\//, `https://x-access-token:${githubToken}@`);
          await runGit(gitBin, ["remote", "set-url", "origin", authedUrl], { cwd: gitDir });
          api.logger.debug(`workspace-ops: mergePreview set token-authed URL for fetch agentId=${agentId}`);
          fetchArgs = ["fetch", "origin", targetBranch];
        } else {
          fetchArgs = ["fetch", "origin", targetBranch];
        }
        const fetchR = await runGit(gitBin, fetchArgs, { cwd: gitDir, timeoutMs: 60_000 });
        if (!fetchR.ok) {
          api.logger.warn(
            `workspace-ops: mergePreview git fetch origin ${targetBranch} failed (non-fatal) ` +
            `exit=${fetchR.code} stderr=${fetchR.stderr.slice(0, 300)} agentId=${agentId}`
          );
          // Non-fatal — remote ref may already be up to date locally; continue.
        } else {
          api.logger.debug(`workspace-ops: mergePreview git fetch ok target=${targetBranch} agentId=${agentId}`);
        }

        // Restore clean URL (strip token) so it is not persisted in git config.
        if (githubToken && repoUrl) {
          await runGit(gitBin, ["remote", "set-url", "origin", repoUrl], { cwd: gitDir });
          api.logger.debug(`workspace-ops: mergePreview restored clean origin URL agentId=${agentId}`);
        }

        // 3. Record source SHA (current HEAD = feature branch tip).
        const sourceShaR = await runGit(gitBin, ["rev-parse", "HEAD"], { cwd: gitDir });
        const sourceSha = sourceShaR.stdout.trim();
        api.logger.debug(`workspace-ops: mergePreview source_sha=${sourceSha.slice(0, 12)} agentId=${agentId}`);

        // 4. Record target SHA.
        const targetShaR = await runGit(gitBin, ["rev-parse", `origin/${targetBranch}`], { cwd: gitDir });
        if (!targetShaR.ok) {
          api.logger.error(
            `workspace-ops: mergePreview cannot resolve origin/${targetBranch} — ` +
            `fetch ok=${fetchR.ok} stderr=${fetchR.stderr.slice(0, 300)} agentId=${agentId}`
          );
          respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST,
            `workspace.mergePreview: cannot resolve origin/${targetBranch} — fetch may have failed`));
          return;
        }
        const targetSha = targetShaR.stdout.trim();
        api.logger.debug(`workspace-ops: mergePreview target_sha=${targetSha.slice(0, 12)} agentId=${agentId}`);

        // 5. List files changed by this branch vs the target (for the merge report).
        const changedR = await runGit(
          gitBin,
          ["diff", "--name-only", `origin/${targetBranch}...HEAD`],
          { cwd: gitDir },
        );
        const changedFiles = changedR.stdout.trim().split("\n").filter(Boolean);
        api.logger.info(
          `workspace-ops: mergePreview changed_files=${changedFiles.length} agentId=${agentId}`
        );

        // 6. Log current branch for diagnostics before attempting merge.
        const branchR = await runGit(gitBin, ["branch", "--show-current"], { cwd: gitDir });
        api.logger.info(
          `workspace-ops: mergePreview current_branch=${branchR.stdout.trim() || "(detached)"} ` +
          `source_sha=${sourceSha.slice(0, 12)} target_sha=${targetSha.slice(0, 12)} agentId=${agentId}`
        );

        // 7. Attempt the merge preview (--no-commit --no-ff).
        //    On success: leave in place for the integration-tester.
        //    On conflict: abort and report.
        const mergeR = await runGit(
          gitBin,
          ["merge", `origin/${targetBranch}`, "--no-commit", "--no-ff"],
          { cwd: gitDir, timeoutMs: 60_000 },
        );

        if (!mergeR.ok) {
          api.logger.warn(
            `workspace-ops: mergePreview git merge failed exit=${mergeR.code} ` +
            `stdout=${mergeR.stdout.trim().slice(0, 200)} stderr=${mergeR.stderr.trim().slice(0, 300)} agentId=${agentId}`
          );
          // Merge conflicted — collect conflict files then abort.
          const statusR = await runGit(gitBin, ["status", "--porcelain"], { cwd: gitDir });
          const conflictFiles = extractConflictFiles(statusR.stdout);

          const abortR = await runGit(gitBin, ["merge", "--abort"], { cwd: gitDir });
          if (!abortR.ok) {
            api.logger.error(
              `workspace-ops: mergePreview git merge --abort failed — workspace may be in inconsistent state ` +
              `agentId=${agentId} exit=${abortR.code} stderr=${abortR.stderr.slice(0, 300)}`
            );
          } else {
            api.logger.debug(`workspace-ops: mergePreview merge aborted cleanly agentId=${agentId}`);
          }

          api.logger.warn(
            `workspace-ops: mergePreview conflict agentId=${agentId} ` +
            `conflict_file_count=${conflictFiles.length} files=${JSON.stringify(conflictFiles)}`,
          );
          respond(true, {
            success: false,
            conflict_files: conflictFiles,
            changed_files: changedFiles,
            source_sha: sourceSha,
            target_sha: targetSha,
            preview_merge_sha: "",
          });
          return;
        }

        // 7. Write the merge report to the workspace path if requested.
        //    The control panel also archives this to GCS; the workspace copy
        //    is transient context for the integration-tester agent.
        if (writeReportTo) {
          const absReport = path.resolve(workspaceDir, writeReportTo);
          // Safety: must stay inside the workspace.
          if (!absReport.startsWith(workspaceDir)) {
            api.logger.error(
              `workspace-ops: mergePreview write_report_to path traversal rejected — ` +
              `requested=${writeReportTo} resolved=${absReport} workspace=${workspaceDir} agentId=${agentId}`
            );
          } else {
            try {
              await fs.mkdir(path.dirname(absReport), { recursive: true });
              // The control panel generates the full merge report; we write a
              // brief placeholder here so the integration-tester can find the file.
              const reportContent = [
                "# Merge Preview Report",
                "",
                `Source: ${sourceSha}`,
                `Target (origin/${targetBranch}): ${targetSha}`,
                `Changed files: ${changedFiles.length}`,
                "",
                "Merge preview succeeded — no conflicts detected.",
                "The workspace is in merged preview state.",
                "The full report will be archived to GCS by the control panel.",
              ].join("\n");
              await fs.writeFile(absReport, reportContent, "utf8");
              api.logger.info(`workspace-ops: wrote merge report placeholder to ${absReport}`);
            } catch (err) {
              api.logger.warn(`workspace-ops: could not write report to ${writeReportTo}: ${String(err)}`);
            }
          }
        }

        api.logger.info(
          `workspace-ops: mergePreview success agentId=${agentId} source=${sourceSha} target=${targetSha}`,
        );
        respond(true, {
          success: true,
          conflict_files: [],
          changed_files: changedFiles,
          source_sha: sourceSha,
          target_sha: targetSha,
          preview_merge_sha: "",  // --no-commit means no SHA yet; populated after real merge
        });
      } catch (err) {
        api.logger.error(`workspace-ops: mergePreview unhandled error: ${String(err)}`);
        // Best-effort cleanup on unexpected failure.
        try { await runGit(gitBin, ["merge", "--abort"], { cwd: gitDir }); } catch {}
        respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR, String(err)));
      }
    });

    // ── workspace.gitDiff ──────────────────────────────────────────────────
    //
    // Request:  { session_key, baseRef, headRef }
    // Response: { diff }
    //
    // Returns the diff between two refs. Used by the integrate gate to generate
    // the net diff (target..feature) for the human approval card.
    api.registerGatewayMethod("workspace.gitDiff", async ({ params, respond }) => {
      const p = params as Record<string, unknown>;

      const agentId = parseSessionKeyAgentId(p.session_key);
      if (!agentId) {
        api.logger.error(
          `workspace-ops: gitDiff invalid session_key — expected 'agent:<id>:<rest>', got: ${String(p.session_key)}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST,
          `workspace.gitDiff: session_key must be 'agent:<id>:<rest>', got: ${String(p.session_key)}`));
        return;
      }

      const baseRef = typeof p.baseRef === "string" ? p.baseRef.trim() : "";
      const headRef = typeof p.headRef === "string" ? p.headRef.trim() : "";

      if (!baseRef || !headRef) {
        api.logger.error(
          `workspace-ops: gitDiff missing baseRef or headRef agentId=${agentId} ` +
          `baseRef=${String(p.baseRef)} headRef=${String(p.headRef)}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST,
          "workspace.gitDiff: baseRef and headRef are required"));
        return;
      }

      const gitBin = resolveGitBin();
      if (!gitBin) {
        api.logger.error(
          `workspace-ops: gitDiff git binary not found — tried: ${GIT_CANDIDATES.join(", ")}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR,
          "workspace.gitDiff: git binary not found"));
        return;
      }

      let workspaceDir: string;
      try {
        const core = await getCoreAPI();
        workspaceDir = core.resolveAgentWorkspaceDir(api.config, agentId);
      } catch (err) {
        api.logger.error(
          `workspace-ops: gitDiff workspace resolution failed agentId=${agentId}: ${String(err)}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR,
          `workspace.gitDiff: failed to resolve workspace: ${String(err)}`));
        return;
      }

      // Find the actual git repo dir — coder clones into a subdirectory of the workspace.
      let gitDir: string;
      try {
        const resolved = await findGitRepoDir(workspaceDir);
        if (!resolved) {
          api.logger.error(
            `workspace-ops: gitDiff no git repo found in workspace agentId=${agentId} workspace=${workspaceDir}`
          );
          respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST,
            `workspace.gitDiff: no git repository found in workspace ${workspaceDir}`));
          return;
        }
        gitDir = resolved;
      } catch (err) {
        api.logger.error(
          `workspace-ops: gitDiff git repo lookup failed agentId=${agentId}: ${String(err)}`
        );
        respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR,
          `workspace.gitDiff: git repo lookup failed: ${String(err)}`));
        return;
      }

      api.logger.info(
        `workspace-ops: gitDiff start agentId=${agentId} base=${baseRef} head=${headRef} workspace=${workspaceDir} gitDir=${gitDir}`,
      );

      const githubTokenDiff = typeof p.github_token === "string" ? p.github_token.trim() : "";

      try {
        // Fetch both refs to ensure they're up to date.
        const diffFetchArgs = githubTokenDiff
          ? ["-c", `http.extraheader=Authorization: token ${githubTokenDiff}`, "fetch", "origin", baseRef, headRef]
          : ["fetch", "origin", baseRef, headRef];
        const fetchR = await runGit(gitBin, diffFetchArgs, { cwd: gitDir, timeoutMs: 60_000 });
        if (!fetchR.ok) {
          api.logger.warn(
            `workspace-ops: gitDiff git fetch failed (non-fatal) ` +
            `exit=${fetchR.code} stderr=${fetchR.stderr.slice(0, 300)} agentId=${agentId}`
          );
        } else {
          api.logger.debug(`workspace-ops: gitDiff git fetch ok base=${baseRef} head=${headRef} agentId=${agentId}`);
        }

        const diffR = await runGit(
          gitBin,
          ["diff", `origin/${baseRef}...${headRef}`],
          { cwd: gitDir, timeoutMs: 60_000 },
        );

        if (!diffR.ok) {
          const stderrFull = diffR.stderr;
          const stderrTrunc = stderrFull.slice(0, 600);
          if (stderrFull.length > 600) {
            api.logger.warn(
              `workspace-ops: gitDiff stderr truncated from ${stderrFull.length} to 600 chars agentId=${agentId}`
            );
          }
          api.logger.error(
            `workspace-ops: gitDiff git diff failed exit=${diffR.code} stderr=${stderrTrunc} agentId=${agentId}`
          );
          respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR,
            `workspace.gitDiff: git diff failed: ${stderrTrunc}`));
          return;
        }

        api.logger.info(
          `workspace-ops: gitDiff success diff_chars=${diffR.stdout.length} gitDir=${gitDir} agentId=${agentId}`
        );
        respond(true, { diff: diffR.stdout });
      } catch (err) {
        api.logger.error(`workspace-ops: gitDiff unhandled error agentId=${agentId}: ${String(err)}`);
        respond(false, undefined, errorShape(ErrorCodes.INTERNAL_ERROR, String(err)));
      }
    });
  },
};

export default plugin;
