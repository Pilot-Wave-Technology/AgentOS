"""Notion author enrichment and file attachment extraction helpers."""
import logging

_log = logging.getLogger(__name__)


async def _enrich_author_email(author: dict, access_token: str) -> dict:
    """Return a copy of `author` with person.email and name populated from the Notion Users API.

    Notion webhook payloads often send partial user objects (just id + type) without
    person.email or name, even when the integration has user.email:read scope.
    We always call GET /users/{id} when either field is missing.
    Returns the original dict unchanged on any failure.
    """
    if not author.get("id") or not access_token:
        return author

    # Skip API call only when both email and name are already present.
    already_has_email = bool((author.get("person") or {}).get("email"))
    already_has_name = bool(author.get("name"))
    if already_has_email and already_has_name:
        return author

    try:
        from notion_client import AsyncClient
        notion_client = AsyncClient(auth=access_token)
        user_data = await notion_client.users.retrieve(user_id=author["id"])
        email = (user_data.get("person") or {}).get("email") or ""
        name = user_data.get("name") or ""

        if email or name:
            enriched = dict(author)
            if email:
                enriched["person"] = dict(author.get("person") or {})
                enriched["person"]["email"] = email
            if name and not enriched.get("name"):
                enriched["name"] = name
            _log.debug("Enriched Notion author %s email=%s name=%s", author["id"], bool(email), bool(name))
            return enriched
    except Exception as exc:
        _log.warning("Failed to fetch Notion user info for %s: %s", author.get("id"), exc)

    return author


async def _extract_notion_attachments(page_id: str, notion_client) -> list:
    """Walk the block tree of a Notion page and return ChannelAttachment for each file block.

    Notion file blocks expose a time-limited signed URL — we extract it and
    pass the empty download_headers dict because the URL is pre-signed.
    Only top-level blocks are scanned (no recursive child fetch) to keep the
    API call count bounded for large pages.
    """
    from app.channels.base import ChannelAttachment

    try:
        response = await notion_client.blocks.children.list(block_id=page_id)
    except Exception as exc:
        _log.warning("Notion _extract_notion_attachments: blocks.children.list failed page=%s: %s", page_id, exc)
        return []

    attachments = []
    for block in response.get("results", []):
        block_type = block.get("type", "")
        if block_type not in ("file", "pdf", "image", "video"):
            continue
        block_data = block.get(block_type, {})
        file_type = block_data.get("type", "")  # "external" or "file" (hosted by Notion)
        url = ""
        if file_type == "file":
            url = (block_data.get("file") or {}).get("url", "")
        elif file_type == "external":
            url = (block_data.get("external") or {}).get("url", "")
        if not url:
            continue

        caption_parts = block_data.get("caption") or []
        name = "".join(p.get("plain_text", "") for p in caption_parts).strip()
        if not name:
            name = url.split("?")[0].rsplit("/", 1)[-1] or "attachment"

        mime_map = {"pdf": "application/pdf", "image": "image/png", "video": "video/mp4"}
        mime = mime_map.get(block_type, "application/octet-stream")

        attachments.append(ChannelAttachment(
            name=name,
            url=url,
            mimetype=mime,
            download_headers={},  # Notion file URLs are pre-signed
        ))

    _log.debug("Notion _extract_notion_attachments: page=%s found %d attachments", page_id, len(attachments))
    return attachments
