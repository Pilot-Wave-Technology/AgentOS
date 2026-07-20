"""Notion Bot inbound adapter — uses an internal integration token.

Subclasses NotionInboundAdapter and overrides only what differs:
- Platform slug is 'notion_bot' (separate DB row, separate bindings)
- No webhook signature verification (internal token integrations don't sign)
- get_integration resolves via 'notion_bot' platform row

All event handling, routing, and binding logic is inherited unchanged.
"""
import logging

from app.channels.notion.inbound import NotionInboundAdapter
from app.channels import base
from app.channels.base import route_to_session as _route_to_session

_log = logging.getLogger(__name__)

PLATFORM = "notion_bot"


class NotionBotAdapter(NotionInboundAdapter):
    def verify_request(self, body: bytes, headers: dict) -> bool:
        return True  # internal token integrations don't send HMAC signatures

    async def handle_event(self, body: bytes, headers: dict) -> None:
        import json as _json
        import uuid as _uuid
        try:
            payload = _json.loads(body)
        except Exception:
            _log.warning("notion_bot: invalid JSON payload")
            return

        delivery_id = (
            headers.get("x-notion-delivery-id")
            or headers.get("X-Notion-Delivery-Id")
            or str(_uuid.uuid4())
        )
        event_type = payload.get("type", "")
        _log.info("notion_bot event: type=%s delivery=%s", event_type, delivery_id)

        if await base.is_duplicate(PLATFORM, delivery_id):
            _log.info("notion_bot: duplicate delivery=%s — skipping", delivery_id)
            return

        if event_type == "comment.created":
            await self._handle_comment(payload)
        elif event_type == "page.content_updated":
            await self._handle_page_update(payload)
        else:
            _log.info("notion_bot: unhandled event type=%s", event_type)

    async def _handle_comment(self, payload: dict) -> None:
        from app.database import get_pool

        # Notion webhooks are signal-only:
        #   data.page_id  = which page the comment is on
        #   entity.id     = comment id (fetch text from API)
        #   authors[0].id = who posted it
        data = payload.get("data", {})
        entity = payload.get("entity", {})
        authors = payload.get("authors", [])

        page_id = data.get("page_id", "")
        comment_id = entity.get("id", "")
        author_id = authors[0].get("id", "") if authors else ""
        author_type = authors[0].get("type", "") if authors else ""
        author_name = authors[0].get("name", "") if authors else ""

        _log.info("notion_bot comment: page_id=%s comment_id=%s author=%s type=%s", page_id, comment_id, author_id, author_type)

        if not page_id:
            _log.warning("notion_bot comment: no page_id in payload data=%s", data)
            return

        # Drop comments posted by any bot/integration (includes our own relay)
        if author_type == "bot":
            _log.debug("notion_bot: dropping bot-authored comment %s", comment_id)
            return

        if comment_id:
            from app.database import get_pool as _get_pool
            is_echo = await _get_pool().fetchval(
                "SELECT id FROM messages WHERE external_message_id = $1 LIMIT 1",
                comment_id,
            )
            if is_echo:
                _log.debug("notion_bot: dropping echo of outbound comment %s", comment_id)
                return

        binding = await base.get_binding(PLATFORM, page_id)
        if not binding:
            _log.warning("notion_bot comment: no binding for page_id=%s", page_id)
            return

        pool = get_pool()
        integration = await pool.fetchrow(
            "SELECT bot_external_id, credentials_data FROM platform_integrations WHERE platform = $1 AND status = 'active'",
            PLATFORM,
        )
        if not integration:
            _log.warning("notion_bot comment: no active integration")
            return

        if author_id and author_id == integration.get("bot_external_id"):
            _log.debug("notion_bot: dropping echo from our own bot (id match)")
            return

        # Fetch comment text from Notion API — webhook only carries the signal
        text = ""
        access_token = integration.get("credentials_data") or ""
        if comment_id and access_token:
            try:
                from notion_client import AsyncClient
                notion = AsyncClient(auth=access_token)
                response = await notion.comments.list(block_id=page_id)
                for c in response.get("results", []):
                    if c.get("id") == comment_id:
                        text = "".join(
                            rt.get("plain_text", "") for rt in c.get("rich_text", [])
                        ).strip()
                        break
            except Exception as exc:
                _log.warning("notion_bot: failed to fetch comment %s: %s", comment_id, exc)

        if not text:
            _log.info("notion_bot comment: empty text comment_id=%s", comment_id)
            return

        _actor_id = await pool.fetchval(
            "SELECT actor_id FROM platform_user_map WHERE platform=$1 AND external_user_id=$2",
            PLATFORM, author_id,
        )
        _uid = await pool.fetchval("SELECT uid FROM users WHERE actor_id=$1", _actor_id) if _actor_id else None
        sender = _uid or author_id or "notion_bot_user"
        # Resolution order: users.display_name → Notion webhook name → sender uid
        _display_name = await pool.fetchval("SELECT display_name FROM users WHERE uid=$1", _uid) if _uid else None
        sender_name = _display_name or author_name or sender
        await _route_to_session(binding, sender, text, platform=PLATFORM, sender_name=sender_name)

    async def _handle_page_update(self, payload: dict) -> None:
        page_id = (payload.get("data") or {}).get("page_id", "")
        if not page_id:
            return

        binding = await base.get_binding(PLATFORM, page_id)
        if not binding:
            return

        sender = "notion_bot_user"
        text = f"[Notion page updated: {page_id}]"
        await _route_to_session(binding, sender, text, platform=PLATFORM)
