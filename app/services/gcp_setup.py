"""GCP service account setup via operator's short-lived OAuth access token.

Called exclusively from the Connect-via-Google OAuth flow
(app/routers/gcp_oauth.py). Uses the IAM REST API directly with the operator's
Bearer token — no google-cloud-iam SDK dependency needed, and the token is
never persisted anywhere in the control panel.

Two operations:
  1. create_sa   — POST .../serviceAccounts  (idempotent: 409 → fetch existing)
  2. bind_token_creator — read-modify-write the SA's IAM policy to add
     roles/iam.serviceAccountTokenCreator for the Cloud Run / VM SA.

After this returns, the Cloud Run SA can call GenerateAccessToken on the new
SA, which is what GcpBroker.provision() does per task.
"""
from __future__ import annotations

import logging

import httpx

_log = logging.getLogger(__name__)

_IAM_BASE = "https://iam.googleapis.com/v1"
_TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"


class GcpSetupError(Exception):
    """Raised when the GCP IAM API returns an unrecoverable error."""


async def create_sa_and_bind(
    access_token: str,
    project_id: str,
    sa_name: str,
    cloud_run_sa: str,
) -> str:
    """Create a service account and bind tokenCreator to cloud_run_sa.

    Returns the SA email. Raises GcpSetupError on IAM API failure.
    Idempotent: if the SA already exists it is reused; the binding is
    merged into the existing IAM policy.

    cloud_run_sa is the email of the Cloud Run service account that
    will mint per-task impersonation tokens for this SA.
    """
    if not cloud_run_sa:
        raise GcpSetupError(
            "GCP_VM_SA_EMAIL is not configured. Set it in the control-panel "
            "environment so the tokenCreator binding has a principal to add."
        )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        sa_email = await _create_sa(client, headers, project_id, sa_name)
        await _bind_token_creator(client, headers, project_id, sa_email, cloud_run_sa)

    return sa_email


# ── private helpers ───────────────────────────────────────────────────────────

async def _create_sa(
    client: httpx.AsyncClient,
    headers: dict,
    project_id: str,
    sa_name: str,
) -> str:
    """POST .../serviceAccounts. Returns the SA email.

    409 Conflict → the SA already exists; fetch and return its email.
    Any other non-2xx → GcpSetupError.
    """
    url = f"{_IAM_BASE}/projects/{project_id}/serviceAccounts"
    payload = {
        "accountId": sa_name,
        "serviceAccount": {"description": "Created by AgentOS Connect-via-Google"},
    }
    resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code == 409:
        # SA exists — just resolve the email.
        sa_email = f"{sa_name}@{project_id}.iam.gserviceaccount.com"
        _log.info("_create_sa: SA %s already exists in %s, reusing", sa_name, project_id)
        get_resp = await client.get(
            f"{_IAM_BASE}/projects/{project_id}/serviceAccounts/{sa_email}",
            headers=headers,
        )
        if get_resp.status_code != 200:
            raise GcpSetupError(
                f"SA already exists but fetch failed ({get_resp.status_code}): "
                f"{_iam_message(get_resp)}"
            )
        return get_resp.json().get("email", sa_email)

    if resp.status_code not in (200, 201):
        _log.error("_create_sa: IAM create failed %d: %s", resp.status_code, resp.text[:500])
        raise GcpSetupError(
            f"Failed to create service account ({resp.status_code}): {_iam_message(resp)}"
        )

    sa_email = resp.json().get("email", f"{sa_name}@{project_id}.iam.gserviceaccount.com")
    _log.info("_create_sa: created %s", sa_email)
    return sa_email


async def _bind_token_creator(
    client: httpx.AsyncClient,
    headers: dict,
    project_id: str,
    sa_email: str,
    cloud_run_sa: str,
) -> None:
    """Merge roles/iam.serviceAccountTokenCreator for cloud_run_sa into the SA policy.

    Uses getIamPolicy → merge → setIamPolicy so existing bindings on the SA
    are preserved. The read-modify-write is not atomic, but in practice two
    operators racing to set the same binding on the same SA at the same second
    is not a realistic concern; worst case one write wins and the policy is
    still correct.
    """
    resource = f"projects/{project_id}/serviceAccounts/{sa_email}"
    member = f"serviceAccount:{cloud_run_sa}"

    # 1. Read existing policy.
    get_resp = await client.post(
        f"{_IAM_BASE}/{resource}:getIamPolicy",
        headers=headers,
        json={},
    )
    if get_resp.status_code != 200:
        raise GcpSetupError(
            f"getIamPolicy failed ({get_resp.status_code}): {_iam_message(get_resp)}"
        )

    policy = get_resp.json()
    bindings = policy.get("bindings", [])

    # 2. Merge tokenCreator binding.
    found = False
    for b in bindings:
        if b.get("role") == _TOKEN_CREATOR_ROLE:
            if member not in b.get("members", []):
                b.setdefault("members", []).append(member)
                _log.info(
                    "_bind_token_creator: added %s to existing tokenCreator binding on %s",
                    cloud_run_sa, sa_email,
                )
            else:
                _log.info(
                    "_bind_token_creator: %s already in tokenCreator binding on %s — no-op",
                    cloud_run_sa, sa_email,
                )
            found = True
            break
    if not found:
        bindings.append({"role": _TOKEN_CREATOR_ROLE, "members": [member]})
        _log.info(
            "_bind_token_creator: created new tokenCreator binding on %s for %s",
            sa_email, cloud_run_sa,
        )

    policy["bindings"] = bindings

    # 3. Write policy back.
    set_resp = await client.post(
        f"{_IAM_BASE}/{resource}:setIamPolicy",
        headers=headers,
        json={"policy": policy},
    )
    if set_resp.status_code != 200:
        raise GcpSetupError(
            f"setIamPolicy failed ({set_resp.status_code}): {_iam_message(set_resp)}"
        )

    _log.info("_bind_token_creator: tokenCreator bound on %s → %s", sa_email, cloud_run_sa)


def _iam_message(resp: httpx.Response) -> str:
    """Extract a human-readable message from a GCP IAM API error response."""
    try:
        return resp.json().get("error", {}).get("message", resp.text[:300])
    except Exception:
        return resp.text[:300]
