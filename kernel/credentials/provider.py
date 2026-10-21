"""Ec4d — what mints a token, behind one interface.

TWO IMPLEMENTATIONS AND ONE SEAM. `stub` mints a fake token and needs no
cloud, which is how the whole path -- authorise, mint, write the lease,
hand over the pointer, revoke -- is testable on a laptop. `gcp` mints a
real impersonated token and only works where an ambient identity exists.
Nothing above this line knows which is in use.

That matters more than tidiness. The first time this system holds a real
credential should be the last step, not the first, and it should exercise
code every other part of which has already been tested.

WHY THE WORKER MINTS AND NOT THE SANDBOX. The sandbox joins the worker's
network namespace, so it can reach whatever the worker can reach. If the
sandbox could mint, it could mint for any target the host identity is
permitted to impersonate, and the grant would stop being the boundary.
The worker mints for exactly the identity `authorize.granted_identity`
returned, and the sandbox receives a lease it cannot widen.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

# A minted token's life. Short on purpose: a lease is revoked when the
# run ends, and the TTL is what bounds the window when revocation does
# not happen -- a crashed worker, a killed container, a lease file
# somebody copied. GCP's floor is 5 minutes and its default is an hour;
# an hour is a long time for a credential nobody is tracking.
DEFAULT_TTL_S = 900

# OAuth scopes. `cloud-platform` is everything the impersonated account
# can do, which makes the SA's own roles the only bound. Narrower scopes
# are a second, independent bound and are worth requesting where the work
# is known -- reading logs does not need write anything.
SCOPES_BY_KIND = {
    "gcp_test_sa": ("https://www.googleapis.com/auth/cloud-platform",),
    "gcp_deploy_sa": ("https://www.googleapis.com/auth/cloud-platform",),
}
DEFAULT_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


class MintFailed(Exception):
    """The provider could not mint, and why. Never a silent None.

    A run that should have had an identity and silently did not is a run
    that fails later, somewhere unrelated, with a permission error nobody
    traces back to here.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(f"[{code}] {message}")


class Provider(Protocol):
    """Mints and revokes for one cloud."""

    name: str

    async def mint(self, identity: dict, execution_id: int,
                   ttl_s: int = DEFAULT_TTL_S) -> dict:
        """A token for `identity`, or raise `MintFailed`.

        Returns `{"token", "expires_at", "sa_email", "project_id"}`.
        `identity` is what `authorize.granted_identity` returned -- the
        provider does NOT decide who may have what, it only mints what it
        is told. Keeping the decision out of here is what lets the stub
        and the real provider be swapped without moving the boundary.
        """
        ...

    async def revoke(self, handle: dict) -> bool:
        """Best effort. The TTL is the real guarantee.

        Revocation of a short-lived cloud credential is not reliably
        possible -- GCP has no per-token revoke, AWS cannot recall an
        STS session, Azure cannot withdraw an issued token. So this
        returns False honestly rather than pretending, and the lease file
        deletion (which IS reliable) is done by the caller.
        """
        ...

    def lease_payload(self, minted: dict, identity: dict) -> dict:
        """What goes in the lease FILE, in this cloud's own format.

        Deliberately the provider's and not `issue`'s. The three clouds
        hand back different things -- GCP one bearer token, AWS a triple
        (key id, secret, session token), Azure a federated token that is
        exchanged again -- and no single shape flattens that without
        lying about one of them.
        """
        ...

    def sandbox_pointers(self, lease_container_path: str) -> dict:
        """Environment variables naming the lease file. PATHS ONLY.

        Every one of the three clouds has a file convention --
        GOOGLE_APPLICATION_CREDENTIALS, AWS_SHARED_CREDENTIALS_FILE,
        AZURE_FEDERATED_TOKEN_FILE -- which is what lets the pointer
        model hold everywhere. A provider returning a VALUE here (an
        `AWS_SECRET_ACCESS_KEY`, say) would put the secret in every child
        process's environ, where it cannot be revoked and has no id to
        point at. `issue` refuses that rather than trusting it.
        """
        ...


def scopes_for(kind: str) -> tuple:
    return SCOPES_BY_KIND.get(kind, DEFAULT_SCOPES)


def provider_for(name: str) -> Provider:
    """The provider by name. Unknown names raise rather than defaulting.

    Defaulting to the stub would be the worst possible failure: a run
    that believes it has cloud access, gets a fake token, and fails
    against the real API with an authentication error that looks like a
    misconfigured service account. Defaulting to the real one would be
    worse still.
    """
    if name == "stub":
        from kernel.credentials.stub import StubProvider

        return StubProvider()
    if name == "gcp":
        from kernel.credentials.gcp import GcpProvider

        return GcpProvider()
    if name == "aws":
        from kernel.credentials.aws import AwsProvider

        return AwsProvider()
    if name == "azure":
        from kernel.credentials.azure import AzureProvider

        return AzureProvider()
    if name == "sidecar":
        # THE CLOUD DEPLOYMENT SHAPE. `gcp` mints in-process and so needs
        # this namespace to reach the metadata server -- which the
        # sandbox shares. `sidecar` mints somewhere the sandbox is not.
        from kernel.credentials.sidecar import SidecarProvider

        return SidecarProvider()
    raise MintFailed(
        "E_NO_SUCH_PROVIDER",
        f"{name!r} is not a credential provider. Known: 'stub' (a fake "
        f"token, no cloud); 'gcp', 'aws' and 'azure' (real, and each "
        f"needs an ambient identity in THIS namespace -- which the "
        f"sandbox shares); and 'sidecar' (mints where the sandbox is "
        f"not, which is the cloud deployment shape).")
