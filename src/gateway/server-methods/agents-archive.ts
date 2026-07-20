/**
 * GCS archive of an agent's memory + session transcripts.
 *
 * Used by `agents.archive-and-delete` to back up everything that matters
 * about an agent to a long-term bucket before pi removes its workspace
 * and sessions directories from local disk. Only fires the actual delete
 * if every file uploaded cleanly — partial-archive does NOT delete.
 *
 * Upload mechanism: `gcloud storage cp` subprocess. The gateway VM's
 * service account already has GCS write perms via cloudbuild; no new
 * npm dependency.
 *
 * Bucket layout:
 *   gs://<bucket>/<goalId>/<normalisedAgentId>/MEMORY.md
 *   gs://<bucket>/<goalId>/<normalisedAgentId>/memory/<YYYY-MM-DD>.md  (and any nested)
 *   gs://<bucket>/<goalId>/<normalisedAgentId>/sessions/<uuid>.jsonl
 *   gs://<bucket>/<goalId>/<normalisedAgentId>/sessions/sessions.json
 *
 * Static workspace md files (SOUL.md, AGENTS.md, IDENTITY.md, TOOLS.md,
 * RESOURCES.md, USER.md, BOOTSTRAP.md, HEARTBEAT.md) are intentionally
 * NOT archived — they are regenerable templates that the control panel
 * re-injects on agent re-creation. Only the conversation-shaped state is
 * unique-and-lossy: MEMORY.md, the per-day memory log, and session JSONLs.
 */

import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import type { Dirent } from "node:fs";
import path from "node:path";

import { createSubsystemLogger } from "../../logging/subsystem.js";

const log = createSubsystemLogger("agent-archive");

export type ArchivedFile = {
  /** Full local filesystem path that was uploaded. */
  path: string;
  /** Destination gs:// URI. */
  gsUri: string;
  /** Size of the local file at upload time. */
  bytes: number;
};

export type ArchiveFailure = {
  path: string;
  error: string;
};

export type ArchiveResult = {
  bucket: string;
  uploaded: ArchivedFile[];
  failed: ArchiveFailure[];
  totalBytes: number;
};

type Target = { localPath: string; relativeKey: string };

/**
 * Walks the agent's workspace and sessions dirs, picks the durable files,
 * and uploads each to GCS.
 *
 * Returns `failed: []` only when every file uploaded successfully; callers
 * (agents.archive-and-delete) should refuse to proceed with the delete
 * unless `failed.length === 0`.
 *
 * If workspace/sessions dirs don't exist (agent was never spawned, or
 * already cleaned up), returns `uploaded: [], failed: []` — caller treats
 * that as a clean success and proceeds to delete.
 */
export async function archiveAgentToBucket(params: {
  agentId: string;
  goalId: string;
  bucket: string;
  workspaceDir: string;
  sessionsDir: string;
}): Promise<ArchiveResult> {
  const { agentId, goalId, bucket, workspaceDir, sessionsDir } = params;
  const normalisedAgentId = agentId.toLowerCase();

  const targets: Target[] = [];

  // MEMORY.md at the workspace root.
  await collectSingleFile(path.join(workspaceDir, "MEMORY.md"), "MEMORY.md", targets);

  // memory/ recursively (daily logs etc.).
  await collectTree(path.join(workspaceDir, "memory"), "memory", targets);

  // sessions/ recursively (jsonl transcripts + sessions.json index).
  await collectTree(sessionsDir, "sessions", targets);

  if (targets.length === 0) {
    log.info(
      `[archive] nothing to upload: agent=${agentId} goalId=${goalId} ` +
        `workspaceDir=${workspaceDir} sessionsDir=${sessionsDir}`,
    );
    return { bucket, uploaded: [], failed: [], totalBytes: 0 };
  }

  log.info(
    `[archive] start: agent=${agentId} goalId=${goalId} bucket=${bucket} ` +
      `targets=${targets.length}`,
  );

  const uploaded: ArchivedFile[] = [];
  const failed: ArchiveFailure[] = [];
  let totalBytes = 0;

  for (const target of targets) {
    const gsUri =
      `gs://${bucket}/${encodeURIComponent(goalId)}/` +
      `${encodeURIComponent(normalisedAgentId)}/${target.relativeKey}`;
    try {
      const stat = await fs.stat(target.localPath);
      await runGcloudCp(target.localPath, gsUri);
      uploaded.push({ path: target.localPath, gsUri, bytes: stat.size });
      totalBytes += stat.size;
      log.info(`[archive] uploaded ${target.localPath} -> ${gsUri} (${stat.size}B)`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      failed.push({ path: target.localPath, error: msg });
      log.error(`[archive] FAILED ${target.localPath} -> ${gsUri}: ${msg}`);
    }
  }

  log.info(
    `[archive] done: agent=${agentId} uploaded=${uploaded.length} failed=${failed.length} ` +
      `totalBytes=${totalBytes}`,
  );

  return { bucket, uploaded, failed, totalBytes };
}

async function collectSingleFile(
  fullPath: string,
  relativeKey: string,
  out: Target[],
): Promise<void> {
  try {
    const stat = await fs.stat(fullPath);
    if (stat.isFile()) out.push({ localPath: fullPath, relativeKey });
  } catch (err) {
    if (!isEnoent(err)) throw err;
    // Missing — skip silently.
  }
}

/** Recursively collect every file under `rootDir`, keying each by its
 *  path relative to `relativePrefix` so the GCS structure mirrors the
 *  on-disk structure (e.g. `memory/2026-06-01.md`). */
async function collectTree(
  rootDir: string,
  relativePrefix: string,
  out: Target[],
): Promise<void> {
  let entries: Dirent[];
  try {
    entries = await fs.readdir(rootDir, { withFileTypes: true });
  } catch (err) {
    if (isEnoent(err)) return;
    throw err;
  }
  for (const e of entries) {
    const childPath = path.join(rootDir, e.name);
    const childKey = `${relativePrefix}/${e.name}`;
    if (e.isFile()) {
      out.push({ localPath: childPath, relativeKey: childKey });
    } else if (e.isDirectory()) {
      await collectTree(childPath, childKey, out);
    }
    // Symlinks / sockets / other: skip.
  }
}

function isEnoent(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    "code" in err &&
    (err as NodeJS.ErrnoException).code === "ENOENT"
  );
}

function runGcloudCp(localPath: string, gsUri: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn("gcloud", ["storage", "cp", "--quiet", localPath, gsUri], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", (err) => reject(err));
    child.on("close", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(
          new Error(
            `gcloud storage cp exited ${code}: ${stderr.trim().slice(0, 800) || "(no stderr)"}`,
          ),
        );
      }
    });
  });
}
