import fs from "node:fs";
import path from "node:path";
import { resolveAgentWorkspaceDir, resolveDefaultAgentId } from "../../agents/agent-scope.js";
import { loadConfig } from "../../config/config.js";
import {
  loadSessionStore,
  resolveMainSessionKey,
  type SessionEntry,
  updateSessionStore,
} from "../../config/sessions.js";
import { normalizeAgentId, parseAgentSessionKey } from "../../routing/session-key.js";
import { GATEWAY_CLIENT_IDS } from "../protocol/client-info.js";
import {
  ErrorCodes,
  errorShape,
  validateSessionsCompactParams,
  validateSessionsDeleteParams,
  validateSessionsListParams,
  validateSessionsPatchParams,
  validateSessionsPiCompactParams,
  validateSessionsPreviewParams,
  validateSessionsResetParams,
  validateSessionsResolveParams,
  validateSessionsStatusParams,
} from "../protocol/index.js";
import { resolveMemoryFlushSettings } from "../../auto-reply/reply/memory-flush.js";
import { compactEmbeddedPiSessionDirect } from "../../agents/pi-embedded-runner/compact.js";
import {
  archiveSessionTranscriptsForSession,
  cleanupSessionBeforeMutation,
  emitSessionUnboundLifecycleEvent,
  performGatewaySessionReset,
} from "../session-reset-service.js";
import {
  archiveFileOnDisk,
  listSessionsFromStore,
  loadCombinedSessionStoreForGateway,
  loadSessionEntry,
  pruneLegacyStoreKeys,
  readSessionPreviewItemsFromTranscript,
  resolveGatewaySessionStoreTarget,
  resolveSessionModelRef,
  resolveSessionTranscriptCandidates,
  type SessionsPatchResult,
  type SessionsPreviewEntry,
  type SessionsPreviewResult,
  readSessionMessages,
} from "../session-utils.js";
import { applySessionsPatchToStore } from "../sessions-patch.js";
import { resolveSessionKeyFromResolveParams } from "../sessions-resolve.js";
import type { GatewayClient, GatewayRequestHandlers, RespondFn } from "./types.js";
import { assertValidParams } from "./validation.js";

/**
 * Cheap token estimator. char/4 is a good-enough proxy for tokens across
 * Claude / Gemini / OpenAI tokenizers; we deliberately don't pull in a
 * full tokenizer (~MB-scale dep) just for compaction budgeting.
 */
function estimateLineTokens(line: string): number {
  return Math.ceil(line.length / 4);
}

/**
 * Truncate the `content` field of a JSONL transcript line in-place when
 * the whole line is over the per-line cap. We preserve all metadata
 * (role, tool name, timestamps) so the structural shape of the
 * conversation remains intact for the LLM; only the giant payload is
 * elided.
 */
function truncateOversizeLine(line: string, maxLineTokens: number): {
  line: string;
  truncated: boolean;
} {
  if (estimateLineTokens(line) <= maxLineTokens) {
    return { line, truncated: false };
  }
  let parsed: Record<string, unknown> | null = null;
  try {
    parsed = JSON.parse(line) as Record<string, unknown>;
  } catch {
    return { line, truncated: false };
  }
  // Find any string-typed payload field worth shrinking.
  const candidates = ["content", "output", "result", "text"];
  let elided = false;
  for (const field of candidates) {
    const value = parsed[field];
    if (typeof value !== "string") continue;
    const charBudget = Math.max(200, maxLineTokens * 4 - 200);
    if (value.length <= charBudget) continue;
    const elidedChars = value.length - charBudget;
    parsed[field] =
      value.slice(0, Math.floor(charBudget / 2)) +
      `\n[...truncated: ${elidedChars} chars elided during compaction...]\n` +
      value.slice(value.length - Math.floor(charBudget / 2));
    elided = true;
  }
  if (!elided) {
    return { line, truncated: false };
  }
  return { line: JSON.stringify(parsed), truncated: true };
}

