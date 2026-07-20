"""GCP credential broker — VM-metadata impersonation per task.

How it works:

  1. The VM SA (sa-openclaw@...) holds `roles/iam.serviceAccountTokenCreator`
     on each target SA. The Cloud Run SA also holds tokenCreator on each
     target SA so the control panel can mint the initial per-task token.
     No serviceAccountKeyAdmin is required anywhere.

  2. On provision(), the broker calls iamcredentials.googleapis.com
     GenerateAccessToken for the target SA, embedding the task ID in the
     request User-Agent so GCP audit logs capture it under
     callerSuppliedUserAgent. The resulting 1-hour access token is written
     to the story workspace as a valid ADC file (story-scoped on disk).
     The dispatch block instructs the agent to:
       - export GOOGLE_APPLICATION_CREDENTIALS=<ADC path> for the Python
         and Node google-cloud-* SDKs (read natively)
       - derive CLOUDSDK_AUTH_ACCESS_TOKEN at runtime from the ADC file
         for the gcloud/gsutil CLI (in-process; no persistent gcloud
         config change)
     The literal token is NEVER interpolated into the dispatch message,
     because that message becomes positional argv to `claude -p` on the
     VM and would be globally readable via /proc/<pid>/cmdline to every
     sibling task on the same host.

  3. On revoke(), the workspace ADC file is deleted. The token expires on
     its own within 1 hour. No IAM API call is needed to revoke — there is
     no static key to delete.

  4. For emergency mid-task revocation (rare), operators call
     `iam.serviceAccounts.disable` on the target SA. All impersonations stop
     within seconds. This is the "big red button" — coarse but effective.

Audit trail:
  Every provision() emits one GCP audit log entry for GenerateAccessToken
  with callerSuppliedUserAgent = "agentos/1.0 task:<id> goal:<id> phase:<p>".
  Subsequent SDK API calls (GCS, Cloud Run, etc.) show the target SA as
  principal with no task ID — attributable via SA + timestamp window since
  each target SA is bound to at most one task at a time.

See: documentation/CREDENTIAL_VM_IMPERSONATION_MIGRATION.md §3 for the full design.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from app.services.credential_brokers.base import CredentialBundle


_log = logging.getLogger(__name__)

# Kept in sync with resource_provisioning._SA_EMAIL_RE / _GCP_PROJECT_RE.
_SA_EMAIL_RE = re.compile(
    r'^[a-zA-Z][a-zA-Z0-9\-]+@[a-zA-Z0-9\-]+\.iam\.gserviceaccount\.com$'
)
_GCP_PROJECT_RE = re.compile(r'^[a-z][a-z0-9\-]{4,28}[a-z0-9]$')

# Workspace-relative path of the ADC file.
_CREDENTIALS_PATH = ".gcp/credentials.json"

# Mode 0o600 = owner read/write only.
_CREDENTIALS_MODE = 0o600

# Access token lifetime in seconds. 1h is the default max; can be extended
# to 12h via org policy iam.allowServiceAccountCredentialLifetimeExtension
# if tasks routinely run longer. Most integration tests and deployments
# complete in under 15 minutes.
_TOKEN_LIFETIME_SECONDS = 3600

# OAuth scopes requested on the impersonated token. cloud-platform is broad
# but necessary until we have per-phase scope narrowing (deferred per plan).
_TOKEN_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class _ImpersonationError(RuntimeError):
    """Raised when GenerateAccessToken fails. Caller logs + surfaces BLOCKED."""


class GcpBroker:
    """Broker for `gcp_test_sa` / `gcp_deploy_sa` resources.

    One instance per role binding. The role distinguishes BLOCKED message
    templates and which SA-resource the dispatcher feeds in.
    """

    kind = "gcp"

    def __init__(self, role: str) -> None:
        """role: 'test' (integration-tester) or 'deploy' (deployment)"""
        if role not in ("test", "deploy"):
            raise ValueError(f"GcpBroker role must be 'test'|'deploy', got {role!r}")
        self.role = role
        self._role_label = "integration testing" if role == "test" else "deployment"

    async def provision(
        self,
        resource_metadata: dict,
        phase: str,
        task_id: str,
        story_id: str,
        workspace_path: str,
        instance_id: str,
    ) -> CredentialBundle | None:
        """Mint an impersonated access token and write it to the story
        workspace as an ADC file. Returns a CredentialBundle with the
        GOOGLE_APPLICATION_CREDENTIALS path and a cleanup_handle for revoke().

        Returns None if the resource is missing/malformed — caller emits the
        appropriate BLOCKED message.

        Returns a bundle with an error-shaped dispatch_block if the
        GenerateAccessToken call fails — agent emits PHASE_TASK_BLOCKED with
        operator guidance.
        """
        sa_email = (resource_metadata.get("sa_email") or "").strip()
        target_project = (resource_metadata.get("target_project_id") or "").strip()
        target_region = (resource_metadata.get("target_region") or "").strip()
        goal_id = (resource_metadata.get("goal_id") or "").strip()

        if target_project and not _GCP_PROJECT_RE.match(target_project):
            _log.warning(
                "gcp broker: invalid target_project_id %r for task T%s — clearing",
                target_project, task_id,
            )
            target_project = ""

        if not sa_email or not _SA_EMAIL_RE.match(sa_email):
            _log.warning(
                "gcp broker: missing/invalid sa_email %r for %s task T%s",
                sa_email, phase, task_id,
            )
            return None

        if not instance_id or not story_id:
            _log.warning(
                "gcp broker: cannot provision without instance_id+story_id "
                "(task T%s) — workspace not provisioned?",
                task_id,
            )
            return None

        _log.info(
            "gcp broker: minting %s impersonation for T%s (SA=%s, phase=%s)",
            self._role_label, task_id, sa_email, phase,
        )

        # 1. Mint an impersonated access token via GenerateAccessToken.
        #    User-Agent carries task context into GCP audit logs.
        try:
            access_token, expiry_iso = await asyncio.to_thread(
                _mint_impersonated_token,
                target_sa=sa_email,
                task_id=task_id,
                goal_id=goal_id,
                phase=phase,
            )
        except _ImpersonationError as exc:
            _log.warning(
                "gcp broker: impersonation failed for SA %s (task T%s): %s",
                sa_email, task_id, exc,
            )
            return CredentialBundle(
                kind=self.kind,
                dispatch_block=self._impersonation_failed_block(sa_email, task_id, str(exc)),
                cleanup_handle={},
            )

        # 2. Build the ADC JSON. The `authorized_user` format is the only
        #    standard ADC type that carries a pre-minted access token the
        #    SDK will use immediately without re-minting.
        #
        #    `expiry` is REQUIRED for the SDK to trust the embedded `token`.
        #    Without it, google-auth's `valid` heuristics + REFRESH_THRESHOLD
        #    (225s) trigger an immediate refresh attempt against the
        #    placeholder refresh_token/client_id and fail with `invalid_client`
        #    BEFORE any API call lands. With it set, the SDK uses `token`
        #    directly until ~3.75 min before expiry; refresh is only attempted
        #    at end-of-life and results in a clean auth error the agent
        #    surfaces as PHASE_TASK_BLOCKED (operator re-dispatches for a
        #    fresh token).
        adc = {
            "type": "authorized_user",
            "client_id": "agentos-broker",
            "client_secret": "not-used",
            "refresh_token": "not-used",
            "token": access_token,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        if target_project:
            adc["quota_project_id"] = target_project
        if expiry_iso:
            adc["expiry"] = expiry_iso

        adc_json = json.dumps(adc, indent=2)

        # 3. Push the ADC file to the VM via the admin-only writeFile RPC.
        from app.openclaw import gateway_admin, GatewayError  # noqa: PLC0415
        try:
            await gateway_admin.call(
                "shca.workspace.writeFile",
                {
                    "agentId": instance_id,
                    "storyId": story_id,
                    "path":    _CREDENTIALS_PATH,
                    "content": adc_json,
                    "mode":    _CREDENTIALS_MODE,
                },
                timeout=30,
            )
        except GatewayError as exc:
            _log.warning(
                "gcp broker: writeFile RPC failed for task T%s (SA=%s): %s",
                task_id, sa_email, exc,
            )
            return CredentialBundle(
                kind=self.kind,
                dispatch_block=self._writefile_failed_block(sa_email, task_id, str(exc)),
                cleanup_handle={},
            )

        abs_path = f"{workspace_path.rstrip('/')}/{_CREDENTIALS_PATH}"

        _log.info(
            "gcp broker: provisioned %s token for T%s (SA=%s, expires=%s)",
            self._role_label, task_id, sa_email, expiry_iso,
        )

        # access_token is intentionally NOT passed to _build_dispatch_block —
        # see the SECURITY comment in that method. The token reaches the
        # agent ONLY via the workspace ADC file (story-scoped on disk),
        # never via the dispatch message (which becomes claude-cli argv,
        # globally readable to sibling processes on the VM).
        dispatch_block = self._build_dispatch_block(
            abs_path=abs_path,
            sa_email=sa_email,
            target_project=target_project,
            target_region=target_region,
            task_id=task_id,
        )

        return CredentialBundle(
            kind=self.kind,
            dispatch_block=dispatch_block,
            cleanup_handle={
                "kind":      self.kind,
                "agent_id":  instance_id,
                "story_id":  story_id,
                "file_path": _CREDENTIALS_PATH,
                "target_sa": sa_email,   # for logging in revoke / watchdog
            },
        )

    async def revoke(self, cleanup_handle: dict) -> None:
        """Delete the workspace ADC file. The access token expires on its
        own within 1h — no IAM API call needed. Idempotent; logs but never
        raises so the watchdog can sweep missed cases safely."""
        agent_id = cleanup_handle.get("agent_id") or ""
        story_id = cleanup_handle.get("story_id") or ""
        file_path = cleanup_handle.get("file_path") or ""
        target_sa = cleanup_handle.get("target_sa") or "<unknown>"

        if not (agent_id and story_id and file_path):
            return

        from app.openclaw import gateway_admin, GatewayError  # noqa: PLC0415
        try:
            await gateway_admin.call(
                "shca.workspace.deleteFile",
                {
                    "agentId": agent_id,
                    "storyId": story_id,
                    "path":    file_path,
                },
                timeout=15,
            )
            _log.info(
                "gcp broker: cleaned up ADC file for SA=%s (task agent=%s)",
                target_sa, agent_id[:12] if agent_id else "-",
            )
        except GatewayError as exc:
            _log.warning(
                "gcp broker: deleteFile RPC failed (agent=%s story=%s path=%s): %s "
                "— token expires within 1h; watchdog will retry file cleanup",
                agent_id, story_id[:12] if story_id else "-", file_path, exc,
            )

    # ── internals: dispatch block and BLOCKED message templates ──────────

    def _build_dispatch_block(
        self,
        abs_path: str,
        sa_email: str,
        target_project: str,
        target_region: str,
        task_id: str,
    ) -> str:
        # SECURITY: the dispatch message becomes positional argv to the
        # `claude -p` subprocess (see agent_os_openclaw/src/agents/cli-spawn.ts:125).
        # Anything pasted into this string is globally readable via
        # /proc/<pid>/cmdline to every other process running as the openclaw
        # user — i.e. every sibling task on the VM. The access_token MUST
        # NOT be interpolated here. Instead the dispatch block instructs
        # the agent to derive CLOUDSDK_AUTH_ACCESS_TOKEN at runtime from
        # the workspace ADC file, which is story-scoped on disk. The file
        # path in argv leaks nothing more than the workspace structure
        # (already visible via /proc/<pid>/cwd anyway).
        project_str = f"`{target_project}`" if target_project else "(not configured — check goal resource)"
        region_str = f"`{target_region}`" if target_region else "(not configured)"
        lines = [
            f"\n## GCP Credentials (this task only)\n",
            f"**Target project:** {project_str}  ",
            f"**Target region:** {region_str}  ",
            f"**Role:** {self._role_label}  ",
            f"**Impersonated SA:** `{sa_email}`\n",
            f"An impersonated access token has been written to:\n",
            f"```\n{abs_path}\n```\n",
            f"Export ALL of the variables below before running any GCP-touching "
            f"command. `GOOGLE_APPLICATION_CREDENTIALS` covers the Python "
            f"`google-cloud-*` and Node `@google-cloud/*` SDKs. "
            f"`CLOUDSDK_AUTH_ACCESS_TOKEN` covers the `gcloud` / `gsutil` CLI — "
            f"it injects the token in-process so no persistent gcloud config "
            f"change is needed or permitted. The token value is read from the "
            f"ADC file at runtime (never from this message) — this keeps the "
            f"token out of process argv where sibling tasks on the same VM "
            f"could read it.\n",
            f"```bash",
            f'export GOOGLE_APPLICATION_CREDENTIALS="{abs_path}"',
            f'export CLOUDSDK_AUTH_ACCESS_TOKEN="$(python3 -c '
            f"'import json,sys;print(json.load(open(sys.argv[1]))[\"token\"])' "
            f'"$GOOGLE_APPLICATION_CREDENTIALS")"',
        ]
        if target_project:
            lines += [
                f'export CLOUDSDK_CORE_PROJECT="{target_project}"',
                f'export GOOGLE_CLOUD_PROJECT="{target_project}"   # Node SDK reads this',
            ]
        lines += [
            f"```\n",
            f"⚠️ **CRITICAL — do NOT run any of these commands:**\n",
            f"- `gcloud auth login`",
            f"- `gcloud auth activate-service-account`",
            f"- `gcloud config set account`",
            f"- `gcloud auth application-default login`\n",
            f"These commands permanently overwrite the VM's own service account "
            f"configuration for ALL future operations on this machine — not just "
            f"this task. The `CLOUDSDK_AUTH_ACCESS_TOKEN` export above already "
            f"makes `gcloud` / `gsutil` use the task credential in-process "
            f"without modifying any persistent config.\n",
            f"The credentials are valid for up to 1 hour and are revoked at "
            f"task end. If a 401/403 appears during a GCP call, do NOT mock, "
            f"do NOT substitute, do NOT fall back to gcloud auth — emit:\n",
            f"`PHASE_TASK_BLOCKED T{task_id} — Service account `{sa_email}` "
            f"lacks permission for <operation>. Grant the missing IAM role "
            f"and re-dispatch.`\n",
            f"All operations are recorded in GCP Cloud Audit Logs under "
            f"`{sa_email}`'s identity.",
        ]
        return "\n".join(lines)

    def _impersonation_failed_block(self, sa_email: str, task_id: str, reason: str) -> str:
        return (
            f"\n## GCP Credentials (this task only)\n\n"
            f"⚠️ Could not mint an impersonated access token for `{sa_email}`.\n"
            f"Reason: {reason}\n\n"
            f"The Cloud Run service account likely lacks "
            f"`roles/iam.serviceAccountTokenCreator` on the target SA.\n\n"
            f"Emit: `PHASE_TASK_BLOCKED T{task_id} — GCP impersonation failed. "
            f"Operator must grant serviceAccountTokenCreator on the "
            f"{self._role_label} SA to the control panel service account.`\n"
        )

    def _writefile_failed_block(self, sa_email: str, task_id: str, reason: str) -> str:
        return (
            f"\n## GCP Credentials (this task only)\n\n"
            f"⚠️ Could not write credentials file to the workspace.\n"
            f"Reason: {reason}\n\n"
            f"This is an internal infrastructure issue — the token was "
            f"minted successfully but couldn't be delivered to the runner.\n\n"
            f"Emit: `PHASE_TASK_BLOCKED T{task_id} — Could not deliver GCP "
            f"credentials to runner. Internal infrastructure issue — retry "
            f"the task.`\n"
        )


# ── IAM Credentials API helper ────────────────────────────────────────────────

def _mint_impersonated_token(
    target_sa: str,
    task_id: str,
    goal_id: str,
    phase: str,
    lifetime_seconds: int = _TOKEN_LIFETIME_SECONDS,
) -> tuple[str, str]:
    """Synchronous mint — call via asyncio.to_thread.

    Uses google.auth.impersonated_credentials so the control panel's
    Cloud Run SA impersonates `target_sa` via its tokenCreator binding.

    The User-Agent header is injected into the underlying requests.Session
    before the GenerateAccessToken call, so GCP audit logs capture the
    task context in requestMetadata.callerSuppliedUserAgent.

    Returns (access_token, expiry_iso_string).
    Raises _ImpersonationError on any failure.
    """
    try:
        import google.auth  # noqa: PLC0415
        import google.auth.impersonated_credentials  # noqa: PLC0415
        import google.auth.transport.requests  # noqa: PLC0415
    except ImportError as exc:
        raise _ImpersonationError(f"google-auth not installed: {exc}")

    try:
        source_creds, _ = google.auth.default(
            scopes=_TOKEN_SCOPES,
        )
    except Exception as exc:
        raise _ImpersonationError(f"ADC discovery failed: {exc}")

    request = google.auth.transport.requests.Request()

    # Inject task context so GCP audit log captures it.
    # request.session is a requests.Session; headers set here are sent
    # on every call through this transport, including the GenerateAccessToken
    # call that impersonated_credentials makes on .refresh().
    user_agent = f"agentos/1.0 task:{task_id} goal:{goal_id} phase:{phase}"
    request.session.headers["User-Agent"] = user_agent

    target_creds = google.auth.impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=target_sa,
        target_scopes=_TOKEN_SCOPES,
        lifetime=lifetime_seconds,
    )

    try:
        target_creds.refresh(request)
    except Exception as exc:
        raise _ImpersonationError(
            f"GenerateAccessToken failed for {target_sa}: {exc}"
        )

    if not target_creds.token:
        raise _ImpersonationError(
            f"GenerateAccessToken returned empty token for {target_sa}"
        )

    # Format as `YYYY-MM-DDTHH:MM:SSZ` (Z-suffix, no offset). google-auth's
    # `from_authorized_user_info` strips a trailing Z and parses with
    # %Y-%m-%dT%H:%M:%S; older versions reject the `+00:00` offset form.
    # `target_creds.expiry` is a naive datetime in UTC by Google convention.
    expiry_iso = ""
    if target_creds.expiry:
        expiry_iso = target_creds.expiry.replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    _log.info(
        "gcp broker: GenerateAccessToken succeeded (SA=%s, task=%s, expires=%s, "
        "user_agent=%r)",
        target_sa, task_id, expiry_iso, user_agent,
    )

    return target_creds.token, expiry_iso
