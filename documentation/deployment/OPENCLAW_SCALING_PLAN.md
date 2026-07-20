# Openclaw scaling plan

> Goal: take the openclaw gateway from a single VM to a horizontally-scalable
> fleet on GCP, with per-organisation deployment that is one-touch via Terraform
> and elastic without disrupting in-flight work.

---

## Context

Today we run one openclaw VM (`openclaw-v1-instance`) fronting all GO, Assistant,
and IA traffic from the Cloud Run control panel. As the user count grows we
hit predictable ceilings:

- ~50 concurrent WebSocket sessions before V8 event-loop latency degrades
  streaming.
- ~6–8 concurrent `claude-cli` subprocesses before CPU saturates.
- ~2 GB of accumulated workspace data before manual disk hygiene starts.

We want to:
1. scale beyond those ceilings without rewriting the fork,
2. make per-organisation setup one Terraform invocation,
3. make adding/removing capacity boring — no impact on in-flight chats.

The codebase audit (see `c:/Project collaborator all/openclaw_fork_03122026/`)
established that the fork already supports horizontal scaling structurally:

- Listen port is configurable (`OPENCLAW_GATEWAY_PORT` env, `--port` flag, or
  `gateway.port` in `openclaw.json`).
- State directory is configurable (`OPENCLAW_STATE_DIR`).
- Per-session lockfiles with PID validation already exist
  (`src/agents/session-write-lock.ts`).
- No global pidfiles, port singletons, or singleton patterns that block
  multiple processes from coexisting.

So this plan is mostly **infrastructure**: how we deploy the fork, route to it,
and operate it. Almost no fork code changes.

---

## Architecture

### Deployment shape (per organisation)

```
                    terraform apply -var="org=acme" -var="vm_count=2"
                                    │
                                    ▼
        ┌──────────────────────────────────────────────────────┐
        │ Per-org GCP project (or per-org subnet in shared VPC)│
        │                                                      │
        │   ┌─────────────────────────────────────┐           │
        │   │ Regional MIG (multi-zone)           │           │
        │   │   instance template:                │           │
        │   │     - boot image: openclaw-vNNN     │           │
        │   │     - startup script: start.sh      │           │
        │   │     - SA: cloudsql + secret-mgr     │           │
        │   │     - tag: openclaw-gateway         │           │
        │   │   size: vm_count                    │           │
        │   │                                     │           │
        │   │  ┌──────┐ ┌──────┐ ┌──────┐        │           │
        │   │  │ vm-A │ │ vm-B │ │ vm-C │ …      │           │
        │   │  └──────┘ └──────┘ └──────┘        │           │
        │   └─────────────────────────────────────┘           │
        │                                                      │
        │      Each VM: 4 gateway processes,                  │
        │              local FS workspaces,                    │
        │              local FS git worktrees,                 │
        │              local FS session JSONs.                 │
        │                                                      │
        │      Boot script:                                    │
        │       - pull config bucket (openclaw.json + IA      │
        │         template files)                              │
        │       - start N gateway processes on consecutive    │
        │         ports                                        │
        │       - each process self-registers in Cloud SQL    │
        │                                                      │
        │   ┌─────────────────────────────────────┐           │
        │   │ Cloud SQL — multi_agent_acp         │           │
        │   │   gateway_endpoints                 │           │
        │   │   threads.gateway_endpoint          │           │
        │   │   stories.gateway_endpoint          │           │
        │   └─────────────────────────────────────┘           │
        │                  ▲                                   │
        └──────────────────┼───────────────────────────────────┘
                           │
                  ┌────────┴──────────┐
                  │ control panel     │
                  │ (Cloud Run)       │
                  │ reads endpoint    │
                  │ pool from Postgres│
                  └───────────────────┘
```

### Why no shared filesystem

We considered Filestore (NFS) and GCS via gcsfuse for shared workspace
storage. Walking through what state actually needs cross-VM access:

