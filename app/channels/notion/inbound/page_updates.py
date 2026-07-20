"""Notion page.content_updated webhook handler — story card sync and generic page updates."""
import logging

from .attachments import _enrich_author_email, _extract_notion_attachments

_log = logging.getLogger(__name__)

_NOTION_LABEL_TO_STATE: dict[str, str] = {
    "Proposed":    "proposed",
    "Accepted":    "accepted",
    "In Progress": "in_progress",
    "Blocked":     "blocked",
    "Done":        "done",
    "Rejected":    "rejected",
    "Cancelled":   "cancelled",
}

# States users cannot set from Notion — system-controlled only.
_SYSTEM_ONLY_STATES = {"blocked", "cancelled", "proposed"}

_STATE_TO_ACTION = {
    "accepted":    "accept",
    "done":        "done",
    "rejected":    "reject",
}


def _resolve_notion_action(current_state: str, notion_status: str) -> str | None:
    """Map (current DB state, Notion Status label) → AgentOS action string.

    Returns:
        action string on valid human transition
        "__system_only__" when the target is system-controlled (blocked/cancelled/proposed)
        None for echo/no-op or unknown status
    """
    target = _NOTION_LABEL_TO_STATE.get(notion_status)
    if not target or target == current_state:
        return None   # echo or no-op
    if target in _SYSTEM_ONLY_STATES:
        return "__system_only__"
    if target == "in_progress":
        # Blocked → in_progress is "unblock"; accepted → in_progress is "start"
        return "unblock" if current_state == "blocked" else "start"
    return _STATE_TO_ACTION.get(target)


async def _handle_page_update(payload: dict) -> None:
    """Page content update — check for story card status changes, ingest files, route notice."""
    from app.channels import base
    from app.channels.base import route_to_session as _route_to_session
    from app.channels.notion import auth as notion_auth
    from app.channels.notion import identity as notion_id
    from app.database import get_pool

    data    = payload.get("data", {})
    authors = payload.get("authors", [])

    page_id = data.get("page_id", "")
    author  = authors[0] if authors else {}

    if not page_id:
        return

    if author.get("type") == "bot":
        _log.debug("Notion: dropping page update from bot author")
        return

    binding = await base.get_binding("notion", page_id)
    if not binding:
        binding = await base.get_binding("notion_bot", page_id)

    access_token = ""
    if binding:
        integration = await notion_auth.get_integration(binding["goal_id"])
        if integration and author.get("id") and author.get("id") == integration.get("bot_external_id"):
            _log.debug("Notion: dropping page update echo from our own bot (id match)")
            return
        if integration:
            from app.channels.notion.secrets import resolve_token
            access_token = await resolve_token(
                integration.get("credentials_kind", ""),
                integration.get("credentials_data"),
                integration.get("credentials_ref"),
            ) or ""
    else:
        from app.database import get_pool as _gp
        from app.channels.notion.secrets import resolve_token as _rt
        _row = await _gp().fetchrow(
            """SELECT pi.credentials_kind, pi.credentials_data, pi.credentials_ref
               FROM platform_integrations pi
               WHERE pi.platform IN ('notion', 'notion_bot') AND pi.status='active'
               LIMIT 1""",
        )
        if _row:
            access_token = await _rt(_row["credentials_kind"], _row["credentials_data"], _row["credentials_ref"]) or ""

    if not access_token:
        _log.debug("Notion page update: no access token available page=%s", page_id)
        return

    if author:
        author = await _enrich_author_email(author, access_token)

    # ── Story card status sync ──────────────────────────────────────────────
    try:
        from notion_client import AsyncClient
        notion_client = AsyncClient(auth=access_token)
        page_data = await notion_client.pages.retrieve(page_id)
    except Exception as exc:
        _log.warning("Notion page update: could not retrieve page=%s: %s", page_id, exc)
        return

    story_id_prop = (
        page_data.get("properties", {})
        .get("Story ID", {})
        .get("rich_text", [{}])[0]
        .get("plain_text", "")
    ) if page_data.get("properties") else ""

    if story_id_prop:
        story = await get_pool().fetchrow(
            "SELECT id, goal_id, state, title FROM stories WHERE id=$1 LIMIT 1",
            story_id_prop,
        )
        if story:
            allowlist = (binding.get("email_domain_allowlist") or []) if binding else []
            actor_id = await notion_id.resolve_notion_actor(
                author, email_domain_allowlist=allowlist or None,
            ) if author else None
            await _handle_story_card_update(story, page_data, notion_client, actor_id)
            return

    if not binding:
        _log.debug("Notion page update: no binding and no story ID for page=%s", page_id)
        return

    allowlist = binding.get("email_domain_allowlist") or []
    actor_id = await notion_id.resolve_notion_actor(author, email_domain_allowlist=allowlist or None) if author else None
    if not actor_id:
        _log.info("Notion page update: actor unresolved — dropping")
        return

    uid    = await get_pool().fetchval("SELECT uid FROM users WHERE actor_id = $1", actor_id)
    sender = uid or str(actor_id)

    try:
        story_id   = str(binding.get("current_story_id", "")) if binding.get("current_story_id") else ""
        attachments = await _extract_notion_attachments(page_id, notion_client)
        if attachments:
            from app.channels.base import ingest_attachments
            count = await ingest_attachments(binding["goal_id"], story_id or None, attachments, sender)
            _log.info(
                "Notion page update: ingested %d/%d attachments page=%s",
                count, len(attachments), page_id,
            )
    except Exception as exc:
        _log.warning("Notion attachment ingest failed page=%s: %s", page_id, exc)

    text = f"[Notion page updated: {page_id}]"
    await _route_to_session(binding, sender, text, platform="notion")


