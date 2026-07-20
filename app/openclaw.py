"""
OpenClaw gateway WebSocket bridge and HTTP helpers.

## Protocol

The OpenClaw gateway exposes a SINGLE WebSocket endpoint (no path routing).
Auth and multiplexing happen at the application protocol level.

Connection flow:
  1. Connect to ws://<host>:<port>  (no path)
  2. Send connect handshake:
       { "type": "req", "id": "connect-1", "method": "connect",
         "params": { "minProtocol": 1, "maxProtocol": 10,
                     "client": { "id": "control-panel", ... },
                     "auth": { "token": "<token>" } } }
  3. Receive hello-ok:
       { "type": "hello-ok", "protocol": 3, "server": { "connId": "..." } }
  4. Send agent message:
       { "type": "req", "id": "<req-uuid>", "method": "agent",
         "params": { "agentId": "...", "sessionKey": "<pid>-<agent_id>",
                     "message": "...", "idempotencyKey": "<uuid>" } }
  5. Receive ack:
       { "type": "res", "id": "<req-uuid>", "ok": true,
         "payload": { "runId": "<run-uuid>" } }
  6. Receive stream events until state == "final":
       { "type": "event", "event": "chat",
         "payload": { "runId": "...", "sessionKey": "...",
                      "seq": N, "state": "delta"|"final"|"aborted"|"error",
                      "message": "..." } }

## Session model

Each (pid, agent_id) pair has ONE persistent authenticated gateway WS,
managed by SessionManager. Browser WS connections are ephemeral subscribers.

Run tracking:
  - On send: req_id -> thread_id  (we know which browser thread sent the msg)
  - On ack (res): runId -> thread_id  (gateway assigns runId; req_id removed)
  - On event: runId -> thread_id  (route token deltas to correct subscribers)

                ┌─────────────────────────────────────┐
                │           FastAPI process            │
                │                                      │
  Browser WS ──►│ subscribe()    SessionManager        │
  (ephemeral)   │     │         ┌────────────────────┐ │
                │     └────────►│ _subscribers set   │ │──► Firestore
                │               │                    │ │    (always)
                │               │ _gw_ws (persistent)│◄│──── OpenClaw WS
                │               │ _runs / _buffers   │ │    (idle-timeout)
                │               └────────────────────┘ │
                └─────────────────────────────────────┘
"""
import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Callable, Optional
from uuid import uuid4

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from app.config import get_settings


# ── Session key helpers ──────────────────────────────────────────────────────

def make_goal_session_key(agent_id: str, goal_id: str, thread_id: str) -> str:
    """Build the gateway sessionKey for a goal/chat session."""
    return f"agent:{agent_id}:{goal_id.lower()}-{thread_id}"


def make_task_session_key(agent_id: str, task_id: str) -> str:
    """Build the gateway sessionKey for a task session."""
    return f"agent:{agent_id}:task-{task_id}"


# ── Gateway error wrapper ────────────────────────────────────────────────────

class GatewayError(Exception):
    """Structured error from the OpenClaw gateway.

    Preserves the full error shape returned by the gateway protocol:
      { code, message, details?, retryable?, retryAfterMs? }
    """

    def __init__(
        self,
        message: str,
        code: str = "UNKNOWN",
        details: object = None,
        retryable: bool = False,
        retry_after_ms: int = 0,
        method: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.details = details
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.method = method

    @classmethod
    def from_gateway_payload(cls, payload: object, method: str = "") -> "GatewayError":
        """Parse the error shape from a gateway res frame."""
        if isinstance(payload, dict):
            return cls(
                message=payload.get("message", str(payload)),
                code=payload.get("code", "UNKNOWN"),
                details=payload.get("details"),
                retryable=payload.get("retryable", False),
                retry_after_ms=payload.get("retryAfterMs", 0),
                method=method,
            )
        return cls(message=str(payload), method=method)

    def to_browser_json(self) -> dict:
        """Convert to the JSON shape broadcast to browsers."""
        d = {
            "type": "error",
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.details:
            d["details"] = self.details
        if self.retry_after_ms:
            d["retry_after_ms"] = self.retry_after_ms
        if self.method:
            d["source"] = self.method
        return d

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 30 * 60
IDLE_CHECK_INTERVAL_SECONDS = 60

_CLIENT_INFO = {
    "id": "gateway-client",
    "version": "1.0.0",
    "platform": "linux",
    "mode": "backend",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

# Gateway frames can exceed the websockets default 1 MiB max_size — large
# diff-review cards, big agent responses, and workspace file/RPC results all
# arrive as a single frame. Without a higher cap the client raises
# PayloadTooBig and the whole connection dies, dropping every event after it
# (e.g. the diff-review approval card never reaches the user). 32 MiB headroom.
_WS_MAX_SIZE = 32 * 1024 * 1024


def _ws_url() -> str:
    return get_settings().openclaw_ws_url


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_settings().openclaw_auth_token}"}


async def gateway_health() -> dict:
    """Poll OpenClaw /health endpoint."""
    http_base = _ws_url().replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{http_base}/health", headers=_auth_headers())
            return resp.json()
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)}


async def gateway_readiness() -> dict:
    """Poll OpenClaw /readyz endpoint. Returns uptimeMs when available."""
    http_base = _ws_url().replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{http_base}/readyz", headers=_auth_headers())
            return resp.json()
    except Exception as exc:
        logger.debug("gateway_readiness failed: %s", exc)
        return {}


async def push_task_reply(
    agent_id: str,
    session_key: str,
    content: str,
) -> bool:
    """Inject a message into an agent's chat session via the gateway.

    Recorded as a user (inbound) message — this is a human's answer relayed
    to the agent, not the agent's own output.
    """
    try:
        await gateway_admin.call("chat.inject", {
            "sessionKey": session_key,
            "message": content,
            "role": "user",
        })
        return True
    except Exception as exc:
        logger.warning("push_task_reply failed for %s: %s", agent_id, exc)
        return False


# ── Gateway admin client ─────────────────────────────────────────────────────

