"""DEPRECATED — superseded by app/channels/slack/inbound.py (oauth_install/oauth_callback).
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Slack OAuth2 install flow — adds a workspace to the control panel.

Flow:
  1. Admin clicks [Add Workspace] → redirect to Slack OAuth
  2. Slack redirects back to /slack/oauth/callback?code=...
  3. We exchange the code for a bot token + workspace info
  4. Store in slack_workspaces table
  5. Redirect to admin settings with success message
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth import get_current_user
from app.config import get_settings
from app.slack.config import upsert_workspace

_log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/oauth/install")
async def slack_oauth_install(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Redirect the admin to Slack's OAuth consent screen."""
    settings = get_settings()
    if not settings.slack_client_id:
        raise HTTPException(status_code=500, detail="SLACK_CLIENT_ID not configured")

    # Bot token scopes requested at OAuth install. Must match the scopes
    # declared in the Slack app's manifest. Adding new ones here without
    # updating the manifest (or vice versa) results in a smaller granted
    # scope set than expected — Slack grants the intersection of the two.
    scopes = ",".join([
        "app_mentions:read",
        "channels:history",
        "channels:join",
        "channels:manage",
        "channels:read",
        "chat:write",
        "chat:write.customize",
        "commands",
        "files:read",
        "groups:history",
        "groups:read",
        "groups:write",
        "im:history",
        "im:read",
        "im:write",
        "mpim:read",
        "reactions:write",
        "users:read",
        "users:read.email",
    ])

    # Build the OAuth URL. Cloud Run terminates TLS at the load balancer,
    # so request.base_url returns http://. Force https:// for the callback.
    base = str(request.base_url).replace("http://", "https://")
    callback_url = f"{base}slack/oauth/callback"
    slack_url = (
        f"https://slack.com/oauth/v2/authorize"
        f"?client_id={settings.slack_client_id}"
        f"&scope={scopes}"
        f"&redirect_uri={callback_url}"
    )
    _log.info("Redirecting %s to Slack OAuth: %s", user.get("email"), slack_url[:80])
    return RedirectResponse(url=slack_url)


@router.get("/oauth/callback")
async def slack_oauth_callback(
    request: Request,
    code: str = "",
    error: str = "",
    user: dict = Depends(get_current_user),
):
    """Handle the redirect from Slack after OAuth consent."""
    if error:
        _log.warning("Slack OAuth error: %s", error)
        # Redirect to admin page with error
        return RedirectResponse(url="/admin/slack?error=" + error, status_code=303)

    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")

    settings = get_settings()
    base = str(request.base_url).replace("http://", "https://")
    callback_url = f"{base}slack/oauth/callback"

    # Exchange the code for tokens
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
                "redirect_uri": callback_url,
            },
        )
        data = resp.json()

    if not data.get("ok"):
        error_msg = data.get("error", "unknown")
        _log.error("Slack OAuth token exchange failed: %s", error_msg)
        return RedirectResponse(url=f"/admin/slack?error={error_msg}", status_code=303)

    # Extract workspace info
    workspace_id = data.get("team", {}).get("id", "")
    workspace_name = data.get("team", {}).get("name", "")
    bot_token = data.get("access_token", "")
    bot_user_id = data.get("bot_user_id", "")
    app_id = data.get("app_id", "")
    scopes = (data.get("scope", "") or "").split(",")

    if not workspace_id or not bot_token:
        _log.error("Slack OAuth response missing workspace_id or bot_token: %s", data)
        return RedirectResponse(url="/admin/slack?error=missing_token", status_code=303)

    # Store the workspace
    await upsert_workspace(
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        bot_token=bot_token,
        bot_user_id=bot_user_id,
        app_id=app_id,
        installed_by=user.get("uid", ""),
        scopes=scopes,
    )

    _log.info(
        "Slack workspace installed: %s (%s) by %s",
        workspace_name, workspace_id, user.get("email"),
    )
    return RedirectResponse(url="/admin/slack?success=1", status_code=303)
