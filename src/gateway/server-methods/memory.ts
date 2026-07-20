/**
 * Memory RPCs for the control panel's Option B memory system.
 *
 * Four methods, all admin-scoped (caller is the agentos gateway client):
 *
 *   memory.append            — append a line to MEMORY.md or memory/<date>.md
 *                              (control-panel event bridge calls this on
 *                              whitelisted ledger events)
 *   memory.list              — list MEMORY.md + memory/*.md in the agent
 *                              workspace, with size and mtime (used by the
 *                              snapshot logic in agentos)
 *   memory.ensure_user_file  — idempotent create of
 *                              ~/.openclaw/user-memory/<uid>/USER_MEMORY.md
 *   memory.symlink_user_file — symlink the per-user file into an agent's
 *                              workspace as USER_MEMORY.md
 *
 * Path discipline is enforced inside each handler — these RPCs deliberately
 * cannot write outside MEMORY.md / memory/*.md / the per-user shared file.
 */

import { promises as fs } from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { loadConfig } from "../../config/config.js";
import { listAgentIds, resolveAgentWorkspaceDir } from "../../agents/agent-scope.js";
import { normalizeAgentId } from "../../routing/session-key.js";
import { ErrorCodes, errorShape } from "../protocol/index.js";
import type { GatewayRequestHandlers } from "./types.js";

/** Inline the resolution that agents.ts keeps private — same logic. */
function resolveAgentIdOrNull(agentIdRaw: string, cfg: ReturnType<typeof loadConfig>): string | null {
  const agentId = normalizeAgentId(agentIdRaw);
  const allowed = new Set(listAgentIds(cfg));
  return allowed.has(agentId) ? agentId : null;
}

// ────────────────────────────────────────────────────────────────────────────
// Path discipline

/** Memory files allowed by memory.append and memory.list. */
const MEMORY_FILE_REGEX = /^(MEMORY\.md|memory\/[A-Za-z0-9._-]+\.md)$/;

function isAllowedMemoryFilename(filename: string): boolean {
  return MEMORY_FILE_REGEX.test(filename);
}

/** Resolve ~/.openclaw user-memory dir for a user uid. Sanitize uid. */
function userMemoryDir(userUid: string): string {
  const safeUid = userUid.replace(/[^A-Za-z0-9_-]/g, "");
  if (!safeUid || safeUid !== userUid) {
    throw new Error(`invalid userUid: ${userUid}`);
  }
  // Mirror the OPENCLAW_STATE_DIR resolution used elsewhere — fall back
  // to ~/.openclaw if not set.
  const stateDir = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), ".openclaw");
  return path.join(stateDir, "user-memory", safeUid);
}

function userMemoryFilePath(userUid: string): string {
  return path.join(userMemoryDir(userUid), "USER_MEMORY.md");
}

// ────────────────────────────────────────────────────────────────────────────
// Handlers

