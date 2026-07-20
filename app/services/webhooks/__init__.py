"""Webhook receiver and dispatch layer for git provider events.

The receiver (receiver.py) exposes POST /webhooks/git, validates signatures,
deduplicates deliveries, and routes to event_dispatcher.py.

Provider-specific parsing lives in github_handler.py and gitlab_handler.py.
The dispatcher sees only provider-agnostic GitEvent objects.

The dedup helper (dedup.py) is shared and reusable by future channel inbound
adapters (e.g. app/channels/github/).

See documentation/story-integration/WEBHOOKS.md for the full design.
"""
from .receiver import router as webhook_router

__all__ = ["webhook_router"]