| State | Lifetime | Cross-VM access? |
|---|---|---|
| GO workspace (per goal) | Long | No — single thread, sticky-routed |
| Assistant workspace (per user×goal) | Long | No — single thread, sticky-routed |
| IA template workspace | Long, read-mostly | Replicated at boot from config bucket |
| IA instance workspace (per task) | Short | No — task lives entirely on one VM |
| Git worktree (per story) | Story-length | No, IF all story agents land on same VM |
| Session JSONs | Per-thread | No — sticky-routed |
| `openclaw.json` config | Long | Pulled from config bucket on boot |

The only thing that genuinely needs co-location is the per-story git worktree —
multiple agents (planner, dev-ia, test-ia, review-ia) on the same story all
need the same worktree. Solved by hashing on `story_id`, not by sharing storage.

Local-FS workspaces with the right routing key give us everything Filestore
would, without the ~$200/month base cost or the gcsfuse POSIX-semantics
question.

### Routing model

Two hash strategies based on what the thread is doing:

| Thread type | Hash key | Reasoning |
|---|---|---|
| GO chat | `(goal_id)` | One persistent thread per goal |
| Assistant chat | `(goal_id, user_id)` | One persistent thread per pairing |
| IA task chain | `story_id` | All agents on a story share the worktree |

Hash function: **sticky-by-record consistent assignment**, not modulo-N.

```sql
-- Persisted on first thread send
threads.gateway_endpoint TEXT NULL    -- e.g. 'ws://10.0.0.5:18789'
stories.gateway_endpoint TEXT NULL    -- assigned at story creation

-- Endpoint registry
CREATE TABLE gateway_endpoints (
  url       TEXT PRIMARY KEY,
  status    TEXT NOT NULL,            -- 'active' | 'draining' | 'down'
  last_seen TIMESTAMPTZ DEFAULT now(),
  added_at  TIMESTAMPTZ DEFAULT now()
);
```

Routing logic (control panel side):
1. If `<thread/story>.gateway_endpoint` is set AND that endpoint is `active`
   in the registry, use it.
2. Otherwise, pick a random `active` endpoint (or hash-consistent if you want
   deterministic placement) and persist it.
3. If the persisted endpoint becomes `down`, on next send: clear the column
   and re-assign. The user perceives this as "retry." In-flight work is lost
   regardless.

Why sticky-by-record over consistent hashing:
- Adding a VM doesn't reshuffle anything — existing rows keep their
  endpoint. Only new threads/stories see the new pool.
- Removing a VM is just `UPDATE gateway_endpoints SET status='draining'`
  — existing assignments stay valid until the work completes.
- Zero-disruption scaling.

### Multi-process per VM

A `c3-standard-16` (16 vCPU) VM runs 4 gateway processes on consecutive
ports (18789–18792). Each process is single-threaded V8, so 4 processes give
us 4× the event-loop concurrency on one box. The remaining 12 cores absorb
`claude-cli` subprocesses, network handlers, and OS overhead.

Per the audit, the fork already supports this:
- `OPENCLAW_GATEWAY_PORT=<n>` env var per process.
- `OPENCLAW_STATE_DIR` shared across all processes on the VM (they coordinate
  via existing per-session lockfiles for the rare cross-process write
  contention).
- `browser.cdpPortRangeStart` should be staggered (e.g. 18800, 18820, 18840,
  18860) per process to avoid race on first-free-port allocation. Likely
  irrelevant for our workload (no browser tooling) but worth setting.

### Service discovery via Postgres

Every gateway process self-registers on startup:

```sql
INSERT INTO gateway_endpoints (url, status, last_seen)
VALUES ($1, 'active', now())
ON CONFLICT (url) DO UPDATE
  SET status='active', last_seen=now();
```

Heartbeat every 30 seconds: `UPDATE gateway_endpoints SET last_seen=now()
WHERE url=$1`. The control panel filters out endpoints with
`last_seen < now() - interval '2 minutes'` from its routing table. Stale rows
get garbage-collected by a nightly cron.

