"""Notion comment.created webhook handler — routes comments to SHCA/GO sessions."""
import logging

from .attachments import _enrich_author_email

_log = logging.getLogger(__name__)


async def _handle_comment(payload: dict) -> None:
    """Route a Notion comment to the mapped SHCA or GO session.

    Member pages are bound to the user's SHCA thread (actor_id IS NOT NULL).
    Goal pages (actor_id IS NULL) accept comments from any resolved actor.
    Story pages have no binding — comments receive a redirect reply and are dropped.
    """
    from app.channels import base
    from app.channels.base import route_to_session as _route_to_session
    from app.channels.notion import auth as notion_auth
    from app.channels.notion import identity as notion_id
    from app.database import get_pool

    data = payload.get("data", {})
    entity = payload.get("entity", {})
    authors = payload.get("authors", [])

    page_id    = data.get("page_id", "")
    comment_id = entity.get("id", "")
    author     = authors[0] if authors else {}

    _log.info("notion comment: page_id=%s comment_id=%s author_type=%s", page_id, comment_id, author.get("type", ""))

    if not page_id:
        _log.warning("notion comment: no page_id in payload data=%s", data)
        return

    if author.get("type") == "bot":
        _log.debug("notion: dropping bot-authored comment %s", comment_id)
        return

    if comment_id:
        is_echo = await get_pool().fetchval(
            "SELECT id FROM messages WHERE external_message_id = $1 LIMIT 1",
            comment_id,
        )
        if is_echo:
            _log.debug("notion: dropping echo of outbound comment %s", comment_id)
            return

    binding = await base.get_binding("notion", page_id)
    if not binding:
        _log.info("notion comment: no binding for page_id=%s — posting redirect", page_id)
        try:
            from app.channels.notion.secrets import resolve_token
            any_integration = await get_pool().fetchrow(
                """SELECT credentials_kind, credentials_data, credentials_ref
                   FROM platform_integrations
                   WHERE platform IN ('notion', 'notion_bot') AND status = 'active'
                   LIMIT 1""",
            )
            if any_integration:
                _token = await resolve_token(
                    any_integration["credentials_kind"],
                    any_integration["credentials_data"],
                    any_integration["credentials_ref"],
                ) or ""
                if _token:
                    from notion_client import AsyncClient
                    notion = AsyncClient(auth=_token)
                    await notion.comments.create(
                        parent={"page_id": page_id},
                        rich_text=[{"type": "text", "text": {
                            "content": "This story page is view-only. To chat with your assistant, use your personal page in the Members section of this goal.",
                        }}],
                    )
        except Exception as exc:
            _log.debug("notion: story page redirect failed page=%s: %s", page_id, exc)
        return

    integration = await notion_auth.get_integration(binding["goal_id"])
    if integration and author.get("id") and author.get("id") == integration.get("bot_external_id"):
        _log.debug("Notion: dropping echo from our own bot (id match)")
        return

    access_token = ""
    if integration:
        from app.channels.notion.secrets import resolve_token
        access_token = await resolve_token(
            integration.get("credentials_kind", ""),
            integration.get("credentials_data"),
            integration.get("credentials_ref"),
        ) or ""

    author = await _enrich_author_email(author, access_token)
    _log.debug("notion comment: enriched author=%s", {k: v for k, v in author.items() if k != "avatar_url"})

    allowlist = binding.get("email_domain_allowlist") or []
    actor_id  = await notion_id.resolve_notion_actor(author, email_domain_allowlist=allowlist or None)
    if not actor_id:
        _log.info("Notion comment: actor unresolved/pending_review — dropping")
        return

    # Member-page bindings have actor_id set to the page owner.
    # Reject comments from anyone other than the owner with a static reply.
    binding_actor_id = binding.get("actor_id")
    if binding_actor_id and str(actor_id) != str(binding_actor_id):
        _log.info(
            "Notion comment: non-assigned commenter page=%s commenter=%s assigned=%s — static reply",
            page_id, actor_id, binding_actor_id,
        )
        if access_token:
            try:
                from notion_client import AsyncClient
                notion = AsyncClient(auth=access_token)
                await notion.comments.create(
                    parent={"page_id": page_id},
                    rich_text=[{"type": "text", "text": {
                        "content": "This page belongs to another member. Only the page owner can chat with the assistant here.",
                    }}],
                )
            except Exception as exc:
                _log.debug("notion: static reply failed page=%s: %s", page_id, exc)
        return

    text = ""
    if comment_id and access_token:
        try:
            from notion_client import AsyncClient
            notion_client = AsyncClient(auth=access_token)
            comment = await notion_client.comments.retrieve(comment_id)
            text = "".join(
                rt.get("plain_text", "") for rt in comment.get("rich_text", [])
            ).strip()
        except Exception as exc:
            _log.warning("notion: failed to fetch comment %s: %s", comment_id, exc)

    if not text:
        _log.info("notion comment: empty text comment_id=%s page_id=%s", comment_id, page_id)
        return

    user_row = await get_pool().fetchrow(
        "SELECT uid, display_name FROM users WHERE actor_id = $1", actor_id,
    )
    sender = user_row["uid"] if user_row else str(actor_id)
    sender_name = (
        (user_row["display_name"] if user_row else None)
        or author.get("name")
        or sender
    )
    _log.info("notion comment: routing sender=%s sender_name=%r author_name=%r", sender, sender_name, author.get("name"))
    await _route_to_session(binding, sender, text, platform="notion", sender_name=sender_name)
