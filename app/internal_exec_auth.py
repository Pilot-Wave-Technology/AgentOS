"""Proof that a caller is one particular execution, and nothing else.

WHY NOT THE SHARED INTERNAL BEARER, which every other `/internal/` route
uses. That token authorises the bearer for the whole internal surface,
and these routes are called from the execution pod's WORKER container --
which runs concurrently with the sandbox and shares its network
namespace. A secret reachable from there is a secret the model can use.

THE INIT CONTAINER IS A DIFFERENT POSITION, and that is why
`/internal/resource-object` may keep the shared bearer:
`workspace-compose` runs to completion BEFORE the sandbox starts, so
`OPENCLAW_AUTH_TOKEN` is never co-resident with model-controlled code.
The worker has no such protection, so it gets a token that proves one
thing: which run it is.

THE WORKER HAS THE ANSWER AND NOT THE KEY. The conductor derives the
token at spec build from AGENT_TOKEN_SIGNING_KEY and injects it; the
control panel holds the same key and recomputes what execution N must
present. Neither the worker nor the sandbox can mint one for another
run.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)


def bearer(request: Request) -> str:
    """The presented token, with an optional `Bearer ` prefix removed."""
    raw = request.headers.get("Authorization") or ""
    if raw[:7].lower() == "bearer ":
        return raw[7:].strip()
    return raw.strip()


def require_execution_token(request: Request, execution_id: int) -> None:
    """Refuse unless the caller holds THIS execution's workspace token."""
    presented = bearer(request)
    if not presented:
        raise HTTPException(status_code=401,
                            detail="an execution token is required")
    try:
        from kernel.workspace import podfs_auth

        expected = podfs_auth.derive(execution_id)
    except Exception as exc:                                # noqa: BLE001
        # The key is missing on THIS side, which is a deployment fault
        # and not the caller's. Saying so beats a 401 that sends someone
        # looking at the pod.
        log.exception("cannot derive the expected execution token")
        raise HTTPException(
            status_code=500,
            detail=f"this service cannot verify execution tokens: {exc}")
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=403,
            detail=f"that token does not belong to execution "
                   f"{execution_id}")
