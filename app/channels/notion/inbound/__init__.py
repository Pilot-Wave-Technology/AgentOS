"""Notion inbound adapter — receives webhook events and routes to agent sessions.

Notion webhooks are signals only; content must be fetched via the API.
Supported event types we act on: comment.created (page comments → SHCA).
All others are logged and acknowledged.

Echo prevention: events from our own bot (bot_external_id in
platform_integrations) are dropped before routing.
"""
import json
import logging
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from app.channels import base

from .comments import _handle_comment
from .page_updates import _handle_page_update

_log = logging.getLogger(__name__)

_SUPPORTED_EVENTS = {"comment.created", "page.content_updated"}


class NotionInboundAdapter:
    def verify_request(self, body: bytes, headers: dict) -> bool:
        """Return True if the webhook signature is valid (or if no secret is configured)."""
        from app.channels.notion.auth import verify_signature
        import os
        secret = os.environ.get("NOTION_WEBHOOK_SECRET", "")
        if not secret:
            return True  # verification disabled — allow (dev/test mode)
        return verify_signature(body, headers, secret)

    async def handle_event(self, body: bytes, headers: dict) -> None:
        try:
            payload = json.loads(body)
        except Exception:
            _log.warning("Notion: invalid JSON payload")
            return

        delivery_id = (
            headers.get("x-notion-delivery-id")
            or headers.get("X-Notion-Delivery-Id")
            or str(uuid.uuid4())
        )
        event_type = payload.get("type", "")

        if await base.is_duplicate("notion", delivery_id):
            return

        if event_type not in _SUPPORTED_EVENTS:
            _log.info("Notion: unhandled event type=%s delivery=%s", event_type, delivery_id)
            return

        if event_type == "comment.created":
            await _handle_comment(payload)
        elif event_type == "page.content_updated":
            await _handle_page_update(payload)

    async def oauth_install(self, request: Request, user: dict):
        from app.channels.notion.oauth import build_install_url, get_app_credentials
        from fastapi.responses import RedirectResponse

        creds = await get_app_credentials()
        if not creds.get("client_id") or not creds.get("client_secret"):
            return JSONResponse(
                {"error": "Notion app credentials not configured — fill in client ID and secret on /settings/integrations"},
                status_code=500,
            )

        redirect_uri = creds.get("redirect_uri") or ""
        url = build_install_url(creds["client_id"], redirect_uri)
        _log.info("Redirecting %s to Notion OAuth", user.get("email"))
        return RedirectResponse(url=url)

    async def oauth_callback(self, request: Request, user: dict):
        from fastapi.responses import RedirectResponse
        from app.channels.notion.oauth import exchange_code, upsert_integration

        error = request.query_params.get("error", "")
        if error:
            _log.warning("Notion OAuth error: %s", error)
            return RedirectResponse(url=f"/settings/integrations?error={error}", status_code=303)

        code = request.query_params.get("code", "")
        if not code:
            return JSONResponse({"error": "Missing OAuth code"}, status_code=400)

        try:
            token_data = await exchange_code(code)
        except Exception as exc:
            _log.error("Notion OAuth token exchange failed: %s", exc)
            return RedirectResponse(url="/settings/integrations?error=token_exchange_failed", status_code=303)

        if not token_data.get("access_token"):
            _log.error("Notion OAuth response missing access_token: %s", token_data)
            return RedirectResponse(url="/settings/integrations?error=missing_token", status_code=303)

        try:
            await upsert_integration(token_data)
        except Exception as exc:
            _log.error("Notion OAuth storage failed: %s", exc)
            return RedirectResponse(url="/settings/integrations?error=storage_failed", status_code=303)

        _log.info(
            "Notion OAuth install complete: workspace=%s by %s",
            token_data.get("workspace_name"), user.get("email"),
        )
        return RedirectResponse(url="/settings/integrations?success=1", status_code=303)
