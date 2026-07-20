/**
 * Per-agent control-plane authentication (shared-token security fix).
 *
 * Historically every CP-calling tool read the single shared OPENCLAW_GATEWAY_TOKEN
 * from the env and sent it as the bearer. This helper lets a tool instead fetch a
 * short-lived, per-agent, goal-scoped JWT from pwt (`POST /internal/agent-token`)
 * and cache it per (agentId, goalId).
 *
 * FAIL-SAFE BY DESIGN: when the feature is disabled (AGENT_SCOPED_TOKENS_ENABLED
 * not "true") OR on ANY error (no url, unresolved agent/goal, network, non-2xx,
 * bad body) it returns the shared OPENCLAW_GATEWAY_TOKEN — i.e. exactly today's
 * behaviour. It never throws. So converting a tool to use this is behaviour-
 * preserving until the flag is turned on, and degrades gracefully if minting fails.
 *
 * See SHARED_TOKEN_SECURITY_FIX_PLAN.md (pwt mints; the gateway only fetches).
 */
import { resolveAgentIdFromSessionKey } from "../../routing/session-key.js";

export function getControlPanelUrl(): string {
  return (process.env.CONTROL_PANEL_URL || "").replace(/\/$/, "");
}

function sharedToken(): string {
  return process.env.OPENCLAW_GATEWAY_TOKEN || "";
}

function scopedTokensEnabled(): boolean {
  // Default ON — opt out with AGENT_SCOPED_TOKENS_ENABLED=false. The CONTROL PLANE is
  // the real gate: if it can't mint (no signing key / feature off, e.g. on an env that
  // isn't ready), getControlPanelToken() fails safe to the shared token below. So
  // enabling here is harmless where pwt isn't ready, and needs no per-VM env change.
  return (process.env.AGENT_SCOPED_TOKENS_ENABLED || "").toLowerCase() !== "false";
}

function resolveGoalIdFromSession(sessionKey?: string): string {
  if (!sessionKey) return "";
  // Session key format: agent:{agentId}:{goalId}-{threadId}
  const parts = sessionKey.split(":");
  if (parts.length >= 3) {
    const goalThread = parts.slice(2).join(":");
    const dashIdx = goalThread.indexOf("-");
    return dashIdx > 0 ? goalThread.substring(0, dashIdx) : goalThread;
  }
  return "";
}

type CacheEntry = { token: string; expEpoch: number };
const _tokenCache = new Map<string, CacheEntry>();

/**
 * Returns the bearer token to use for a control-plane call.
 * Per-agent JWT when enabled and resolvable; otherwise the shared token. Never throws.
 */
export async function getControlPanelToken(
  agentSessionKey?: string,
  goalIdHint?: string,
): Promise<string> {
  if (!scopedTokensEnabled()) return sharedToken();
  try {
    const url = getControlPanelUrl();
    const agentId = agentSessionKey ? resolveAgentIdFromSessionKey(agentSessionKey) : "";
    const goalId = goalIdHint || resolveGoalIdFromSession(agentSessionKey);
    if (!url || !agentId || !goalId) return sharedToken();

    const cacheKey = `${agentId}::${goalId}`;
    const nowEpoch = Math.floor(Date.now() / 1000);
    const cached = _tokenCache.get(cacheKey);
    if (cached && cached.expEpoch > nowEpoch) return cached.token;

    // On any failure, negative-cache the shared-token fallback for 60s so a
    // not-ready / erroring control plane isn't hit on every single tool call.
    const fallback = (): string => {
      const t = sharedToken();
      _tokenCache.set(cacheKey, { token: t, expEpoch: nowEpoch + 60 });
      return t;
    };

    const res = await fetch(`${url}/internal/agent-token`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${sharedToken()}`,
      },
      body: JSON.stringify({ agent_id: agentId, goal_id: goalId }),
    });
    if (!res.ok) return fallback();
    const data = (await res.json()) as { token?: string; expires_in?: number };
    if (!data?.token) return fallback();
    // Cache the JWT until ~60s before it expires (then re-mint).
    _tokenCache.set(cacheKey, {
      token: data.token,
      expEpoch: nowEpoch + Math.max((data.expires_in || 3600) - 60, 60),
    });
    return data.token;
  } catch {
    return sharedToken();
  }
}

/**
 * Drop-in replacement for the old per-file `getControlPanelConfig()`: same
 * `{ url, token }` shape, but `token` is the per-agent JWT when enabled.
 * `await getControlPanelAuth(opts?.agentSessionKey)` at each call site.
 */
export async function getControlPanelAuth(
  agentSessionKey?: string,
  goalIdHint?: string,
): Promise<{ url: string; token: string }> {
  return { url: getControlPanelUrl(), token: await getControlPanelToken(agentSessionKey, goalIdHint) };
}