On graceful shutdown (SIGTERM):
```sql
UPDATE gateway_endpoints SET status='draining' WHERE url=$1;
-- wait for active threads/stories assigned to this endpoint to finish
DELETE FROM gateway_endpoints WHERE url=$1;
```

This is service-discovery without standing up Service Directory or Consul.
Postgres is already in the data path; one small table.

---

## What gets deployed

### The Terraform module

`modules/openclaw-fleet/`:

```hcl
module "openclaw_fleet" {
  source            = "./modules/openclaw-fleet"

  project_id        = "acme-prod"
  region            = "${GCP_REGION}"
  vm_count          = 2
  machine_type      = "c3-standard-16"
  processes_per_vm  = 4
  fork_image        = "openclaw-2026-04-30-abc123"   # output of packer build

  cloudsql_instance = module.control_panel.cloudsql_instance
  config_bucket     = "gs://openclaw-config-acme"
}

output "control_panel_env" {
  value = {
    OPENCLAW_AUTH_TOKEN = module.openclaw_fleet.auth_token
    # No OPENCLAW_WS_URL — the control panel discovers endpoints
    # from gateway_endpoints table.
  }
}
```

What it provisions:
- Subnet (or reuses existing).
- Service account (`roles/cloudsql.client`, `roles/secretmanager.secretAccessor`,
  `roles/storage.objectViewer` for the config bucket).
- Instance template referencing the pre-baked image and a startup script.
- Regional MIG of size `vm_count` with `surge_size=1, max_unavailable=0`.
- Firewall rule allowing the control panel's serverless VPC connector to reach
  the gateway port range (18789–18799) on the MIG tag.
- Cloud Monitoring alert policy on instance health.

### The boot image

Packer build (`packer/openclaw.pkr.hcl`):
- Base: `debian-12` or COS.
- Bake: Node.js 20, the openclaw fork (`/opt/openclaw/`), `jq`, `gcsfuse` (only
  if we ever revisit GCS), and the boot script (`/opt/openclaw/start.sh`).
- Tag with build date + commit sha → `openclaw-2026-04-30-abc123`.

### The boot script (`/opt/openclaw/start.sh`)

```bash
#!/bin/bash
set -e

# 1. Pull org-specific config (idempotent — overwrites local copy)
gsutil rsync -r gs://${CONFIG_BUCKET}/state/ /var/openclaw/state/
mkdir -p /var/openclaw/state

# 2. Discover own IP from instance metadata
MY_IP=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip)

export OPENCLAW_STATE_DIR=/var/openclaw/state
export OPENCLAW_REGISTRY_DSN="$(gcloud secrets versions access latest \
  --secret=cloudsql-dsn)"

# 3. Start N gateway processes
for i in $(seq 0 $((PROCESSES_PER_VM - 1))); do
  PORT=$((18789 + i))
  CDP_START=$((18800 + i*20))

  OPENCLAW_GATEWAY_PORT=$PORT \
  OPENCLAW_PUBLIC_URL=ws://${MY_IP}:${PORT} \
  OPENCLAW_BROWSER_CDP_PORT_RANGE_START=$CDP_START \
    /opt/openclaw/run-gateway.sh \
    > /var/log/openclaw-${PORT}.log 2>&1 &
done

# 4. Trap SIGTERM for graceful drain (delegated to a wrapper script)
trap 'kill -TERM $(jobs -p); wait' SIGTERM

wait
```

`run-gateway.sh` is a thin wrapper that:
1. Calls the fork: `node /opt/openclaw/openclaw.mjs gateway run`.
2. On boot, registers `OPENCLAW_PUBLIC_URL` into `gateway_endpoints`.
3. On SIGTERM, marks the row `draining`, waits for active threads to finish
   (capped at `DRAIN_TIMEOUT_SECONDS=1800`), then deletes the row.

The registration + drain logic is the only fork-side patch we need (~50 lines).

### Config bucket layout (`gs://openclaw-config-${org}/`)

