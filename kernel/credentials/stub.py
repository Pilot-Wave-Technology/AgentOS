"""Ec4d — a provider that mints nothing real, so the path is testable.

WHAT THIS IS FOR. Every part of the credential road except the mint
itself is ordinary software: authorising against grants, writing a lease
file with the right mode, handing the sandbox a pointer, revoking on
terminal state, refusing an artifact that holds a secret. All of it can
be wrong, and none of it needs a cloud to be wrong in.

So the stub exists to let that road be walked end to end on a laptop,
before the system holds anything worth stealing. The first real token
should be the LAST step of this build, running code every other part of
which has already been exercised.

THE TOKEN IS DELIBERATELY SHAPED LIKE A REAL ONE -- `ya29.` and long --
because the leak guards match on shape. A stub token that looked like
`fake-token` would sail past `holds_secret` and the guards would be
proven by nothing. It is equally deliberately marked STUB in the middle,
so a stub token appearing in a real request fails loudly at the API
rather than looking like an expiry.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging

from kernel.credentials.provider import DEFAULT_TTL_S, MintFailed

logger = logging.getLogger(__name__)

# Long enough to match the `ya29.` shape the scrubber looks for, and
# unmistakable to a person reading a log.
_MARK = "STUB-NOT-A-REAL-CREDENTIAL"


class StubProvider:
    """Mints a deterministic fake. Never contacts anything."""

    name = "stub"

    async def mint(self, identity: dict, execution_id: int,
                   ttl_s: int = DEFAULT_TTL_S) -> dict:
        sa = str((identity or {}).get("account") or "").strip()
        if not sa:
            # The same refusal the real provider gives, so a caller that
            # mishandles it is caught here rather than in production.
            raise MintFailed(
                "E_NO_IDENTITY",
                "mint called with no service account; `granted_identity` "
                "returns None when nothing is granted and that means do "
                "not mint, not mint anyway")

        # Deterministic per (execution, account): a re-mint inside one run
        # returns the same value, so a test can assert the lease was not
        # rewritten without reasoning about clocks.
        seed = hashlib.sha256(
            f"{execution_id}:{sa}".encode()).hexdigest()[:32]
        expires = (_dt.datetime.now(_dt.timezone.utc)
                   + _dt.timedelta(seconds=ttl_s))
        logger.info("stub credential: minted for %s (execution %s, %ss)",
                    sa, execution_id, ttl_s)
        return {
            "token": f"ya29.{_MARK}-{seed}",
            "expires_at": expires.isoformat(),
            "account": sa,
            "scope": (identity or {}).get("scope", ""),
            "provider": self.name,
        }

    def lease_payload(self, minted: dict, identity: dict) -> dict:
        """Shaped like the real thing, because a stub whose lease looked
        different would exercise a different code path than the one that
        matters."""
        return {"type": "authorized_user",
                "access_token": minted["token"],
                "expires_at": minted["expires_at"],
                "project_id": minted.get("scope", "")}

    def sandbox_pointers(self, lease_container_path: str) -> dict:
        return {"GOOGLE_APPLICATION_CREDENTIALS": lease_container_path}

    async def revoke(self, handle: dict) -> bool:
        """Nothing to revoke. Honest about it, like the real one.

        The lease FILE is deleted by the caller and that is what actually
        stops the run using it; neither provider can recall a token that
        has already been handed out.
        """
        logger.info("stub credential: revoke is a no-op (execution %s)",
                    (handle or {}).get("execution_id"))
        return False


def is_stub_token(token: str) -> bool:
    """Did this come from the stub?

    Used by the guard that stops a stub token being treated as real. It
    is cheaper to detect here than to debug an authentication failure
    against a live API.
    """
    return _MARK in str(token or "")
