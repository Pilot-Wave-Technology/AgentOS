"""Notion credential helpers — API key and OAuth flows.

Credentials are NEVER stored directly; only Secret Manager resource names
are persisted in platform_integrations.credentials_ref.
"""
import hashlib
import hmac
import logging
from typing import Optional

from app.database import get_pool

_log = logging.getLogger(__name__)


async def get_integration(goal_id: str) -> Optional[dict]:
    """Return Notion integration details for a goal, or None if not enabled.

    Joins platform_integrations (app-level) with goal_platform_integrations
    (goal-level opt-in) so callers get credentials + goal config in one shot.
    """
    row = await get_pool().fetchrow(
        """
        SELECT pi.id AS integration_id, pi.workspace_id, pi.credentials_kind,
               pi.credentials_data, pi.credentials_ref,
               pi.bot_external_id, pi.bot_actor_id, pi.status,
               gpi.email_domain_allowlist, gpi.enabled
        FROM platform_integrations      pi
        JOIN goal_platform_integrations gpi ON gpi.integration_id = pi.id
        WHERE pi.platform  = 'notion'
          AND pi.status    = 'active'
          AND gpi.goal_id  = $1
          AND gpi.enabled  = true
        """,
        goal_id,
    )
    return dict(row) if row else None


def verify_signature(body: bytes, headers: dict, signing_secret: str) -> bool:
    """Verify Notion webhook HMAC-SHA256 signature.

    Notion signs with X-Notion-Signature: sha256=<hex>.
    Returns False (not raises) on any mismatch — callers should 401.
    """
    sig_header = (
        headers.get("x-notion-signature")
        or headers.get("X-Notion-Signature")
        or ""
    )
    if not sig_header.startswith("sha256="):
        return False
    expected = hmac.HMAC(
        signing_secret.encode(), body, digestmod=hashlib.sha256
    ).hexdigest()
    received = sig_header[len("sha256="):]
    return hmac.compare_digest(expected, received)
