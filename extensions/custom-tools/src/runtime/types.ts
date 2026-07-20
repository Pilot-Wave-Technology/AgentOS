/**
 * Shape of a tool manifest — what the admin declares the tool can do.
 *
 * Stored alongside the tool source in tools.manifest (JSONB) in the
 * control panel. The runtime constructs capabilities matching exactly
 * what the manifest grants and nothing else.
 */
export interface ToolManifest {
  capabilities?: {
    net?: NetGrant;
    storage?: StorageGrant;
    secrets?: SecretsGrant;
    mcp?: McpGrant;
  };
  limits?: {
    /** Per-call wall-clock cap. Default 30_000 ms; hard ceiling 120_000. */
    timeout_ms?: number;
  };
}

export interface NetGrant {
  /** Hostnames the tool is allowed to call (exact match, no wildcards). */
  hosts: string[];
}

export interface StorageGrant {
  /** "read" = list+read only; "readwrite" = also write+delete. */
  mode: "read" | "readwrite";
}

export interface SecretsGrant {
  /** Credential names the tool may resolve via secrets.get(). */
  names: string[];
}

export interface McpGrant {
  /** MCP server URLs the tool may call (exact match, must include scheme). */
  servers: string[];
}

/**
 * Per-call context the gateway passes to the runtime.
 * Built fresh on every tool invocation — never reused across calls.
 */
export interface AgentCallContext {
  tool_name: string;
  agent_id: string;
  /** Absolute filesystem path to this agent's workspace. */
  workspace_path: string;
  /** AgentOS control panel base URL (for credential lookups). */
  control_panel_url: string;
  /** Bearer token for AgentOS internal endpoints. */
  control_panel_token: string;
  /** Free-form session identifier — included in audit events. */
  session_key: string;
}

/** Tool the runtime knows how to execute. */
export interface RuntimeTool {
  name: string;
  source_code: string;
  manifest: ToolManifest;
}

/** Result returned from net.fetch — a fixed, simplified shape (not a native Response). */
export interface NetResponse {
  status: number;
  ok: boolean;
  headers: Record<string, string>;
  text: () => string;
  json: () => unknown;
}
