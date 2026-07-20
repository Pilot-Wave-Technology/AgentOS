"""Notion-specific actor resolution.

Extracts user identity from Notion webhook payloads and delegates
to the platform-agnostic resolve_actor() in channels.base.
"""
import logging
from typing import Optional
import uuid

from app.channels.base import resolve_actor

_log = logging.getLogger(__name__)


async def resolve_notion_actor(
    notion_user: dict,
    email_domain_allowlist: Optional[list[str]] = None,
) -> Optional[uuid.UUID]:
    """Resolve a Notion user object to an internal actor_id.

    `notion_user` is the Notion API Person object:
      { "object": "user", "id": "<uuid>", "type": "person",
        "person": { "email": "..." }, "name": "..." }

    Bots have type="bot" — always returns None for bots (caller filters echoes).
    """
    if notion_user.get("type") == "bot":
        return None

    external_user_id = notion_user.get("id", "")
    email = (notion_user.get("person") or {}).get("email")
    display_name = notion_user.get("name")

    return await resolve_actor(
        platform="notion",
        external_user_id=external_user_id,
        email=email,
        display_name=display_name,
        email_domain_allowlist=email_domain_allowlist,
    )
