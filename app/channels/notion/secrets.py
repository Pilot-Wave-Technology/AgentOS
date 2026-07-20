"""Resolve Notion credentials.

For oauth integrations, credentials_data holds the access token directly.
For api_key integrations, credentials_ref is a Secret Manager resource name.
Never returns or logs raw secrets.
"""
import logging
from typing import Optional

_log = logging.getLogger(__name__)


async def resolve_token(credentials_kind: str, credentials_data: Optional[str], credentials_ref: Optional[str]) -> Optional[str]:
    """Return the Notion API token for an integration row. Returns None on failure."""
    if credentials_data and credentials_kind in ("oauth", "api_key"):
        return credentials_data.strip()

    if credentials_ref:
        try:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            response = client.access_secret_version(name=credentials_ref)
            return response.payload.data.decode("utf-8").strip()
        except Exception as exc:
            _log.warning("Failed to resolve Notion credentials_ref: %s", exc)

    return None
