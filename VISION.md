# Vision — Gateway branch (`agent_os_openclaw`)

This document covers **this branch's** purpose and direction: the AgentOS
gateway, the runtime that actually executes the agents. For OpenClaw's own
project vision — what OpenClaw is, its priorities, and what it will and won't
merge — see [upstream's VISION.md](https://github.com/openclaw/openclaw/blob/main/VISION.md).

AgentOS is **one repository, two branches**:

| Branch | Role |
|--------|------|
| **`agent_os_openclaw`** ← *this branch* | **Gateway** — runs the agents |
| `agent_os_platform` | **Control plane** — goals, stories, agents, and the UI that drives the gateway |

The two are deployed together and talk to each other over a VPC-internal
WebSocket. See the [repository](https://github.com/Pilot-Wave-Technology/AgentOS)
for both branches.

## Why this branch exists

AgentOS needs somewhere to *actually run* agents — call LLMs, execute tools,
manage per-agent workspaces, and hold long-running conversations. Rather than
build an agent runtime from scratch, this branch takes OpenClaw's gateway and
adapts it into a focused, deployable runtime for AgentOS. OpenClaw's messaging
channels (chat apps, voice, and the like) were removed in this fork: the
gateway's client is the AgentOS control plane, and people reach agents through
the control plane's UI and integrations.

The control plane (`agent_os_platform`) is the brain: it decides which agents
exist, what they may do, and what work is queued. This branch is the hands: it
receives that intent over the WebSocket, executes it, and reports back. Keeping
those two concerns in separate branches means the runtime can stay close to
upstream OpenClaw while the product logic evolves independently.

## Principles

**Minimal surface area over upstream.** Every line of branch-specific code is a
line that must be carried forward on every rebase against upstream OpenClaw. So
the bar for patching core source is high: prefer consuming an existing gateway
interface — an HTTP endpoint, an RPC method, a broadcast event — over modifying
it. When a patch to core source is unavoidable, keep it small, documented, and
ideally something worth contributing back upstream.

**Capability-scoped extensions.** New behavior AgentOS needs is added as
plugins with a narrow, explicit contract rather than as sprawling edits to the
gateway core. The custom-tools plugin is the model: admin-defined tools live in
the control plane's database, not in source, so adding or changing a tool is a
data change plus a live reload — no source patch, no redeploy.

**Security-hardened agent execution.** This gateway runs one process serving
many AgentOS users at once — a materially different trust model than upstream's
single-operator assumption. It runs on a private VPC, is not exposed to the
public internet, and treats agent-executed tools and subprocesses as untrusted.
Hardening the multi-tenant boundary is a standing priority, not a one-time
addition.

**Self-provisioning deploy.** The runtime should be simple to stand up and
simple to recover. A single VM clones the branch and provisions itself on boot
via [`scripts/gateway-provision.sh`](scripts/gateway-provision.sh) — build,
configure, install wrappers and hooks, start the service. Updates flow through
one push-to-rebuild pipeline ([`cloudbuild.yaml`](cloudbuild.yaml)); the
infrastructure that stands it up lives in [`terraform/`](terraform/). Deployment
is documented end-to-end in [`deploy/README.md`](deploy/README.md).

## Scope — what this branch will not do

- **Re-implement OpenClaw's product decisions.** Model routing, the plugin
  system, CLI UX — if AgentOS needs a change to that behavior, the change
  belongs upstream or should be reachable through an existing extension point.
- **Accumulate features unrelated to the AgentOS integration.** This is not a
  general-purpose OpenClaw distribution. It is the AgentOS runtime — which is
  why OpenClaw's messaging channels are gone rather than maintained here.
- **Maintain long-lived divergence in files it doesn't need to touch.** If a
  patch to core source turns out to be a genuine upstream improvement, the right
  outcome is to contribute it upstream and drop it from this branch.

## Where this is headed

- **Shrink the fork.** Continue moving branch-specific patches either into
  plugins or upstream, so each OpenClaw rebase gets cheaper over time.
- **Harden the multi-tenant boundary.** Tighten capability scoping around
  agent-executed tools and subprocesses as the workload grows.
- **Smoother provisioning and recovery.** Make first-boot provisioning and
  failure recovery increasingly hands-off, so operating the runtime stays a
  small, well-understood surface.

## Credits & license

This gateway is derived from
[OpenClaw](https://github.com/openclaw/openclaw), originally under the MIT
License (© the upstream authors). The AgentOS additions on top — provisioning,
integration, and infrastructure — are released under the **Apache License 2.0**
(© Port33, inc.); see [`LICENSE`](LICENSE). Upstream's MIT copyright and
permission notice are retained alongside it.