```
state/
  openclaw.json                       # canonical allowlist + defaults
  workspace-planner-ia/               # IA template workspaces
    SOUL.md
    AGENTS.md
    BOOTSTRAP.md
  workspace-dev-ia/
    SOUL.md
    AGENTS.md
    BOOTSTRAP.md
  workspace-test-ia/
    ...
  workspace-review-ia/
    ...
```

Updates: `gsutil cp` to the bucket → next time any VM boots (or a manual
rolling restart via `gcloud compute instance-groups managed rolling-action
restart`), the new config is pulled.

For inline updates (e.g. allowlist change for a new model), either:
- Run rolling restart (~5 min).
- Set `OPENCLAW_CONFIG_CACHE_MS=0` and `gsutil cp` — every gateway process
  re-reads on the next request, no restart needed.

---

## Day-2 operations

| Action | How |
|---|---|
| Deploy openclaw for a new org | `terraform apply` (5–10 min) |
| Add capacity | Bump `vm_count`, `terraform apply`. New VMs join automatically. |
| Remove capacity | Lower `vm_count`, `terraform apply`. MIG sends SIGTERM, drain happens. |
| Update fork code | Rebuild image (`packer build`), bump `fork_image` var, apply. MIG does rolling replace (drain → new VM). |
| Update allowlist (add a model) | Edit `openclaw.json` in config bucket, `gsutil cp` it back. |
| Inspect fleet health | `SELECT url, status, last_seen FROM gateway_endpoints WHERE last_seen > now() - interval '2 minutes';` |
| Inspect distribution | `SELECT gateway_endpoint, count(*) FROM threads GROUP BY 1; SELECT gateway_endpoint, count(*) FROM stories GROUP BY 1;` |
| Drain a single VM | Set status='draining' on its endpoints in the registry, wait, then `gcloud compute instances delete`. |

### Adding capacity (no in-flight impact)

```
1. terraform apply -var="vm_count=4"
   → MIG provisions vm-3, vm-4
2. each new VM boots, runs start.sh, pulls config, starts 4 gateway processes
3. each gateway process inserts into gateway_endpoints (status='active')
4. control panel sees 8 → 16 endpoints on next 30s refresh
5. existing threads/stories: unchanged, still pinned to their original endpoints
6. new threads/stories: distributed across all 16 endpoints
```

### Removing capacity (drain-then-terminate)

```
1. terraform apply -var="vm_count=2"
   → MIG selects vm-3, vm-4 for termination, sends SIGTERM
2. each gateway process on those VMs:
   a. UPDATE gateway_endpoints SET status='draining' WHERE url=...
   b. waits for assigned threads/stories to finish (cap: 30 min)
   c. DELETE FROM gateway_endpoints WHERE url=...
   d. exits cleanly
3. MIG terminates the VM
```

If the drain timeout fires, in-progress threads on the dying VM are killed.
Their subprocesses (`claude-cli`) die with the host. Control panel surfaces
this as an error; user retries; new thread gets re-hashed to a surviving VM.

### Replacing a VM (image bump or OS patch)

```
1. packer build → openclaw-2026-05-15-def456
2. terraform apply -var="fork_image=openclaw-2026-05-15-def456"
3. MIG rolling-replaces VMs one at a time:
   - surge=1: provision new VM first
   - new VM boots, registers endpoints
   - control panel sees both old + new endpoints active
   - SIGTERM old VM → drain → delete
   - move to next
4. zero capacity dip; bounded thread loss (only if drain times out)
```

### Self-healing

MIG default behaviour. Health check (HTTP GET on the gateway's `/health`
endpoint) fails for 3 consecutive 30-second checks → instance marked unhealthy
→ MIG terminates and replaces. Threads on the dead VM are lost (their
subprocesses too). Re-route happens on user retry.

---

## What changes in the control panel

Small, focused changes:

1. **`gateway_endpoints` table** + heartbeat-driven endpoint pool helper in
   `app/openclaw.py`. Reads endpoints with `last_seen > now() - 2min` and
   `status = 'active'`.

