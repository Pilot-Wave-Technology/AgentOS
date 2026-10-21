"""Ec4d — minting from a namespace the sandbox is not in.

THE HOLE THIS CLOSES. The sandbox joins the worker's network namespace,
so anything the worker can reach it can reach. Minting in-process
therefore needs the worker's namespace to reach the metadata server --
and the moment it can, model-authored bash can take the HOST's identity
directly and impersonate anything IAM allows, regardless of grants. The
grant stops being the boundary.

So on a cloud deployment the shape is:

    worker + sandbox namespace   metadata BLOCKED (169.254.169.254)
    sidecar, its own namespace   metadata reachable
    worker -> sidecar            over the docker network

The sandbox can still REACH the sidecar -- that is unavoidable and
accepted. Two things stop it being useful:

  * AUTHENTICATION. The sidecar requires a shared secret that lives in
    the WORKER's environment. `_safe_env` in the sandbox is an allowlist,
    and the secret is not on it, so the sandbox has nothing to present.
  * AUTHORISATION. Even with the secret, the sidecar mints only what
    `granted_identity` returns for the execution named -- so the worst a
    stolen secret buys is the credential that execution was already
    given.

Without the first, a sandbox could name another execution and collect
its credential; that is why authorisation alone is not enough here.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from kernel.credentials.provider import DEFAULT_TTL_S, MintFailed

logger = logging.getLogger(__name__)

# B1 — fallbacks spelled out here rather than hidden in environ.get calls.
_URL_ENV = "AGENTOS_CREDENTIAL_SIDECAR_URL"
_TOKEN_ENV = "AGENTOS_CREDENTIAL_SIDECAR_TOKEN"
DEFAULT_TIMEOUT_S = 20


class SidecarProvider:
    """Asks a sidecar to mint. Holds no cloud access of its own."""

    name = "sidecar"

    async def mint(self, identity: dict, execution_id: int,
                   ttl_s: int = DEFAULT_TTL_S) -> dict:
        import asyncio

        url = (os.environ.get(_URL_ENV) or "").strip()
        token = (os.environ.get(_TOKEN_ENV) or "").strip()
        if not url:
            raise MintFailed(
                "E_NO_SIDECAR",
                f"{_URL_ENV} is not set, so there is nowhere to mint from. "
                f"On a cloud deployment the worker's namespace is blocked "
                f"from the metadata server on purpose; the sidecar is how "
                f"a token is obtained.")
        if not token:
            # REFUSED RATHER THAN TRIED. An unauthenticated sidecar is one
            # the sandbox can call, and the sandbox shares this namespace.
            raise MintFailed(
                "E_NO_SIDECAR_TOKEN",
                f"{_TOKEN_ENV} is not set. The sidecar must authenticate "
                f"its caller: the sandbox can reach it too, and the only "
                f"thing it lacks is this secret.")

        body = json.dumps({
            "execution_id": int(execution_id),
            "account": identity.get("account"),
            "cloud": identity.get("cloud"),
            "kind": identity.get("kind"),
            "resource_id": identity.get("resource_id"),
            "ttl_s": int(ttl_s),
        }).encode("utf-8")
        return await asyncio.to_thread(_post, url, token, body)

    def lease_payload(self, minted: dict, identity: dict):
        """DELEGATED TO THE CLOUD THAT MINTED IT.

        The sidecar is a transport, not a cloud: what it returns is an
        AWS triple or a GCP token depending on the grant, and the file
        format has to follow. Writing one shape here would break two
        clouds out of three.
        """
        from kernel.credentials.provider import provider_for

        return provider_for(identity["cloud"]).lease_payload(minted, identity)

    def sandbox_pointers(self, lease_container_path: str) -> dict:
        """Cannot be answered without knowing the cloud, so it is not.

        `issue.sdk_pointers` resolves pointers from the grant's cloud rather
        than from the transport, and a wrong answer here would name the
        wrong variable -- AWS tooling looking at GOOGLE_APPLICATION_
        CREDENTIALS finds nothing and reports no credentials at all.
        """
        return {}

    async def revoke(self, handle: dict) -> bool:
        """False, like every provider. The lease file deletion is the
        part that works, and the TTL is the real bound."""
        return False


def _post(url: str, token: str, body: bytes) -> dict:
    """Synchronous call. Run via `asyncio.to_thread` -- the worker's loop
    drives every other execution and must not wait on an IAM round trip."""
    request = urllib.request.Request(
        url.rstrip("/") + "/mint", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request,
                                    timeout=DEFAULT_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:                                   # noqa: BLE001
            pass
        # 401/403 is the interesting one and reads very differently from a
        # refused impersonation: it means this worker could not prove who
        # it was, not that the grant was wrong.
        raise MintFailed(
            "E_SIDECAR_REFUSED",
            f"the credential sidecar refused ({exc.code}): {detail}") from exc
    except Exception as exc:                                # noqa: BLE001
        raise MintFailed(
            "E_SIDECAR_UNREACHABLE",
            f"could not reach the credential sidecar at {url}: {exc}") from exc

    for field in ("token", "expires_at", "account"):
        if not payload.get(field):
            raise MintFailed(
                "E_SIDECAR_BAD_RESPONSE",
                f"the sidecar returned no {field}")
    payload.setdefault("provider", "sidecar")
    return payload
