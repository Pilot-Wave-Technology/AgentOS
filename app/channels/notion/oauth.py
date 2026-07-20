"""Notion OAuth2 install flow.

Flow:
  1. Admin fills in client_id, client_secret, redirect_uri on /admin/integrations
  2. Saved to platform_integrations.config via save_app_credentials()
  3. Admin clicks [Connect Notion] → redirect to Notion OAuth consent
  4. Notion redirects back to redirect_uri?code=...
  5. exchange_code() reads client_id/secret from config, exchanges for access token
  6. upsert_integration() stores access token in credentials_data (no Secret Manager)
  7. Redirect to /admin/integrations?success=1

credential layout:
  config.client_id      — OAuth app client ID
  config.client_secret  — OAuth app client secret (never overwritten by access token)
  credentials_data      — runtime access token (written after OAuth completes)
  redirect_uri column   — OAuth redirect URI
"""
import json
import logging

import httpx

from app.database import get_pool

_log = logging.getLogger(__name__)

_NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"
_NOTION_AUTH_URL = "https://api.notion.com/v1/oauth/authorize"


def build_install_url(client_id: str, redirect_uri: str) -> str:
    return (
        f"{_NOTION_AUTH_URL}"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&owner=user"
    )


async def get_app_credentials() -> dict:
    """Fetch OAuth credentials from DB.

    client_id and client_secret live in config JSONB so they are never
    overwritten by the access token stored in credentials_data.
    """
    row = await get_pool().fetchrow(
        """SELECT redirect_uri, config, credentials_data AS access_token
           FROM platform_integrations
           WHERE platform = 'notion'""",
    )
    if not row:
        return {}
    d = dict(row)
    raw_cfg = d.get("config") or {}
    cfg = json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
    d["config"] = cfg
    d["client_id"] = cfg.get("client_id", "")
    d["client_secret"] = cfg.get("client_secret", "")
    return d


async def save_app_credentials(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    parent_database_id: str = "",
) -> None:
    """Upsert app-level OAuth credentials before the install flow runs."""
    existing = await get_app_credentials()
    config = dict(existing.get("config") or {})
    config["client_id"] = client_id
    config["client_secret"] = client_secret
    if parent_database_id:
        config["parent_database_id"] = parent_database_id
    await get_pool().execute(
        """
        INSERT INTO platform_integrations
            (platform, credentials_kind, redirect_uri, config, status)
        VALUES ('notion', 'oauth', $1, $2, 'pending')
        ON CONFLICT (platform) DO UPDATE
            SET redirect_uri     = EXCLUDED.redirect_uri,
                config           = EXCLUDED.config,
                credentials_kind = 'oauth',
                updated_at       = now()
        """,
        redirect_uri, json.dumps(config),
    )


async def exchange_code(code: str) -> dict:
    """Exchange OAuth code for access token using credentials stored in DB."""
    creds = await get_app_credentials()
    client_id = creds.get("client_id", "")
    client_secret = creds.get("client_secret", "")
    redirect_uri = creds.get("redirect_uri", "")

    if not client_id or not client_secret:
        raise ValueError("Notion app credentials not configured — fill in client ID and secret on /admin/integrations")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _NOTION_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=(client_id, client_secret),
            headers={"Notion-Version": "2022-06-28"},
        )
        _log.info("Notion token exchange: status=%s body=%s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()


async def upsert_integration(token_data: dict) -> None:
    """Store the OAuth access token and workspace info in platform_integrations."""
    workspace_id = token_data.get("workspace_id", "")
    bot_id = token_data.get("bot_id", "")
    access_token = token_data.get("access_token", "")

    await get_pool().execute(
        """
        UPDATE platform_integrations
           SET workspace_id     = $1,
               bot_external_id  = $2,
               credentials_data = $3,
               status           = 'active',
               updated_at       = now()
         WHERE platform = 'notion'
        """,
        workspace_id, bot_id, access_token,
    )
    _log.info("Notion integration updated: workspace=%s bot=%s", workspace_id, bot_id)