/**
 * Walk transcript lines newest-first, keeping lines whose cumulative
 * estimated tokens fit within `keptBudgetTokens`. When a single line is
 * over `maxSingleLineTokens`, its content body is truncated in-place via
 * `truncateOversizeLine` before its token cost is counted.
 *
 * Falls back to legacy `maxLines` semantics when `keptBudgetTokens` is
 * not set.
 */
function pickCompactionKeptLines(params: {
  lines: string[];
  keptBudgetTokens?: number;
  maxSingleLineTokens?: number;
  maxLines: number;
  force: boolean;
}): {
  keptLines: string[];
  truncatedLines: number;
  originalTotalTokens: number;
  keptTotalTokens: number;
} {
  const { lines, keptBudgetTokens, maxSingleLineTokens, maxLines } = params;
  const originalTotalTokens = lines.reduce((sum, l) => sum + estimateLineTokens(l), 0);

  // Token-budget mode.
  if (typeof keptBudgetTokens === "number") {
    const kept: string[] = [];
    let truncatedLines = 0;
    let cumTokens = 0;
    // Walk newest-first; reverse at the end so chronological order is
    // restored in the kept transcript.
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      let line = lines[i];
      if (typeof maxSingleLineTokens === "number") {
        const out = truncateOversizeLine(line, maxSingleLineTokens);
        line = out.line;
        if (out.truncated) truncatedLines += 1;
      }
      const cost = estimateLineTokens(line);
      if (cumTokens + cost > keptBudgetTokens && kept.length > 0) {
        break;
      }
      kept.push(line);
      cumTokens += cost;
    }
    kept.reverse();
    return {
      keptLines: kept,
      truncatedLines,
      originalTotalTokens,
      keptTotalTokens: cumTokens,
    };
  }

  // Legacy line-count mode.
  const keptLines = lines.slice(-maxLines);
  const keptTotalTokens = keptLines.reduce((s, l) => s + estimateLineTokens(l), 0);
  return {
    keptLines,
    truncatedLines: 0,
    originalTotalTokens,
    keptTotalTokens,
  };
}

/**
 * Find the most-recent assistant-emitted `<COMPACTION_SUMMARY>...
 * </COMPACTION_SUMMARY>` block in a JSONL transcript and return it
 * along with the line index it was found on (so caller can strip the
 * block from that line afterwards).
 *
 * Handles two openclaw message shapes:
 *  - `{role:"assistant", content: "...text..."}`              (simple)
 *  - `{role:"assistant", content:[{type:"text",text:"..."}]}` (multi-segment)
 *
 * In the multi-segment case the agent often splits its narration and
 * the block across separate text parts; the regex spans both because
 * we serialize to JSON first.
 */
function extractCompactionSummary(lines: string[]): {
  content: string;
  sourceLineIdx: number;
} | null {
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const raw = lines[i];
    if (!raw.includes("<COMPACTION_SUMMARY>")) continue;
    let parsed: Record<string, unknown> | null = null;
    try {
      parsed = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      continue;
    }
    // Only consider assistant-role records.
    const role =
      (parsed.role as string | undefined) ??
      ((parsed.message as Record<string, unknown> | undefined)?.role as string | undefined);
    if (role !== "assistant") continue;

    // Pull all text from the message, regardless of whether content is
    // a plain string or an array of {type,text} parts.
    const texts: string[] = [];
    const visit = (val: unknown) => {
      if (typeof val === "string") {
        texts.push(val);
        return;
      }
      if (Array.isArray(val)) {
        for (const item of val) visit(item);
        return;
      }
      if (val && typeof val === "object") {
        const rec = val as Record<string, unknown>;
        if (typeof rec.text === "string") texts.push(rec.text);
        if (rec.content !== undefined) visit(rec.content);
        if (rec.message !== undefined) visit(rec.message);
      }
    };
    visit(parsed);
    const joined = texts.join("\n");
    const match = joined.match(/<COMPACTION_SUMMARY>([\s\S]*?)<\/COMPACTION_SUMMARY>/);
    if (!match) continue;
    const content = match[1].trim();
    if (!content || content === "[Your synopsis here]") continue;
    return { content, sourceLineIdx: i };
  }
  return null;
}

