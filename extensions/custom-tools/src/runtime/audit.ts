/**
 * Structured audit log for every capability call.
 *
 * Each emit() builds a JSON event and pushes it into a local ring buffer. A
 * background flusher (started by initAuditSink) periodically POSTs the buffer
 * to AgentOS's /internal/custom-tools/audit endpoint. If the post fails, the
 * events stay in the buffer up to MAX_BUFFER; oldest entries are dropped when
 * the buffer overflows, so a long AgentOS outage can't OOM the gateway.
 *
 * As a fallback (and for local dev where CONTROL_PANEL_URL isn't set), each
 * event is also written to stdout — Cloud Logging / journalctl picks it up.
 *
 * DI seam: emit() is the only public surface. The sink is configured once
 * via initAuditSink({ url, token }) at plugin startup; capabilities just
 * call emit() without knowing anything about transport.
 */

import type { AgentCallContext } from "./types.js";

export interface AuditEvent {
  /** Capability operation, e.g. "net.fetch", "storage.read", "secrets.get". */
  event: string;
  /** Outcome: "ok" if the operation completed, "denied" if blocked by grant. */
  outcome: "ok" | "denied";
  /** Optional structured payload (URL, path, name — never plaintext values). */
  detail?: Record<string, unknown>;
  /** Optional error reason when outcome="denied". */
  reason?: string;
}

interface StoredEvent {
  ts: string;
  tool: string | null;
  agent_id: string | null;
  session_key: string | null;
  event: string;
  outcome: "ok" | "denied";
  reason: string | null;
  detail: Record<string, unknown> | null;
}

interface AuditSink {
  url: string;
  token: string;
}

const MAX_BUFFER = 2000;     // cap to keep memory bounded on AgentOS outage
const FLUSH_INTERVAL_MS = 5_000;
const FLUSH_BATCH_SIZE = 200;

let sink: AuditSink | null = null;
let buffer: StoredEvent[] = [];
let flushTimer: ReturnType<typeof setInterval> | null = null;

export function initAuditSink(s: AuditSink): void {
  sink = s;
  if (flushTimer) return;
  flushTimer = setInterval(() => {
    flush().catch(() => {
      // swallowed — flush() already logged the failure and kept the buffer.
    });
  }, FLUSH_INTERVAL_MS);
  // Allow process to exit cleanly without waiting on this timer.
  if (typeof flushTimer === "object" && flushTimer && "unref" in flushTimer) {
    (flushTimer as { unref: () => void }).unref();
  }
}

export function stopAuditSink(): void {
  if (flushTimer) {
    clearInterval(flushTimer);
    flushTimer = null;
  }
}

export function emit(ctx: AgentCallContext, ev: AuditEvent): void {
  const stored: StoredEvent = {
    ts: new Date().toISOString(),
    tool: ctx.tool_name ?? null,
    agent_id: ctx.agent_id ?? null,
    session_key: ctx.session_key ?? null,
    event: ev.event,
    outcome: ev.outcome,
    reason: ev.reason ?? null,
    detail: ev.detail ?? null,
  };

  // Always log to stdout — single-line JSON, no PII beyond what AgentOS sees.
  // eslint-disable-next-line no-console
  console.log(JSON.stringify({ kind: "tool_capability", ...stored }));

  // Buffer for AgentOS forwarding. Drop oldest if we're over the cap.
  buffer.push(stored);
  if (buffer.length > MAX_BUFFER) {
    buffer = buffer.slice(-MAX_BUFFER);
  }
}

async function flush(): Promise<void> {
  if (!sink || buffer.length === 0) return;

  // Drain up to one batch from the buffer. If the POST fails we put the
  // events back at the front so order is preserved.
  const batch = buffer.splice(0, FLUSH_BATCH_SIZE);
  try {
    const res = await globalThis.fetch(`${sink.url}/internal/custom-tools/audit`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${sink.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ events: batch }),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
  } catch (err) {
    // Restore the batch at the front and let next tick retry. Cap re-applied
    // so we don't grow unbounded if the failure persists.
    buffer = [...batch, ...buffer];
    if (buffer.length > MAX_BUFFER) buffer = buffer.slice(-MAX_BUFFER);
    // eslint-disable-next-line no-console
    console.log(
      JSON.stringify({
        kind: "tool_capability_audit_flush_failed",
        error: String(err),
        buffered: buffer.length,
      }),
    );
  }
}
