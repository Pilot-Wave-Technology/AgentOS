"""Platform channel adapter and provisioner registry.

Adapters, provisioners, and outbound relays register here so the router and
goals endpoints can dispatch without importing every platform module
unconditionally.

Adding a new platform (Telegram, Google Chat, Teams, …):
  1. Add an entry to _ADAPTERS with the dotted adapter class path.
  2. Add an entry to _PROVISIONERS with the dotted provisioner class path
     (omit if the platform needs no goal/member setup).
  3. Add an entry to _OUTBOUND_RELAYS with the dotted relay function path
     (omit if the platform has no outbound relay).
  4. That's it — no changes to goals.py or settings.html needed.
     Wire chat.py / goal_thread.py to relay_all_outbound() when ready.
"""
from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.channels.base import ChannelProvisioner
    from app.channels.notion.inbound import NotionInboundAdapter
    from app.channels.slack.inbound import SlackInboundAdapter
    from app.channels.slack.provisioner import SlackProvisioner

_log = logging.getLogger(__name__)

# Map platform slug → dotted module path of the inbound adapter class
_ADAPTERS: dict[str, str] = {
    "notion":     "app.channels.notion.inbound.NotionInboundAdapter",
    "notion_bot": "app.channels.notion_bot.adapter.NotionBotAdapter",
    "slack":      "app.channels.slack.inbound.SlackInboundAdapter",
}

# Map platform slug → dotted module path of the ChannelProvisioner subclass.
# Platforms with no goal/member setup are simply omitted.
_PROVISIONERS: dict[str, str] = {
    "notion":     "app.channels.notion.provisioner.NotionProvisioner",
    "notion_bot": "app.channels.notion.provisioner.NotionBotProvisioner",
    "slack":      "app.channels.slack.provisioner.SlackProvisioner",
}


def _load(module_path: str, kind: str):
    try:
        mod_name, cls_name = module_path.rsplit(".", 1)
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name)
        return cls()
    except Exception as exc:
        _log.warning("Failed to load %s for %s: %s", kind, module_path, exc)
        return None


def get_adapter(platform: str):
    """Return an instantiated inbound adapter, or None if unknown/unloadable."""
    module_path = _ADAPTERS.get(platform)
    if not module_path:
        _log.warning("No adapter registered for platform: %s", platform)
        return None
    return _load(module_path, "adapter")


def get_provisioner(platform: str) -> "ChannelProvisioner | None":
    """Return an instantiated ChannelProvisioner, or None if not registered."""
    module_path = _PROVISIONERS.get(platform)
    if not module_path:
        return None
    return _load(module_path, "provisioner")


# Map platform slug → dotted module path of the outbound relay function.
# Each function must accept (goal_id: str, thread_id: str, content: str,
# msg_id=None) and is called after every agent message.
# Platforms with no outbound relay are simply omitted.
_OUTBOUND_RELAYS: dict[str, str] = {
    "notion":     "app.channels.notion.outbound.relay_agent_message",
    "notion_bot": "app.channels.notion.outbound.relay_agent_message",
    "slack":      "app.channels.slack.outbound.relay_agent_message",
}


def _load_relays(relay_map: dict[str, str], kind: str) -> list[tuple[str, object]]:
    """Load a relay map into (platform, callable) pairs, skipping failures."""
    result = []
    for platform, module_path in relay_map.items():
        try:
            mod_name, fn_name = module_path.rsplit(".", 1)
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            result.append((platform, fn))
        except Exception as exc:
            _log.warning("Failed to load %s relay for %s: %s", kind, platform, exc)
    return result


def get_outbound_relays() -> list[tuple[str, object]]:
    """Return (platform, relay_fn) pairs for all registered outbound relays.

    Each relay_fn accepts (goal_id, thread_id, content, msg_id=None).
    """
    return _load_relays(_OUTBOUND_RELAYS, "outbound")


# Map platform slug → dotted path of goal-channel relay function.
# Each function must accept (goal_id: str, content: str, username: str = "System").
# Called by base.relay_to_goal_channel for system/lifecycle messages targeting
# the goal's top-level channel (e.g. Notion goal page, Slack goal channel).
_GOAL_CHANNEL_RELAYS: dict[str, str] = {
    "notion": "app.channels.notion.outbound.post_to_goal_page",
    "slack":  "app.channels.slack.outbound.post_to_goal_page",
}


def get_goal_channel_relays() -> list[tuple[str, object]]:
    """Return (platform, relay_fn) pairs for all registered goal-channel relays."""
    return _load_relays(_GOAL_CHANNEL_RELAYS, "goal_channel")


# Map platform slug → dotted path of thread-channel relay function.
# Each function must accept (goal_id: str, thread_id: str, content: str, username: str = "System").
# Called by base.relay_to_thread_channel for messages targeting a specific thread's channel.
_THREAD_CHANNEL_RELAYS: dict[str, str] = {
    "notion": "app.channels.notion.outbound.post_to_thread_page",
    "slack":  "app.channels.slack.outbound.post_to_thread_page",
}


def get_thread_channel_relays() -> list[tuple[str, object]]:
    """Return (platform, relay_fn) pairs for all registered thread-channel relays."""
    return _load_relays(_THREAD_CHANNEL_RELAYS, "thread_channel")


# Map platform slug → dotted path of lifecycle relay function.
# Each function must accept (goal_id: str, event_type: str, payload: dict, thread_id: str | None = None).
# Called by base.relay_lifecycle_event for story/goal state changes.
_LIFECYCLE_RELAYS: dict[str, str] = {
    "notion": "app.channels.notion.lifecycle.relay_lifecycle_event",
    "slack":  "app.channels.slack.lifecycle.relay_lifecycle_event",
}


def get_lifecycle_relays() -> list[tuple[str, object]]:
    """Return (platform, relay_fn) pairs for all registered lifecycle relays."""
    return _load_relays(_LIFECYCLE_RELAYS, "lifecycle")


def registered_platforms() -> list[str]:
    """All registered platform slugs, in registration order."""
    return list(_ADAPTERS.keys())