/** Apply per-agent caps (maxChars wins as a hard ceiling). Returns the
 * truncated string + a flag indicating whether truncation happened. */
function applySummaryCaps(
  content: string,
  caps: { maxChars: number; maxTokens: number },
): { content: string; truncated: boolean } {
  const charCap = caps.maxChars > 0 ? caps.maxChars : Number.POSITIVE_INFINITY;
  const tokenCap = caps.maxTokens > 0 ? caps.maxTokens * 4 : Number.POSITIVE_INFINITY;
  const limit = Math.min(charCap, tokenCap);
  if (content.length <= limit) return { content, truncated: false };
  const keep = Math.max(200, limit - 80);
  return {
    content: `${content.slice(0, keep)}\n[truncated: ${content.length - keep} chars elided to fit compaction-summary cap]`,
    truncated: true,
  };
}

/** Build the synthetic system-role JSONL line that gets prepended to
 * the kept transcript. Same shape openclaw uses for system messages. */
function buildCompactionSummaryHeader(content: string): string {
  const ts = Date.now();
  return JSON.stringify({
    role: "system",
    content: `[Compaction summary, generated ${new Date(ts).toISOString()}]:\n${content}`,
    timestamp: ts,
  });
}

function requireSessionKey(key: unknown, respond: RespondFn): string | null {
  const raw =
    typeof key === "string"
      ? key
      : typeof key === "number"
        ? String(key)
        : typeof key === "bigint"
          ? String(key)
          : "";
  const normalized = raw.trim();
  if (!normalized) {
    respond(false, undefined, errorShape(ErrorCodes.INVALID_REQUEST, "key required"));
    return null;
  }
  return normalized;
}

function resolveGatewaySessionTargetFromKey(key: string) {
  const cfg = loadConfig();
  const target = resolveGatewaySessionStoreTarget({ cfg, key });
  return { cfg, target, storePath: target.storePath };
}

function rejectWebchatSessionMutation(params: {
  action: "patch" | "delete";
  client: GatewayClient | null;
  isWebchatConnect: (params: GatewayClient["connect"] | null | undefined) => boolean;
  respond: RespondFn;
}): boolean {
  if (!params.client?.connect || !params.isWebchatConnect(params.client.connect)) {
    return false;
  }
  if (params.client.connect.client.id === GATEWAY_CLIENT_IDS.CONTROL_UI) {
    return false;
  }
  params.respond(
    false,
    undefined,
    errorShape(
      ErrorCodes.INVALID_REQUEST,
      `webchat clients cannot ${params.action} sessions; use chat.send for session-scoped updates`,
    ),
  );
  return true;
}

function migrateAndPruneSessionStoreKey(params: {
  cfg: ReturnType<typeof loadConfig>;
  key: string;
  store: Record<string, SessionEntry>;
}) {
  const target = resolveGatewaySessionStoreTarget({
    cfg: params.cfg,
    key: params.key,
    store: params.store,
  });
  const primaryKey = target.canonicalKey;
  if (!params.store[primaryKey]) {
    const existingKey = target.storeKeys.find((candidate) => Boolean(params.store[candidate]));
    if (existingKey) {
      params.store[primaryKey] = params.store[existingKey];
    }
  }
  pruneLegacyStoreKeys({
    store: params.store,
    canonicalKey: primaryKey,
    candidates: target.storeKeys,
  });
  return { target, primaryKey, entry: params.store[primaryKey] };
}

