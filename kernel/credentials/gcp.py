"""Ec4d — minting a real GCP token by impersonation.

NO KEYS. The host has an ambient identity (a Cloud Run or GCE service
account, discovered as ADC) and a `roles/iam.serviceAccountTokenCreator`
binding ON THE TARGET ACCOUNT. Impersonating produces a short-lived
access token for that account and nothing is ever stored that outlives
it. A service-account key file would be a permanent credential sitting
on disk to avoid a five-line API call.

WHERE SCOPE ACTUALLY COMES FROM, in order of how much it binds:

  1. the IAM binding -- the host can impersonate ONLY accounts somebody
     explicitly bound it to, per account, not per project;
  2. the target account's own roles -- what it may do in its project;
  3. the OAuth scopes requested here;
  4. the token lifetime.

Only (1) is outside this system's reach, which is the point: adding a
project is an IAM change a person makes, not a config a run can write.

THE USER-AGENT IS AUDIT CORRELATION, not decoration. GCP records
`requestMetadata.callerSuppliedUserAgent` on the GenerateAccessToken
call, so a cloud-side audit entry can be tied back to the execution and
the grant that authorised it. Without it, the audit log shows the host
identity impersonating an account and nothing about why.
"""
from __future__ import annotations

import asyncio
import logging

from kernel.credentials.provider import (DEFAULT_TTL_S, MintFailed,
                                         scopes_for)

logger = logging.getLogger(__name__)


class GcpProvider:
    """Impersonates a granted service account using the host's identity."""

    name = "gcp"

    async def mint(self, identity: dict, execution_id: int,
                   ttl_s: int = DEFAULT_TTL_S) -> dict:
        sa = str((identity or {}).get("account") or "").strip()
        if not sa:
            raise MintFailed(
                "E_NO_IDENTITY",
                "mint called with no service account; `granted_identity` "
                "returns None when nothing is granted and that means do "
                "not mint, not mint anyway")

        scopes = list(scopes_for(str(identity.get("kind") or "")))
        agent = (f"agentos/1.0 execution:{execution_id} "
                 f"grant:{identity.get('resource_id', '?')} "
                 f"project:{identity.get('scope', '?')}")
        token, expires = await asyncio.to_thread(
            _impersonate, sa, scopes, int(ttl_s), agent)

        logger.info("gcp credential: minted for %s (execution %s, %ss, "
                    "scopes=%d)", sa, execution_id, ttl_s, len(scopes))
        return {"token": token, "expires_at": expires, "account": sa,
                "scope": identity.get("scope", ""),
                "provider": self.name}

    def lease_payload(self, minted: dict, identity: dict) -> dict:
        """The RECORD: what this is, who it acts as, when it dies.

        NOT an ADC file, and it used to claim to be one. It wrote
        `{"type": "authorized_user", "access_token": ...}`, and
        google-auth refuses that shape outright -- `authorized_user`
        requires client_id, client_secret and refresh_token, so
        `google.auth.default()` raised DefaultCredentialsError and every
        run reported no credentials while holding a perfectly good
        token. There is no ADC shape for a bare access token; pretending
        otherwise produced a file that satisfied nothing.

        So this is the record, and `sidecar_files` writes the thing
        tooling actually reads.
        """
        return {"kind": "gcp_access_token",
                "access_token": minted["token"],
                "expires_at": minted["expires_at"],
                "project_id": minted.get("scope", "")}

    def sidecar_files(self, minted: dict, identity: dict) -> dict:
        """The raw token, alone in a file, because that is what gcloud
        reads (`--access-token-file` / CLOUDSDK_AUTH_ACCESS_TOKEN_FILE).

        Still a POINTER for the sandbox: a path, revocable, with a lease
        id behind it. The token is in the file, never in the
        environment."""
        return {".token": str(minted["token"])}

    def sandbox_pointers(self, lease_container_path: str) -> dict:
        """CLOUDSDK_AUTH_ACCESS_TOKEN_FILE, not
        GOOGLE_APPLICATION_CREDENTIALS.

        Pointing GOOGLE_APPLICATION_CREDENTIALS at the record made
        things WORSE than leaving it unset: google-auth finds it, fails
        to parse it, and raises -- so a library that would otherwise
        have fallen back reports a hard credential error instead.
        """
        token_file = lease_container_path[:-len(".json")] + ".token" \
            if lease_container_path.endswith(".json") \
            else lease_container_path + ".token"
        return {"CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": token_file}

    async def revoke(self, handle: dict) -> bool:
        """False, honestly.

        GCP has no per-token revoke -- the only lever is disabling the
        account, which would break every other run using it. So the TTL
        is the real bound and deleting the lease file (done by the
        caller) is what stops THIS run reading it again. Returning True
        here would claim a guarantee that does not exist.
        """
        return False


def _impersonate(target_sa: str, scopes: list, lifetime_s: int,
                 user_agent: str) -> tuple[str, str]:
    """Synchronous mint. Call via `asyncio.to_thread`.

    Split out so the blocking google-auth calls never run on the event
    loop: the worker's loop also drives every other execution, and a slow
    IAM call would stall all of them.
    """
    try:
        import google.auth
        import google.auth.impersonated_credentials as impersonated
        import google.auth.transport.requests as gtransport
    except ImportError as exc:
        raise MintFailed(
            "E_NO_GOOGLE_AUTH",
            f"google-auth is not installed in the worker, so no GCP token "
            f"can be minted: {exc}") from exc

    try:
        source, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception as exc:                                # noqa: BLE001
        # THE COMMONEST REAL FAILURE, and it must not read as a bad
        # grant. No ADC means this host has no ambient identity at all --
        # running off-cloud, or a Cloud Run service with no service
        # account -- which is a deployment fact, not a permissions one.
        raise MintFailed(
            "E_NO_AMBIENT_IDENTITY",
            f"this host has no application default credentials, so it "
            f"cannot impersonate anything: {exc}. On GCP this means the "
            f"workload has no service account; off GCP it means there is "
            f"nothing to impersonate FROM.") from exc

    request = gtransport.Request()
    # Rides every call this transport makes, including the
    # GenerateAccessToken that `refresh` triggers below.
    request.session.headers["User-Agent"] = user_agent

    creds = impersonated.Credentials(
        source_credentials=source,
        target_principal=target_sa,
        target_scopes=scopes,
        lifetime=int(lifetime_s),
    )
    try:
        creds.refresh(request)
    except Exception as exc:                                # noqa: BLE001
        # NAMES THE BINDING, because this is the failure a person will
        # actually hit and the fix is one IAM change. Without the target
        # in the message it reads as "permission denied" from a stack
        # ten frames deep.
        raise MintFailed(
            "E_IMPERSONATION_REFUSED",
            f"could not impersonate {target_sa}: {exc}. The host identity "
            f"needs roles/iam.serviceAccountTokenCreator ON THAT ACCOUNT "
            f"(not project-wide), and the account must exist and be "
            f"enabled.") from exc

    if not creds.token:
        raise MintFailed(
            "E_EMPTY_TOKEN",
            f"impersonating {target_sa} returned no token")
    expiry = creds.expiry.isoformat() if creds.expiry else ""
    return creds.token, expiry