2. **Routing layer in `app/openclaw.py`** — extends `_AgentSession` keys to
   include the resolved endpoint URL. New helper:
   ```python
   async def _route_for_thread(thread_id: str) -> str:
       row = await pool.fetchrow("""
           SELECT t.gateway_endpoint, ge.status
             FROM threads t
             LEFT JOIN gateway_endpoints ge ON ge.url = t.gateway_endpoint
            WHERE t.id = $1
       """, thread_id)
       if row and row["gateway_endpoint"] and row["status"] == "active":
           return row["gateway_endpoint"]
       return await _assign_endpoint(thread_id)
   ```

3. **Routing for stories** — `_route_for_story` parallel helper, used when an
   IA task chain starts under a story. All subsequent agent.create calls for
   that story's task chain go to the same endpoint.

4. **Migration `025_gateway_endpoints.sql`**:
   ```sql
   CREATE TABLE gateway_endpoints (
     url       TEXT PRIMARY KEY,
     status    TEXT NOT NULL CHECK (status IN ('active','draining','down')),
     last_seen TIMESTAMPTZ DEFAULT now(),
     added_at  TIMESTAMPTZ DEFAULT now()
   );

   ALTER TABLE threads  ADD COLUMN gateway_endpoint TEXT;
   ALTER TABLE stories  ADD COLUMN gateway_endpoint TEXT;

   CREATE INDEX ON threads  (gateway_endpoint) WHERE gateway_endpoint IS NOT NULL;
   CREATE INDEX ON stories  (gateway_endpoint) WHERE gateway_endpoint IS NOT NULL;
   ```

5. **No change to `OPENCLAW_WS_URL`** — that env var goes away. Endpoint pool
   is now Postgres-driven.

Total control-panel changes: ~half a day.

---

## What changes in the openclaw fork

Single small patch (~50 lines):

1. **Self-registration on startup.** Read `OPENCLAW_REGISTRY_DSN` env var. On
   gateway boot, INSERT into `gateway_endpoints`. Start a heartbeat task that
   updates `last_seen` every 30 seconds.

2. **Graceful drain on SIGTERM.** Trap SIGTERM in the gateway entry point
   (extend the existing shutdown path). Mark endpoint `draining`, refuse new
   `agents.create` / new chat runs that don't already have state on this
   process, wait for active runs to complete, then DELETE the endpoint and
   exit.

3. **Optional: `/health` HTTP endpoint** for MIG health checks. The fork already
   has an HTTP server on the same multiplexed port — adding `GET /health`
   returning `200 OK` is trivial.

We could fork-patch directly, but cleaner to wrap in `run-gateway.sh` which
adds the registration / heartbeat / drain logic externally and only signals
the inner Node process. That way the fork stays pristine.

---

## Phasing

### Phase 1 — single-VM hardening (1 week)

Doesn't actually scale yet, but lays the foundations:

- Migration `025_gateway_endpoints.sql`.
- Add the `_route_for_thread` / `_route_for_story` helpers (return a single
  hardcoded endpoint for now).
- Wire the threads.gateway_endpoint and stories.gateway_endpoint columns
  end-to-end.
- Build the registration wrapper script for the fork.
- Re-deploy the existing single VM with the wrapper script. Confirm
  registration + heartbeat work end-to-end.

End state: same single VM, but the control panel reads the endpoint from
Postgres instead of a hardcoded env var. Verifies the routing plumbing
without the multi-VM complexity.

### Phase 2 — multi-process per VM (3–5 days)

- Update the boot script to start 4 gateway processes on consecutive ports.
- Stagger CDP port ranges.
- Verify per-session lockfile coordination across 4 processes on the same
  state dir.
- Confirm capacity gain via load test (chat throughput, claude-cli
  concurrency).

End state: one VM, 4× the per-VM concurrency. Workload distribution still
limited to one VM.

### Phase 3 — multi-VM via MIG (1 week)