export const sessionsHandlers: GatewayRequestHandlers = {
  "sessions.list": ({ params, respond }) => {
    if (!assertValidParams(params, validateSessionsListParams, "sessions.list", respond)) {
      return;
    }
    const p = params;
    const cfg = loadConfig();
    const { storePath, store } = loadCombinedSessionStoreForGateway(cfg);
    const result = listSessionsFromStore({
      cfg,
      storePath,
      store,
      opts: p,
    });
    respond(true, result, undefined);
  },
  "sessions.preview": ({ params, respond }) => {
    if (!assertValidParams(params, validateSessionsPreviewParams, "sessions.preview", respond)) {
      return;
    }
    const p = params;
    const keysRaw = Array.isArray(p.keys) ? p.keys : [];
    const keys = keysRaw
      .map((key) => String(key ?? "").trim())
      .filter(Boolean)
      .slice(0, 64);
    const limit =
      typeof p.limit === "number" && Number.isFinite(p.limit) ? Math.max(1, p.limit) : 12;
    const maxChars =
      typeof p.maxChars === "number" && Number.isFinite(p.maxChars)
        ? Math.max(20, p.maxChars)
        : 240;

    if (keys.length === 0) {
      respond(true, { ts: Date.now(), previews: [] } satisfies SessionsPreviewResult, undefined);
      return;
    }

    const cfg = loadConfig();
    const storeCache = new Map<string, Record<string, SessionEntry>>();
    const previews: SessionsPreviewEntry[] = [];

    for (const key of keys) {
      try {
        const storeTarget = resolveGatewaySessionStoreTarget({ cfg, key, scanLegacyKeys: false });
        const store =
          storeCache.get(storeTarget.storePath) ?? loadSessionStore(storeTarget.storePath);
        storeCache.set(storeTarget.storePath, store);
        const target = resolveGatewaySessionStoreTarget({
          cfg,
          key,
          store,
        });
        const entry = target.storeKeys.map((candidate) => store[candidate]).find(Boolean);
        if (!entry?.sessionId) {
          previews.push({ key, status: "missing", items: [] });
          continue;
        }
        const items = readSessionPreviewItemsFromTranscript(
          entry.sessionId,
          target.storePath,
          entry.sessionFile,
          target.agentId,
          limit,
          maxChars,
        );
        previews.push({
          key,
          status: items.length > 0 ? "ok" : "empty",
          items,
        });
      } catch {
        previews.push({ key, status: "error", items: [] });
      }
    }

    respond(true, { ts: Date.now(), previews } satisfies SessionsPreviewResult, undefined);
  },
  "sessions.resolve": async ({ params, respond }) => {
    if (!assertValidParams(params, validateSessionsResolveParams, "sessions.resolve", respond)) {
      return;
    }
    const p = params;
    const cfg = loadConfig();

    const resolved = await resolveSessionKeyFromResolveParams({ cfg, p });
    if (!resolved.ok) {
      respond(false, undefined, resolved.error);
      return;
    }
    respond(true, { ok: true, key: resolved.key }, undefined);
  },
  "sessions.patch": async ({ params, respond, context, client, isWebchatConnect }) => {
    if (!assertValidParams(params, validateSessionsPatchParams, "sessions.patch", respond)) {
      return;
    }
    const p = params;
    const key = requireSessionKey(p.key, respond);
    if (!key) {
      return;
    }
    if (rejectWebchatSessionMutation({ action: "patch", client, isWebchatConnect, respond })) {
      return;
    }

    const { cfg, target, storePath } = resolveGatewaySessionTargetFromKey(key);
    const applied = await updateSessionStore(storePath, async (store) => {
      const { primaryKey } = migrateAndPruneSessionStoreKey({ cfg, key, store });
      return await applySessionsPatchToStore({
        cfg,
        store,
        storeKey: primaryKey,
        patch: p,
        loadGatewayModelCatalog: context.loadGatewayModelCatalog,
      });
    });
    if (!applied.ok) {
      respond(false, undefined, applied.error);
      return;
    }
    const parsed = parseAgentSessionKey(target.canonicalKey ?? key);
    const agentId = normalizeAgentId(parsed?.agentId ?? resolveDefaultAgentId(cfg));
    const resolved = resolveSessionModelRef(cfg, applied.entry, agentId);
    const result: SessionsPatchResult = {
      ok: true,
      path: storePath,
      key: target.canonicalKey,
      entry: applied.entry,
      resolved: {
        modelProvider: resolved.provider,
        model: resolved.model,
      },
    };
    respond(true, result, undefined);
  },
  "sessions.reset": async ({ params, respond }) => {
    if (!assertValidParams(params, validateSessionsResetParams, "sessions.reset", respond)) {
      return;
    }
    const p = params;
    const key = requireSessionKey(p.key, respond);
    if (!key) {
      return;
    }

    const reason = p.reason === "new" ? "new" : "reset";
    const result = await performGatewaySessionReset({
      key,
      reason,
      commandSource: "gateway:sessions.reset",
    });
    if (!result.ok) {
      respond(false, undefined, result.error);
      return;
    }
    respond(true, { ok: true, key: result.key, entry: result.entry }, undefined);
  },
  "sessions.delete": async ({ params, respond, client, isWebchatConnect }) => {
    if (!assertValidParams(params, validateSessionsDeleteParams, "sessions.delete", respond)) {
      return;
    }
    const p = params;
    const key = requireSessionKey(p.key, respond);
    if (!key) {
      return;
    }
    if (rejectWebchatSessionMutation({ action: "delete", client, isWebchatConnect, respond })) {
      return;
    }

    const { cfg, target, storePath } = resolveGatewaySessionTargetFromKey(key);
    const mainKey = resolveMainSessionKey(cfg);
    if (target.canonicalKey === mainKey) {
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, `Cannot delete the main session (${mainKey}).`),
      );
      return;
    }

    const deleteTranscript = typeof p.deleteTranscript === "boolean" ? p.deleteTranscript : true;

    const { entry, legacyKey, canonicalKey } = loadSessionEntry(key);
    const mutationCleanupError = await cleanupSessionBeforeMutation({
      cfg,
      key,
      target,
      entry,
      legacyKey,
      canonicalKey,
      reason: "session-delete",
    });
    if (mutationCleanupError) {
      respond(false, undefined, mutationCleanupError);
      return;
    }
    const sessionId = entry?.sessionId;
    const deleted = await updateSessionStore(storePath, (store) => {
      const { primaryKey } = migrateAndPruneSessionStoreKey({ cfg, key, store });
      const hadEntry = Boolean(store[primaryKey]);
      if (hadEntry) {
        delete store[primaryKey];
      }
      return hadEntry;
    });

    const archived =
      deleted && deleteTranscript
        ? archiveSessionTranscriptsForSession({
            sessionId,
            storePath,
            sessionFile: entry?.sessionFile,
            agentId: target.agentId,
            reason: "deleted",
          })
        : [];
    if (deleted) {
      const emitLifecycleHooks = p.emitLifecycleHooks !== false;
      await emitSessionUnboundLifecycleEvent({
        targetSessionKey: target.canonicalKey ?? key,
        reason: "session-delete",
        emitHooks: emitLifecycleHooks,
      });
    }

    respond(true, { ok: true, key: target.canonicalKey, deleted, archived }, undefined);
  },
  "sessions.get": ({ params, respond }) => {
    const p = params;
    const key = requireSessionKey(p.key ?? p.sessionKey, respond);
    if (!key) {
      return;
    }
    const limit =
      typeof p.limit === "number" && Number.isFinite(p.limit)
        ? Math.max(1, Math.floor(p.limit))
        : 200;

    const { target, storePath } = resolveGatewaySessionTargetFromKey(key);
    const store = loadSessionStore(storePath);
    const entry = target.storeKeys.map((k) => store[k]).find(Boolean);
    if (!entry?.sessionId) {
      respond(true, { messages: [] }, undefined);
      return;
    }
    const allMessages = readSessionMessages(entry.sessionId, storePath, entry.sessionFile);
    const messages = limit < allMessages.length ? allMessages.slice(-limit) : allMessages;
    respond(true, { messages }, undefined);
  },
  "sessions.compact": async ({ params, respond }) => {
    if (!assertValidParams(params, validateSessionsCompactParams, "sessions.compact", respond)) {
      return;
    }
    const p = params;
    const key = requireSessionKey(p.key, respond);
    if (!key) {
      return;
    }

    // Token-budget mode wins when set; otherwise fall back to legacy
    // line-count mode for backward compatibility with older callers.
    const keptBudgetTokens =
      typeof p.keptBudgetTokens === "number" && Number.isFinite(p.keptBudgetTokens)
        ? Math.max(1, Math.floor(p.keptBudgetTokens))
        : undefined;
    const maxSingleLineTokens =
      typeof p.maxSingleLineTokens === "number" && Number.isFinite(p.maxSingleLineTokens)
        ? Math.max(1, Math.floor(p.maxSingleLineTokens))
        : undefined;
    const maxLines =
      typeof p.maxLines === "number" && Number.isFinite(p.maxLines)
        ? Math.max(1, Math.floor(p.maxLines))
        : 400;
    const force = p.force === true;

    const { cfg, target, storePath } = resolveGatewaySessionTargetFromKey(key);
    // Lock + read in a short critical section; transcript work happens outside.
    const compactTarget = await updateSessionStore(storePath, (store) => {
      const { entry, primaryKey } = migrateAndPruneSessionStoreKey({ cfg, key, store });
      return { entry, primaryKey };
    });
    const entry = compactTarget.entry;
    const sessionId = entry?.sessionId;
    if (!sessionId) {
      respond(
        true,
        {
          ok: true,
          key: target.canonicalKey,
          compacted: false,
          reason: "no sessionId",
        },
        undefined,
      );
      return;
    }

    const filePath = resolveSessionTranscriptCandidates(
      sessionId,
      storePath,
      entry?.sessionFile,
      target.agentId,
    ).find((candidate) => fs.existsSync(candidate));
    if (!filePath) {
      respond(
        true,
        {
          ok: true,
          key: target.canonicalKey,
          compacted: false,
          reason: "no transcript",
        },
        undefined,
      );
      return;
    }

    const raw = fs.readFileSync(filePath, "utf-8");
    const lines = raw.split(/\r?\n/).filter((l) => l.trim().length > 0);

    // Pick kept lines + per-line truncations.
    const { keptLines, truncatedLines, originalTotalTokens, keptTotalTokens } =
      pickCompactionKeptLines({
        lines,
        keptBudgetTokens,
        maxSingleLineTokens,
        maxLines,
        force,
      });

    // "Nothing to do" check — only when not forced and we didn't drop or
    // truncate any line.
    const droppedLines = lines.length - keptLines.length;
    if (!force && droppedLines === 0 && truncatedLines === 0) {
      respond(
        true,
        {
          ok: true,
          key: target.canonicalKey,
          compacted: false,
          kept: lines.length,
          totalTokens: originalTotalTokens,
        },
        undefined,
      );
      return;
    }

    // Look for an agent-emitted <COMPACTION_SUMMARY> block in the lines
    // we're about to drop (or in the most recent assistant turn if
    // nothing was dropped). Apply per-agent caps and prepend it as a
    // synthetic system line at the head of the kept transcript so the
    // agent has a synopsis on the next turn.
    let summaryHeaderLine: string | null = null;
    let summaryTruncated = false;
    let summaryChars = 0;
    try {
      const summary = extractCompactionSummary(lines);
      if (summary) {
        const agentIdFromKey = target.agentId ?? parseAgentSessionKey(key)?.agentId;
        const memoryFlush = resolveMemoryFlushSettings(cfg, agentIdFromKey ?? undefined);
        const caps = memoryFlush?.compactionSummary ?? {
          maxChars: 20000,
          maxTokens: 5000,
        };
        const capped = applySummaryCaps(summary.content, caps);
        summaryChars = capped.content.length;
        summaryTruncated = capped.truncated;
        summaryHeaderLine = buildCompactionSummaryHeader(capped.content);
      }
    } catch {
      // Extraction is best-effort — compaction continues regardless.
    }

    const archived = archiveFileOnDisk(filePath, "bak");
    const finalLines = summaryHeaderLine
      ? [summaryHeaderLine, ...keptLines]
      : keptLines;
    fs.writeFileSync(filePath, `${finalLines.join("\n")}\n`, "utf-8");

    await updateSessionStore(storePath, (store) => {
      const entryKey = compactTarget.primaryKey;
      const entryToUpdate = store[entryKey];
      if (!entryToUpdate) {
        return;
      }
      delete entryToUpdate.inputTokens;
      delete entryToUpdate.outputTokens;
      delete entryToUpdate.totalTokens;
      delete entryToUpdate.totalTokensFresh;
      entryToUpdate.compactionCount = (entryToUpdate.compactionCount ?? 0) + 1;
      entryToUpdate.updatedAt = Date.now();
    });

    respond(
      true,
      {
        ok: true,
        key: target.canonicalKey,
        compacted: true,
        archived,
        kept: finalLines.length,
        dropped: droppedLines,
        truncatedLines,
        originalTotalTokens,
        keptTotalTokens,
        summaryPrepended: summaryHeaderLine != null,
        summaryChars,
        summaryTruncated,
      },
      undefined,
    );
  },
  "sessions.status": ({ params, respond }) => {
    if (!assertValidParams(params, validateSessionsStatusParams, "sessions.status", respond)) {
      return;
    }
    const p = params;
    const key = requireSessionKey(p.key, respond);
    if (!key) {
      return;
    }

    const { cfg, target, storePath } = resolveGatewaySessionTargetFromKey(key);
    const store = loadSessionStore(storePath);
    const entry = target.storeKeys.map((k) => store[k]).find(Boolean);

    const agentId = target.agentId ?? parseAgentSessionKey(key)?.agentId;
    const memoryFlush = resolveMemoryFlushSettings(cfg, agentId ?? undefined);

    // Effective soft threshold in absolute tokens (pct wins when set).
    const effectiveSoftThresholdTokens =
      memoryFlush && memoryFlush.softThresholdPct && entry?.contextTokens
        ? Math.floor(entry.contextTokens * memoryFlush.softThresholdPct)
        : memoryFlush?.softThresholdTokens;

    // Resolve the agent's workspace to read MEMORY.md size. Best-effort:
    // any failure leaves memoryFile* fields undefined and the UI falls
    // back to "no data."
    let memoryFileChars: number | undefined;
    let memoryFileBytes: number | undefined;
    let memoryFileLimitChars: number | undefined;
    try {
      if (agentId) {
        const workspace = resolveAgentWorkspaceDir(cfg, agentId);
        if (workspace) {
          const memoryPath = path.join(workspace, "MEMORY.md");
          if (fs.existsSync(memoryPath)) {
            const content = fs.readFileSync(memoryPath, "utf-8");
            memoryFileChars = content.length;
            memoryFileBytes = Buffer.byteLength(content, "utf-8");
          }
          // Limit = bootstrapMaxChars from config; falls back to 20k.
          const bootstrapMaxChars = cfg?.agents?.defaults?.bootstrapMaxChars;
          memoryFileLimitChars =
            typeof bootstrapMaxChars === "number" && bootstrapMaxChars > 0
              ? bootstrapMaxChars
              : 20000;
        }
      }
    } catch {
      // Swallow — best-effort.
    }

    respond(
      true,
      {
        ok: true,
        key: target.canonicalKey ?? key,
        sessionId: entry?.sessionId,
        model: entry?.model,
        modelProvider: entry?.modelProvider,
        inputTokens: entry?.inputTokens,
        outputTokens: entry?.outputTokens,
        totalTokens: entry?.totalTokens,
        totalTokensFresh: entry?.totalTokensFresh,
        cacheRead: entry?.cacheRead,
        cacheWrite: entry?.cacheWrite,
        contextTokens: entry?.contextTokens,
        compactionCount: entry?.compactionCount ?? 0,
        // 2026-05-26 counter-based flush gate. CP's auto_flush
        // dispatcher checks `memoryFlushCount > compactionCount`
        // to decide whether to skip dispatch (already flushed this
        // cycle, waiting for next compaction).
        memoryFlushCount: entry?.memoryFlushCount ?? 0,
        // Trigger threshold as fraction of contextWindow. Default 0.3.
        // CP computes `contextTokens * flushThresholdPct` for the
        // absolute trigger threshold.
        flushThresholdPct:
          (memoryFlush as { flushThresholdPct?: number } | null)?.flushThresholdPct ?? 0.3,
        // ── Legacy/deprecated fields, kept for backward compat ──
        memoryFlushLastFiredAtTokens: entry?.memoryFlushLastFiredAtTokens ?? null,
        softThresholdTokens: effectiveSoftThresholdTokens,
        softThresholdPct: memoryFlush?.softThresholdPct,
        reserveTokensFloor: memoryFlush?.reserveTokensFloor,
        memoryFileChars,
        memoryFileBytes,
        memoryFileLimitChars,
      },
      undefined,
    );
  },
  /**
   * Manual trigger of Pi's own compaction (the safeguard pathway when
   * mode=safeguard). Replaces the manual-button use of the legacy
   * line-truncation `sessions.compact` with the real production code
   * path. Pi's compaction:
   *  - Runs the safeguard's LLM-based summarization using our per-agent
   *    `summaryPrompt` as customInstructions (via the 3c8fd32 wire-in).
   *  - Preserves the most recent N turns verbatim (`recentTurnsPreserve`).
   *  - Replaces older messages with the structured summary in-place.
   *  - Does NOT consume any pending user message — Pi compaction happens
   *    on the existing session state; the next user turn proceeds with
   *    the post-compaction context.
   *
   * The user's manual button doesn't pass any auth/group context — we
   * resolve sessionId/sessionFile/workspaceDir/provider/model from the
   * SessionEntry and let `compactEmbeddedPiSessionDirect` fall back to
   * defaults for everything else.
   */
  "sessions.piCompact": async ({ params, respond }) => {
    if (!assertValidParams(params, validateSessionsPiCompactParams, "sessions.piCompact", respond)) {
      return;
    }
    const p = params;
    const key = requireSessionKey(p.key, respond);
    if (!key) {
      return;
    }

    const { cfg, target, storePath } = resolveGatewaySessionTargetFromKey(key);
    const store = loadSessionStore(storePath);
    const entry = target.storeKeys.map((k) => store[k]).find(Boolean);
    if (!entry?.sessionId) {
      respond(
        true,
        {
          ok: false,
          key: target.canonicalKey ?? key,
          compacted: false,
          reason: "no sessionId",
        },
        undefined,
      );
      return;
    }

    const filePath = resolveSessionTranscriptCandidates(
      entry.sessionId,
      storePath,
      entry.sessionFile,
      target.agentId,
    ).find((candidate) => fs.existsSync(candidate));
    if (!filePath) {
      respond(
        true,
        {
          ok: false,
          key: target.canonicalKey ?? key,
          compacted: false,
          reason: "no transcript",
        },
        undefined,
      );
      return;
    }

    const workspaceDir = resolveAgentWorkspaceDir(cfg, target.agentId);
    if (!workspaceDir) {
      respond(
        true,
        {
          ok: false,
          key: target.canonicalKey ?? key,
          compacted: false,
          reason: "no workspace",
        },
        undefined,
      );
      return;
    }

    try {
      const result = await compactEmbeddedPiSessionDirect({
        sessionId: entry.sessionId,
        sessionKey: target.canonicalKey ?? key,
        sessionFile: filePath,
        workspaceDir,
        config: cfg,
        provider: entry.modelProvider,
        model: entry.model,
        trigger: "manual",
        force: true,
      });
      respond(
        true,
        {
          ok: result.ok,
          key: target.canonicalKey ?? key,
          compacted: result.compacted,
          reason: result.reason,
          ...result,
        },
        undefined,
      );
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.UNAVAILABLE, `piCompact failed: ${reason}`),
      );
    }
  },
};
