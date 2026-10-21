import type { ErrorShape } from "./types.js";

export const ErrorCodes = {
  NOT_LINKED: "NOT_LINKED",
  NOT_PAIRED: "NOT_PAIRED",
  AGENT_TIMEOUT: "AGENT_TIMEOUT",
  INVALID_REQUEST: "INVALID_REQUEST",
  UNAVAILABLE: "UNAVAILABLE",
  /** File operation failed due to I/O error (permission denied, disk full, etc.) */
  FILE_WRITE_ERROR: "FILE_WRITE_ERROR",
  /** File path failed security validation (traversal, symlink escape, hardlink) */
  FILE_UNSAFE_PATH: "FILE_UNSAFE_PATH",
  /** Agent not found by the provided agentId */
  AGENT_NOT_FOUND: "AGENT_NOT_FOUND",
  /** Tool execution failed */
  TOOL_EXEC_ERROR: "TOOL_EXEC_ERROR",
  /**
   * An unexpected server-side failure: the request was well-formed and the
   * handler could not complete it. Distinct from UNAVAILABLE (try later) and
   * from INVALID_REQUEST (the caller's fault).
   *
   * ADDED BECAUSE IT WAS ALREADY IN USE — 51 times, across
   * `server-methods/agents.ts`, `server-methods/memory.ts` and the
   * workspace-ops extension, referencing a member that did not exist. Every
   * one of those evaluated to `undefined`.
   *
   * That is not a cosmetic gap. `errorShape` puts the value straight into
   * `{code, message}`, `undefined` is dropped by JSON serialisation, and
   * `ErrorShapeSchema.code` is a REQUIRED NonEmptyString — so the frame went
   * out invalid. A client validating with `validateResponseFrame` (see
   * `gateway/client.ts`) drops such a frame on the floor and never settles
   * its pending promise, turning a clean error into a hang until timeout;
   * the control panel is more forgiving and reported every internal gateway
   * failure as `code="UNKNOWN"`.
   *
   * The compiler had been saying so all along — this is the single missing
   * member behind ~40 of the repo's TS2339 errors. See error-codes.test.ts,
   * which asserts the invariant without needing the whole repo to typecheck.
   */
  INTERNAL_ERROR: "INTERNAL_ERROR",
} as const;

export type ErrorCode = (typeof ErrorCodes)[keyof typeof ErrorCodes];

export function errorShape(
  code: ErrorCode,
  message: string,
  opts?: { details?: unknown; retryable?: boolean; retryAfterMs?: number },
): ErrorShape {
  return {
    code,
    message,
    ...opts,
  };
}