async def _handle_story_card_update(
    story: dict,
    page_data: dict,
    notion_client,
    actor_id,
) -> None:
    """Process a story card page update: validate and apply the status change.

    Reads the Status property from already-fetched page_data (no extra API call).
    Calls apply_story_transition which enforces all AgentOS constraints.
    On failure: posts a comment explaining why and reverts the Notion Status.
    """
    from app.channels.notion.pages import STORY_STATE_TO_NOTION
    from app.services.story_transitions import apply_story_transition
    from app.database import get_pool

    page_id       = page_data.get("id", "")
    goal_id       = str(story["goal_id"])
    story_id      = str(story["id"])
    current_state = story["state"]

    status_prop   = page_data.get("properties", {}).get("Status", {})
    notion_status = (status_prop.get("select") or {}).get("name", "")

    action = _resolve_notion_action(current_state, notion_status)

    if action is None:
        _log.debug("Notion story card: echo or no-op status=%s story=%s", notion_status, story_id)
        return

    if action == "__system_only__":
        reason = f"'{notion_status}' is a system-controlled state and cannot be set from Notion."
        _log.info("Notion story card: system-only state rejected story=%s target=%s", story_id, notion_status)
        await _revert_and_comment(notion_client, page_id, current_state, reason)
        return

    uid = await get_pool().fetchval("SELECT uid FROM users WHERE actor_id = $1", actor_id) if actor_id else None
    if not uid:
        _log.info("Notion story card: could not resolve uid for actor=%s — dropping", actor_id)
        return

    pool = get_pool()
    ok, reason = await apply_story_transition(goal_id, story_id, action, uid, pool)
    if ok:
        _log.info("Notion story card: transition applied story=%s %s→%s by uid=%s", story_id, current_state, action, uid)
    else:
        _log.info("Notion story card: transition rejected story=%s action=%s reason=%s", story_id, action, reason)
        await _revert_and_comment(notion_client, page_id, current_state, reason)


async def _revert_and_comment(notion_client, page_id: str, current_state: str, reason: str) -> None:
    """Post a comment explaining the rejection and revert the Status property to current_state."""
    from app.channels.notion.pages import STORY_STATE_TO_NOTION

    try:
        await notion_client.comments.create(
            parent={"page_id": page_id},
            rich_text=[{"type": "text", "text": {"content": f"⚠️ Status change not applied: {reason}"}}],
        )
    except Exception as exc:
        _log.debug("Notion: could not post rejection comment page=%s: %s", page_id, exc)

    try:
        revert_label = STORY_STATE_TO_NOTION.get(current_state, current_state.title())
        await notion_client.pages.update(
            page_id,
            properties={"Status": {"select": {"name": revert_label}}},
        )
    except Exception as exc:
        _log.debug("Notion: could not revert Status page=%s: %s", page_id, exc)