- Packer image build pipeline.
- Terraform module (`modules/openclaw-fleet/`).
- Config bucket layout + `gsutil rsync` boot step.
- Regional MIG with rolling-replace strategy.
- Health check endpoint + MIG auto-heal.
- Drain-on-SIGTERM fork patch (or wrapper).

End state: production-grade per-org deployment. `terraform apply` provisions
everything; `vm_count` resize is the scaling lever.

### Phase 4 — observability (3–5 days)

- Per-endpoint metrics (active threads, claude-cli subprocess count, request
  latency, 429 rate per provider) → Cloud Monitoring custom metrics.
- Looker Studio dashboard pulling from `gateway_endpoints` + custom metrics.
- Alert policies on:
  - Endpoint heartbeat staleness > 2 min.
  - Per-endpoint thread count > 80% of capacity.
  - 429 rate sustained > 5/min.
  - MIG instance unhealthy > 5 min.

End state: you know the system is healthy without SSHing.

**Total: ~3–4 weeks** of focused work to go from "single hand-managed VM"
to "one-touch elastic fleet per organisation."

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Story-level pinning creates hotspots when one story fans out 10+ IA tasks | Medium for our workload | Multi-process per VM gives 4-way intra-story parallelism. Beyond that, accept queueing. |
| VM dies mid-story → worktree gone | Low (GCE SLA 99.5%) | PROGRESS.md is committed back to remote at task milestones. Worst case: re-run current task. |
| `claude-cli` subprocess pile-up CPU-saturates a VM | Medium with growth | Per-VM semaphore on concurrent CLI spawns. Tune via `MAX_CLI_PER_PROCESS` env. |
| Scale-down drain timeout kills in-flight stories | Low if drain timeout = 30 min | Set drain timeout high enough for typical story length. Schedule scale-down at low-traffic hours. |
| New VM boots before config bucket is up to date | Low | `gsutil rsync` is in start.sh — fails the boot if bucket is unreachable. MIG retries. |
| Postgres `gateway_endpoints` becomes a routing bottleneck | Low (small table, indexed) | Cache the endpoint list in control-panel memory with 30s TTL. |
| Fork update breaks production | Low (Packer artifacts are deterministic) | Canary one VM with the new image first; verify; then rolling-update the rest. |
| Stale endpoints accumulate in the registry | Certain over time | Nightly cron deletes rows with `last_seen < now() - interval '1 day'`. |

---

## What this plan does NOT cover

- **`pwt-gateway` Cloud Run rewrite.** That's the longer-term plan in
  `AGENT_RUNTIME_PLAN.md`. After this scaling work, the urgency to migrate off
  the fork drops significantly. Revisit when there's a reason beyond
  "horizontal scale" — e.g. wanting Cloud Run autoscaling instead of explicit
  `vm_count`, or wanting to drop the VM operational surface entirely.
- **Multi-region active-active.** A single regional MIG is one region. For
  multi-region, you'd run separate MIGs per region with a routing layer
  upstream. Out of scope for V1.
- **Per-customer hardware isolation.** Today, "per organisation" means a
  separate Terraform invocation, separate MIG, separate Filestore-or-not,
  separate Cloud SQL. If two customers share a project for cost reasons,
  isolation comes from the org boundary in the database, not from the fleet
  topology. Real hardware isolation = separate GCP projects per customer.
- **Cost-optimised tiering** (e.g. spot VMs for IA workers, on-demand for
  GO/Assistant). Possible add-on; not in scope here.

---

## Critical files

**Existing code that defines the contract — read before writing any V1 code.**

- `c:/Project collaborator all/multi_agent_control_panel/app/openclaw.py` —
  every method the control panel calls. New routing layer plugs into here.
- `c:/Project collaborator all/multi_agent_control_panel/app/services/agent_driver.py`
  — `ensure_go_session` / `ensure_shca_session` callers. Must work with
  Postgres-discovered endpoints.
- `c:/Project collaborator all/multi_agent_control_panel/app/llm_models.py` —
  whitelist (already done, no change).
