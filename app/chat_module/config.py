"""chat_module configuration — self-contained and env-driven.

All chat-queue knobs live here (keeps the module separable). Shared infra
values (DB, gateway URL/token) are pulled from `app.config` so connections
have a single source of truth.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


@dataclass(frozen=True)
class ChatConfig:
    # Master switch: route conversational ingress through the durable queue.
    # OFF = legacy direct-send path (current behavior) is used.
    queue_enabled: bool
    # Whether THIS process runs the dispatcher drain loop. Set on the dedicated
    # dispatcher service; OFF on the web tier (which only ingests + fans out).
    dispatcher_enabled: bool
    # Whether THIS process runs the fan-out listener (web tier: ON).
    fanout_enabled: bool

    # Collect windows (ms). GO batches multiple members within the window;
    # SHCA is opportunistic (0 = drain whatever is already queued, no wait).
    collect_window_go_ms: int
    collect_window_shca_ms: int
    # Hard cap on messages folded into a single turn (safety).
    collect_max_batch: int

    # Reaper: requeue rows stuck in 'processing' beyond this many seconds.
    reaper_timeout_s: int
    reaper_interval_s: int
    # Dispatcher poll fallback (NOTIFY is primary; this catches missed signals).
    dispatch_poll_interval_s: int

    # This process/instance id, written to messages.claimed_by.
    instance_id: str

    # This process's gateway identity (for the registry upsert at startup).
    # Distinct per service (e.g. gw-main vs gw-acp) since their ws_urls differ.
    gateway_id: str
    gateway_ws_url: str
    gateway_auth_token: str

    database_url: str


@lru_cache
def get_chat_config() -> ChatConfig:
    s = get_settings()
    return ChatConfig(
        queue_enabled=_env_bool("CHAT_QUEUE_ENABLED", False),
        dispatcher_enabled=_env_bool("CHAT_DISPATCHER_ENABLED", False),
        fanout_enabled=_env_bool("CHAT_FANOUT_ENABLED", True),
        # FCFS: no batching window for either agent type. Each message is its
        # own turn, processed in arrival order (see collect.first_turn_slice).
        collect_window_go_ms=_env_int("CHAT_COLLECT_WINDOW_GO_MS", 0),
        collect_window_shca_ms=_env_int("CHAT_COLLECT_WINDOW_SHCA_MS", 0),
        collect_max_batch=_env_int("CHAT_COLLECT_MAX_BATCH", 20),
        reaper_timeout_s=_env_int("CHAT_REAPER_TIMEOUT_S", 180),
        reaper_interval_s=_env_int("CHAT_REAPER_INTERVAL_S", 30),
        dispatch_poll_interval_s=_env_int("CHAT_DISPATCH_POLL_INTERVAL_S", 15),
        instance_id=os.environ.get("CHAT_INSTANCE_ID")
        or f"{socket.gethostname()}:{os.getpid()}",
        gateway_id=os.environ.get("CHAT_GATEWAY_ID", "gw-default"),
        gateway_ws_url=s.openclaw_ws_url,
        gateway_auth_token=s.openclaw_auth_token,
        database_url=s.database_url,
    )