class GatewayAdmin:
    """
    Shared admin WebSocket connection for management operations.

    Unlike _AgentSession (one WS per agent for chat streaming), this is a
    single connection used for CRUD: agents.create, agents.files.set,
    skills.install, models.list, config.patch, etc.

    Concurrent callers share the connection; each call gets a unique req_id
    and an asyncio.Future that resolves when the gateway sends the matching
    response frame.
    """

    def __init__(self):
        self._ws = None
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._pending_methods: dict[str, str] = {}  # req_id -> method name (for error context)
        self._reader_task: asyncio.Task | None = None

    # ── Public API ────────────────────────────────────────────────────────

    async def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        """Send a gateway request and await the response payload.

        Raises GatewayError with structured code/details on rejection.
        Raises asyncio.TimeoutError if the gateway doesn't respond in time.
        """
        await self._ensure_connected()
        req_id = str(uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[req_id] = future
        self._pending_methods[req_id] = method

        await self._ws.send(json.dumps({
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        }))

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Gateway call %s timed out after %ds (req=%s)", method, timeout, req_id)
            raise GatewayError(
                f"Gateway call {method} timed out after {timeout}s",
                code="AGENT_TIMEOUT",
                retryable=True,
                retry_after_ms=2000,
                method=method,
            )
        finally:
            self._pending.pop(req_id, None)
            self._pending_methods.pop(req_id, None)

    async def shutdown(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws:
            await self._ws.close()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._pending_methods.clear()

    # ── Connection management ─────────────────────────────────────────────

    async def _ensure_connected(self):
        if self._ws is not None and not self._ws.closed:
            return
        async with self._lock:
            if self._ws is not None and not self._ws.closed:
                return
            await self._do_connect()

    async def _do_connect(self):
        ws = await websockets.connect(_ws_url(), max_size=_WS_MAX_SIZE)

        # Step 1: connect.challenge
        challenge = json.loads(await ws.recv())
        if not (challenge.get("type") == "event" and challenge.get("event") == "connect.challenge"):
            await ws.close()
            raise Exception(f"Expected connect.challenge, got: {json.dumps(challenge)[:200]}")

        # Step 2: connect handshake
        await ws.send(json.dumps({
            "type": "req",
            "id": "admin-connect",
            "method": "connect",
            "params": {
                "minProtocol": 1,
                "maxProtocol": 10,
                "client": _CLIENT_INFO,
                "auth": {"token": get_settings().openclaw_auth_token},
                "scopes": ["operator.admin"],
            },
        }))

        # Step 3: hello-ok
        hello = json.loads(await ws.recv())
        if hello.get("type") == "res" and hello.get("ok"):
            hello = hello.get("payload", {})
        if hello.get("type") != "hello-ok":
            await ws.close()
            raise Exception(f"Expected hello-ok, got: {json.dumps(hello)[:200]}")

        conn_id = hello.get("server", {}).get("connId", "?")
        logger.info("GatewayAdmin connected (connId=%s)", conn_id)

        self._ws = ws
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self):
        """Read frames and resolve pending futures.

        Preserves the full structured error from gateway responses
        as GatewayError so callers can inspect code/details/retryable.
        """
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.warning("GatewayAdmin: malformed frame (not JSON): %s", raw[:200])
                    continue

                if msg.get("type") == "res":
                    req_id = msg.get("id")
                    method = self._pending_methods.get(req_id, "")
                    future = self._pending.get(req_id)
                    if future and not future.done():
                        if msg.get("ok"):
                            future.set_result(msg.get("payload", {}))
                        else:
                            err_payload = msg.get("error", msg.get("payload", "Unknown gateway error"))
                            gw_err = GatewayError.from_gateway_payload(err_payload, method=method)
                            # INVALID_REQUEST (e.g. unknown method) means the gateway
                            # doesn't know about this RPC — typically an optional plugin
                            # that isn't enabled yet. Log at DEBUG so it stays invisible
                            # in production logs. All other rejection codes stay at ERROR.
                            if gw_err.code == "INVALID_REQUEST":
                                logger.debug(
                                    "Gateway rejected %s (req=%s): [%s] %s",
                                    method, req_id, gw_err.code, gw_err,
                                )
                            else:
                                logger.error(
                                    "Gateway rejected %s (req=%s): [%s] %s",
                                    method, req_id, gw_err.code, gw_err,
                                )
                            future.set_exception(gw_err)
                    self._pending_methods.pop(req_id, None)
        except ConnectionClosed as exc:
            logger.warning("GatewayAdmin WS closed (code=%s reason=%s) — will reconnect on next call",
                           exc.code if hasattr(exc, 'code') else '?',
                           exc.reason if hasattr(exc, 'reason') else '?')
        except asyncio.CancelledError:
            return
        finally:
            self._ws = None
            # Fail any pending futures with connection-loss error
            for req_id, fut in self._pending.items():
                if not fut.done():
                    method = self._pending_methods.get(req_id, "")
                    fut.set_exception(GatewayError(
                        "Gateway connection lost",
                        code="UNAVAILABLE",
                        retryable=True,
                        retry_after_ms=2000,
                        method=method,
                    ))
            self._pending.clear()
            self._pending_methods.clear()

    # ── Convenience methods ───────────────────────────────────────────────

    async def list_agents(self) -> dict:
        return await self.call("agents.list")

    async def create_agent(self, name: str, workspace: str, emoji: str = "") -> dict:
        params = {"name": name, "workspace": workspace}
        if emoji:
            params["emoji"] = emoji
        return await self.call("agents.create", params)

    async def update_agent(self, agent_id: str, **fields) -> dict:
        params = {"agentId": agent_id}
        for key in ("name", "workspace", "model", "avatar"):
            if key in fields and fields[key] is not None:
                params[key] = fields[key]
        return await self.call("agents.update", params)

    async def delete_agent(self, agent_id: str, delete_files: bool = True) -> dict:
        return await self.call("agents.delete", {"agentId": agent_id, "deleteFiles": delete_files})

    async def archive_and_delete_agent(
        self,
        agent_id: str,
        goal_id: str,
        bucket: str | None = None,
        delete_files: bool = True,
        timeout: float = 180,
    ) -> dict:
        """Canonical agent-removal path: archive MEMORY.md, memory/, and
        sessions/ to GCS first, then delete the gateway agent. Raises
        GatewayError if archive or delete fails — caller can retry.

        Bucket precedence: explicit `bucket` arg → configured
        `agent_archive_bucket` (env AGENT_ARCHIVE_BUCKET). Every CP-side delete
        path (router, watchdog, slack-lifecycle goal-delete, task-controller
        IA reaper, SHCA idle reaper, decommission API) routes through here
        so every removed agent has its conversation history persisted.
        """
        from app.config import get_settings as _get_settings
        resolved_bucket = bucket or _get_settings().agent_archive_bucket
        return await self.call(
            "agents.archive-and-delete",
            {
                "agentId": agent_id,
                "goalId": goal_id,
                "bucket": resolved_bucket,
                "deleteFiles": delete_files,
            },
            timeout=timeout,
        )

    @staticmethod
    def extract_goal_id_from_agent_id(agent_id: str) -> str:
        """Best-effort goal extraction for orphan reconciliation. The
        orphan reconciler has no DB binding to look the goal up from, so
        fall back to parsing the gateway agent_id. Returns "orphans" if
        no recognised pattern matches.

        Patterns:
          go-<goal>                              → <goal>
          <name>-shca-<goal>-<uuid...>           → <goal>
          <name>-ia-<goal>-<...>                 → <goal>
        """
        if not agent_id:
            return "orphans"
        aid = agent_id.lower()
        if aid.startswith("go-"):
            tail = aid[3:]
            return tail.split("-")[0] or "orphans"
        for marker in ("-shca-", "-ia-"):
            if marker in aid:
                tail = aid.split(marker, 1)[1]
                seg = tail.split("-")[0]
                if seg:
                    return seg
        return "orphans"

    async def list_agent_files(self, agent_id: str) -> dict:
        return await self.call("agents.files.list", {"agentId": agent_id})

    async def get_agent_file(self, agent_id: str, name: str) -> dict:
        return await self.call("agents.files.get", {"agentId": agent_id, "name": name})

    async def set_agent_file(
        self, agent_id: str, name: str, content: str, encoding: str = "utf8",
    ) -> dict:
        """Write a file into the agent's workspace.

        encoding='utf8' (default): content is written as-is.
        encoding='base64': content is base64-decoded and written as raw
        bytes — use this to push binary files (PDF/DOCX/XLSX/…). Requires
        the gateway to support the encoding param (openclaw fork build
        containing the agents.files.set base64 patch).
        """
        params = {"agentId": agent_id, "name": name, "content": content}
        if encoding and encoding != "utf8":
            params["encoding"] = encoding
        return await self.call("agents.files.set", params)

    async def delete_agent_file(self, agent_id: str, name: str) -> dict:
        """Remove a file from the agent's workspace. Idempotent — if the
        file doesn't exist the gateway returns ok with existed=false."""
        return await self.call("agents.files.delete", {
            "agentId": agent_id, "name": name,
        })

    # Gateway enforces ^[a-z][a-z0-9_]{1,63}$ on skillName (agents.ts:1066).
    # Normalise here so callers using hyphenated names (e.g. "deep-research")
    # don't hit a silent INVALID_REQUEST rejection.
    _SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

    def _normalise_skill_name(self, skill_name: str) -> str:
        normalised = skill_name.lower().replace("-", "_")
        if not self._SKILL_NAME_RE.match(normalised):
            raise ValueError(
                f"skill_name {skill_name!r} cannot be normalised to a valid gateway slug "
                f"(must match ^[a-z][a-z0-9_]{{1,63}}$ after lowercasing and hyphen→underscore)"
            )
        return normalised

    async def set_agent_skill(
        self, agent_id: str, skill_name: str, content: str,
    ) -> dict:
        """Push a SKILL.md to <workspace>/skills/<skill_name>/ on the
        gateway. The agent's workspace skill scanner picks it up on the
        next run — no agent restart needed.

        skill_name is normalised (lowercase, hyphens→underscores) before
        being sent; the gateway enforces ^[a-z][a-z0-9_]{1,63}$.

        Requires the openclaw fork build that ships the agents.skills.set
        handler.
        """
        slug = self._normalise_skill_name(skill_name)
        return await self.call("agents.skills.set", {
            "agentId": agent_id, "skillName": slug, "content": content,
        })

    async def delete_agent_skill(self, agent_id: str, skill_name: str) -> dict:
        """Remove a skill from the agent's workspace. Idempotent — the
        gateway returns ok with existed=false if the directory was missing.
        skill_name is normalised the same way as set_agent_skill."""
        slug = self._normalise_skill_name(skill_name)
        return await self.call("agents.skills.delete", {
            "agentId": agent_id, "skillName": slug,
        })

    async def chat_inject(
        self, session_key: str, message: str, label: str = "",
        role: str = "user",
    ) -> dict:
        """Append a message to an agent's session transcript WITHOUT
        running inference. Used for system notifications that the agent
        should be aware of on its next turn but don't require an
        immediate response. Much cheaper than chat.send (no model tokens).

        role: "user" (default) records the message as an INBOUND message
        addressed to the agent — a system notification / relay it should
        know about but did not author. "assistant" records it as the
        agent's own prior turn. Backend notifications are inbound, so the
        default is "user"; pass "assistant" only to replay the agent's own
        output. Requires the gateway build that accepts the `role` param.

        Raises on failure — caller decides fallback (e.g. session not
        yet created on gateway).
        """
        params: dict = {"sessionKey": session_key, "message": message, "role": role}
        if label:
            params["label"] = label
        return await self.call("chat.inject", params)

    async def list_models(self) -> dict:
        return await self.call("models.list")

    async def get_tools_catalog(self, agent_id: str | None = None) -> dict:
        params = {}
        if agent_id:
            params["agentId"] = agent_id
        return await self.call("tools.catalog", params)

    async def get_skills_status(self, agent_id: str | None = None) -> dict:
        params = {}
        if agent_id:
            params["agentId"] = agent_id
        return await self.call("skills.status", params)

    async def install_skill(self, name: str) -> dict:
        return await self.call("skills.install", {
            "name": name,
            "installId": str(uuid4()),
        }, timeout=120)

    async def get_config(self) -> dict:
        return await self.call("config.get")

    async def tools_reload(self) -> dict:
        """Signal the custom-tools plugin to refresh its in-memory tool cache."""
        return await self.call("tools.reload")

    async def tools_catalog(self) -> dict:
        """Fetch the full tool catalog from OpenClaw (core + all plugin tools)."""
        return await self.call("tools.catalog", {"includePlugins": True})

    async def get_sessions(self, active_minutes: int = 60) -> dict:
        return await self.call("sessions.list", {"activeMinutes": active_minutes})

    async def set_agent_tools_allow(self, agent_id: str, tools_allow: list[str]) -> None:
        """Set per-agent tool allowlist via agents.update.

        OpenClaw behavior:
          - Empty allow list → all tools allowed (default)
          - Non-empty allow list → only listed tools allowed, everything else denied

        Uses agents.update with the tools field — writes to config without
        triggering a gateway restart (unlike config.patch).
        """
        tools_param = {"allow": tools_allow} if tools_allow else {"allow": []}
        try:
            await self.call("agents.update", {
                "agentId": agent_id,
                "tools": tools_param,
            })
            if tools_allow:
                logger.info("Set tool allowlist for agent %s: %s", agent_id, tools_allow)
            else:
                logger.info("Cleared tool allowlist for agent %s (all tools allowed)", agent_id)
        except Exception as exc:
            logger.warning("Failed to set tool allowlist for %s: %s", agent_id, exc)

    async def set_agent_tools(
        self, agent_id: str, allow: list[str], deny: list[str],
    ) -> None:
        """Set per-agent tool allow and deny lists in a single agents.update call.

        Cancellation (exact match) is performed upstream in resolver.py before this
        is called — tools present in both lists are already removed from both by the
        time they arrive here.

        OpenClaw semantics:
          - If a tool appears in both allow and deny, deny wins (gateway-side).
            Our resolver removes such conflicts before reaching this call, so the
            gateway never sees a conflict — the lists are already clean.
          - Empty allow list = all tools allowed (OpenClaw default).
          - deny list is only sent when non-empty.
        """
        tools_param: dict = {"allow": allow}
        if deny:
            tools_param["deny"] = deny
        try:
            await self.call("agents.update", {"agentId": agent_id, "tools": tools_param})
            logger.info(
                "Set tools for agent %s: allow=%d deny=%d",
                agent_id, len(allow), len(deny),
            )
        except Exception as exc:
            logger.warning("Failed to set tools for agent %s: %s", agent_id, exc)

    async def chat_abort(self, session_key: str, run_id: str | None = None) -> dict:
        """Abort the in-flight turn(s) on a session via the gateway's chat.abort
        RPC. With no run_id, aborts ALL runs on the session. The gateway returns
        any partial assistant text and broadcasts a chat event with
        state='aborted'. Used by the kill switch to force-stop a running
        IA / conversational turn (chat.send / agent runs registered in the
        gateway's chatAbortControllers). Does NOT reach claude-code-phase turns
        (those use the separate shca.claude.abort RPC)."""
        params: dict = {"sessionKey": session_key}
        if run_id:
            params["runId"] = run_id
        return await self.call("chat.abort", params)

    async def reset_session(self, session_key: str) -> dict:
        """Reset an OpenClaw session. Creates a new sessionId, archives old transcript."""
        return await self.call("sessions.reset", {"key": session_key, "reason": "reset"})

    async def delete_session(self, session_key: str, delete_transcript: bool = True) -> dict:
        """Delete an OpenClaw session and optionally its transcript."""
        return await self.call("sessions.delete", {
            "key": session_key,
            "deleteTranscript": delete_transcript,
        })


# Process-global admin singleton
gateway_admin = GatewayAdmin()


def _extract_chat_text(message: object) -> str:
    """
    Extract plain text from a gateway chat message object.
    Format: {role, content: [{type: "text", text: "..."}, ...], timestamp}

    Scans ALL content parts for type="text" entries (not just the first).
    Gemini may include a "thinking" block as content[0], with the actual
    text response in content[1] or later.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return ""
    text_parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text", "")
            # Strip directive tags like [[reply_to_current]]
            if text.startswith("[[") and "]]" in text:
                text = text[text.index("]]") + 2:]
            if text.strip():
                text_parts.append(text.strip())
    return "\n".join(text_parts)


# Internal control tokens an agent emits to mean "say nothing": a deliberate
# silent turn (NO_REPLY / bare NO — e.g. a memory-flush turn, or declining to respond
# to a system note) or a heartbeat ack (HEARTBEAT_OK). These must never surface as a
# chat message or trigger the lost-envelope fallback. "NO" is the model's common
# shorthand for the silent signal; only a turn whose ENTIRE text is "NO" (after
# stripping brackets/emphasis) is suppressed — a real answer ("No." / "No, ...") is
# never bare "NO" and still shows. The Notion relay filters the same set
# (channels/notion/outbound/_binding.py); this is the core chat-path filter.
_INTERNAL_SIGNALS = {"NO_REPLY", "NO", "HEARTBEAT_OK"}


def _is_internal_signal(text: str) -> bool:
    """True if `text` is purely an internal control token (optionally wrapped in
    brackets / markdown emphasis the model sometimes adds)."""
    t = (text or "").strip().strip("[]_*`> ").upper()
    return t in _INTERNAL_SIGNALS


# ── Session manager ───────────────────────────────────────────────────────────

class _AgentSession:
    """
    Owns one persistent authenticated WebSocket to the OpenClaw gateway
    for a single (pid, agent_id) pair.

    Run tracking:
      _runs[req_id]  = thread_id   (set on send, before ack)
      _runs[run_id]  = thread_id   (set on ack, req_id entry removed)
      _buffers[run_id] = [chunk…]  (accumulated delta tokens)

    Resource lifecycle:
      _resource_lifecycle manages credential provisioning and refresh.
      _resources_ready gates the first message until resources are provisioned.
    """

    def __init__(
        self,
        pid: str,
        agent_id: str,
        goal_context: dict,
        on_message: Callable,
        on_notify: Callable,
        on_idle: Callable,
        thread_id: str = "",
    ):
        self.pid = pid
        self.agent_id = agent_id
        # For SHCA sessions this is the user-conversation thread id (session
        # is keyed (pid, agent_id, thread_id) in SessionManager). For
        # IA / GO sessions it's currently "" — those agents route messages
        # through a goal-scoped session key and track thread via the message
        # envelope. Stored here so helpers like notify_resources_refreshed
        # can build the exact sessionKey the gateway expects for chat.inject.
        self.thread_id = thread_id
        self.goal_context = goal_context
        self._on_message = on_message
        self._on_notify = on_notify
        self._on_idle = on_idle

        self._subscribers: dict[str, set[Callable]] = {}
        self._gw_ws = None
        self._gw_ready = asyncio.Event()  # Set AFTER handshake completes
        self._resources_ready = asyncio.Event()  # Set AFTER resource provisioning completes
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_activity: float = time.monotonic()

        # Refresh gating — mid-session refreshes (GO grants, branch creation,
        # doc upload, token rotation, etc.) re-render RESOURCES.md on the
        # gateway. While that's in flight, block send_to_gateway so the
        # agent never reads a stale or absent RESOURCES.md. Counter supports
        # concurrent refreshes: only the outermost enter clears the event,
        # only the outermost exit re-sets it.
        self._refresh_in_flight = 0
        self._was_ready_before_refresh = False

        # Resource lifecycle manager (set during start)
        self._resource_lifecycle = None

        # req_id -> thread_id (pending ack) OR run_id -> thread_id (streaming)
        self._runs: dict[str, str] = {}
        # run_id -> latest cumulative text from delta events (replaced on each delta)
        self._buffers: dict[str, str] = {}

        # Compaction tracking — populated from `stream: "compaction"` agent_events
        # (both pi's sync auto_compaction and our async-compaction-manager).
        # While a thread_id is in this set, the chat_module dispatcher defers
        # dispatch for that thread until the matching `phase: "end"` event
        # arrives (which triggers a chat_dispatch NOTIFY to wake the queue).
        # Keyed by thread_id because pi sessions are per-(agent, thread).
        self._compacting_threads: set[str] = set()

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, thread_id: str, send_fn: Callable) -> Callable:
        self._touch()
        self._subscribers.setdefault(thread_id, set()).add(send_fn)

        def unsubscribe():
            self._subscribers.get(thread_id, set()).discard(send_fn)

        return unsubscribe

    def is_compacting(self, thread_id: str) -> bool:
        """Return True if the gateway is currently compacting this thread's
        pi session — observed via `stream: "compaction"` agent_events. Used
        by the chat_module dispatcher to defer dispatch until the matching
        compaction-end event arrives.
        """
        return thread_id in self._compacting_threads

    def _thread_id_from_session_key(self, session_key: str) -> str | None:
        """Extract thread_id from a gateway sessionKey of the form
        `agent:<gw_agent>:<pid_lower>-<thread_id>`. Returns None if the
        prefix doesn't match this session.
        """
        if not session_key:
            return None
        gw_agent = getattr(self, "_gateway_agent_id", None) or self.agent_id
        my_prefix = f"agent:{gw_agent}:{self.pid}-".lower()
        sk_lower = session_key.lower()
        if not sk_lower.startswith(my_prefix):
            return None
        return session_key[len(my_prefix):]

    def _is_run_state_tracked(self) -> bool:
        """Only SHCA and GO sessions get live run-state tracking.
        IAs have task activity timelines already.
        """
        if self.agent_id.startswith("go-"):
            return True
        if hasattr(self, "_gateway_agent_id") and self._gateway_agent_id:
            return True
        return False

    async def _set_run_state(self, thread_id: str, run_id: str,
                              status: str, current_tool: str = "") -> None:
        """Upsert the chat_run_state row for this thread."""
        if not self._is_run_state_tracked() or not thread_id:
            return
        try:
            from app.database import get_pool
            pool = get_pool()
            await pool.execute("""
                INSERT INTO chat_run_state (thread_id, run_id, status, current_tool, started_at, updated_at)
                VALUES ($1, $2, $3, $4, now(), now())
                ON CONFLICT (thread_id) DO UPDATE SET
                    run_id = $2, status = $3, current_tool = $4, updated_at = now()
            """, thread_id, run_id, status, current_tool)
        except Exception as exc:
            logger.debug("chat_run_state upsert failed for %s: %s", thread_id, exc)

    async def _clear_run_state(self, thread_id: str) -> None:
        """Delete the chat_run_state row — the run is done."""
        # chat_module: signal any co-located dispatcher waiting on this thread's
        # turn to complete. Done FIRST (before the run-state-tracked guard) so it
        # always fires on every turn-end path (final / error / recovery /
        # fallback / lifecycle:end), regardless of run-state tracking.
        waiters = getattr(self, "_turn_waiters", None)
        if waiters:
            fut = waiters.get(thread_id)
            if fut is not None and not fut.done():
                fut.set_result(True)
        if not self._is_run_state_tracked() or not thread_id:
            return
        try:
            from app.database import get_pool
            pool = get_pool()
            await pool.execute(
                "DELETE FROM chat_run_state WHERE thread_id = $1", thread_id,
            )
        except Exception as exc:
            logger.debug("chat_run_state clear failed for %s: %s", thread_id, exc)

    @asynccontextmanager
    async def refresh_guard(self):
        """Wrap a mid-session RESOURCES.md refresh so incoming sends wait.

        On enter: if this is the outermost refresh AND the session is
        currently marked ready, clear `_resources_ready` so any
        send_to_gateway() call starting now will block on the event.
        On exit: if this is the outermost refresh AND we cleared on
        enter, re-set the event so waiting sends resume.

        If `_resources_ready` was NOT set on enter (initial provisioning
        still running), we do nothing — initial provisioning's own
        finally block will set the event once it's done, which is the
        correct signal for sends anyway.

        The counter lets concurrent refreshes compose: only the outer
        refresh manipulates the event, inner refreshes just increment
        and decrement.
        """
        self._refresh_in_flight += 1
        if self._refresh_in_flight == 1:
            self._was_ready_before_refresh = self._resources_ready.is_set()
            if self._was_ready_before_refresh:
                self._resources_ready.clear()
        try:
            yield
        finally:
            self._refresh_in_flight -= 1
            if self._refresh_in_flight == 0 and self._was_ready_before_refresh:
                self._resources_ready.set()
                self._was_ready_before_refresh = False
                # Mid-session refresh just completed. Nudge the agent to
                # re-read RESOURCES.md on its next turn — otherwise its
                # cached bootstrap read keeps it answering from stale
                # content. Fires exactly once per outermost refresh, so
                # nested refreshes collapse into a single inject.
                try:
                    await self._notify_resources_refreshed()
                except Exception as exc:
                    logger.warning(
                        "notify_resources_refreshed failed for %s/%s: %s",
                        self.pid, self.agent_id, exc,
                    )

    async def _notify_resources_refreshed(self) -> None:
        """Inject a system message into the transcript telling the agent
        to re-read RESOURCES.md before its next response.

        chat.inject appends to the transcript WITHOUT triggering
        inference — the message sits there until the next real user turn,
        when the agent sees it in context and follows the instruction.

        DEDUPE: hash the current RESOURCES.md content; only inject when
        the hash differs from the last-injected hash. Otherwise the
        refresh loop (periodic GitHub-token refresh + other triggers)
        would re-inject the same notification 10+ times per session,
        bloating context with identical system messages the agent has
        already seen and read.

        Skip if we don't have a thread_id to build a sessionKey from
        (IA persistent workers currently land here with thread_id='' —
        they're task-driven and re-read at dispatch time anyway).
        """
        if not self.thread_id:
            return
        gw_agent = getattr(self, "_gateway_agent_id", None) or self.agent_id

        # Fetch the current RESOURCES.md to inline it (saves the agent a tool
        # call). Retry briefly — a transient fetch failure used to drop us onto
        # the generic fallback message AND skip the dedupe (see below), which is
        # why a fetch hiccup turned into a per-refresh stream of identical
        # "[System] RESOURCES.md has been updated" notes.
        import hashlib
        resources_content: Optional[str] = None
        for _attempt in range(2):
            try:
                result = await gateway_admin.get_agent_file(gw_agent, "RESOURCES.md")
                content = result.get("content") if isinstance(result, dict) else None
                if isinstance(content, str) and content.strip():
                    resources_content = content
                    break
            except Exception as exc:
                logger.debug(
                    "RESOURCES.md fetch failed for %s/%s (attempt %d): %s",
                    self.pid, self.agent_id, _attempt + 1, exc,
                )

        if resources_content:
            message = (
                "[System] Resources updated for this goal. Current resource set:\n\n"
                f"{resources_content}"
            )
        else:
            message = (
                "[System] RESOURCES.md has been updated (resource allocations "
                "changed). Read the current RESOURCES.md before your next "
                "response so your answer reflects the latest resource set."
            )

        # Dedupe on the MESSAGE we're about to send (not just the file content).
        # This collapses repeated identical notifications — including the generic
        # fallback, which previously bypassed the dedupe and re-injected on every
        # refresh-loop tick whenever the content fetch failed.
        current_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        if current_hash == getattr(self, "_last_resources_notified_hash", None):
            logger.debug(
                "RESOURCES.md notify unchanged since last (%s/%s) — skipping",
                self.pid, self.agent_id,
            )
            return

        # Route through the durable queue as a SYSTEM_INJECT so it serializes
        # with turns under the per-thread advisory lock and rides the next turn,
        # instead of a raw chat.inject that can land mid-turn and be clobbered by
        # an in-turn compaction. Falls back to chat.inject when the queue is off.
        delivered = False
        try:
            from app.chat_module.system_ingress import enqueue_system_inject
            agent_type = await self._resolve_chat_agent_type()
            msg_id = await enqueue_system_inject(
                thread_id=self.thread_id, goal_id=self.pid, agent_id=self.agent_id,
                gateway_agent_id=gw_agent, agent_type=agent_type, content=message,
            )
            delivered = msg_id is not None
        except Exception as exc:
            logger.warning(
                "RESOURCES.md notify enqueue failed (%s/%s): %s — falling back to inject",
                self.pid, self.agent_id, exc,
            )
        if not delivered:
            # Match send_to_gateway's session-key format exactly — pid is
            # lowercased at the boundary because the gateway canonicalises.
            session_key = f"agent:{gw_agent}:{self.pid.lower()}-{self.thread_id}"
            await gateway_admin.chat_inject(
                session_key=session_key, message=message, label="system",
            )

        self._last_resources_notified_hash = current_hash
        logger.info(
            "RESOURCES.md notify sent (%s): %s/%s thread=%s bytes=%d",
            "queued" if delivered else "injected",
            self.pid, self.agent_id, self.thread_id[:12] if self.thread_id else "-",
            len(resources_content) if resources_content else 0,
        )

    async def send_to_gateway(self, raw: str, *, persisted_message_id: str | None = None,
                              queue_kind: str = "user"):
        """
        Convert a browser message frame to OpenClaw protocol and send.
        Browser sends: { "type": "message", "content": "...", "thread_id": "..." }

        Uses chat.send (not agent) — produces streaming chat events (delta/final).
        Session key format: "agent:{agentId}:{pid}-{thread_id}" — one session per
        user thread, routed to the correct agent.

        Waits for BOTH gateway handshake AND resource provisioning before sending,
        so the agent always has its RESOURCES.md when it starts processing.
        """
        self._touch()
        logger.info("send_to_gateway: enter pid=%s agent=%s queue_kind=%s", self.pid, self.agent_id, queue_kind)

        # Kill switch — conversational freeze BACKSTOP. This is the single
        # chokepoint for all conversational ingress, so it's the one place
        # guaranteed to catch a human turn during a system/goal freeze even if a
        # per-adapter gate is missed or a new ingress path is added later. It
        # runs BEFORE the queue branch so frozen turns are neither enqueued nor
        # sent. queue_kind != 'user' (system_wake / lifecycle injects) is allowed
        # through — those are not human turns. FAILS OPEN: a bug here must never
        # wedge the whole platform; the per-adapter gates own the nice UX.
        if queue_kind == "user":
            try:
                from app.services import execution_control  # noqa: PLC0415
                block = await execution_control.conversation_block(self.pid)
                if block is not None:
                    try:
                        thread_id = (json.loads(raw) or {}).get("thread_id", "")
                    except Exception:
                        thread_id = ""
                    logger.info(
                        "send_to_gateway DROPPED (frozen scope=%s) %s/%s thread=%s reason=%s",
                        block.scope, self.pid, self.agent_id,
                        (thread_id or "")[:12], block.reason or "<none>",
                    )
                    if thread_id:
                        await self._broadcast(thread_id, json.dumps({
                            "type": "system_notice",
                            "code": "EXECUTION_PAUSED",
                            "scope": block.scope,
                            "content": (
                                f"⏸ Execution is paused ({block.scope}). "
                                f"Resume to continue."
                                + (f" Reason: {block.reason}" if block.reason else "")
                            ),
                        }))
                    return
            except Exception:
                logger.exception(
                    "send_to_gateway: kill-switch backstop errored — failing OPEN "
                    "(message will be sent) for %s/%s", self.pid, self.agent_id,
                )
        else:
            # system_wake (autonomous nudges: task-progress cards, GO→SHCA
            # kickoffs) are normally allowed through here BECAUSE the durable
            # queue's own freeze-defer (chat_module/dispatcher) holds them until
            # resume. When the queue is DISABLED there is no defer stage, so a
            # frozen goal would otherwise receive the nudge LIVE. Drop it in that
            # case. No user notice — this is not a human turn. Fails OPEN.
            try:
                from app.chat_module.config import get_chat_config as _gcc  # noqa: PLC0415
                if not _gcc().queue_enabled:
                    from app.services import execution_control  # noqa: PLC0415
                    block = await execution_control.conversation_block(self.pid)
                    if block is not None:
                        logger.info(
                            "send_to_gateway DROPPED system_wake (frozen scope=%s, "
                            "queue disabled) %s/%s", block.scope, self.pid, self.agent_id,
                        )
                        return
            except Exception:
                logger.exception(
                    "send_to_gateway: system_wake freeze check errored — failing "
                    "OPEN for %s/%s", self.pid, self.agent_id,
                )

        # chat_module cutover: when the durable queue is enabled this session
        # does NOT talk to the gateway directly — it enqueues for the dispatcher
        # service. Single chokepoint for ALL conversational ingress (browser,
        # go_events, orchestrator, slack, sentinel, shca). Flag OFF = legacy
        # direct-send path below (unchanged).
        try:
            from app.chat_module.config import get_chat_config
            if get_chat_config().queue_enabled:
                logger.info(
                    "send_to_gateway: enqueuing via chat_module pid=%s agent=%s queue_kind=%s",
                    self.pid, self.agent_id, queue_kind,
                )
                await self._enqueue_for_dispatch(raw, persisted_message_id, queue_kind)
                return
        except Exception:
            logger.warning(
                "chat_module enqueue failed (%s/%s) — falling back to direct send",
                self.pid, self.agent_id, exc_info=True,
            )

        # Wait for gateway handshake AND resource provisioning to complete
        # BEFORE sending. Applies to every agent type (SHCA, IA, GO) — cold
        # provisioning routinely takes 30–60s (template-file sync + MEMORY
        # + IDENTITY + GitHub install-token + docs + RESOURCES.md render)
        # and an early send causes the agent to boot, read its RESOURCES.md,
        # hit ENOENT, and proceed with no resource context.
        #
        # Both events fire exactly once per session lifetime:
        #   _gw_ready         — set after the gateway handshake
        #   _resources_ready  — set in _provision_resources' `finally` block,
        #                       so it fires on success AND on failure
        #                       (failure also broadcasts a GatewayError to
        #                       the UI via on_resource_error).
        # After the first message both events stay set, so subsequent sends
        # return immediately from the waits.
        #
        # The 5-minute safety timeout exists only to break a genuinely
        # stuck session (crash before the finally, gateway never auth'd).
        # On timeout we DROP the message rather than send to an
        # un-provisioned agent — the user sees no response and can retry;
        # by then the stuck session will have evicted or recovered.
        READINESS_TIMEOUT_S = 300
        logger.info(
            "send_to_gateway: waiting on readiness gates pid=%s agent=%s gw_ready=%s resources_ready=%s",
            self.pid, self.agent_id, self._gw_ready.is_set(), self._resources_ready.is_set(),
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(self._gw_ready.wait(), self._resources_ready.wait()),
                timeout=READINESS_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Workspace not ready after %ds for %s/%s — dropping message "
                "(gw_ready=%s resources_ready=%s)",
                READINESS_TIMEOUT_S, self.pid, self.agent_id,
                self._gw_ready.is_set(), self._resources_ready.is_set(),
            )
            return

        logger.info("send_to_gateway: readiness gates passed pid=%s agent=%s", self.pid, self.agent_id)
        if not self._gw_ws:
            logger.warning("Gateway WS not open for %s/%s — message dropped", self.pid, self.agent_id)
            return

        try:
            parsed = json.loads(raw)
        except Exception:
            logger.warning("send_to_gateway: failed to parse raw frame pid=%s agent=%s", self.pid, self.agent_id)
            return

        if parsed.get("type") != "message":
            logger.debug(
                "send_to_gateway: ignoring non-message frame type=%r pid=%s agent=%s",
                parsed.get("type"), self.pid, self.agent_id,
            )
            return

        thread_id = parsed.get("thread_id", "")
        content = parsed.get("content", "")
        req_id = str(uuid4())

        # Register req_id -> thread_id before sending (ack will upgrade to runId)
        self._runs[req_id] = thread_id

        # Session key encodes the agent: "agent:{agentId}:{pid}-{thread_id}"
        # For SHCA per-conversation instances, use the instance_id as the agent.
        # This gives each thread its own isolated conversation history on the gateway.
        #
        # pid is lowercased at this boundary because the gateway canonicalises
        # every incoming sessionKey to lowercase (parseAgentSessionKey /
        # normalizeAgentId in the fork) and echoes the same canonical form
        # back in chat/agent events. Sending lowercase keeps our outgoing
        # byte-for-byte identical to the echo.
        gw_agent = getattr(self, "_gateway_agent_id", None) or self.agent_id
        session_key = make_goal_session_key(gw_agent, self.pid, thread_id)

        logger.info(
            "send_to_gateway: dispatching chat.send pid=%s agent=%s thread=%s req_id=%s session_key=%s",
            self.pid, self.agent_id, thread_id[:12], req_id[:8], session_key,
        )
        await self._gw_ws.send(json.dumps({
            "type": "req",
            "id": req_id,
            "method": "chat.send",
            "params": {
                "sessionKey": session_key,
                "message": content,
                "idempotencyKey": str(uuid4()),
            },
        }))
        logger.debug(
            "send_to_gateway: frame transmitted pid=%s agent=%s thread=%s req_id=%s",
            self.pid, self.agent_id, thread_id[:12], req_id[:8],
        )

        # Record "agent is thinking" so reloads can restore the indicator.
        # Cleared on final/error/lifecycle:end. SHCA + GO only.
        await self._set_run_state(thread_id, req_id, "thinking", "")

    async def _enqueue_for_dispatch(self, raw: str, persisted_message_id, queue_kind) -> None:
        """Route a conversational message into the durable chat_module queue
        instead of the gateway. The dispatcher service runs the turn and
        persists/fans-out the reply. Captures routing (incl. the resolved
        gateway agent id) so the dispatcher never has to re-derive it."""
        try:
            parsed = json.loads(raw)
        except Exception:
            return
        if parsed.get("type") != "message":
            return
        thread_id = parsed.get("thread_id", "")
        content = parsed.get("content", "")
        from app.chat_module import repository as chat_repo
        from app.chat_module.models import MessageKind
        gw_agent = getattr(self, "_gateway_agent_id", None) or self.agent_id
        agent_type = await self._resolve_chat_agent_type()
        await chat_repo.upsert_thread_routing(
            thread_id=thread_id, goal_id=self.pid, agent_id=self.agent_id,
            gateway_agent_id=gw_agent, agent_type=agent_type, gateway_id=None,
        )
        try:
            kind = MessageKind(queue_kind)
        except ValueError:
            kind = MessageKind.USER
        if persisted_message_id:
            # The caller (chat router) already INSERTed the row — just flip it
            # to 'queued' + signal, so we don't double-persist.
            await chat_repo.mark_existing_queued(message_id=persisted_message_id, kind=kind)
        else:
            await chat_repo.enqueue(
                thread_id=thread_id, sender="system", role="human",
                kind=kind, content=content,
            )

    async def _resolve_chat_agent_type(self) -> str:
        cached = getattr(self, "_chat_agent_type", None)
        if cached:
            return cached
        agent_type = "SHCA"
        try:
            from app.database import get_pool
            row = await get_pool().fetchval(
                "SELECT type FROM agents WHERE id = $1", self.agent_id,
            )
            if row:
                agent_type = row
        except Exception:
            pass
        self._chat_agent_type = agent_type
        return agent_type

    def _ensure_turn_waiters(self) -> dict:
        w = getattr(self, "_turn_waiters", None)
        if w is None:
            w = {}
            self._turn_waiters = w
        return w

    async def run_turn_direct(
        self, content: str, thread_id: str, *, idempotency_key: str,
        timeout_s: float = 360.0,
    ) -> bool:
        """Co-located chat_module dispatcher entrypoint.

        Sends ONE turn to the gateway via the normal session path (so the
        gateway reply flows back through this session's event handler →
        on_message → process_go_response + ledger + Slack + notifications +
        summarization — i.e. ALL reply side-effects fire unchanged), then waits
        for the turn to complete so the dispatcher can serialize per thread.

        Returns True when the turn finished (final/error/lifecycle:end), False
        on readiness/timeout (caller leaves the batch for the reaper; the
        idempotency_key dedupes a re-send at the gateway)."""
        self._touch()
        try:
            await asyncio.wait_for(
                asyncio.gather(self._gw_ready.wait(), self._resources_ready.wait()),
                timeout=300,
            )
        except asyncio.TimeoutError:
            logger.error("run_turn_direct: workspace not ready %s/%s — dropping",
                         self.pid, self.agent_id)
            return False
        if not self._gw_ws:
            logger.warning("run_turn_direct: gateway WS not open %s/%s",
                           self.pid, self.agent_id)
            return False

        gw_agent = getattr(self, "_gateway_agent_id", None) or self.agent_id
        session_key = make_goal_session_key(gw_agent, self.pid, thread_id)
        req_id = str(uuid4())
        self._runs[req_id] = thread_id

        waiters = self._ensure_turn_waiters()
        fut = asyncio.get_event_loop().create_future()
        waiters[thread_id] = fut
        try:
            await self._gw_ws.send(json.dumps({
                "type": "req", "id": req_id, "method": "chat.send",
                "params": {"sessionKey": session_key, "message": content,
                           "idempotencyKey": idempotency_key},
            }))
            await self._set_run_state(thread_id, req_id, "thinking", "")
            await asyncio.wait_for(fut, timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            logger.warning("run_turn_direct: turn timed out %s/%s thread=%s",
                           self.pid, self.agent_id, thread_id)
            return False
        finally:
            if waiters.get(thread_id) is fut:
                waiters.pop(thread_id, None)

    @property
    def subscriber_count(self) -> int:
        return sum(len(s) for s in self._subscribers.values())

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_forever())
            # Surface exceptions from the background task instead of losing them
            self._task.add_done_callback(self._task_done_callback)

    @staticmethod
    def _task_done_callback(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("AgentSession background task died: %s", exc, exc_info=exc)

    async def stop(self):
        self._running = False
        self._gw_ready.clear()
        self._resources_ready.clear()
        # Stop the credential refresh loop
        if self._resource_lifecycle:
            await self._resource_lifecycle.stop()
        if self._gw_ws:
            await self._gw_ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Internal ──────────────────────────────────────────────────────────────

    def _touch(self):
        self._last_activity = time.monotonic()

    def _is_idle(self) -> bool:
        # GO sessions: never idle — must stay alive for orchestrator messages
        if self.agent_id.startswith("go-"):
            return False

        # SHCA sessions working on a story: never idle — need GO replies
        is_shca = hasattr(self, "_gateway_agent_id") and self._gateway_agent_id
        if is_shca and getattr(self, "_story_mode", False):
            return False

        # SHCA general mode: normal idle timeout
        if is_shca:
            return (time.monotonic() - self._last_activity) > IDLE_TIMEOUT_SECONDS

        # IA / legacy sessions: idle = no subscribers AND no activity
        if self.subscriber_count > 0:
            return False
        return (time.monotonic() - self._last_activity) > IDLE_TIMEOUT_SECONDS

    async def _run_forever(self):
        await asyncio.gather(
            self._connect_loop(),
            self._idle_watchdog(),
        )

    async def _connect_loop(self):
        logger.info("Connect loop started for %s/%s — gateway URL: %s",
                    self.pid, self.agent_id, _ws_url())
        backoff = 1
        while self._running:
            try:
                await self._connect()
                backoff = 1
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                # Zombie guard: GO sessions never idle-evict, so a session whose goal
                # was deleted would otherwise reconnect forever. Once we've been failing
                # a while (backoff maxed), verify the goal still exists; if it's gone,
                # self-terminate instead of hammering the gateway indefinitely.
                if backoff >= 60 and await self._goal_deleted():
                    logger.info("Session %s/%s: goal deleted — terminating zombie session",
                                self.pid, self.agent_id)
                    self._running = False
                    try:
                        self._on_idle()   # evict from SessionManager
                    except Exception:
                        pass
                    return
                logger.error("Gateway WS error (%s/%s): [%s] %s — retrying in %ds",
                             self.pid, self.agent_id, type(exc).__name__, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _goal_deleted(self) -> bool:
        """True if this session's goal is NOT active (status != 'active') or is missing.
        Only status='active' goals keep a live session. Fail-safe: on a DB error return
        False so a transient DB blip never reaps a live session."""
        try:
            from app.database import get_pool
            status = await get_pool().fetchval("SELECT status FROM goals WHERE id=$1", self.pid)
            return status != "active"
        except Exception:
            return False

    async def _idle_watchdog(self):
        while self._running:
            await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
            if self._is_idle():
                logger.info("Session idle timeout (%s/%s) — closing", self.pid, self.agent_id)
                await self.stop()
                self._on_idle()
                return

    async def _connect(self):
        gw_url = _ws_url()  # Single endpoint, no path

        async with websockets.connect(gw_url, max_size=_WS_MAX_SIZE) as ws:
            # DO NOT set self._gw_ws until handshake completes — otherwise
            # send_to_gateway() can fire on an unauthenticated connection
            # and the gateway silently drops the message (no ack ever comes
            # back, so _runs keeps a stale req_id that never maps to a runId).

            # Step 1: Wait for connect.challenge (gateway always sends this first)
            challenge_raw = await ws.recv()
            challenge = json.loads(challenge_raw)
            if not (challenge.get("type") == "event" and challenge.get("event") == "connect.challenge"):
                raise Exception(f"Expected connect.challenge, got: {challenge_raw[:200]}")

            # Step 2: Send connect handshake (nonce only needed for device auth, not token auth)
            await ws.send(json.dumps({
                "type": "req",
                "id": "connect-1",
                "method": "connect",
                "params": {
                    "minProtocol": 1,
                    "maxProtocol": 10,
                    "client": _CLIENT_INFO,
                    "auth": {"token": get_settings().openclaw_auth_token},
                    "scopes": ["operator.admin"],
                },
            }))

            # Step 3: Wait for hello-ok (wrapped in a res frame)
            hello_raw = await ws.recv()
            hello = json.loads(hello_raw)
            if hello.get("type") == "res" and hello.get("ok"):
                hello = hello.get("payload", {})
            if hello.get("type") != "hello-ok":
                raise Exception(f"Expected hello-ok, got: {hello_raw[:200]}")

            conn_id = hello.get("server", {}).get("connId", "?")
            logger.info("Gateway WS authenticated: %s/%s (connId=%s)", self.pid, self.agent_id, conn_id)

            # NOW the connection is fully authenticated — allow sends
            self._gw_ws = ws
            self._gw_ready.set()

            # Step 4: Receive frames
            async for raw in ws:
                await self._dispatch(raw)

        # Connection closed — block new sends until reconnect.
        # Notify browsers about stale in-flight runs so they stop spinning.
        self._gw_ws = None
        self._gw_ready.clear()
        if self._runs:
            stale_threads = set(self._runs.values())
            logger.warning(
                "Gateway WS dropped with %d in-flight runs across threads: %s",
                len(self._runs), stale_threads,
            )
            for thread_id in stale_threads:
                await self._broadcast(thread_id, json.dumps({
                    "type": "error",
                    "code": "WS_RECONNECTING",
                    "message": "Connection to agent lost — reconnecting. Response may arrive via polling.",
                    "retryable": True,
                }))
            self._runs.clear()
            self._buffers.clear()

    async def _dispatch(self, raw: str):
        """Route gateway frames to browser subscribers and persist to DB.

        Strategy:
        - Forward ALL agent events to browser (passthrough for real-time display)
        - Accumulate assistant text in buffer (for DB persistence)
        - On chat:final — persist to PostgreSQL + ledger
        - If chat:final has no text — retry fetch from transcript
        """
        self._touch()

        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Debug: log incoming gateway frames for GO sessions (agent/chat events only)
        if self.agent_id.startswith("go-"):
            msg_type = msg.get("type", "?")
            event_name = msg.get("event", "")
            if event_name in ("agent", "chat"):
                payload = msg.get("payload", {})
                logger.debug("GO frame: type=%s event=%s stream=%s runId=%s",
                             msg_type, event_name, payload.get("stream", ""), payload.get("runId", ""))

        msg_type = msg.get("type")
        event_name = msg.get("event")

        # ── Ack: req_id -> runId mapping ──────────────────────────────────────
        if msg_type == "res":
            req_id = msg.get("id")
            thread_id = self._runs.pop(req_id, None)
            if thread_id is None:
                logger.debug(
                    "dispatch: res frame req_id=%s not a tracked chat.send — ignoring pid=%s agent=%s",
                    (req_id or "?")[:8], self.pid, self.agent_id,
                )
                return

            if not msg.get("ok"):
                err_payload = msg.get("error", msg.get("payload", "Gateway rejected request"))
                gw_err = GatewayError.from_gateway_payload(err_payload, method="chat.send")
                logger.error("GW rejected (req=%s): [%s] %s", req_id, gw_err.code, gw_err)
                await self._broadcast(thread_id, json.dumps(gw_err.to_browser_json()))
                return

            run_id = msg.get("payload", {}).get("runId")
            if run_id:
                self._runs[run_id] = thread_id
                self._buffers[run_id] = ""
                logger.info(
                    "dispatch: ACK ok req_id=%s run_id=%s thread=%s pid=%s agent=%s",
                    req_id[:8], run_id[:12], thread_id[:12], self.pid, self.agent_id,
                )
            else:
                logger.warning(
                    "dispatch: ACK ok but no runId in payload — req_id=%s thread=%s pid=%s agent=%s",
                    req_id[:8], thread_id[:12], self.pid, self.agent_id,
                )

        # ── Agent events → forward to browser + accumulate ────────────────────
        elif msg_type == "event" and event_name == "agent":
            payload = msg.get("payload", {})
            run_id = payload.get("runId")
            stream = payload.get("stream")
            data = payload.get("data", {})

            # Filter events from other sessions
            session_key = payload.get("sessionKey", "")
            # Use instance ID if available (SHCA per-conversation), else template ID
            gw_agent = getattr(self, "_gateway_agent_id", None) or self.agent_id
            # Case-insensitive prefix check: gateway may normalise parts
            # of the session key (e.g. lowercase the pid/workspace), so the
            # echoed sessionKey on broadcast events doesn't always match
            # our outgoing one byte-for-byte. Without this, uppercase goal
            # IDs (AUTH, BMIWP, …) make every event fail the filter and
            # the UI sits on "Agent is thinking…" forever.
            my_prefix = f"agent:{gw_agent}:{self.pid}-".lower()
            if session_key and not session_key.lower().startswith(my_prefix):
                return

            # ── Compaction events: handled BEFORE the run_id→thread_id lookup
            # because async-compaction-manager emits with a synthetic run_id
            # (cmpct-async-<sessionKey>-<startedAt>) that's never registered
            # in self._runs. The triggering turn's real run_id is already
            # cleared from _runs by the time the splice completes, so a
            # _runs.get() would fail for sync compaction's end events too.
            # Compaction routes by sessionKey, not run_id.
            if stream == "compaction":
                comp_thread_id = self._thread_id_from_session_key(session_key)
                if not comp_thread_id:
                    return
                phase = data.get("phase", "")
                if phase == "start":
                    self._compacting_threads.add(comp_thread_id)
                    logger.info(
                        "compaction START: agent=%s thread=%s source=%s — chat_module will defer dispatch",
                        self.agent_id, comp_thread_id, data.get("source", "sync"),
                    )
                elif phase == "end":
                    will_retry = bool(data.get("willRetry", False))
                    if will_retry:
                        logger.debug(
                            "compaction END (willRetry=true): agent=%s thread=%s — still compacting, hold dispatch",
                            self.agent_id, comp_thread_id,
                        )
                        return
                    self._compacting_threads.discard(comp_thread_id)
                    logger.info(
                        "compaction END: agent=%s thread=%s ok=%s reason=%s — releasing dispatch hold",
                        self.agent_id, comp_thread_id,
                        data.get("ok", True), data.get("reason", ""),
                    )
                    try:
                        from app.chat_module.repository import notify_dispatch
                        await notify_dispatch(comp_thread_id)
                    except Exception:
                        logger.debug(
                            "chat_module: notify_dispatch on compaction-end failed for thread=%s "
                            "— poll loop will catch up", comp_thread_id,
                            exc_info=True,
                        )
                return

            thread_id = self._runs.get(run_id)
            if not thread_id:
                logger.debug(
                    "dispatch: agent event run_id=%s not in _runs — dropped pid=%s agent=%s",
                    (run_id or "?")[:12], self.pid, self.agent_id,
                )
                return

            # Forward ALL agent events to browser as-is
            await self._broadcast(thread_id, json.dumps({
                "type": "agent_event",
                "stream": stream,
                "data": data,
            }))

            # Also handle specific streams for backward compat + persistence
            if stream == "assistant":
                text = data.get("text", "")
                if text:
                    self._buffers[run_id] = text
                    # Also send as token for existing UI rendering
                    await self._broadcast(thread_id, json.dumps({
                        "type": "token",
                        "token": text,
                    }))

            elif stream == "tool":
                phase = data.get("phase", "")
                tool_name = data.get("name", "")
                if phase == "start":
                    await self._broadcast(thread_id, json.dumps({
                        "type": "tool_start",
                        "name": tool_name,
                    }))
                    await self._set_run_state(thread_id, run_id, "using_tool", tool_name)
                elif phase == "result":
                    await self._broadcast(thread_id, json.dumps({
                        "type": "tool_result",
                        "name": tool_name,
                        "is_error": data.get("isError", False),
                    }))
                    await self._set_run_state(thread_id, run_id, "thinking", "")

            elif stream == "lifecycle":
                phase = data.get("phase", "")
                if phase == "end":
                    full_content = self._buffers.pop(run_id, "")
                    if full_content and _is_internal_signal(full_content):
                        # Silent turn (NO_REPLY / HEARTBEAT_OK) — suppress.
                        logger.info(
                            "lifecycle:end silent signal %r for run %s — suppressed",
                            full_content[:24], run_id[:12],
                        )
                        self._runs.pop(run_id, None)
                        await self._clear_run_state(thread_id)
                        return
                    if full_content:
                        # Persist first so we have the canonical message id
                        # to ship in the broadcast — lets the browser tag
                        # the streamed bubble and keep lastMsgId honest.
                        msg_id = await self._on_message(thread_id, "agent", full_content)
                        await self._broadcast(thread_id, json.dumps({
                            "type": "token_stream_end",
                            "full_content": full_content,
                            "message_id": msg_id,
                        }))
                        await self._on_notify(thread_id, self.agent_id, full_content)
                        self._runs.pop(run_id, None)
                        await self._clear_run_state(thread_id)
                        logger.info(
                            "dispatch: lifecycle:end complete pid=%s agent=%s thread=%s run_id=%s chars=%d msg_id=%s",
                            self.pid, self.agent_id, thread_id[:12] if thread_id else "?",
                            (run_id or "?")[:12], len(full_content), msg_id,
                        )
                    else:
                        logger.debug(
                            "dispatch: lifecycle:end empty buffer pid=%s agent=%s thread=%s run_id=%s — deferring to chat:final",
                            self.pid, self.agent_id, thread_id[:12] if thread_id else "?",
                            (run_id or "?")[:12],
                        )

                elif phase == "error":
                    error_msg = data.get("error", "Agent error")
                    logger.warning(
                        "Gateway lifecycle:error agent=%s goal=%s thread=%s: %s",
                        getattr(self, "_gateway_agent_id", None) or self.agent_id,
                        self.pid, thread_id[:12] if thread_id else "", error_msg,
                    )
                    await self._broadcast(thread_id, json.dumps({
                        "type": "error",
                        "code": "AGENT_RUN_ERROR",
                        "message": str(error_msg),
                    }))
                    self._runs.pop(run_id, None)
                    self._buffers.pop(run_id, None)
                    await self._clear_run_state(thread_id)

        # ── Chat events → extract final text, persist ─────────────────────────
        elif msg_type == "event" and event_name == "chat":
            payload = msg.get("payload", {})
            run_id = payload.get("runId")
            state = payload.get("state")

            session_key = payload.get("sessionKey", "")
            gw_agent = getattr(self, "_gateway_agent_id", None) or self.agent_id
            # Case-insensitive prefix check: gateway may normalise parts
            # of the session key (e.g. lowercase the pid/workspace), so the
            # echoed sessionKey on broadcast events doesn't always match
            # our outgoing one byte-for-byte. Without this, uppercase goal
            # IDs (AUTH, BMIWP, …) make every event fail the filter and
            # the UI sits on "Agent is thinking…" forever.
            my_prefix = f"agent:{gw_agent}:{self.pid}-".lower()
            if session_key and not session_key.lower().startswith(my_prefix):
                return

            thread_id = self._runs.get(run_id)
            if not thread_id:
                logger.debug(
                    "dispatch: chat event run_id=%s not in _runs — dropped pid=%s agent=%s",
                    (run_id or "?")[:12], self.pid, self.agent_id,
                )
                return

            if state == "delta":
                # Streaming token from the chat protocol path.
                # Extract cumulative text and broadcast to browser subscribers.
                delta_msg = payload.get("message")
                text = _extract_chat_text(delta_msg) if delta_msg else ""
                if text:
                    self._buffers[run_id] = text
                    await self._broadcast(thread_id, json.dumps({
                        "type": "token",
                        "token": text,
                    }))

            elif state == "final":
                final_msg = payload.get("message")
                full_content = _extract_chat_text(final_msg) if final_msg else ""

                # If the final envelope carried no text (common Gemini
                # tool-call pattern, where the gateway sends state:final with
                # message:undefined), fall back to the cumulative delta text we
                # already streamed this turn — the same buffer the lifecycle:end
                # path uses. Without this, a turn whose answer streamed fine but
                # whose final envelope is empty gets discarded, runs the 20s
                # transcript poll, and may surface the "envelope lost" placeholder
                # despite the full reply sitting in self._buffers[run_id].
                if not full_content:
                    buffered = (self._buffers.get(run_id) or "").strip()
                    if buffered:
                        logger.info(
                            "chat:final empty for run %s — recovered %d chars "
                            "from delta buffer", run_id[:12], len(buffered),
                        )
                        full_content = buffered

                # Intentional silent turn (NO_REPLY / HEARTBEAT_OK) — e.g. a
                # memory-flush turn, or the agent declining to respond to a
                # system note. Surface nothing: no message, no broadcast, no
                # lost-envelope fallback. Just end the turn cleanly. (Without
                # this the token leaks as a chat bubble — and the buffer
                # recovery above would surface it.)
                if full_content and _is_internal_signal(full_content):
                    logger.info(
                        "chat:final silent signal %r for run %s — suppressed",
                        full_content[:24], run_id[:12],
                    )
                    self._runs.pop(run_id, None)
                    self._buffers.pop(run_id, None)
                    await self._clear_run_state(thread_id)
                    return

                if full_content:
                    # Got the text — persist first so we can ship the
                    # canonical msg_id in the broadcast (see notes in the
                    # lifecycle:end path).
                    msg_id = await self._on_message(thread_id, "agent", full_content)
                    await self._broadcast(thread_id, json.dumps({
                        "type": "token_stream_end",
                        "full_content": full_content,
                        "message_id": msg_id,
                    }))
                    await self._on_notify(thread_id, self.agent_id, full_content)
                    # Slack relay happens inside the on_message callbacks
                    # (goal_thread.py for GO, chat.py for SHCA) so it fires on
                    # every code path that persists an agent message — primary
                    # chat:final, transcript recovery, and envelope-lost
                    # fallback.
                    self._runs.pop(run_id, None)
                    self._buffers.pop(run_id, None)
                    await self._clear_run_state(thread_id)
                    logger.info(
                        "dispatch: chat:final complete pid=%s agent=%s thread=%s run_id=%s chars=%d msg_id=%s",
                        self.pid, self.agent_id, thread_id[:12] if thread_id else "?",
                        (run_id or "?")[:12], len(full_content), msg_id,
                    )
                    # NOTE (2026-05-26): CP-side auto-flush dispatch is
                    # DISABLED. Investigation showed the openclaw-internal
                    # flush (runMemoryFlushIfNeeded in
                    # auto-reply/reply/agent-runner-memory.ts) already
                    # fires in-turn for gateway agents when totalTokens
                    # crosses flushThresholdPct — confirmed by
                    # memoryFlushLastFiredAtTokens being written on the
                    # session entry. Running the CP dispatcher too would
                    # double-flush (one in-turn + one separate turn).
                    # The openclaw side is the single authority: it owns
                    # the threshold check, the counter-based cycle gate
                    # (memoryFlushCount > compactionCount), and the
                    # per-agent-type prompts (pushed at provisioning).
                    # Leaving the maybe_dispatch_auto_flush helper in the
                    # codebase for now but not invoking it.
                    pass
                else:
                    # No text in chat:final — retry from transcript in background
                    logger.warning(
                        "dispatch: chat:final empty — scheduling transcript retry pid=%s agent=%s thread=%s run_id=%s",
                        self.pid, self.agent_id, thread_id[:12] if thread_id else "?",
                        (run_id or "?")[:12],
                    )
                    asyncio.create_task(self._retry_fetch_response(
                        run_id, thread_id, session_key
                    ))

            elif state in ("aborted", "error"):
                error_msg = payload.get("errorMessage", f"Agent {state}")
                logger.warning(
                    "Gateway chat:%s agent=%s goal=%s thread=%s session_key=%s: %s",
                    state,
                    getattr(self, "_gateway_agent_id", None) or self.agent_id,
                    self.pid, thread_id[:12] if thread_id else "", session_key, error_msg,
                )
                await self._broadcast(thread_id, json.dumps({
                    "type": "error",
                    "code": "AGENT_RUN_ERROR",
                    "message": error_msg,
                }))
                self._runs.pop(run_id, None)
                self._buffers.pop(run_id, None)
                await self._clear_run_state(thread_id)

    async def _retry_fetch_response(
        self, run_id: str, thread_id: str, session_key: str,
    ):
        """Retry fetching agent response from session transcript.

        Called when chat:final has no content (Gemini tool-call pattern).
        Retries sessions.preview with delays to handle transcript write lag.
        """
        for attempt in range(5):
            await asyncio.sleep(2 + attempt)  # 2s, 3s, 4s, 5s, 6s
            try:
                preview = await gateway_admin.call("sessions.preview", {
                    "keys": [session_key],
                    "limit": 20,      # widen — tool-heavy responses have many messages
                    "maxChars": 32000,
                })
                previews = preview.get("previews", [])
                if not previews:
                    continue

                messages = previews[0].get("messages", [])
                if not messages:
                    continue

                # Scope to the CURRENT turn: only consider assistant messages
                # that appear AFTER the last user message in the transcript.
                # Without this scope, a tool/action-only turn (no text reply)
                # would surface the PRIOR turn's assistant text as if it were
                # this turn's reply (stale-bubble bug).
                last_user_idx = -1
                for i, m in enumerate(messages):
                    if isinstance(m, dict) and m.get("role") == "user":
                        last_user_idx = i
                candidate_msgs = messages[last_user_idx + 1:] if last_user_idx >= 0 else messages

                # Find last assistant message with text content (within scope)
                for msg in reversed(candidate_msgs):
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    content = msg.get("content", "")
                    full_content = ""
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = part.get("text", "")
                                if text.startswith("[[") and "]]" in text:
                                    text = text[text.index("]]") + 2:]
                                if text.strip():
                                    full_content = text.strip()
                                    break
                    elif isinstance(content, str) and content.strip():
                        full_content = content.strip()

                    if full_content and _is_internal_signal(full_content):
                        # Recovered an intentional silent token from the
                        # transcript — suppress, don't surface or fall back.
                        logger.info(
                            "transcript recovery: silent signal %r for run %s — suppressed",
                            full_content[:24], run_id[:12],
                        )
                        self._runs.pop(run_id, None)
                        await self._clear_run_state(thread_id)
                        return

                    if full_content:
                        logger.info("Recovered %d chars from transcript (attempt %d)", len(full_content), attempt + 1)
                        msg_id = await self._on_message(thread_id, "agent", full_content)
                        await self._broadcast(thread_id, json.dumps({
                            "type": "token_stream_end",
                            "full_content": full_content,
                            "message_id": msg_id,
                        }))
                        await self._on_notify(thread_id, self.agent_id, full_content)
                        self._runs.pop(run_id, None)
                        # Clear run-state (and resolve any co-located dispatcher
                        # waiter) — the turn is genuinely done on this path too.
                        await self._clear_run_state(thread_id)
                        return
            except Exception as exc:
                logger.debug("Transcript fetch attempt %d failed: %s", attempt + 1, exc)

        logger.warning(
            "Failed to recover response for run %s after 5 attempts — "
            "broadcasting fallback message. The tool call may still have "
            "succeeded (check the task/story board).",
            run_id[:12],
        )
        # Surface something to the user so they don't see a perpetual spinner.
        # Any side effects (tasks created, resources linked) are already
        # persisted — this is just the final text envelope that got lost.
        fallback = (
            "_I finished that step but the response envelope was lost in "
            "transit. If I was creating a task or story, check the board — "
            "it likely succeeded. Please resend your last message if you'd "
            "like me to continue._"
        )
        try:
            msg_id = await self._on_message(thread_id, "agent", fallback)
            await self._broadcast(thread_id, json.dumps({
                "type": "token_stream_end",
                "full_content": fallback,
                "message_id": msg_id,
            }))
        except Exception as exc:
            logger.warning("Fallback broadcast failed: %s", exc)
        self._runs.pop(run_id, None)
        self._buffers.pop(run_id, None)
        await self._clear_run_state(thread_id)

    async def _broadcast(self, thread_id: str, raw: str):
        subs = self._subscribers.get(thread_id, set())
        dead = set()
        for send_fn in subs:
            try:
                await send_fn(raw)
            except Exception as exc:
                logger.debug("Broadcast to subscriber failed (thread=%s): %s — removing", thread_id, exc)
                dead.add(send_fn)
        if dead:
            logger.info("Removed %d dead subscriber(s) from thread %s", len(dead), thread_id)
            subs -= dead

    def evict_thread(self, thread_id: str):
        """Remove all state for a single thread without killing the session.

        Clears subscribers and in-flight runs for this thread. The shared
        gateway WS stays alive for other threads.

        Returns True if the session now has zero subscribers (caller may
        want to evict the whole session).
        """
        self._subscribers.pop(thread_id, None)
        # Remove run_id entries pointing to this thread
        stale = [k for k, v in self._runs.items() if v == thread_id]
        for k in stale:
            self._runs.pop(k, None)
            self._buffers.pop(k, None)
        logger.info(
            "Evicted thread %s from session %s/%s (removed %d runs, %d threads remain)",
            thread_id, self.pid, self.agent_id, len(stale), len(self._subscribers),
        )
        return self.subscriber_count == 0

    async def _broadcast_error(self, error: GatewayError):
        """Broadcast an error to ALL subscribers across all threads.

        Used for session-level errors (provisioning failure, credential expiry)
        that aren't tied to a specific thread/run.
        """
        raw = json.dumps(error.to_browser_json())
        for thread_id in list(self._subscribers.keys()):
            await self._broadcast(thread_id, raw)


async def _build_identity_md(pool, goal_id: str, thread_id: str) -> str:
    """Build IDENTITY.md for an SHCA instance.

    If the thread has an imported story, returns full story context (refreshed
    on every session provision). Otherwise returns generic assistant identity.
    """
    # Phase 4 Stage 4: look up the active story directly via the
    # threads ← stories.active_thread_id relationship in the database.
    # The old "scan chat history for 'Story Mode Active' marker" path
    # is gone — it was load-bearing on a magic string in conversation
    # content, which is brittle and prevented IDENTITY.md from being
    # used as a clean source of truth. The pre-turn state snapshot
    # (story_state.insert_state_snapshot_message) is the live source;
    # this function provides the workspace-file backup.
    story = await pool.fetchrow(
        """SELECT * FROM stories
            WHERE active_thread_id = $1
              AND state IN ('accepted', 'in_progress', 'blocked')
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1""",
        thread_id,
    )

    if not story:
        return "# Identity\n\nYou are a personal assistant. Help the human with whatever they need.\n"

    # ── Story Mode: build live context ──
    story = dict(story)
    sid = story["id"]
    gid = story["goal_id"]

    goal = await pool.fetchrow("SELECT name, objective FROM goals WHERE id = $1", gid)

    # Task progress
    tasks = await pool.fetch("""
        SELECT id, title, state, result_summary FROM tasks
        WHERE story_id = $1 AND status = 'active'
        ORDER BY created_at
    """, sid)
    done_count = sum(1 for t in tasks if t["state"] in ("done", "approved"))

    # Sibling stories
    siblings = await pool.fetch("""
        SELECT id, title, state FROM stories
        WHERE goal_id = $1 AND id != $2 AND state != 'cancelled'
        ORDER BY created_at
    """, gid, sid)

    # Resources
    resources = await pool.fetch("""
        SELECT name, kind, uri, human_context FROM goal_resources
        WHERE goal_id = $1 AND status = 'active' ORDER BY kind, name
    """, gid)

    parts = ["# Story Mode Active\n"]
    parts.append(f"You are working on a story for the goal \"{goal['name'] if goal else gid}\".\n")

    parts.append("## Your Story")
    parts.append(f"**Title:** {story['title']}")
    parts.append(f"**ID:** `{sid}`")
    parts.append(f"**Priority:** {story.get('priority', 'medium')}")
    parts.append(f"**State:** {story['state']}\n")

    if story.get("description"):
        parts.append(f"**Description:**\n{story['description']}\n")
    if story.get("acceptance_criteria"):
        parts.append(f"**Acceptance Criteria:**\n{story['acceptance_criteria']}\n")
    if story.get("estimated_effort"):
        parts.append(f"**Estimated Effort:** {story['estimated_effort']}\n")

    # Progress summary
    parts.append("## Progress Summary")
    if story.get("progress_summary"):
        parts.append(story["progress_summary"])
    else:
        parts.append("No progress yet — this story just started.")
    parts.append("")

    # Tasks
    parts.append(f"## Tasks ({done_count}/{len(tasks)} complete)")
    if tasks:
        for t in tasks:
            result = f" — {t['result_summary'][:100]}" if t.get("result_summary") else ""
            parts.append(f"- **{t['title']}** [{t['state']}]{result}")
    else:
        parts.append("No tasks created yet.")
    parts.append("")

    # Goal context
    if goal and goal.get("objective"):
        parts.append(f"## Goal Context")
        parts.append(f"**Objective:** {goal['objective']}\n")

    # Sibling stories
    if siblings:
        parts.append("## Other Stories")
        for s in siblings:
            parts.append(f"- {s['title']} [{s['state']}]")
        parts.append("")

    # Resources
    if resources:
        parts.append("## Available Resources")
        for r in resources:
            ctx = f" — {r['human_context'][:80]}" if r.get("human_context") else ""
            parts.append(f"- **{r['name']}** [{r['kind']}]{ctx}")
        parts.append("")

    # Story resource deps (needs/produces)
    story_resources = await pool.fetch("""
        SELECT sr.relation, gr.name, gr.kind, gr.id
        FROM story_resources sr
        JOIN goal_resources gr ON gr.id = sr.resource_id
        WHERE sr.story_id = $1
        ORDER BY sr.relation, gr.name
    """, sid)
    if story_resources:
        needs = [r for r in story_resources if r["relation"] == "needs"]
        produces = [r for r in story_resources if r["relation"] in ("produces", "modifies")]
        if needs:
            parts.append("## Resources This Story Needs")
            for r in needs:
                parts.append(f"- **{r['name']}** [{r['kind']}] (id: `{r['id']}`)")
            parts.append("")
        if produces:
            parts.append("## Resources This Story Produces/Modifies")
            for r in produces:
                parts.append(f"- **{r['name']}** [{r['kind']}] (id: `{r['id']}`)")
            parts.append("")

    # Working branch (if a branch resource is linked as "produces")
    branch_resource = await pool.fetchrow("""
        SELECT gr.branch_name, gr.repo_name, gr.repo_owner, gr.base_branch, gr.id
        FROM story_resources sr
        JOIN goal_resources gr ON gr.id = sr.resource_id
        WHERE sr.story_id = $1 AND gr.kind = 'branch' AND sr.relation = 'produces'
        LIMIT 1
    """, sid)
    if branch_resource:
        parts.append("## Your Working Branch")
        parts.append(f"**Branch:** {branch_resource['branch_name']}")
        parts.append(f"**Repository:** {branch_resource['repo_owner']}/{branch_resource['repo_name']}")
        parts.append(f"**Base branch:** {branch_resource['base_branch'] or 'main'}")
        parts.append(f"**Resource ID:** `{branch_resource['id']}`")
        parts.append("All work for this story should happen on this branch.")
        parts.append("Do NOT create new branches. Commit and push to this branch.")
        parts.append("")

    # Upstream handoff notes (from completed dependency stories)
    if story.get("depends_on"):
        upstream = await pool.fetch("""
            SELECT title, progress_summary FROM stories
            WHERE id = ANY($1) AND state = 'done' AND progress_summary != ''
        """, story["depends_on"])
        if upstream:
            parts.append("## Upstream Handoff")
            parts.append("Completed stories that this work builds on:")
            for u in upstream:
                parts.append(f"### From: {u['title']}")
                parts.append(u["progress_summary"])
            parts.append("")

    # Decision log
    decisions = await pool.fetch("""
        SELECT decision, rationale, created_at FROM goal_decisions
        WHERE goal_id = $1 ORDER BY created_at
    """, gid)
    if decisions:
        parts.append("## Key Decisions")
        for d in decisions:
            date = d["created_at"].strftime("%Y-%m-%d") if d.get("created_at") else ""
            rationale = f" ({d['rationale']})" if d.get("rationale") else ""
            parts.append(f"- {d['decision']}{rationale} [{date}]")
        parts.append("")

    # Instructions — division of labor
    parts.append("""## How To Work On This Story

### Your role
You are the human's advisor and coordinator. You help them:
- Understand what needs to be done
- Plan the approach
- Create tasks for coding agents to execute
- Guide the human on things they need to do manually (branch setup, installs, reviews)
- Track progress and report to the GO

### For coding work: CREATE TASKS
You do NOT write code directly. You create tasks via `create_task` with detailed descriptions.
The planner agent receives the task and delegates to coding agents who have proper workspace access.

Example: Instead of trying to "git clone" and "pip install" yourself, create a task:
  create_task(title="Set up backend project structure", description="Create backend/ directory with Flask app, requirements.txt, README.md...")

### For human-side work: GUIDE THE HUMAN
Some work requires the human to act (installing system packages, setting up SSH keys, creating PRs, etc.).
Tell the human clearly what they need to do and why.

Example: "You'll need to install python3.12-venv on the VM: `sudo apt install python3.12-venv`"

### For research/planning: DO IT YOURSELF
Use web_search, resource_read, etc. to research approaches, understand the codebase, and plan.
This is YOUR job — don't create tasks for research.

### Communication
- Use `message_go` to communicate with the Goal Orchestrator (questions, updates, requests)
- Use `story_update` to post progress updates
- Use `register_resource` to register new resources you create
- When done, send a completion report via `message_go(type='update')`""")

    return "\n".join(parts)


class SessionManager:
    """Process-global registry of active _AgentSession objects.

    Session lifecycle:
      1. get_or_create() — creates session + starts provisioning
      2. Agent session connects to gateway (handshake)
      3. Resource provisioning completes (RESOURCES.md written, refresh loop started)
      4. First message is gated until BOTH gateway and resources are ready
      5. On idle timeout — session + resource lifecycle torn down
      6. On next get_or_create() — fresh session + fresh provisioning
    """

    def __init__(self):
        self._sessions: dict[tuple, _AgentSession] = {}

    async def enqueue_silent_flush(
        self, *, goal_id: str, agent_id: str, agent_type: str, thread_id: str,
        user_message: str, system_prompt: str,
    ) -> bool:
        """Queue a memory-flush turn as a durable SYSTEM_WAKE instead of the
        direct admin RPC.

        This is the preferred path when the chat queue is enabled: the flush
        becomes a first-class queue turn — durable (reaper-retried, never
        dropped), serialized via the dispatcher's per-thread advisory lock (so
        it never overlaps a user turn on the same session), and — thanks to FCFS
        — its own isolated turn in arrival order. Its `[NO_REPLY]` reply is
        suppressed at the persistence boundary by the internal-signal guard.

        chat.send (the queue's dispatch method) can't carry `extraSystemPrompt`,
        so the flush system prompt is folded into the message body ahead of the
        user prompt. Returns True if enqueued; False if the queue is off or the
        gateway agent id can't be resolved (caller falls back to send_silent_flush).
        """
        if not thread_id:
            return False
        try:
            from app.chat_module.config import get_chat_config
            if not get_chat_config().queue_enabled:
                return False
        except Exception:
            return False

        gateway_agent_id = await self._resolve_gateway_agent_id_for_flush(
            agent_id=agent_id, thread_id=thread_id,
        )
        if not gateway_agent_id:
            logger.warning(
                "enqueue_silent_flush: cannot resolve gateway agent id for "
                "agent=%s goal=%s thread=%s — falling back to admin RPC",
                agent_id, goal_id, thread_id,
            )
            return False

        # Fold the system framing in front of the imperative user prompt, since
        # the queue path has no separate system slot.
        content = (
            f"{system_prompt.strip()}\n\n{user_message.strip()}"
            if system_prompt and system_prompt.strip()
            else user_message
        )
        try:
            from app.chat_module.system_ingress import enqueue_system_wake
            msg_id = await enqueue_system_wake(
                thread_id=thread_id, goal_id=goal_id, agent_id=agent_id,
                gateway_agent_id=gateway_agent_id, agent_type=agent_type,
                content=content,
            )
        except Exception:
            logger.exception(
                "enqueue_silent_flush: enqueue failed goal=%s agent=%s thread=%s",
                goal_id, agent_id, thread_id,
            )
            return False
        if msg_id is None:
            return False
        logger.info(
            "enqueue_silent_flush: queued SYSTEM_WAKE flush gateway_agent=%s thread=%s msg=%s",
            gateway_agent_id, thread_id[:12] if thread_id else "-", msg_id,
        )
        return True

    async def send_silent_flush(
        self, *, goal_id: str, agent_id: str, thread_id: str,
        user_message: str, system_prompt: str, idempotency_key: str,
    ) -> bool:
        """Dispatch a silent flush turn via gateway admin RPC.

        Routes through `gateway_admin.call("agent", ...)` — the always-on
        admin WebSocket the FastAPI process maintains. Does NOT need a
        per-(goal, agent, thread) _AgentSession to exist, and does NOT
        require any browser tab to be open. Mirrors the dispatch path
        `task_controller` uses for IA tasks (task_controller.py:2239).

        The agent's reply starts with [NO_REPLY] per the prompt convention
        and is filtered out of the UI. Tool side effects (writes to
        MEMORY.md + memory/<today>.md) are still applied by the gateway
        because the always-on memory-file append-only guard fires on
        path, not on trigger.

        Arguments:
          agent_id   — CP-side identifier (template id for SHCA like
                       'abhishek-shca'; gateway id for GO like 'go-sum01').
                       Resolved to the gateway instance id internally.
          thread_id  — the conversation thread (SHCA chat thread or GO
                       goal-thread).

        Returns True if the gateway accepted the run, False on any
        failure (resolution, admin RPC error, etc.).
        """
        # Resolve template id → gateway instance id for SHCA (and pass
        # through for GO since its instance id matches its template id).
        gateway_agent_id = await self._resolve_gateway_agent_id_for_flush(
            agent_id=agent_id, thread_id=thread_id,
        )
        if not gateway_agent_id:
            logger.warning(
                "send_silent_flush: cannot resolve gateway agent id for "
                "agent=%s goal=%s thread=%s — flush dropped",
                agent_id, goal_id, thread_id,
            )
            return False

        # Build the session key the same way the chat/task paths do so
        # the gateway routes this turn to the same pi session as
        # ordinary chat (continuing the conversation, not forking).
        session_key = (
            make_goal_session_key(gateway_agent_id, goal_id, thread_id)
            if thread_id
            else f"agent:{gateway_agent_id}:"
        )

        try:
            result = await gateway_admin.call(
                "agent",
                {
                    "agentId": gateway_agent_id,
                    "sessionKey": session_key,
                    "message": user_message,
                    **({"extraSystemPrompt": system_prompt} if system_prompt else {}),
                    "idempotencyKey": idempotency_key,
                },
                # 6 min ceiling — sized to cover the gateway's own
                # 5-min compaction-aggregate-timeout (attempt.ts:2121
                # COMPACTION_RETRY_AGGREGATE_TIMEOUT_MS). If the agent
                # is mid-compaction when this dispatch arrives,
                # pi-embedded-runner blocks on waitForCompactionRetry
                # up to that window; a shorter client timeout would
                # falsely time out while the gateway is still working.
                # Timeout is a ceiling, not a floor — happy-path
                # turns still complete in seconds.
                timeout=360,
            )
            run_id = (result or {}).get("runId", "") if isinstance(result, dict) else ""
            logger.info(
                "send_silent_flush: dispatched via admin RPC gateway_agent=%s "
                "thread=%s runId=%s",
                gateway_agent_id, thread_id,
                run_id[:12] if run_id else "?",
            )
            return True
        except Exception:
            logger.exception(
                "send_silent_flush: admin RPC failed for goal=%s gateway_agent=%s thread=%s",
                goal_id, gateway_agent_id, thread_id,
            )
            return False

    async def _resolve_gateway_agent_id_for_flush(
        self, *, agent_id: str, thread_id: str,
    ) -> Optional[str]:
        """Map a CP-side agent identifier to the gateway-side instance id.

        - SHCA: agent_id is the template id ('abhishek-shca'); we look
          up agent_instances by (template_id, thread_id, status='active')
          to find the gateway_agent_id ('abhishek-shca-SUM02-<uuid>').
        - GO: agent_id is already the gateway id ('go-sum01'); the lookup
          returns nothing, and we fall back to agent_id unchanged.

        Returns None only if a SHCA template was passed but no active
        instance exists for the given thread — flush should be skipped
        in that case.
        """
        from app.database import get_pool
        try:
            pool = get_pool()
            # The flush turn must land on the agent's LIVE session so it can
            # flush the accumulated context to MEMORY.md. The gateway persists
            # that session under the RAW instance id (session._gateway_agent_id
            # = threads.instance_id), NOT the gateway-normalized value stored in
            # agent_instances.gateway_agent_id. Using the normalized id here would
            # build a sessionKey the gateway never created — spawning a fresh,
            # empty session with nothing to flush. Prefer threads.instance_id;
            # fall back to the agent_instances lookup for GO / legacy rows.
            if thread_id:
                # Guard on the agent prefix so a shared/handed-off thread can
                # only ever return THIS agent's instance (instance_id is built
                # as f"{agent_id}-{goal_id}-{thread_id}", prefix == agent_id).
                inst = await pool.fetchval(
                    """SELECT instance_id FROM threads
                        WHERE id = $1 AND starts_with(instance_id, $2 || '-')""",
                    thread_id, agent_id,
                )
                if inst:
                    return inst
            row = await pool.fetchval(
                """SELECT gateway_agent_id
                     FROM agent_instances
                    WHERE template_id = $1
                      AND thread_id = $2
                      AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1""",
                agent_id, thread_id,
            )
            if row:
                return row
            # No row: GO case where template_id == gateway_id, OR an SHCA
            # whose instance was destroyed. Pass through; if the agent
            # doesn't exist on the gateway, the admin RPC will surface
            # the error and send_silent_flush will return False.
            return agent_id
        except Exception:
            logger.exception(
                "_resolve_gateway_agent_id_for_flush failed (agent=%s thread=%s)",
                agent_id, thread_id,
            )
            return agent_id

    async def broadcast_to_thread(
        self, pid: str, agent_id: str, thread_id: str, frame: dict,
    ) -> int:
        """Push a custom JSON frame to every WebSocket subscriber of this
        thread. Returns the number of subscribers reached.

        Used by the control panel to inject server-side events into the
        live chat stream without persisting them to the messages table.
        Frames are encoded as JSON text — the client unpacks them and
        renders based on `frame.type`.
        """
        import json as _json
        session = self.find(pid, agent_id, thread_id)
        if not session:
            logger.debug(
                "broadcast_to_thread: no session for %s/%s/%s (frame type=%s)",
                pid, agent_id, thread_id[:12] if thread_id else "-", frame.get("type"),
            )
            return 0
        subs = session._subscribers.get(thread_id, set())
        if not subs:
            logger.debug(
                "broadcast_to_thread: no subscribers for thread %s (session %s/%s, story_mode=%s)",
                thread_id[:12] if thread_id else "-", pid, agent_id,
                getattr(session, "_story_mode", False),
            )
            return 0
        raw = _json.dumps(frame)
        dead = set()
        for send_fn in list(subs):
            try:
                await send_fn(raw)
            except Exception:
                dead.add(send_fn)
        if dead:
            subs -= dead
        logger.debug(
            "broadcast_to_thread: delivered frame type=%s to %d/%d subscribers (thread %s)",
            frame.get("type"), len(subs) - len(dead), len(subs), thread_id[:12] if thread_id else "-",
        )
        return len(subs) - len(dead)

    def find(
        self, pid: str, agent_id: str, thread_id: str = "",
    ) -> Optional["_AgentSession"]:
        """Look up a live session by (pid, agent_id[, thread_id]).

        SHCA sessions are keyed (pid, agent_id, thread_id) — pass all
        three to find a per-conversation instance. IA / GO sessions are
        keyed (pid, agent_id) — pass thread_id="" (or omit) to find
        them. If a thread_id is given but no per-thread session exists,
        falls back to the (pid, agent_id) session; that covers refresh
        paths that know the thread but the target agent is goal-scoped.

        Returns None if no live session in memory. Callers should treat
        None as "no gating needed" — the agent is either idle or a
        persistent worker with no chat session object.
        """
        if thread_id:
            session = self._sessions.get((pid, agent_id, thread_id))
            if session:
                return session
        return self._sessions.get((pid, agent_id))

    def get_or_create(
        self,
        pid: str,
        agent_id: str,
        goal_context: dict,
        on_message: Callable,
        on_notify: Callable,
        thread_id: str = "",
    ) -> _AgentSession:
        # SHCA agents: one session per (goal, agent, thread) — per-conversation instances
        # IA agents: one session per (goal, agent) — shared across threads (legacy)
        agent_type = goal_context.get("agent_type", "")
        if agent_type == "SHCA" and thread_id:
            key = (pid, agent_id, thread_id)
        else:
            key = (pid, agent_id)

        if key not in self._sessions:
            def on_idle():
                self._sessions.pop(key, None)
                logger.info("Session evicted after idle timeout: %s", key)
                # SHCA gateway-side destruction on idle is DISABLED.
                # Previously fired _on_shca_idle which called
                # agents.delete on the gateway, blowing away the
                # workspace + accumulated V2 MEMORY.md. SHCAs now live
                # until their goal is deleted (see goals.py:delete_goal).
                # The CP-side session can still evict from memory to
                # free RAM — the gateway agent persists and a new
                # _AgentSession will reconnect to it on next chat open
                # (and _provision_shca_instance now skips file pushes
                # on resume — see "already exists" path).

            session = _AgentSession(
                pid, agent_id, goal_context, on_message, on_notify, on_idle,
                thread_id=thread_id,
            )
            session.start()
            self._sessions[key] = session
            logger.info("Created agent session: %s (type=%s)", key, agent_type)

            # Provision resources + memory — sets session._resources_ready when done.
            # This runs concurrently with the gateway handshake.
            # send_to_gateway() waits for BOTH before sending the first message.
            asyncio.create_task(self._provision_resources(session, agent_id, pid, thread_id))

        return self._sessions[key]

    async def _on_shca_idle(self, agent_id: str, goal_id: str, thread_id: str):
        """Called when an SHCA session is evicted due to idle timeout.
        Clears instance tracking but does NOT archive conversation memories.
        """
        try:
            from app.database import get_pool
            pool = get_pool()

            # Find and mark the instance as destroyed. Join threads to
            # pick up goal_id, which the archive step needs to scope the
            # GCS path (bucket/<goal_id>/<agent_id>/...).
            inst = await pool.fetchrow("""
                SELECT ai.id, ai.gateway_agent_id, t.goal_id
                  FROM agent_instances ai
                  JOIN threads t ON t.id = ai.thread_id
                 WHERE ai.template_id = $1 AND ai.thread_id = $2
                   AND ai.status = 'active'
            """, agent_id, thread_id)

            if inst:
                # Canonical archive-and-delete path. If archive fails,
                # leave the instance active so the next idle pass can
                # retry — never mark the DB row destroyed without a
                # confirmed archive.
                archive_ok = False
                try:
                    await gateway_admin.archive_and_delete_agent(
                        inst["gateway_agent_id"], inst["goal_id"],
                    )
                    archive_ok = True
                    logger.info(
                        "SHCA idle reaper: archived+destroyed %s (goal=%s)",
                        inst["gateway_agent_id"], inst["goal_id"],
                    )
                except Exception as exc:
                    logger.warning(
                        "SHCA idle reaper: archive-and-delete FAILED for %s (goal=%s) — "
                        "leaving instance active for next pass: %s",
                        inst["gateway_agent_id"], inst["goal_id"], exc,
                    )

                if not archive_ok:
                    return  # don't touch DB; next reap will try again

                await pool.execute("""
                    UPDATE agent_instances SET status = 'destroyed', destroyed_at = now()
                    WHERE id = $1
                """, inst["id"])

            # Clear thread's instance_id
            await pool.execute(
                "UPDATE threads SET instance_id = NULL WHERE id = $1", thread_id,
            )
            logger.info("SHCA idle cleanup: destroyed instance for thread %s", thread_id)
        except Exception as exc:
            logger.warning("SHCA idle cleanup failed for thread %s: %s", thread_id, exc)

    async def _provision_resources(
        self, session: _AgentSession, agent_id: str, goal_id: str, thread_id: str,
    ):
        """Provision resource credentials and memory for a new session.

        For SHCA agents (per-conversation instances):
          1. Creates a NEW gateway agent instance for this conversation
          2. Copies template files (SOUL.md, AGENTS.md)
          3. Generates MEMORY.md from PostgreSQL (global + goal + conversation)
          4. Provisions RESOURCES.md
          5. Registers in agent_instances table

        For IA agents (legacy):
          1. Ensures gateway agent exists (creates if missing)
          2. Provisions RESOURCES.md
          3. Generates thread memory (old MEMORY.md)
        """
        try:
            from app.resource_provisioning import provision_session_credentials
            from app.database import get_pool

            # Error callback: surface resource lifecycle errors to ALL browser subscribers
            async def on_resource_error(code: str, message: str, details: dict, retryable: bool):
                await session._broadcast_error(GatewayError(
                    message=message,
                    code=code,
                    details=details,
                    retryable=retryable,
                    method="resource_provisioning",
                ))

            pool = get_pool()
            agent_row = await pool.fetchrow(
                "SELECT gateway_agent_id, model, scope, type, name, owner_actor_id, "
                "tools_allow, tools_deny, template_id FROM agents WHERE id = $1",
                agent_id,
            )
            if not agent_row:
                logger.error("Agent %s not found in DB", agent_id)
                return

            is_shca = agent_row.get("type") == "SHCA"

            if is_shca and thread_id:
                # ── SHCA: per-conversation instance ──────────────────────
                gw_id = await self._provision_shca_instance(
                    pool, session, agent_id, agent_row, goal_id, thread_id,
                    on_resource_error,
                )
            else:
                # ── IA / legacy: shared gateway agent ────────────────────
                gw_id = agent_row["gateway_agent_id"] or ""
                if gw_id and agent_row.get("scope") == "goal":
                    await self._ensure_gateway_agent(pool, agent_id, agent_row, gw_id)

                # NOTE: thread_memory.write_thread_memory call removed.
                # It used to auto-generate MEMORY.md content ("# Thread Memory
                # / Auto-generated context") on every session start, which
                # conflicted with memory_v2 (which owns MEMORY.md as durable
                # long-term memory). The GO has plenty of other context
                # surfaces (AGENTS.md, IDENTITY.md, RESOURCES.md, MEMORY.md
                # via memory_v2) — thread_memory was redundant.

            # Provision resources (RESOURCES.md + credential refresh loop)
            if gw_id:
                # If this is an SHCA in story mode, scope RESOURCES.md to the
                # story's needs/produces relations. Look up the story whose
                # active_thread_id points at this thread.
                story_id: Optional[str] = None
                if is_shca and thread_id:
                    try:
                        story_id = await pool.fetchval(
                            "SELECT id FROM stories WHERE goal_id = $1 AND active_thread_id = $2",
                            goal_id, thread_id,
                        )
                        logger.info(
                            "_provision_resources: goal=%s thread=%s -> story_id=%s",
                            goal_id, thread_id[:12] if thread_id else "-",
                            story_id or "(none — general mode)",
                        )
                    except Exception as exc:
                        logger.warning("story_id lookup failed for thread %s: %s", thread_id, exc)

                lifecycle = await provision_session_credentials(
                    gateway_admin, agent_id, gw_id, goal_id,
                    on_error=on_resource_error,
                    story_id=story_id,
                    session=session,
                )
                session._resource_lifecycle = lifecycle

        except Exception as exc:
            logger.error(
                "Resource provisioning failed for %s/%s: %s",
                goal_id, agent_id, exc,
            )
            await session._broadcast_error(GatewayError(
                message=f"Resource provisioning failed: {exc}",
                code="RESOURCE_PROVISION_FAILED",
                retryable=True,
                method="resource_provisioning",
            ))
        finally:
            session._resources_ready.set()

    async def _provision_shca_instance(
        self,
        pool,
        session: "_AgentSession",
        agent_id: str,
        agent_row: dict,
        goal_id: str,
        thread_id: str,
        on_resource_error,
    ) -> str:
        """Create a new gateway agent instance for an SHCA conversation.

        Returns the gateway agent ID for the new instance.
        """
        # Instance ID: {template}-{goal_id}-{thread_id} (full UUIDs for traceability)
        instance_id = f"{agent_id}-{goal_id}-{thread_id}"
        workspace = f"~/.openclaw/workspace-{instance_id}"
        model = agent_row.get("model", "google/gemini-2.5-flash")

        # Create gateway agent with dispatchMode flag for ACP tool detection.
        # If the agent already exists on the gateway (resume after CP-side
        # session eviction), we skip ALL file pushes below to avoid
        # clobbering the workspace's accumulated state — most importantly
        # the V2 MEMORY.md the agent has been appending to via flush turns.
        # Resolve the assistant's tool allow/deny and pass them at CREATE. The
        # gateway honors tools ONLY at agents.create (agents.update is a no-op for
        # tools), so this is the one place the allow/deny actually reaches the
        # gateway. Without it the SHCA gateway agent had tools:null → wide open,
        # so the curated allow/deny (and even the sessions_* deny) were NEVER
        # enforced. Prefer the template (default-shca — always the current curated
        # set, same source the GO uses), falling back to the base agent row.
        tools_create_param: dict = {}
        _tpl_id = agent_row.get("template_id")
        _tool_allow: list = []
        _tool_deny: list = []
        if _tpl_id:
            try:
                _trow = await pool.fetchrow(
                    "SELECT tools_allow, tools_deny FROM templates WHERE id = $1", _tpl_id,
                )
                if _trow and _trow["tools_allow"]:
                    _tool_allow = list(_trow["tools_allow"])
                    _tool_deny = list(_trow["tools_deny"] or [])
            except Exception as _texc:
                logger.warning("SHCA tool resolve: template %s lookup failed: %s", _tpl_id, _texc)
        if not _tool_allow:  # fallback to the base agent row's own tools
            _tool_allow = list(agent_row.get("tools_allow") or [])
            _tool_deny = list(agent_row.get("tools_deny") or [])
        if _tool_allow:
            tools_create_param["allow"] = _tool_allow
        if _tool_deny:
            tools_create_param["deny"] = _tool_deny

        is_first_creation = False
        # The gateway normalizes `name` (lowercases; truncates to 64 chars if
        # longer — see agent_os_openclaw's normalizeAgentId()) and reports the
        # normalized value back as `agentId`. token_calls.agent_id later
        # echoes this normalized value verbatim, never our raw `instance_id` —
        # so agent_instances.gateway_agent_id must store the gateway's
        # reported id, not the raw one, or cost-attribution joins against
        # token_calls silently never match for SHCA instances. Only affects
        # what gets persisted to agent_instances below — instance_id itself
        # keeps being used unchanged for file/skill pushes, memory_v2 setup,
        # and session/thread tracking, since that path already works today.
        real_gateway_agent_id = instance_id
        try:
            create_result = await gateway_admin.call("agents.create", {
                "name": instance_id,
                "workspace": workspace,
                "model": model,
                "dispatchMode": "shca",
                **({"tools": tools_create_param} if tools_create_param else {}),
            })
            real_gateway_agent_id = (create_result or {}).get("agentId") or instance_id
            is_first_creation = True
            logger.info(
                "Created SHCA instance %s for thread %s (gateway-normalized id: %s)",
                instance_id, thread_id, real_gateway_agent_id,
            )
        except GatewayError as exc:
            if "already exists" not in str(exc):
                raise
            # The gateway saying "already exists" doesn't prove the instance was
            # fully provisioned — a prior attempt may have created the workspace
            # then crashed before pushing skills/IDENTITY (leaving a ghost). Use
            # CP-tracked agent_files as the source of truth: if it has files we
            # treat this as a genuine resume (skip push, preserve Gemini implicit
            # cache); if it has none, fall through to the first-creation path so
            # files actually get pushed.
            try:
                existing_files = await pool.fetchval(
                    "SELECT count(*) FROM agent_files WHERE agent_id = $1",
                    instance_id,
                ) or 0
            except Exception:
                existing_files = 0
            if existing_files > 0:
                logger.info(
                    "SHCA instance %s already exists with %d tracked files — "
                    "genuine resume, skipping re-push",
                    instance_id, existing_files,
                )
            else:
                logger.warning(
                    "SHCA instance %s already exists on gateway but has NO "
                    "CP-tracked files (ghost / half-provisioned) — forcing "
                    "first-creation re-push so skills + IDENTITY land",
                    instance_id,
                )
                is_first_creation = True

        if not is_first_creation:
            # Resume path: gateway agent already has its workspace + all
            # bootstrap files from when it was first provisioned. Skip the
            # entire push-files block below. Still need to refresh the
            # agent_instances row (it may have been marked destroyed if
            # the SHCA was previously reaped) and stash the gateway id on
            # the session.
            try:
                # Reactivate any previously-destroyed row for this
                # (template, thread) — earlier SHCA reapers (now disabled)
                # may have left rows marked 'destroyed'. agent_instances
                # has no (template_id, thread_id) unique constraint so
                # we can't use ON CONFLICT; do UPDATE then conditional
                # INSERT instead.
                # Note: goal_id intentionally NOT a parameter here — the UPDATE
                # doesn't reference it. Passing it caused asyncpg to error with
                # "could not determine data type of parameter $3" because $3 was
                # passed but unused, swallowing the whole resume bookkeeping
                # and leaving agent_instances un-tracked.
                # gateway_agent_id: keep whatever's already stored rather than
                # overwriting with the raw instance_id — this is a resume with
                # no fresh agents.create response, so instance_id is only a
                # best-effort guess here, and clobbering a previously-correct
                # gateway-normalized value (stored on first creation, see
                # above) on every reconnect is exactly how this bug persisted.
                # Reactivate/refresh the SINGLE best row (prefer the active one,
                # else the newest) — never touch multiple, so this can't produce
                # two active rows against the one-active-per-thread unique index.
                updated = await pool.execute(
                    """UPDATE agent_instances
                          SET status = 'active', destroyed_at = NULL,
                              gateway_agent_id = COALESCE(NULLIF(gateway_agent_id, ''), $2),
                              workspace = $4, model = $5,
                              owner_actor_id = COALESCE(owner_actor_id, $6)
                        WHERE id = (
                          SELECT id FROM agent_instances
                           WHERE template_id = $1 AND thread_id = $3
                           ORDER BY (status = 'active') DESC, created_at DESC
                           LIMIT 1
                        )""",
                    agent_id, instance_id, thread_id, workspace, model,
                    agent_row.get("owner_actor_id"),
                )
                # asyncpg returns 'UPDATE N' as the status string
                rows_updated = int(updated.split(" ")[-1]) if updated and updated.startswith("UPDATE") else 0
                if rows_updated == 0:
                    await pool.execute(
                        """INSERT INTO agent_instances
                            (id, template_id, gateway_agent_id, goal_id, thread_id, workspace, model, status, owner_actor_id)
                           VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, 'active', $7)
                           ON CONFLICT (template_id, goal_id, thread_id)
                               WHERE status = 'active' AND thread_id IS NOT NULL
                           DO NOTHING""",
                        agent_id, instance_id, goal_id, thread_id, workspace, model,
                        agent_row.get("owner_actor_id"),
                    )
                await pool.execute(
                    "UPDATE threads SET instance_id = $1 WHERE id = $2",
                    instance_id, thread_id,
                )
            except Exception as exc:
                logger.warning(
                    "Resume bookkeeping failed for SHCA %s: %s", instance_id, exc,
                )
            session._gateway_agent_id = instance_id
            return instance_id

        # memory_v2: USER_MEMORY.md symlink, snapshot restore, flush-prompt
        # push. user_uid comes from the agent row (owner_actor_id). Non-fatal.
        try:
            from app.memory_v2 import provisioning as _mv2_provisioning
            owner_uid = agent_row.get("owner_actor_id")
            owner_uid_str = str(owner_uid) if owner_uid else None
            await _mv2_provisioning.setup_memory_v2_for_agent(
                agent_id=instance_id, agent_type="SHCA", user_uid=owner_uid_str,
            )
        except Exception:
            logger.exception(
                "memory_v2 setup failed for SHCA instance %s — non-fatal", instance_id,
            )

        # ACP config is embedded in MEMORY.md (as HTML comment) — no separate file needed.
        # OpenClaw's agents.files.set has a filename allowlist, so we can't write arbitrary files.

        # Copy template files (SOUL.md, AGENTS.md, USER.md, IDENTITY.md, ...).
        # Post-migration 038: read from template_files (per-user template) via
        # agents.template_id. Falls back to agent_files for legacy / un-migrated
        # agents so this code keeps working if migration is yet to run.
        try:
            template_id_for_agent = await pool.fetchval(
                "SELECT template_id FROM agents WHERE id = $1", agent_id,
            )
            # Merge shared behavior (base) with per-agent overrides. Base files
            # (AGENTS.md, SOUL.md, ...) come from the agent's template
            # (template_files); per-agent files (USER.md, IDENTITY.md — generated
            # per user) come from agent_files and WIN on filename conflict. This
            # lets an instance point straight at the shared default template while
            # still carrying its owner's generated USER.md/IDENTITY.md.
            # Backward-compatible: legacy per-user templates hold base+user files
            # in template_files AND agent_files duplicates them, so the overlay is
            # a no-op (identical content); agents with only agent_files (no
            # template row / empty template) still resolve via the overlay.
            merged: dict[str, str] = {}
            base_rows: list = []
            if template_id_for_agent:
                base_rows = await pool.fetch(
                    "SELECT filename, content FROM template_files WHERE template_id = $1",
                    template_id_for_agent,
                )
                for f in base_rows:
                    merged[f["filename"]] = f["content"]
            overlay_rows = await pool.fetch(
                "SELECT filename, content FROM agent_files WHERE agent_id = $1",
                agent_id,
            )
            for f in overlay_rows:
                merged[f["filename"]] = f["content"]
            for filename, content in merged.items():
                await gateway_admin.set_agent_file(instance_id, filename, content)
            if merged:
                logger.info(
                    "Synced %d files to SHCA instance %s (%d base from template_files of %s + %d per-agent overlay from agent_files)",
                    len(merged), instance_id, len(base_rows),
                    template_id_for_agent or "<none>", len(overlay_rows),
                )

            # Overwrite OpenClaw's default BOOTSTRAP.md — we use our own agent files
            try:
                await gateway_admin.set_agent_file(
                    instance_id, "BOOTSTRAP.md",
                    "# Ignore this file\n\nYour instructions are in AGENTS.md and SOUL.md. Read those instead.\n"
                )
                logger.info("Overwritten default BOOTSTRAP.md on %s", instance_id)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Failed to sync template files to %s: %s", instance_id, exc)

        # Push every applicable skill into <workspace>/skills/<name>/SKILL.md.
        # Mirrors what go_agent.py does for GO. Required so SHCA can invoke
        # the shca_* operator skills (registered in migration 040).
        try:
            skill_rows = await pool.fetch(
                """
                SELECT name, content
                  FROM skills
                 WHERE status = 'active'
                   AND 'SHCA' = ANY(applicable_to)
                ORDER BY name
                """,
            )
            pushed = 0
            for sk in skill_rows:
                try:
                    await gateway_admin.set_agent_skill(
                        instance_id, sk["name"], sk["content"],
                    )
                    pushed += 1
                except Exception as exc:
                    logger.warning(
                        "skill push %s → %s failed: %s",
                        sk["name"], instance_id, exc,
                    )
            if skill_rows:
                logger.info(
                    "Synced %d/%d SHCA skills to instance %s",
                    pushed, len(skill_rows), instance_id,
                )
        except Exception as exc:
            logger.warning("Failed to sync SHCA skills to %s: %s", instance_id, exc)

        # NOTE: V1 generate_memory_md MEMORY.md push was REMOVED here.
        # memory_v2 (provisioning.py:_seed_memory_files, already ran in
        # setup_memory_v2_for_agent above) owns MEMORY.md. The previous
        # V1 path regenerated MEMORY.md from PostgreSQL agent_memories
        # and pushed it, which clobbered the V2 seed AND any accumulated
        # content from flush turns on subsequent re-provisioning. With
        # SHCA reaping disabled, this path effectively only runs on
        # first creation — but even there V1's output is wrong (it
        # injects content that doesn't match the V2 schema).

        # Write IDENTITY.md — includes story context if working on a story
        try:
            identity_md = await _build_identity_md(pool, goal_id, thread_id)
            await gateway_admin.set_agent_file(instance_id, "IDENTITY.md", identity_md)
            is_story = "Story Mode" in identity_md
            session._story_mode = is_story
            logger.info("Wrote IDENTITY.md for SHCA instance %s (story=%s)",
                         instance_id, "yes" if is_story else "no")
        except Exception as exc:
            logger.warning("IDENTITY.md write failed for %s: %s", instance_id, exc)

        # Register in agent_instances table. gateway_agent_id stores the
        # gateway-normalized id (real_gateway_agent_id), NOT instance_id —
        # everything else here (threads.instance_id, session tracking below)
        # intentionally keeps using the raw instance_id since that's the
        # identifier already used consistently for file/skill pushes and
        # session-key construction elsewhere in this function.
        try:
            inst_row = await pool.fetchrow("""
                INSERT INTO agent_instances
                    (id, template_id, gateway_agent_id, goal_id, thread_id, workspace, model, status, owner_actor_id)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, 'active', $7)
                ON CONFLICT (template_id, goal_id, thread_id) WHERE status = 'active' AND thread_id IS NOT NULL
                DO UPDATE SET gateway_agent_id = EXCLUDED.gateway_agent_id,
                              workspace        = EXCLUDED.workspace,
                              model            = EXCLUDED.model,
                              owner_actor_id   = COALESCE(agent_instances.owner_actor_id, EXCLUDED.owner_actor_id)
                RETURNING id, (xmax = 0) AS inserted
            """, agent_id, real_gateway_agent_id, goal_id, thread_id, workspace, model,
                agent_row.get("owner_actor_id"))

            if inst_row:
                # Register the ledger `actors` row only on a genuine INSERT — a
                # reprovision now DO-UPDATEs the existing row, so don't re-register.
                if inst_row["inserted"]:
                    try:
                        from app.ledger.actors import register_agent_instance_actor
                        await register_agent_instance_actor(inst_row["id"])
                    except Exception as exc:
                        logger.debug("SHCA instance actor pre-register skipped: %s", exc)
                # Set thread's instance_id
                await pool.execute(
                    "UPDATE threads SET instance_id = $1 WHERE id = $2",
                    instance_id, thread_id,
                )
                logger.info(
                    "Registered SHCA instance %s in agent_instances (gateway_agent_id=%s)",
                    instance_id, real_gateway_agent_id,
                )
        except Exception as exc:
            logger.warning("Failed to register SHCA instance %s: %s", instance_id, exc)

        # Store instance_id on the session for session key construction
        session._gateway_agent_id = instance_id

        return instance_id

    async def _ensure_gateway_agent(self, pool, agent_id: str, agent_row: dict, gw_id: str):
        """Ensure a gateway agent exists for non-SHCA (legacy) agents."""
        created = False
        try:
            await gateway_admin.call("agents.create", {
                "name": gw_id,
                "workspace": f"~/.openclaw/workspace-{gw_id}",
                "model": agent_row.get("model", "google/gemini-2.5-flash"),
            })
            created = True
            logger.info("Created gateway agent %s", gw_id)
        except GatewayError as exc:
            if "already exists" not in str(exc):
                logger.warning("Failed to ensure gateway agent %s: %s", gw_id, exc)
        except Exception as exc:
            logger.warning("Failed to ensure gateway agent %s: %s", gw_id, exc)

        if created:
            try:
                agent_files = await pool.fetch(
                    "SELECT filename, content FROM agent_files WHERE agent_id = $1",
                    agent_id,
                )
                for f in agent_files:
                    await gateway_admin.set_agent_file(gw_id, f["filename"], f["content"])
                if agent_files:
                    logger.info("Synced %d files to gateway agent %s", len(agent_files), gw_id)
            except Exception as exc:
                logger.warning("Failed to sync files to %s: %s", gw_id, exc)

            # memory_v2: fire setup hook for GO agents (IAs are no-ops).
            # Best-effort; non-fatal.
            try:
                agent_type = agent_row.get("type") or ""
                if agent_type in ("GO", "SHCA"):
                    from app.memory_v2 import provisioning as _mv2_provisioning
                    owner_uid = agent_row.get("owner_actor_id")
                    owner_uid_str = str(owner_uid) if owner_uid else None
                    await _mv2_provisioning.setup_memory_v2_for_agent(
                        agent_id=gw_id, agent_type=agent_type, user_uid=owner_uid_str,
                    )
            except Exception:
                logger.exception(
                    "memory_v2 setup failed for legacy-path agent %s — non-fatal", gw_id,
                )

    async def reap_deleted_goal_sessions(self) -> int:
        """Evict in-memory sessions whose goal is NOT active (status != 'active'), and
        finalize the stray agent_instances rows. GO sessions never idle-evict, so a
        non-active goal's session otherwise reconnects to the gateway forever (a
        reconnect storm on every gateway restart). Targets the `status` column only
        (active vs deleted/cancelled/…); goal *state* (planning/paused) is separate and
        a live goal keeps status='active'. Runs periodically from the startup watchdog."""
        from app.database import get_pool
        pool = get_pool()
        reaped = 0
        try:
            # DB: finalize instances whose goal is no longer active.
            await pool.execute(
                "UPDATE agent_instances SET status='destroyed' "
                "WHERE status='active' AND goal_id IN (SELECT id FROM goals WHERE status <> 'active')")
            dead_goals = {r["id"] for r in
                          await pool.fetch("SELECT id FROM goals WHERE status <> 'active'")}
        except Exception:
            logger.exception("reap_deleted_goal_sessions: DB step failed — skipping this pass")
            return 0
        if not dead_goals:
            return 0
        # In-memory: evict + stop any live session whose goal is no longer active.
        for key in [k for k in list(self._sessions.keys()) if k[0] in dead_goals]:
            session = self._sessions.pop(key, None)
            if session:
                try:
                    asyncio.create_task(session.stop())
                except Exception:
                    pass
                reaped += 1
        if reaped:
            logger.info("reap_deleted_goal_sessions: evicted %d session(s) for deleted goals", reaped)
        return reaped

    def evict(self, pid: str, agent_id: str, thread_id: str = ""):
        """Force-evict a session. Next get_or_create() will create a fresh one.

        For SHCA agents, pass thread_id to evict the per-conversation session.
        """
        # Try SHCA key first (3-tuple), then legacy key (2-tuple)
        if thread_id:
            key = (pid, agent_id, thread_id)
            session = self._sessions.pop(key, None)
        else:
            key = (pid, agent_id)
            session = self._sessions.pop(key, None)

        if session:
            asyncio.create_task(session.stop())
            logger.info("Session force-evicted: %s", key)

    def evict_thread(self, pid: str, agent_id: str, thread_id: str):
        """Evict a thread's session.

        For SHCA: evicts the per-conversation session (3-tuple key).
        For IA: clears thread from shared session; tears down if last thread.
        """
        # Try SHCA per-conversation key first
        shca_key = (pid, agent_id, thread_id)
        if shca_key in self._sessions:
            session = self._sessions.pop(shca_key, None)
            if session:
                asyncio.create_task(session.stop())
                logger.info("SHCA conversation session evicted: %s", shca_key)
            return

        # Legacy: shared session for IA agents
        key = (pid, agent_id)
        session = self._sessions.get(key)
        if not session:
            return
        no_subscribers_left = session.evict_thread(thread_id)
        if no_subscribers_left:
            logger.info("Last thread evicted — tearing down session %s/%s", pid, agent_id)
            self.evict(pid, agent_id)

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    async def shutdown(self):
        for session in list(self._sessions.values()):
            await session.stop()
        self._sessions.clear()


# Process-global singleton — imported by the chat router
session_manager = SessionManager()
