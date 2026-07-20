# `credential_brokers/`

Pluggable layer that mints per-task cloud credentials for phase tasks
(`integration-tester`, `deployment`). The broker is invisible to the rest of
the platform — `phase_dispatcher` calls `provision()`, gets a `CredentialBundle`,
injects the `dispatch_block` into the agent prompt, and persists the
`cleanup_handle` on the task row. On terminal state, `release_credentials_safe`
calls `revoke()`.

Current implementation: **VM-metadata impersonation** (no SA keys, no static
credentials). See [`CREDENTIAL_VM_IMPERSONATION_MIGRATION.md`](../../../CREDENTIAL_VM_IMPERSONATION_MIGRATION.md) for the full design.

---

## What lives here

| File | Purpose |
|------|---------|
| `base.py` | `CredentialBroker` Protocol + `CredentialBundle` TypedDict. The contract every broker must implement. |
| `__init__.py` | Broker registry (`broker_for(kind)`, `broker_for_bundle_kind(kind)`), `PHASES_NEEDING_CREDENTIALS` set, `release_credentials` / `release_credentials_safe` cleanup helpers. |
| `gcp.py` | GCP broker. Mints a 1-hour impersonated access token via `generateAccessToken`, writes it as an `authorized_user`-format ADC file in the story workspace. Revoke deletes the file; the token expires on its own. |
| `watchdog.py` | Background sweep (30 min cadence). Finds tasks in a terminal state whose `cleanup_handle` was never revoked (e.g. control panel restarted mid-task) and calls `revoke()` to clean up the workspace ADC file. |

---

## Credential lifecycle

```
dispatch_phase_task()
  └─ _build_gcp_credential_block(task, story_id)
       ├─ resource lookup (gcp_environment + gcp_test_sa / gcp_deploy_sa)
       ├─ GcpBroker.provision(resource_metadata, phase, task_id, ...)
       │    ├─ generateAccessToken (IAM Credentials API)  ← User-Agent carries task_id
       │    ├─ writes ADC file to workspace/.gcp/credentials.json
       │    └─ returns CredentialBundle(dispatch_block, cleanup_handle)
       └─ persists cleanup_handle on tasks.credential_cleanup_handle

task reaches terminal state
  └─ release_credentials_safe(task_id, caller)
       └─ GcpBroker.revoke(cleanup_handle)
            └─ deletes workspace ADC file
            (token expires within 1h regardless)
```

---

## GCP broker contract (current)

**`provision()` side-effects:**
- One `generateAccessToken` API call to `iamcredentials.googleapis.com` with
  `User-Agent: agentos/1.0 task:<id> goal:<id> phase:<p>` — captured in GCP
  audit logs under `callerSuppliedUserAgent`.
- Creates `<workspace>/.gcp/credentials.json` (mode 0600) in the story
  workspace with the minted token in `authorized_user` ADC format.
- Persists `cleanup_handle` on the task row — shape:
  ```json
  {"kind": "gcp", "agent_id": "...", "story_id": "...",
   "file_path": "/path/.gcp/credentials.json", "target_sa": "sa@project.iam.gserviceaccount.com"}
  ```

**`revoke()` side-effects:**
- Deletes the workspace ADC file via the gateway `deleteFile` RPC.
- No IAM API call. There is no static key to revoke.
- Idempotent: file-not-found is silently ignored.

**IAM requirement:**
- Cloud Run SA (and VM SA) must hold `roles/iam.serviceAccountTokenCreator`
  **on the target SA** (not at the project level).
- No `roles/iam.serviceAccountKeyAdmin` is required or used.

---

## Conventions

- `provision()` returns `None` for misconfigured resources (missing `sa_email`,
  malformed email). The caller emits a `PHASE_TASK_BLOCKED` message.
  It raises only for infrastructure failures (IAM API down, token write failed).
- `revoke()` is idempotent and best-effort. Logs but does not raise.
  The watchdog sweep catches whatever slips through.
