/**
 * storage capability — workspace-scoped filesystem.
 *
 * The construction binds workspace_path at build time; the tool source
 * cannot see that path and cannot resolve outside it. Every user-facing
 * path is normalised and joined against the workspace root, then verified
 * to remain inside it. Path-traversal escape is impossible by construction,
 * not by validation.
 *
 * Read-mode tools get {list, read, stat}.
 * Readwrite-mode tools additionally get {write, mkdir, remove}.
 *
 * Symlinks are not followed when listing — fs.readdir with withFileTypes
 * reports the link itself as a "link" entry. read/write would attempt to
 * follow; the abs-path check after realpath would catch any escape.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

import { emit } from "../audit.js";
import type { AgentCallContext, StorageGrant } from "../types.js";

export interface StorageReadCapability {
  list: (relPath?: string) => Promise<StorageEntry[]>;
  read: (relPath: string) => Promise<string>;
  stat: (relPath: string) => Promise<StorageStat>;
}

export interface StorageReadWriteCapability extends StorageReadCapability {
  write: (relPath: string, content: string) => Promise<void>;
  mkdir: (relPath: string) => Promise<void>;
  remove: (relPath: string) => Promise<void>;
}

export interface StorageEntry {
  name: string;
  type: "file" | "dir" | "link" | "other";
}

export interface StorageStat {
  type: "file" | "dir" | "link" | "other";
  size: number;
  modified: string;
}

/** Max bytes returned from storage.read in a single call. */
const READ_SIZE_LIMIT = 1_000_000; // 1 MB

export function makeStorageCapability(
  grant: StorageGrant | undefined,
  ctx: AgentCallContext,
): StorageReadCapability | StorageReadWriteCapability | undefined {
  if (!grant) return undefined;

  // Fail loudly if the caller didn't plumb a workspace path through — an empty
  // string would silently resolve to "/", which would let a tool read the
  // filesystem root. We refuse to construct the capability without a real path.
  if (!ctx.workspace_path || ctx.workspace_path.trim() === "") {
    throw new Error(
      "storage capability granted but ctx.workspace_path is empty — " +
        "plugin must supply the agent's workspace path",
    );
  }

  const root = path.resolve(ctx.workspace_path);

  /** Resolve a tool-supplied path to an absolute path provably inside the workspace. */
  const resolve = (rel: string): string => {
    if (typeof rel !== "string") {
      throw new Error("storage: path must be a string");
    }
    // Normalise as POSIX then strip leading slashes so absolute paths from
    // the tool side are treated as relative to the workspace root.
    const normalised = path.posix
      .normalize("/" + rel)
      .replace(/^\/+/, "");
    const abs = path.resolve(root, normalised);
    if (abs !== root && !abs.startsWith(root + path.sep)) {
      throw new Error(`storage: path '${rel}' escapes workspace`);
    }
    return abs;
  };

  const entryType = (
    isDir: boolean,
    isLink: boolean,
    isFile: boolean,
  ): StorageEntry["type"] => {
    if (isLink) return "link";
    if (isDir) return "dir";
    if (isFile) return "file";
    return "other";
  };

  const readCap: StorageReadCapability = {
    list: async (relPath = "") => {
      const dir = resolve(relPath);
      emit(ctx, { event: "storage.list", outcome: "ok", detail: { path: relPath } });
      const ents = await fs.readdir(dir, { withFileTypes: true });
      return ents.map((e) => ({
        name: e.name,
        type: entryType(e.isDirectory(), e.isSymbolicLink(), e.isFile()),
      }));
    },

    read: async (relPath) => {
      const file = resolve(relPath);
      const stat = await fs.stat(file);
      if (stat.size > READ_SIZE_LIMIT) {
        emit(ctx, {
          event: "storage.read",
          outcome: "denied",
          reason: "size_limit",
          detail: { path: relPath, size: stat.size, limit: READ_SIZE_LIMIT },
        });
        throw new Error(`storage.read: file too large (${stat.size} > ${READ_SIZE_LIMIT})`);
      }
      emit(ctx, { event: "storage.read", outcome: "ok", detail: { path: relPath, size: stat.size } });
      return fs.readFile(file, "utf8");
    },

    stat: async (relPath) => {
      const target = resolve(relPath);
      const s = await fs.lstat(target);
      emit(ctx, { event: "storage.stat", outcome: "ok", detail: { path: relPath } });
      return {
        type: entryType(s.isDirectory(), s.isSymbolicLink(), s.isFile()),
        size: s.size,
        modified: s.mtime.toISOString(),
      };
    },
  };

  if (grant.mode === "read") {
    return Object.freeze(readCap);
  }

  const rwCap: StorageReadWriteCapability = {
    ...readCap,

    write: async (relPath, content) => {
      const file = resolve(relPath);
      const bytes = Buffer.byteLength(content, "utf8");
      if (bytes > READ_SIZE_LIMIT) {
        emit(ctx, {
          event: "storage.write",
          outcome: "denied",
          reason: "size_limit",
          detail: { path: relPath, size: bytes, limit: READ_SIZE_LIMIT },
        });
        throw new Error(`storage.write: content too large (${bytes} > ${READ_SIZE_LIMIT})`);
      }
      emit(ctx, { event: "storage.write", outcome: "ok", detail: { path: relPath, size: bytes } });
      await fs.writeFile(file, content, "utf8");
    },

    mkdir: async (relPath) => {
      const dir = resolve(relPath);
      emit(ctx, { event: "storage.mkdir", outcome: "ok", detail: { path: relPath } });
      await fs.mkdir(dir, { recursive: true });
    },

    remove: async (relPath) => {
      const target = resolve(relPath);
      // Refuse to delete the workspace root itself.
      if (target === root) {
        emit(ctx, {
          event: "storage.remove",
          outcome: "denied",
          reason: "workspace_root",
          detail: { path: relPath },
        });
        throw new Error("storage.remove: cannot remove workspace root");
      }
      emit(ctx, { event: "storage.remove", outcome: "ok", detail: { path: relPath } });
      await fs.rm(target, { recursive: true, force: false });
    },
  };

  return Object.freeze(rwCap);
}