export const memoryHandlers: GatewayRequestHandlers = {
  /**
   * memory.append — append content to MEMORY.md or memory/*.md.
   *
   * Params: { agentId, filename, content, triggerReindex? }
   * Response: { ok, bytes_appended, file_size_bytes }
   *
   * Atomic append via fs.appendFile. POSIX guarantees atomicity for writes
   * up to PIPE_BUF (4KB on Linux) so callers should keep lines short.
   */
  "memory.append": async ({ params, respond }) => {
    const agentIdRaw = String(params.agentId ?? "");
    const filename = String(params.filename ?? "");
    const content = String(params.content ?? "");

    if (!agentIdRaw || !filename) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        "memory.append requires agentId and filename",
      ));
      return;
    }
    if (!isAllowedMemoryFilename(filename)) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        `memory.append: filename '${filename}' is not an allowed memory file; ` +
        `must be MEMORY.md or memory/*.md`,
      ));
      return;
    }
    if (!content) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        "memory.append: content is empty",
      ));
      return;
    }

    const cfg = loadConfig();
    const agentId = resolveAgentIdOrNull(agentIdRaw, cfg);
    if (!agentId) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "unknown agent id"));
      return;
    }
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
    const filePath = path.join(workspaceDir, filename);

    try {
      await fs.mkdir(path.dirname(filePath), { recursive: true });
      const bytesAdded = Buffer.byteLength(content, "utf-8");
      await fs.appendFile(filePath, content, { encoding: "utf-8" });
      const stat = await fs.stat(filePath);
      respond(true, {
        ok: true,
        agentId,
        filename,
        bytes_appended: bytesAdded,
        file_size_bytes: stat.size,
      }, undefined);
    } catch (err) {
      const reason = err instanceof Error ? err.message : "unknown append error";
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `memory.append failed for ${filename}: ${reason}`,
      ));
    }
  },

  /**
   * memory.list — list MEMORY.md + memory/*.md in the workspace.
   *
   * Params: { agentId }
   * Response: { entries: [{ filename, bytes, mtimeMs }, ...] }
   *
   * Returns an empty list (not an error) if the memory/ subdir doesn't exist.
   */
  "memory.list": async ({ params, respond }) => {
    const agentIdRaw = String(params.agentId ?? "");
    if (!agentIdRaw) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        "memory.list requires agentId",
      ));
      return;
    }

    const cfg = loadConfig();
    const agentId = resolveAgentIdOrNull(agentIdRaw, cfg);
    if (!agentId) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "unknown agent id"));
      return;
    }
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);

    type Entry = { filename: string; bytes: number; mtimeMs: number };
    const entries: Entry[] = [];

    // MEMORY.md at workspace root
    try {
      const memPath = path.join(workspaceDir, "MEMORY.md");
      const stat = await fs.stat(memPath);
      if (stat.isFile()) {
        entries.push({
          filename: "MEMORY.md",
          bytes: stat.size,
          mtimeMs: Math.floor(stat.mtimeMs),
        });
      }
    } catch {
      // missing MEMORY.md is fine — first session
    }

    // memory/*.md
    try {
      const memDir = path.join(workspaceDir, "memory");
      const dirEntries = await fs.readdir(memDir, { withFileTypes: true });
      for (const ent of dirEntries) {
        if (!ent.isFile()) continue;
        if (!ent.name.endsWith(".md")) continue;
        try {
          const stat = await fs.stat(path.join(memDir, ent.name));
          entries.push({
            filename: `memory/${ent.name}`,
            bytes: stat.size,
            mtimeMs: Math.floor(stat.mtimeMs),
          });
        } catch {
          // skip files we can't stat
        }
      }
    } catch {
      // missing memory/ dir is fine
    }

    respond(true, { agentId, entries }, undefined);
  },

  /**
   * memory.ensure_user_file — idempotent create of the per-user shared
   * USER_MEMORY.md at ~/.openclaw/user-memory/<uid>/USER_MEMORY.md.
   *
   * Params: { userUid }
   * Response: { created, path }
   */
  "memory.ensure_user_file": async ({ params, respond }) => {
    const userUid = String(params.userUid ?? "");
    if (!userUid) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        "memory.ensure_user_file requires userUid",
      ));
      return;
    }

    let targetPath: string;
    try {
      targetPath = userMemoryFilePath(userUid);
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        err instanceof Error ? err.message : "invalid userUid",
      ));
      return;
    }

    try {
      await fs.mkdir(path.dirname(targetPath), { recursive: true });
      let created = false;
      try {
        await fs.stat(targetPath);
      } catch {
        const header = `# User Memory — ${userUid}\n\n` +
          `(This file is shared across all your SHCA agents. Each one ` +
          `can read it; the user's own SHCAs can write to it.)\n`;
        await fs.writeFile(targetPath, header, { encoding: "utf-8" });
        created = true;
      }
      respond(true, { ok: true, created, path: targetPath }, undefined);
    } catch (err) {
      const reason = err instanceof Error ? err.message : "unknown error";
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `memory.ensure_user_file failed: ${reason}`,
      ));
    }
  },

  /**
   * memory.symlink_user_file — create or refresh the USER_MEMORY.md
   * symlink inside an agent's workspace pointing at the shared per-user
   * file. Idempotent: if symlink already exists pointing at the right
   * target, no-op.
   *
   * Params: { agentId, userUid }
   * Response: { linked, target }
   */
  "memory.symlink_user_file": async ({ params, respond }) => {
    const agentIdRaw = String(params.agentId ?? "");
    const userUid = String(params.userUid ?? "");
    if (!agentIdRaw || !userUid) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        "memory.symlink_user_file requires agentId and userUid",
      ));
      return;
    }

    let target: string;
    try {
      target = userMemoryFilePath(userUid);
    } catch (err) {
      respond(false, undefined, errorShape(
        ErrorCodes.INVALID_REQUEST,
        err instanceof Error ? err.message : "invalid userUid",
      ));
      return;
    }

    const cfg = loadConfig();
    const agentId = resolveAgentIdOrNull(agentIdRaw, cfg);
    if (!agentId) {
      respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "unknown agent id"));
      return;
    }
    const workspaceDir = resolveAgentWorkspaceDir(cfg, agentId);
    const linkPath = path.join(workspaceDir, "USER_MEMORY.md");

    try {
      await fs.mkdir(workspaceDir, { recursive: true });

      // If link already exists and points where we want, return no-op.
      try {
        const existing = await fs.readlink(linkPath);
        if (existing === target) {
          respond(true, { ok: true, linked: false, target, reason: "already_linked" }, undefined);
          return;
        }
        // Wrong target — unlink and re-create
        await fs.unlink(linkPath);
      } catch {
        // not a symlink, or doesn't exist — proceed to create
        try {
          // If a regular file exists where we want the symlink, remove it
          await fs.unlink(linkPath);
        } catch {
          // didn't exist — fine
        }
      }

      await fs.symlink(target, linkPath);
      respond(true, { ok: true, linked: true, target }, undefined);
    } catch (err) {
      const reason = err instanceof Error ? err.message : "unknown error";
      respond(false, undefined, errorShape(
        ErrorCodes.INTERNAL_ERROR,
        `memory.symlink_user_file failed: ${reason}`,
      ));
    }
  },
};