- `c:/Project collaborator all/openclaw_fork_03122026/src/cli/gateway-cli/run.ts`
  — port + arg parsing.
- `c:/Project collaborator all/openclaw_fork_03122026/src/config/paths.ts` —
  state dir + port resolution.
- `c:/Project collaborator all/openclaw_fork_03122026/src/agents/session-write-lock.ts`
  — confirms cross-process locking design (already correct for our use).
- `c:/Project collaborator all/openclaw_fork_03122026/src/gateway/server-startup.ts`
  — where to add registration + drain hooks.

**New code to write:**

- `multi_agent_control_panel/migrations/025_gateway_endpoints.sql`.
- `multi_agent_control_panel/app/gateway_routing.py` (or extend `openclaw.py`)
  — endpoint pool reader, routing helpers, sticky-by-record assignment.
- `infra/openclaw-fleet/` — Terraform module.
- `infra/packer/openclaw.pkr.hcl` — image build.
- `infra/openclaw-fleet/scripts/start.sh` — VM boot script.
- `infra/openclaw-fleet/scripts/run-gateway.sh` — registration/drain wrapper
  around the fork.

---

## Verification (per phase)

**Phase 1.** Run today's traffic against the new endpoint-routed control panel
on the existing single VM. Confirm chats stream, transcripts persist,
`gateway_endpoints` shows one row with healthy `last_seen`. Goals don't
notice anything different.

**Phase 2.** With 4 processes on the existing VM, run a load test that opens
80 concurrent Assistant chats. All should stream cleanly. Per-process CPU usage
should distribute roughly evenly. Check `gateway_endpoints` shows 4 rows.

**Phase 3.** Provision a second org via Terraform end-to-end. Open chats in
both orgs. Verify they don't see each other's threads. Resize one org's MIG
from 2 to 3 VMs; verify new VM joins the registry and gets new threads.
Resize back to 2; verify drain works and no in-flight chats die unexpectedly.

**Phase 4.** Dashboard panels populate within 5 minutes of a new metric being
emitted. Alerts fire correctly when an endpoint's heartbeat goes stale.

---

## Cost ceiling (per-org, illustrative)

For an org running 2 VMs × 4 processes (8 endpoints, ~400 concurrent threads
capacity):

- 2 × `c3-standard-16`: ~$200/month
- Cloud SQL: shared with control panel, marginal cost ~$50/month
- Config bucket: <$1/month
- Egress to LLM providers: usage-based, dominates everything else
- **Infra fixed cost: ~$250/month per org.**

Variable cost is provider tokens; that's its own line and orthogonal to the
scaling architecture.

---

## Open questions to resolve before Phase 3

1. **Multi-tenant or single-tenant Postgres?** If multi-tenant, the
   `gateway_endpoints` table needs an `org_id` column and the routing query
   filters on it. If single-tenant (one Cloud SQL per org), no change.
2. **Browser CDP port range** — confirm whether any of our agents actually use
   the browser tooling. If yes, validate stagger doesn't collide. If no, the
   CDP setting is moot.
3. **Drain timeout** — pick a value. Conservative: 30 min. Aggressive: 5 min.
   We'd accept some lost work for faster scale-down.
4. **Health check endpoint** — does the fork already expose one, or do we add
   it? If adding, where in the WS+HTTP multiplex?

These can be answered during Phase 1 implementation.

---

## Summary

The fork is structurally ready for horizontal scale — the audit confirmed no
fork code changes are required for the core mechanics. The plan delivers:

- One-touch per-org deployment via Terraform + MIG.
- Local-FS workspaces (no Filestore tax).
- Self-registering gateways via Postgres.
- Sticky-by-record routing on threads and stories.
- Multi-process per VM for density.
- Drain-on-SIGTERM for boring scale-down.
- ~3–4 weeks of focused work, four phases.

Net architectural change: the gateway stops being "a place" and becomes
"a fleet that registers itself." Adding a VM is `terraform apply`. Removing
one is `terraform apply`. Nothing in the chat experience changes; everything
in the operations experience does.