- `cleanup_handle` shape is broker-defined and opaque to the dispatcher.
  Always include a `"kind"` field so `broker_for_bundle_kind()` can route revoke
  to the right broker even across restarts.

---

## Adding a new cloud provider

1. Create `aws.py` (or `azure.py`, etc.) implementing the `CredentialBroker`
   Protocol from `base.py`.
2. Register it in `__init__.py`:
   ```python
   _BY_KIND["aws_test_role"]   = AwsBroker(role="test")
   _BY_KIND["aws_deploy_role"] = AwsBroker(role="deploy")
   _BY_BUNDLE_KIND["aws"]      = _BY_KIND["aws_test_role"]
   ```
3. Add the new resource kinds to `app/resource_kinds/__init__.py`.
4. The phase→kind mapping in `phase_dispatcher._build_gcp_credential_block`
   currently hardcodes `gcp_test_sa` / `gcp_deploy_sa`. A registry-driven
   phase→kind mapping (plan: P4 of the Resources overhaul) will eliminate
   the per-cloud branch.

---

## Security: cross-task isolation

The OpenClaw VM runs every claude-cli session as the same `openclaw` user.
There is no UID-level isolation between sibling tasks running on the same
host. This shapes the broker contract.

### Where credentials may NOT appear

**Never put the literal credential into the dispatch message body.** The
message becomes positional argv to `claude -p` on the VM
([`agent_os_openclaw/src/agents/cli-spawn.ts:125`](../../../../agent_os_openclaw/src/agents/cli-spawn.ts#L125)).
Anything in argv is globally readable via `/proc/<pid>/cmdline` and `ps -ef`
to every sibling agent on the host. A credential literal there leaks to
every concurrent task for the lifetime of the claude process (up to 30 min).

This is why the GCP broker writes the token to a workspace ADC file
(story-scoped on disk, mode 0600) and the dispatch block instructs the
agent to *derive* `CLOUDSDK_AUTH_ACCESS_TOKEN` at runtime from that file
rather than receive it as a literal in argv:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/.gcp/credentials.json"
export CLOUDSDK_AUTH_ACCESS_TOKEN="$(python3 -c '...' "$GOOGLE_APPLICATION_CREDENTIALS")"
```

The same rule applies to any future broker — AWS access keys, Azure SAS
tokens, database passwords, etc. Write them to a workspace file; derive
env vars in-shell.

### Residual exposure (infra-level, not closable from the broker)

| Vector | Scope | Mitigation |
|---|---|---|
| Workspace ADC file readable to other tasks running as `openclaw` | All sibling agents on the VM | Short token TTL (1h), per-task SA bounds blast radius |
| `/proc/<pid>/environ` readable during gcloud invocation | Brief window per gcloud call | Same as above |
| Claude session jsonl (`~/.claude/projects/...`) | Story-scoped per workspace path | Cleaned up with workspace |

Closing the first two would require per-task UID isolation (separate Linux
user / capability sandbox) — outside the broker layer.

### What the broker enforces today

- `revoke()` deletes the workspace ADC file at task end.
- The 30-min watchdog sweep catches missed cleanups.
- Token mint emits an audit log entry with
  `callerSuppliedUserAgent=agentos/1.0 task:<id> goal:<id> phase:<p>` —
  every downstream GCP call is attributable via SA + timestamp window
  (each target SA is bound to at most one task at a time).

### What the operator-facing prompt enforces

- The dispatch block lists forbidden gcloud commands
  (`gcloud auth login`, `gcloud auth activate-service-account`,
  `gcloud config set account`, `gcloud auth application-default login`)
  because any of these would copy the task credential into the VM's
  persistent gcloud config and break the VM's own SA identity for all
  future operations on the host.
- The integration-tester and deployment phase reminders in
  `phase_dispatcher._PHASE_REMINDERS` repeat the prohibition at the
  top of the dispatch message (defense in depth).
