"""Generate short-lived credentials for GCP resources.

GCS: OAuth access token with devstorage.read_only scope (1 hour expiry)
BigQuery: OAuth access token with bigquery.readonly scope (1 hour expiry)

For per-task impersonation credentials (integration-tester / deployment
phases), the GCP credential broker at
`app/services/credential_brokers/gcp.py` mints ephemeral SA keys via
the IAM Keys API and writes them to the agent's workspace as ADC files.
"""
import asyncio
import logging
from typing import Optional

_log = logging.getLogger(__name__)

# Cached result of get_self_sa_email() — fetched once, reused for the
# lifetime of the process. None means "not yet fetched"; empty string
# means "fetch was attempted and failed / not on GCP".
_self_sa_email_cache: Optional[str] = None
_self_sa_email_fetched: bool = False


async def get_self_sa_email() -> str:
    """Return the service account email this process is running as.

    Priority:
      1. GCP_VM_SA_EMAIL env var — explicit override (useful in local dev or
         if the metadata server is unavailable for any reason).
      2. GCP instance metadata server — available on Cloud Run, GCE, GKE.
         Returns the default SA email with no credentials required.
      3. google.auth.default() credentials object — works on Cloud Run when
         httpx is unavailable but google-auth is present.
      4. Empty string — local dev without GCP; impersonation checks degrade
         gracefully when the caller SA email is unknown.

    Result is cached for the lifetime of the process.
    """
    global _self_sa_email_cache, _self_sa_email_fetched
    if _self_sa_email_fetched:
        return _self_sa_email_cache or ""

    # 1. Explicit env var override
    from app.config import get_settings
    env_val = get_settings().gcp_vm_sa_email
    if env_val:
        _log.info("get_self_sa_email: using GCP_VM_SA_EMAIL env var: %s", env_val)
        _self_sa_email_cache = env_val
        _self_sa_email_fetched = True
        return env_val

    # 2. GCP metadata server (no auth needed — internal endpoint only)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"},
            )
            if resp.status_code == 200:
                email = resp.text.strip()
                _log.info("get_self_sa_email: discovered from metadata server: %s", email)
                _self_sa_email_cache = email
                _self_sa_email_fetched = True
                return email
    except Exception as exc:
        _log.debug("get_self_sa_email: metadata server unavailable (%s), trying google-auth", exc)

    # 3. google.auth.default() — ServiceAccountCredentials carry the email
    try:
        import google.auth
        import google.auth.transport.requests

        def _from_creds() -> str:
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            # Refresh so the service_account_email attribute is populated
            try:
                creds.refresh(google.auth.transport.requests.Request())
            except Exception:
                pass
            return getattr(creds, "service_account_email", "") or ""

        email = await asyncio.to_thread(_from_creds)
        if email:
            _log.info("get_self_sa_email: discovered via google-auth: %s", email)
            _self_sa_email_cache = email
            _self_sa_email_fetched = True
            return email
    except Exception as exc:
        _log.debug("get_self_sa_email: google-auth path failed (%s)", exc)

    _log.warning("get_self_sa_email: could not determine SA email; impersonation checks will be skipped")
    _self_sa_email_cache = ""
    _self_sa_email_fetched = True
    return ""


async def generate_gcs_readonly_token() -> Optional[str]:
    """Generate a short-lived OAuth access token with devstorage.read_only scope.

    The agent uses this with gsutil to access any bucket the service account can read.
    Scoped to read-only — cannot upload, delete, or modify objects.

    Returns: access token string, or None on failure.
    """
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/devstorage.read_only"],
        )
        await asyncio.to_thread(
            credentials.refresh, google.auth.transport.requests.Request()
        )
        _log.info("Generated GCS readonly token")
        return credentials.token

    except Exception as exc:
        _log.warning("Failed to generate GCS readonly token: %s", exc)
        return None


async def generate_bq_readonly_token(
    gcp_project: str = "",
) -> Optional[str]:
    """Generate a short-lived OAuth access token with bigquery.readonly scope.

    Uses Application Default Credentials (the control panel's service account)
    to generate a token that can only run SELECT queries.

    Returns: access token string, or None on failure.
    """
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
        )
        await asyncio.to_thread(
            credentials.refresh, google.auth.transport.requests.Request()
        )
        _log.info("Generated BQ readonly token for project %s", gcp_project or project)
        return credentials.token

    except Exception as exc:
        _log.warning("Failed to generate BQ readonly token: %s", exc)
        return None


