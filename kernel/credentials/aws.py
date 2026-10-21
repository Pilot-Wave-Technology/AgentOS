"""Ec4d — minting real AWS credentials by assuming a role.

NO KEYS, same as GCP and for the same reason. The host has an ambient
identity (an instance profile, an ECS task role, or an IRSA/OIDC
association) and the TARGET ROLE's trust policy names it as a permitted
principal. `sts:AssumeRole` then produces a temporary triple that expires
on its own. A long-lived access key pair would be a permanent credential
on disk to avoid one API call.

WHERE SCOPE COMES FROM, and the shape differs from GCP in one way worth
knowing. GCP's permission to impersonate is a binding ON the target
account; AWS's is a TRUST POLICY on the target role naming who may
assume it. Same effect -- the reachable set is decided outside this
system by a person -- but the thing you edit lives on the other side.

  1. the role's trust policy -- who may assume it at all;
  2. the role's own permission policies -- what it may do;
  3. the session policy passed at assume time, which can only NARROW (2);
  4. the session duration.

THREE VALUES, NOT ONE. GCP hands back a bearer token; AWS hands back an
access key id, a secret, and a session token, and every one of them is
needed. That is why `lease_payload` returns an INI rather than the ADC
JSON shape -- `AWS_SHARED_CREDENTIALS_FILE` is the file convention the
CLI and every SDK read, and it is a path, so the pointer model holds.
"""
from __future__ import annotations

import asyncio
import logging

from kernel.credentials.provider import DEFAULT_TTL_S, MintFailed

logger = logging.getLogger(__name__)

# The profile name inside the credentials file. `default` so a tool that
# was given no profile still finds it -- an executor should not have to
# know how the credential arrived.
PROFILE = "default"

# STS refuses under 900s, and the ceiling is the role's own
# MaxSessionDuration. Clamped rather than passed through so a caller
# asking for 60 gets a working session instead of a refusal about a
# number it did not choose.
MIN_DURATION_S = 900


class AwsProvider:
    """Assumes a granted role using the host's ambient identity."""

    name = "aws"

    async def mint(self, identity: dict, execution_id: int,
                   ttl_s: int = DEFAULT_TTL_S) -> dict:
        role_arn = str((identity or {}).get("account") or "").strip()
        if not role_arn:
            raise MintFailed(
                "E_NO_IDENTITY",
                "mint called with no role; `granted_identity` returns None "
                "when nothing is granted and that means do not mint, not "
                "mint anyway")

        # THE SESSION NAME IS AUDIT CORRELATION. CloudTrail records it on
        # every call the session makes, so a cloud-side event ties back to
        # the execution and the grant. AWS allows [\w+=,.@-]{2,64}.
        session = f"agentos-{execution_id}-{identity.get('resource_id', 'x')}"
        session = "".join(c for c in session if c.isalnum() or c in "+=,.@-")

        duration = max(MIN_DURATION_S, int(ttl_s))
        minted = await asyncio.to_thread(
            _assume, role_arn, session[:64], duration)

        logger.info("aws credential: assumed %s (execution %s, %ss)",
                    role_arn, execution_id, duration)
        minted.update({"account": role_arn,
                       "scope": identity.get("scope", ""),
                       "provider": self.name})
        return minted

    def lease_payload(self, minted: dict, identity: dict) -> dict | str:
        """An INI file, which is what `AWS_SHARED_CREDENTIALS_FILE` reads.

        Returned as a STRING: this is not JSON and pretending otherwise
        would mean the SDKs could not read it. `write_lease` takes either.

        The provenance `issue` adds to a dict lease cannot ride along
        here, so it goes in comments -- a file found on disk should still
        answer "whose is this and why".
        """
        return (
            f"# agentos lease -- execution {minted.get('agentos_execution')}\n"
            f"# role     {minted.get('account', '')}\n"
            f"# account  {minted.get('scope', '')}\n"
            f"# expires  {minted.get('expires_at', '')}\n"
            f"[{PROFILE}]\n"
            f"aws_access_key_id = {minted['access_key_id']}\n"
            f"aws_secret_access_key = {minted['secret_access_key']}\n"
            f"aws_session_token = {minted['session_token']}\n")

    def sandbox_pointers(self, lease_container_path: str) -> dict:
        return {"AWS_SHARED_CREDENTIALS_FILE": lease_container_path,
                "AWS_PROFILE": PROFILE}

    async def revoke(self, handle: dict) -> bool:
        """False, honestly. An STS session cannot be recalled.

        AWS can revoke by attaching a deny policy keyed on token issue
        time, which affects every session of that role -- the same
        all-or-nothing lever GCP has. The TTL and the lease deletion are
        the real bounds.
        """
        return False


def _assume(role_arn: str, session_name: str, duration_s: int) -> dict:
    """Synchronous. Call via `asyncio.to_thread` -- the worker's loop
    drives every other execution and must not wait on an STS round trip."""
    try:
        import boto3
        import botocore.exceptions as boto_errors
    except ImportError as exc:
        raise MintFailed(
            "E_NO_BOTO3",
            f"boto3 is not installed in the worker, so no AWS credential "
            f"can be minted: {exc}") from exc

    try:
        sts = boto3.client("sts")
    except Exception as exc:                                # noqa: BLE001
        raise MintFailed(
            "E_NO_AMBIENT_IDENTITY",
            f"could not create an STS client, so there is nothing to "
            f"assume FROM: {exc}") from exc

    try:
        got = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name,
                              DurationSeconds=int(duration_s))
    except boto_errors.NoCredentialsError as exc:
        # Distinguished from a refused assume, because the fixes are
        # completely different: this one is a deployment fact.
        raise MintFailed(
            "E_NO_AMBIENT_IDENTITY",
            f"this host has no AWS credentials of its own, so it cannot "
            f"assume anything: {exc}") from exc
    except Exception as exc:                                # noqa: BLE001
        raise MintFailed(
            "E_ASSUME_REFUSED",
            f"could not assume {role_arn}: {exc}. That role's TRUST POLICY "
            f"must name this host's identity as a permitted principal -- "
            f"unlike GCP, the permission lives on the target role, not on "
            f"the caller.") from exc

    creds = (got or {}).get("Credentials") or {}
    for field in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
        if not creds.get(field):
            raise MintFailed(
                "E_INCOMPLETE_SESSION",
                f"assuming {role_arn} returned no {field}; all three are "
                f"needed and a partial set fails later, opaquely")

    expiry = creds.get("Expiration")
    return {"access_key_id": creds["AccessKeyId"],
            "secret_access_key": creds["SecretAccessKey"],
            "session_token": creds["SessionToken"],
            # `token` is the seam's generic field; for AWS the session
            # token is the part that is secret-shaped and short-lived.
            "token": creds["SessionToken"],
            "expires_at": expiry.isoformat() if expiry else ""}
