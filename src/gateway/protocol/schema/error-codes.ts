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
