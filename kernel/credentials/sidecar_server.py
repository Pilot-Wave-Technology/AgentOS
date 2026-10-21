"""Ec4d — the sidecar: mints where the sandbox is not.

RUN IT IN ITS OWN NETWORK NAMESPACE, with metadata reachable, while the
worker's namespace (which the sandbox shares) has 169.254.169.254
blocked. That separation is the whole point; this process is the only
one on the host permitted to turn the host identity into a token.

IT ASSUMES ITS CALLER IS HOSTILE. The sandbox shares the worker's
namespace and can therefore reach this service. Two independent checks
stand between that and a credential:

  1. A SHARED SECRET the caller must present. It lives in the WORKER's
     environment and is NOT on `_safe_env`'s allowlist, so the sandbox
     has nothing to send. Without this, a sandbox could name another
     execution and collect its credential.
  2. AUTHORISATION AGAINST THE GRANT. Even holding the secret, a caller
     gets only what `granted_identity` returns for the execution named --
     so the worst a stolen secret buys is a credential that execution
     already had.

The request says which execution; it does NOT say which account. That is
deliberate: a caller that could name the target would be choosing its own
identity, and the grant would stop being the boundary.
"""
from __future__ import annotations

import hmac
import json
import logging
import os

logger = logging.getLogger(__name__)

TOKEN_ENV = "AGENTOS_CREDENTIAL_SIDECAR_TOKEN"
MAX_TTL_S = 3600


class SidecarRefused(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message
        super().__init__(message)


def _expected_token() -> str:
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    if not token:
        # REFUSE TO START UNAUTHENTICATED. A sidecar with no secret is
        # one the sandbox can call, and it is running in the one
        # namespace that can mint.
        raise SidecarRefused(
            500, f"{TOKEN_ENV} is not set; this service will not mint "
                 f"without a way to authenticate its caller")
    return token


def check_auth(header: str | None) -> None:
    """Constant-time comparison of the bearer token."""
    expected = _expected_token()
    presented = ""
    if header and header.lower().startswith("bearer "):
        presented = header[7:].strip()
    if not presented or not hmac.compare_digest(presented, expected):
        raise SidecarRefused(401, "bad or missing bearer token")


async def handle_mint(conn, body: dict) -> dict:
    """Authorise against the grant, then mint. Returns the token payload.

    `conn` is a database connection: authorisation is a question about
    grants, and answering it from the request body would make the caller
    the authority.
    """
    from kernel.credentials.authorize import NotAuthorised, granted_identity
    from kernel.credentials.provider import (DEFAULT_TTL_S, MintFailed,
                                             provider_for)

    try:
        execution_id = int(body.get("execution_id") or 0)
    except (TypeError, ValueError):
        raise SidecarRefused(400, "execution_id must be an integer")
    if execution_id <= 0:
        raise SidecarRefused(400, "execution_id is required")

    try:
        identity = await granted_identity(conn, execution_id)
    except NotAuthorised as refused:
        raise SidecarRefused(
            403, f"{refused.code}: {refused.message}") from refused
    if identity is None:
        raise SidecarRefused(
            403, f"execution {execution_id} is granted no cloud identity")

    # THE CALLER'S OPINION IS CHECKED, NEVER USED. If it named an account
    # at all, it must be the one the grant resolved to -- a mismatch means
    # the worker and this service disagree about who this run is, and
    # minting the grant's answer anyway would hide that.
    claimed = str(body.get("account") or body.get("sa_email") or "").strip()
    if claimed and claimed.lower() != identity["account"].lower():
        raise SidecarRefused(
            409, f"caller asked for {claimed!r}; execution {execution_id} "
                 f"is granted {identity['account']!r}")

    ttl = int(body.get("ttl_s") or DEFAULT_TTL_S)
    ttl = max(60, min(ttl, MAX_TTL_S))

    try:
        # THE CLOUD COMES FROM THE GRANT, never from the request. A
        # caller that could choose its provider could choose which cloud
        # to be authorised against.
        minted = await provider_for(identity["cloud"]).mint(
            identity, execution_id, ttl_s=ttl)
    except MintFailed as failed:
        raise SidecarRefused(
            502, f"{failed.code}: {failed.message}") from failed

    logger.info("sidecar: minted for execution %s as %s in %s (grant %s, "
                "%ss)", execution_id, identity["account"], identity["cloud"],
                identity["resource_id"], ttl)
    return minted


def error_body(exc: SidecarRefused) -> bytes:
    """What a refused caller is told. Names the reason, never the token."""
    return json.dumps({"refused": exc.message}).encode("utf-8")
