"""Ec4d — authorise, mint, write the lease, hand over a pointer.

THE WHOLE ROAD IS HERE so there is one place to read it:

    granted_identity(conn, execution)      the grant is the boundary
      -> provider.mint(identity)           the worker mints, never the sandbox
        -> credentials.write_lease(...)    a file, mode 0600, under the workspace
          -> AGENTOS_CREDENTIALS + id      a POINTER, safe to print

WHAT THE SANDBOX GETS is the pointer and the lease id, never the token.
`kernel/workspace/credentials.py` explains why at length: a value in the
environment is in every child's `environ`, cannot be revoked mid-run, and
has no id to point at afterwards. The pointer has none of those
problems.

NO IDENTITY IS THE NORMAL CASE. Almost no run needs cloud reach, and
`issue` returning None must be ordinary and quiet. What must never be
quiet is a grant that exists and cannot be honoured -- that raises,
because a run which should have had an identity and silently did not
fails later, somewhere unrelated, with a permission error nobody traces
back to this decision.
"""
from __future__ import annotations

import logging

from kernel.credentials.authorize import NotAuthorised, granted_identity
from kernel.credentials.provider import DEFAULT_TTL_S, MintFailed, provider_for

logger = logging.getLogger(__name__)

# A lease id is a filename, and it names the run it belongs to so an
# orphan found on disk can be traced without opening it.
def lease_id_for(execution_id: int) -> str:
    return f"exec-{execution_id}-cloud"


async def _place(conn, execution_id: int, workspace_path: str,
                 lease_id: str, payload: dict, sidecars: dict) -> list:
    """Put the lease on the filesystem that holds the workspace."""
    import asyncio                                          # noqa: PLC0415
    import functools                                        # noqa: PLC0415

    from kernel.workspace.browse import workspace_host      # noqa: PLC0415
    from kernel.workspace.credentials import place_lease    # noqa: PLC0415
    from kernel.workspace.podfs_client import (             # noqa: PLC0415
        credentials as remote_place, tree_is_remote)

    # Reached from `runner.prepare_workspace`, which is the CONDUCTOR's
    # today -- so this is not a live failure, and it is the same wrong
    # predicate as the two that were. Corrected here rather than left for
    # whoever moves workspace preparation into the pod to rediscover.
    if not tree_is_remote():
        return place_lease(workspace_path, lease_id, payload, sidecars)
    host = await workspace_host(conn, execution_id)
    return await asyncio.to_thread(
        functools.partial(remote_place, host, workspace_path, lease_id,
                          payload, sidecars, execution_id=execution_id))


async def issue(conn, execution_id: int, workspace_path: str,
                provider_name: str, ttl_s: int = DEFAULT_TTL_S) -> dict | None:
    """Give this execution its cloud identity, or None if it has none.

    Returns `{"lease_id", "cloud", "account", "scope", "region",
    "expires_at", "granted_via", "scope_verified", "provider"}` -- note
    the absence of `token`. The token is in the file; this return value
    is safe to log, and is logged.

    Raises `NotAuthorised` or `MintFailed`. Both are loud on purpose.
    """
    identity = await granted_identity(conn, execution_id)
    if identity is None:
        return None

    provider = provider_for(provider_name)
    minted = await provider.mint(identity, execution_id, ttl_s=ttl_s)

    # A STUB TOKEN MUST NOT PASS AS REAL. Cheaper to catch here than to
    # debug an authentication failure against a live API that reads like
    # a misconfigured service account.
    from kernel.credentials.stub import is_stub_token

    if provider_name != "stub" and is_stub_token(minted.get("token", "")):
        raise MintFailed(
            "E_STUB_TOKEN_FROM_REAL_PROVIDER",
            f"provider {provider_name!r} returned a stub token; refusing "
            f"to write it as a real credential")


    # THE FORMAT IS THE PROVIDER'S. GCP wants an ADC file, AWS an INI
    # with three values, Azure a federated token -- and a single shape
    # here would have to lie about two of them.
    payload = dict(provider.lease_payload(minted, identity))
    # Provenance is ours, and goes in every cloud's lease: a file found
    # on disk should answer "whose is this and why" without a database.
    payload.update({
        "agentos_cloud": identity["cloud"],
        "agentos_account": minted["account"],
        "agentos_scope": minted.get("scope", ""),
        "agentos_execution_id": execution_id,
        "agentos_granted_via": identity["resource_id"],
        "agentos_provider": minted.get("provider", provider_name),
    })

    lease_id = lease_id_for(execution_id)

    # AND THE FILES THE TOOLING ACTUALLY READS. The lease is the record;
    # `gcloud` wants a file holding the raw token and nothing else. A
    # provider that needs none declares none, which is most of them.
    sidecars = (getattr(provider, "sidecar_files", None)
                and provider.sidecar_files(minted, identity)) or {}

    # WHERE THE WORKSPACE IS, WHICH IS NOT WHERE THIS RUNS. This wrote
    # both with plain filesystem calls; the issuer runs in the conductor
    # and on a pod substrate `/workspace` is not there, so a goal with a
    # cloud grant silently got no identity -- which the docstring above
    # calls fatal for a reason: "a run that should have had an identity
    # and silently did not fails later, somewhere unrelated, with a
    # permission error nobody traces back to this decision."
    await _place(conn, execution_id, workspace_path, lease_id, payload,
                 sidecars)

    pointers = sdk_pointers(identity["cloud"], execution_id)

    out = {"lease_id": lease_id, "cloud": identity["cloud"],
           "account": minted["account"], "scope": minted.get("scope", ""),
           "region": identity.get("region", ""),
           "expires_at": minted["expires_at"],
           "granted_via": identity["resource_id"],
           "scope_verified": identity.get("scope_verified", False),
           "pointers": pointers,
           "provider": minted.get("provider", provider_name)}
    # Safe to log: every field here is a name, not a secret.
    logger.info("execution %s: %s identity %s in %s via grant %s (%s)",
                execution_id, out["cloud"], out["account"], out["scope"],
                out["granted_via"], out["provider"])
    return out


