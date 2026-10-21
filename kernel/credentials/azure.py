"""Ec4d — minting real Azure credentials for a managed identity.

THE ONE THAT DOES NOT FIT THE OTHER TWO, and it is worth saying why
rather than forcing it.

GCP impersonates an account and AWS assumes a role; both take an ambient
identity and hand back a credential for a DIFFERENT principal. Azure has
no equivalent primitive. What it has is:

  * USER-ASSIGNED MANAGED IDENTITIES -- the host is assigned several and
    asks the IMDS endpoint for a token for whichever `client_id` it
    names. There is no "become another principal" step; the host either
    is assigned that identity or it is not.
  * WORKLOAD IDENTITY FEDERATION -- a projected token file is exchanged
    for one belonging to an app registration.

THE CONSEQUENCE FOR SCOPING, stated plainly because it is a real
weakness. For GCP and AWS the reachable set is a per-target permission a
person edits (a tokenCreator binding, a trust policy). For Azure with
user-assigned identities, the reachable set is "every identity assigned
to this host", and assignment is coarser -- so the grant is doing more of
the work here and IAM less. `clouds.py` already records the related fact
that a client id does not encode its subscription, so the pairing cannot
be derived either.

That is not a reason to skip Azure. It is a reason to keep the target
identities assigned to an AgentOS host few and narrow, and to know that
`scope_verified` comes back False for this cloud.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from kernel.credentials.provider import DEFAULT_TTL_S, MintFailed

logger = logging.getLogger(__name__)

# The instance metadata endpoint. Reachable only from a namespace that
# has not been cut off from link-local -- which is exactly the namespace
# the sidecar runs in and the worker's does not.
IMDS = ("http://169.254.169.254/metadata/identity/oauth2/token"
        "?api-version=2018-02-01")

# What the token is FOR. ARM is the management plane; a run that needs
# data-plane access to storage or Key Vault needs a different audience,
# and that is a per-kind decision the way OAuth scopes are for GCP.
DEFAULT_RESOURCE = "https://management.azure.com/"


class AzureProvider:
    """Fetches a token for a granted managed identity."""

    name = "azure"

    async def mint(self, identity: dict, execution_id: int,
                   ttl_s: int = DEFAULT_TTL_S) -> dict:
        client_id = str((identity or {}).get("account") or "").strip()
        if not client_id:
            raise MintFailed(
                "E_NO_IDENTITY",
                "mint called with no managed identity; `granted_identity` "
                "returns None when nothing is granted and that means do "
                "not mint, not mint anyway")

        # TTL IS NOT OURS TO SET. Azure returns a token with its own
        # lifetime and offers no duration parameter, so `ttl_s` bounds
        # the LEASE and not the token. Said here because a caller
        # reasonably assumes otherwise from the other two providers.
        token, expires = await asyncio.to_thread(
            _imds_token, client_id, DEFAULT_RESOURCE)

        logger.info("azure credential: token for %s (execution %s; lease "
                    "%ss, token lifetime is Azure's)",
                    client_id, execution_id, ttl_s)
        return {"token": token, "expires_at": expires, "account": client_id,
                "scope": identity.get("scope", ""), "provider": self.name}

    def lease_payload(self, minted: dict, identity: dict) -> dict:
        """JSON, read by our own tooling rather than by a CLI convention.

        Azure's CLI reads a token cache whose format is not contractual,
        so pretending to write one would be guessing. The token is here
        under a documented key and `AZURE_ACCESS_TOKEN_FILE` points at
        it; a task that needs `az` should exchange it explicitly.
        """
        return {"type": "azure_managed_identity",
                "access_token": minted["token"],
                "expires_at": minted["expires_at"],
                "client_id": minted.get("account", ""),
                "subscription_id": minted.get("scope", ""),
                "resource": DEFAULT_RESOURCE}

    def sandbox_pointers(self, lease_container_path: str) -> dict:
        return {"AZURE_ACCESS_TOKEN_FILE": lease_container_path}

    async def revoke(self, handle: dict) -> bool:
        """False, honestly. An issued Azure token cannot be withdrawn."""
        return False


def _imds_token(client_id: str, resource: str) -> tuple[str, str]:
    """Synchronous. Call via `asyncio.to_thread`."""
    url = f"{IMDS}&resource={urllib.parse.quote(resource)}"
    if client_id:
        url += f"&client_id={urllib.parse.quote(client_id)}"
    request = urllib.request.Request(url, headers={"Metadata": "true"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MintFailed(
            "E_IDENTITY_NOT_ASSIGNED",
            f"the metadata endpoint refused a token for {client_id} "
            f"({exc.code}). Unlike GCP and AWS there is no 'become another "
            f"principal' step: this host must have that user-assigned "
            f"managed identity ASSIGNED to it.") from exc
    except Exception as exc:                                # noqa: BLE001
        raise MintFailed(
            "E_NO_AMBIENT_IDENTITY",
            f"could not reach the Azure instance metadata endpoint, so "
            f"there is no identity to use: {exc}") from exc

    token = payload.get("access_token")
    if not token:
        raise MintFailed("E_EMPTY_TOKEN",
                         f"no access_token returned for {client_id}")
    return token, str(payload.get("expires_on") or "")

