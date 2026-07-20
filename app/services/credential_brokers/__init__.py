"""Credential brokers — provision cloud credentials to phase tasks.

A broker translates a goal_resource into a CredentialBundle that contains
(a) a dispatch_block of markdown injected into the agent prompt, telling
the agent how to consume the credentials, and (b) a cleanup_handle that
revoke() uses to tear them down at task end.

Brokers are selected by resource kind: a `gcp_test_sa` resource is served
by the `gcp` broker for the integration-tester phase; a `gcp_deploy_sa`
resource is served by the `gcp` broker for the deployment phase. Future
clouds add their own broker module behind the same Protocol.

See: documentation/CREDENTIAL_VM_IMPERSONATION_MIGRATION.md for the current design.
     documentation/CREDENTIAL_BROKERING_PLAN.md §4 is the historical Phase 1 (SA keys).
"""
from __future__ import annotations

import json
import logging

from app.services.credential_brokers.base import (
    CredentialBroker,
    CredentialBundle,
)
from app.services.credential_brokers.gcp import GcpBroker


_log = logging.getLogger(__name__)


# Phases that may receive injected credentials. Outside this set the
# broker layer is skipped entirely.
PHASES_NEEDING_CREDENTIALS = {"integration-tester", "deployment"}


def broker_for(kind: str) -> CredentialBroker | None:
    """Return the broker handling a goal_resource of this kind, or None
    if the kind is not a credential-bearing resource."""
    return _BY_KIND.get(kind)


def broker_for_bundle_kind(bundle_kind: str) -> CredentialBroker | None:
    """Return a broker whose `kind` attribute matches `bundle_kind`. Used
    by release_credentials() to dispatch revoke based on the bundle's
    `kind` field (e.g. 'gcp')."""
    return _BY_BUNDLE_KIND.get(bundle_kind)


async def release_credentials(task_id: str) -> bool:
    """Strict cleanup hook. Reads the task's credential_cleanup_handle,
    finds the right broker, calls revoke(), clears the handle field.

    Called from:
      - sentinel_parser when a task transitions to DONE/BLOCKED/FAILED
      - phase_dispatcher when the dispatch RPC itself errors out
      - the watchdog sweep (as a belt-and-suspenders backup)
      - task cancellation paths

    Idempotent: safe to call multiple times for the same task. Returns
    True if a cleanup was performed; False if the task had no handle
    (no credentials provisioned).
    """
    from app.database import get_pool  # noqa: PLC0415

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT credential_cleanup_handle FROM tasks WHERE id = $1",
        task_id,
    )
    if not row:
        return False

    raw = row["credential_cleanup_handle"]
    if not raw:
        return False

    # asyncpg returns JSONB as dict; only parse if a driver returns str.
    if isinstance(raw, str):
        try:
            handle = json.loads(raw)
        except (ValueError, TypeError):
            _log.warning(
                "release_credentials: malformed cleanup_handle on task %s — "
                "clearing without revoke",
                task_id,
            )
            await pool.execute(
                "UPDATE tasks SET credential_cleanup_handle = NULL, "
                "updated_at = NOW() WHERE id = $1",
                task_id,
            )
            return False
    else:
        handle = raw

    if not handle:
        return False

    # The handle's 'kind' field selects the broker. Default to 'gcp' for
    # handles written before the kind tag was standardised.
    bundle_kind = handle.get("kind") or "gcp"
    broker = broker_for_bundle_kind(bundle_kind)
    if broker is None:
        _log.warning(
            "release_credentials: no broker registered for bundle kind %r "
            "(task %s) — clearing without revoke",
            bundle_kind, task_id,
        )
        await pool.execute(
            "UPDATE tasks SET credential_cleanup_handle = NULL, "
            "updated_at = NOW() WHERE id = $1",
            task_id,
        )
        return False

    try:
        await broker.revoke(handle)
        _log.info(
            "release_credentials: revoked %s credentials for task %s",
            bundle_kind, task_id,
        )
    except Exception:
        # revoke() is documented as best-effort + idempotent and should
        # not raise — but we belt-and-suspenders log any leak. The
        # watchdog sweep catches whatever doesn't get cleaned.
        _log.exception(
            "release_credentials: broker.revoke() raised for task %s — "
            "leaving handle in place for watchdog retry",
            task_id,
        )
        return False

    await pool.execute(
        "UPDATE tasks SET credential_cleanup_handle = NULL, "
        "updated_at = NOW() WHERE id = $1",
        task_id,
    )
    return True


async def release_credentials_safe(task_id: str, caller: str) -> None:
    """Best-effort wrapper around release_credentials() — never raises.

    Use at every non-sentinel terminal transition (operator cancel,
    task_controller failure paths, review-rejection, Slack /delete, etc.)
    so the credential's IAM key + workspace file don't sit around waiting
    for the 30-min watchdog sweep.

    Idempotent: if the task has no credential_cleanup_handle (most tasks
    don't), this is a single DB lookup that returns False. Cheap to call
    everywhere.

    `caller` is a free-form label that prefixes the log lines so we can
    trace which transition fired the cleanup (e.g. "tasks_router:cancel",
    "task_controller:dispatch_retry_exhausted").
    """
    try:
        released = await release_credentials(task_id)
        if released:
            _log.info(
                "%s: released credentials for task %s", caller, task_id,
            )
    except Exception:
        _log.exception(
            "%s: release_credentials raised for task %s — watchdog will retry",
            caller, task_id,
        )


_BY_KIND: dict[str, CredentialBroker] = {
    "gcp_test_sa":   GcpBroker(role="test"),
    "gcp_deploy_sa": GcpBroker(role="deploy"),
}

# Reverse map: bundle.kind ('gcp', 'aws', ...) → broker instance. Brokers
# of the same family share an instance so revoke() doesn't need to know
# which resource kind originally produced the bundle.
_BY_BUNDLE_KIND: dict[str, CredentialBroker] = {
    "gcp": _BY_KIND["gcp_test_sa"],  # role only matters for provision(),
                                       # not revoke() — share one instance
}


__all__ = [
    "CredentialBroker",
    "CredentialBundle",
    "GcpBroker",
    "PHASES_NEEDING_CREDENTIALS",
    "broker_for",
    "broker_for_bundle_kind",
    "release_credentials",
    "release_credentials_safe",
]