async def release(workspace_path: str, execution_id: int,
                  provider_name: str = "") -> bool:
    """Delete the lease when the run ends. Returns whether a file went.

    THE FILE DELETION IS THE PART THAT WORKS. Neither provider can recall
    a token already handed out -- GCP has no per-token revoke, only
    disabling the account -- so the TTL is the real bound and this is
    what stops the run reading it again. Saying that plainly is better
    than a `revoke()` that returns True and means nothing.
    """
    # LOCAL ONLY, and it has no callers -- `credentials/sweep.py` is
    # the release path now ("The narrow sweep IS the release path"), and
    # it is the one that was taught to reach a pod. Kept working rather
    # than given a host it has no connection to resolve.
    from kernel.workspace.credentials import revoke as revoke_file

    gone = revoke_file(workspace_path, lease_id_for(execution_id))
    if provider_name:
        try:
            await provider_for(provider_name).revoke(
                {"execution_id": execution_id})
        except Exception:                                   # noqa: BLE001
            # Best effort by contract. The file is gone, which is the
            # part that matters; a provider that cannot revoke must not
            # turn cleanup into a failure.
            logger.exception("provider revoke failed for execution %s",
                             execution_id)
    logger.info("execution %s: lease %s", execution_id,
                "deleted" if gone else "was already gone")
    return gone


async def pointers_for(conn, execution_id: int,
                       workspace_container_path: str = "/workspace") -> dict:
    """The SDK variables this run needs, or `{}` when it holds no identity.

    RESOLVED FROM THE GRANT, not from the provider name. `sidecar` is a
    way of minting and not a cloud: asking it would name the wrong
    variables, and AWS tooling pointed at GOOGLE_APPLICATION_CREDENTIALS
    reports no credentials at all rather than an error.

    Returns `{}` rather than raising when the grant cannot be honoured.
    The refusal that matters already happened in `issue`, at workspace
    time; repeating it at dispatch would turn one clear failure into two
    unrelated-looking ones.
    """
    try:
        identity = await granted_identity(conn, execution_id)
    except Exception:                                       # noqa: BLE001
        logger.warning("execution %s: no credential pointers (not "
                       "authorised)", execution_id)
        return {}
    if identity is None:
        return {}
    return sdk_pointers(identity["cloud"], execution_id,
                        workspace_container_path)


def sdk_pointers(cloud: str, execution_id: int,
                 workspace_container_path: str = "/workspace") -> dict:
    """The file-convention variables this cloud's tooling reads.

    ONE FUNCTION, because this was briefly three: `issue` computed them
    for the lease report, `pointers_for` computed them for the sandbox
    env, and `credential_env` in sandbox_spec.py computed them for
    nobody -- and only the dead one carried the guard below. That is the
    same shape as `scrub` sitting unwired: a check that cannot run.

    POINTERS COME FROM THE CLOUD, NOT THE TRANSPORT. `sidecar` is a way
    of minting, not a cloud; asking it which variables to set would name
    the wrong ones, and AWS tooling pointed at
    GOOGLE_APPLICATION_CREDENTIALS reports no credentials at all rather
    than an error.

    THE GUARD: nothing credential-shaped goes in the environment, and a
    variable that NAMES A FILE must hold a path. A provider returning a
    VALUE (an `AWS_SECRET_ACCESS_KEY`, say) would put the secret in every
    child process's environ, where it cannot be revoked mid-run and has
    no lease id to point at -- which is the entire premise of the pointer
    model. Such a variable is dropped loudly, not passed on.

    The path half is deliberately narrow. `AWS_PROFILE=default` selects a
    SECTION inside the INI file and is not a path; requiring one of every
    variable would have dropped it and left boto reading an unnamed
    profile -- a guard breaking the thing it was added to protect.
    """
    from kernel.conductor.sandbox_spec import lease_container_path
    from kernel.workspace.credentials import holds_secret

    try:
        lease_file = lease_container_path(execution_id,
                                          workspace_container_path)
        offered = provider_for(cloud).sandbox_pointers(lease_file)
    except Exception:                                       # noqa: BLE001
        logger.exception("execution %s: could not resolve pointers for %s",
                         execution_id, cloud)
        return {}

    out = {}
    for name, value in (offered or {}).items():
        text = str(value or "")
        if not text:
            continue
        names_a_file = name.endswith(("_FILE", "_CREDENTIALS"))
        if holds_secret(text) or (names_a_file and not text.startswith("/")):
            logger.error(
                "cloud %r offered %s as a value, not a path; dropping it "
                "-- a credential in the environment cannot be revoked and "
                "has no lease to point at", cloud, name)
            continue
        out[name] = text
    return out
