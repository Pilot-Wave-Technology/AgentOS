# Architecture

This document covers this branch's own additions on top of upstream OpenClaw:
how the gateway connects to the AgentOS control plane, what the fork's own code
does, and how the deployment is put together.

It does **not** re-document OpenClaw itself — for the gateway's own internals
(agent loop, sessions, channels, plugin system), see the upstream docs under
[`documentation/`](documentation/), starting with
[`documentation/concepts/agent-loop.md`](documentation/concepts/agent-loop.md)
and
[`documentation/gateway/configuration-reference.md`](documentation/gateway/configuration-reference.md).

This branch (`agent_os_openclaw`) is a fork of
[OpenClaw](https://github.com/openclaw/openclaw) (MIT-licensed, © the upstream
authors). The AgentOS-specific additions on top — the extensions, the core
gateway patches, and the deployment code — are © Port33, inc. under the Apache
License 2.0. See [`NOTICE.md`](NOTICE.md) for the exact split.

---

## Table of contents

- [One repo, two branches](#one-repo-two-branches)
- [Control plane → gateway: WebSocket RPC](#control-plane--gateway-websocket-rpc)
- [Gateway → control plane: HTTP callbacks](#gateway--control-plane-http-callbacks)
- [Gateway-side extensions](#gateway-side-extensions)
- [Core gateway patches](#core-gateway-patches)
- [Deployment topology](#deployment-topology)
- [Repository layout](#repository-layout)
- [Further reading](#further-reading)

---

## One repo, two branches

AgentOS ships as a single repository —
[`Pilot-Wave-Technology/AgentOS`](https://github.com/Pilot-Wave-Technology/AgentOS)
— with two long-lived branches that deploy in order:

| Branch | Role | Language |
|---|---|---|
| **`agent_os_openclaw`** ← *this branch* | **Gateway** — the runtime that executes the AI agents: agent runtime, workspace management, LLM calls, custom-tool execution | Node.js / TypeScript |
| `agent_os_platform` | **Control plane** — goals, stories, executions, memory, platform integrations, and the UI that drives the gateway | Python / FastAPI / PostgreSQL |

This is **Stage ① of a deployment: the gateway** — the process that actually
runs the agents. Deploy it first; the control plane reuses its VPC and secrets
and connects to it.

The gateway runs as a single OpenClaw process (as the unprivileged `openclaw`
user) on a Google Cloud Compute Engine VM. The control plane reaches the VM
over a VPC-internal path restricted to private ranges. Two independent channels
connect the two sides — one initiated by the control plane, one initiated by the
gateway/agents.

## Control plane → gateway: WebSocket RPC

The control plane opens a WebSocket connection to the gateway on port `18789`
and authenticates with a pre-shared bearer token (`OPENCLAW_GATEWAY_TOKEN`).
This is OpenClaw's own native gateway RPC surface — nothing in this fork
implements or wraps it. The control plane drives the gateway with calls such as:

- `agents.create` / `agents.delete` — spin up or tear down an agent instance
- `agents.files.set` — write workspace files (e.g. `SOUL.md`, `RESOURCES.md`, `MEMORY.md`)
- `chat.send` — send a message to an agent and stream back its response
- `tools.reload` — tell the custom-tools extension to refresh its tool cache (see below)

## Gateway → control plane: HTTP callbacks

The reverse direction — an agent reporting progress or completion back to the
control plane — does not use the WebSocket. Agents run shell commands inside
their workspace, and two thin wrapper scripts under
[`scripts/wrappers/`](scripts/wrappers/) turn those into authenticated HTTP
calls against the control plane's internal API:

| Wrapper | Purpose |
|---|---|
| `message-assistant` | Post a progress update from a task agent back to its owning assistant agent |
| `task-report` | Report a task's final status, summary, and structured result |

Both wrappers authenticate against the control plane's internal API and resolve
the calling agent's ID from its environment or from the workspace directory
name.

## Gateway-side extensions

[`extensions/`](extensions/) carries the fork-authored plugins that couple the
gateway to AgentOS. Each one is a self-contained OpenClaw plugin — the plugin
coupling itself flows through the plugin RPC interface and needs no core edits
(the fork's separate core patches are listed [below](#core-gateway-patches)):

| Plugin | Origin | Purpose |
|---|---|---|
| `custom-tools/` | Fork-authored | Loads admin-defined tools (HTTP, OpenAPI, TypeScript/Deno sandbox, MCP) from the control plane's database at runtime — no gateway restart needed. See [`extensions/custom-tools/README.md`](extensions/custom-tools/README.md) for the full lifecycle. |
| `insights/` | Fork-authored | Read-only RPCs for the control plane's dashboard — e.g. `sandbox.status` (container runtime health, active sandbox count). See [`extensions/insights/README.md`](extensions/insights/README.md). |
| `workspace-ops/` | Fork-authored | Admin RPCs for the merge/review flow — `workspace.mergePreview` (runs a preview merge in the agent's workspace) and `workspace.gitDiff` (net diff for human review before integration). See [`extensions/workspace-ops/README.md`](extensions/workspace-ops/README.md). |
| `model-registry/` | Fork-authored | Serves the agent-facing model catalog from the control plane: polls `/internal/model_catalog` and contributes those providers/models to the gateway's model config; the `models.reload` RPC is the fast path after an admin edit. See [`extensions/model-registry/README.md`](extensions/model-registry/README.md). |
| `misc-models/` | Fork-authored | Purpose-scoped model overrides for direct model calls outside the agent catalog — including the gateway's compaction summarizer: polls `/internal/misc_model_settings`; `misc-models.reload` refreshes it on demand. |
| `memory-core/` | Upstream, kept in use | Not fork-authored, but deliberately retained — it's the active memory-search backend agents use. |

`shared/` and `test-utils/` hold supporting test helpers for the fork-authored
plugins above; they aren't standalone plugins.

The fork-authored plugins above were kept after a cleanup pass that removed the
bundled upstream extensions this deployment doesn't use (channels and
integrations it doesn't need). Re-check that removal before re-enabling anything
from OpenClaw's full extension set.

## Core gateway patches

Beyond the plugin layer, this fork also carries direct patches to core gateway
`src/` for AgentOS-specific needs:

- **Workspace file + git RPCs** — the surface the control plane uses to write an
  agent's workspace files and inspect its git state, backing the story
  integration flow.
- **Memory consolidation and compaction** — tuning of how an agent's context is
  summarized and trimmed over long-running sessions.
- **Kill-switch abort RPCs** — abort surfaces the control plane calls to
  immediately stop an agent (the platform-level pause/kill switch).
- **Cost / usage tracking** — per-turn token and usage accounting surfaced back
  to the control plane.
- **Per-agent auth hardening** — scoped JWTs issued per agent (instead of a
  single shared gateway token for agent-initiated calls), plus secret scrubbing
  on values that pass through agent-visible surfaces.

These are **not** a clean, unmodified upstream tree — they're real fork patches
that must be carried forward on every upstream rebase. The standing rule for
*new* changes is to prefer consuming an existing gateway interface over patching
source (see the "Minimal surface area" principle in [`VISION.md`](VISION.md)); that rule governs future work and does not
describe the current patch set, which predates it and accumulated incrementally.
There is no single document enumerating every patch — `git log` against this
fork's divergence point from upstream is the source of truth.

## Deployment topology

Deployment is **branch-based and single-environment**: pushing to
`agent_os_openclaw` triggers a redeploy of the gateway VM. There are three
deploy artifacts, all at the repo root:

- [`terraform/`](terraform/) — a GCP VM module (`main.tf`, `variables.tf`,
  `outputs.tf`, `startup.sh.tpl`) that stands up the VM, VPC network, reserved
  internal address, firewalls, and archive bucket. It creates no secrets or
  service accounts — it **reads** the ones the platform branch's
  `prerequisites.sh` creates. Point `repo_url` in your
  `terraform.tfvars` at your fork; the VM clones it on boot.
- [`cloudbuild.yaml`](cloudbuild.yaml) — the push-to-deploy pipeline: on every
  push to your fork's branch, the VM pulls, rebuilds, and restarts.
- [`scripts/gateway-provision.sh`](scripts/gateway-provision.sh) — the single
  provisioning script (the source of truth for both first boot and redeploy).

```
first boot                          redeploy
──────────                          ────────
terraform apply                     push to agent_os_openclaw
     │                                    │
     ▼                                    ▼
VM created → startup.sh              Cloud Build (cloudbuild.yaml)
     │                                    │
     └──────────────┬─────────────────────┘
                    ▼
        scripts/gateway-provision.sh   (idempotent; converges both paths)
                    │
     pnpm install + build
       → refresh rotated secrets
       → write openclaw.json (as the openclaw user)
       → install CLI wrappers + Claude Code hooks
       → (re)start the gateway units
```

The gateway runs directly as a Node process under the unprivileged `openclaw`
user (Claude Code refuses `bypassPermissions` under root, and the agents' CLI
backend requires it). The control plane's URL and the shared gateway token are
delivered as secrets that Terraform wires up, so the gateway can be stood up
before the control plane exists and picks up its counterpart on the next
provision.

See [`deploy/README.md`](deploy/README.md) for the step-by-step deployment
guide and [`terraform/README.md`](terraform/README.md) for the module
reference. The Claude Code hooks the provisioning step installs are in
[`infra/hooks/`](infra/hooks/).

## Repository layout

```
agent_os_openclaw/
├── src/, ui/                       Upstream OpenClaw layout, with AgentOS-specific
│                                   patches layered in — see "Core gateway patches"
├── apps/, packages/                Upstream OpenClaw — unmodified
├── extensions/
│   ├── custom-tools/               Fork-authored — see above
│   ├── insights/                   Fork-authored — see above
│   ├── workspace-ops/              Fork-authored — see above
│   ├── model-registry/             Fork-authored — see above
│   ├── misc-models/                Fork-authored — see above
│   ├── memory-core/                Upstream, kept in use — see above
│   └── shared/, test-utils/        Test helpers for the fork-authored plugins
├── terraform/                      Our addition — GCP VM module (main.tf, …, startup.sh.tpl)
├── cloudbuild.yaml                 Our addition — push-to-deploy pipeline
├── scripts/
│   ├── gateway-provision.sh        Our addition — on-VM build + configure + restart
│   └── wrappers/                   Our addition — agent → control plane HTTP callbacks
├── infra/
│   └── hooks/                      Our addition — Claude Code guardrail hooks
├── documentation/                  Upstream OpenClaw docs
├── deploy/README.md                Our addition — the deployment guide
├── reset.sh                        Our addition — tears down the gateway stack
└── start-openclaw.sh               Our addition — legacy local helper; the VM does
                                    not use it (systemd runs the gateway, and
                                    provisioning is scripts/gateway-provision.sh)
```

## Further reading

- [`deploy/README.md`](deploy/README.md) — the step-by-step deployment guide (start here)
- [`terraform/README.md`](terraform/README.md) — the deployment reference
- [`extensions/custom-tools/README.md`](extensions/custom-tools/README.md), [`extensions/insights/README.md`](extensions/insights/README.md), [`extensions/workspace-ops/README.md`](extensions/workspace-ops/README.md), [`extensions/model-registry/README.md`](extensions/model-registry/README.md) — the documented fork-authored extensions (`misc-models` is described in its `openclaw.plugin.json` and above)
- [`VISION.md`](VISION.md) — this branch's principles, including the rule for changing this fork (prefer consuming existing gateway interfaces over patching source)
- The [`agent_os_platform` branch](https://github.com/Pilot-Wave-Technology/AgentOS/tree/agent_os_platform) — control-plane-side architecture and the domain model (goal / story / execution) that ultimately drives what gets sent over the WebSocket RPC described here
