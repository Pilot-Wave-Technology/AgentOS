"""Credential broker Protocol and CredentialBundle shape.

Brokers implement this Protocol to provision/revoke per-task credentials.
Each provider (GCP, AWS, Azure, ...) ships its own broker module.
"""
from __future__ import annotations

from typing import Protocol, TypedDict


class CredentialBundle(TypedDict):
    """Result of a successful provision() call.

    `kind` is the broker family (e.g. 'gcp') so the dispatcher can sanity-
    check the result against the requesting phase.

    `dispatch_block` is markdown appended to the agent's task message —
    a fully-formed `## Cloud Credentials (this task only)` section that
    explains where the creds are and how to use them. Each broker owns
    its prompt.

    `cleanup_handle` is opaque per-broker state that revoke() needs to
    tear down whatever provision() created. It's persisted on the task
    row so cleanup survives control-panel restarts.
    """
    kind: str
    dispatch_block: str
    cleanup_handle: dict


class CredentialBroker(Protocol):
    """Per-provider credential broker.

    A broker is stateless beyond config; per-task state lives in the
    CredentialBundle's cleanup_handle.

    Conventions:
      * provision() returns None when the resource is unusable (e.g.
        missing required field). The caller emits a BLOCKED message.
        The broker should NOT raise for misconfiguration; raise only
        for true infrastructure failures (IAM API down, etc.).
      * revoke() is best-effort + idempotent. It must tolerate being
        called twice, or being called when provision() partially failed.
    """

    kind: str  # 'gcp' | 'aws' | 'azure' | ...

    async def provision(
        self,
        resource_metadata: dict,
        phase: str,
        task_id: str,
        story_id: str,
        workspace_path: str,
        instance_id: str,
    ) -> CredentialBundle | None:
        """Mint credentials for this task. Returns None if the resource
        is too incomplete to use; the caller surfaces a BLOCKED message.
        """
        ...

    async def revoke(self, cleanup_handle: dict) -> None:
        """Tear down whatever provision() created. Idempotent — repeated
        calls must not error. Logs but does not raise on failure; the
        sweep watchdog catches anything that slips through.

        For token-based brokers (GCP impersonation), this typically means
        deleting the workspace ADC file. The token itself expires on its
        own; there is no static credential to revoke via an API call.
        """
        ...
