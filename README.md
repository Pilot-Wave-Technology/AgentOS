# AgentOS

**AgentOS is an open-source platform for running long-lived software-development goals with a fleet of LLM agents — with a human in the loop at every decision point.**

You give it a goal. A Goal Orchestrator breaks it into **stories**; stories break into **tasks**; tasks are dispatched to task-scoped agents, reviewed by reviewer agents, and held at a human approval gate before anything lands. A per-user Assistant with persistent memory surfaces the plan and answers questions along the way. You drive all of it from a web UI or Slack. Every state change is written to an append-only, hash-chained ledger, so nothing is lost and everything is traceable.

```
Goal
 ├─ GO Agent (Goal Orchestrator) ── proposes stories from the goal + team resources
 ├─ Stories   [proposed → accepted → in_progress → blocked → done]      ← humans accept/reject/rewrite
 │   └─ Tasks [pending → ready → in_progress → automated_review → awaiting_approval → done]
 │        └─ IA (Independent Agent) ── task-scoped; output checked by an RA before the human gate
 └─ Assistant ── per-(goal, user), 3-scope persistent memory; surfaces the plan, handles approvals
```

Beyond the core loop, AgentOS runs **councils** (Goal Review, Security, Platform Research), a deterministic **git dependency-graph engine** for cross-goal repo work, **custom tools** admins define through the UI (HTTP / OpenAPI / TypeScript / MCP — live in under a minute, no redeploy), and a **kill switch** to pause or force-kill work at system or goal scope.

---

## Demos

| Manage AgentOS | AgentOS — how to use |
|---|---|
| [![Manage AgentOS](https://img.youtube.com/vi/aKoZoHrSEeQ/hqdefault.jpg)](https://www.youtube.com/watch?v=aKoZoHrSEeQ) | [![AgentOS — how to use](https://img.youtube.com/vi/hFO1hyO3150/hqdefault.jpg)](https://www.youtube.com/watch?v=hFO1hyO3150) |

---

## How the repository is laid out

AgentOS is **one repository with three branches**. There is no application code on `main` — each branch is self-contained code-wise (no shared code on `main`). Deploy order is strict, though: the gateway stands up first, then the control plane (see [Deploying](#deploying)).

| Branch | Role | Stack | Deploy |
|---|---|---|---|
| **`main`** ← you are here | Front door: license, this overview, and the deployment map | — | — |
| **`agent_os_openclaw`** | **Gateway** — the agent runtime that actually executes the work; creates the shared VPC and archive bucket | Node.js / TypeScript (derived from OpenClaw) | **Stage ①** (first) |
| **`agent_os_platform`** | **Control plane** — goals, stories, tasks, memory, credentials, approvals, integrations, and the web UI | Python 3.12 / FastAPI / PostgreSQL | **Stage ②** |

The control plane and gateway talk over a VPC-internal WebSocket: the control plane creates and destroys agent instances and sends messages; the gateway hosts the agents and streams responses back. **Neither is useful without the other**, and the gateway must be deployed first because the control plane reuses its VPC and archive bucket. (The secrets themselves — LLM keys, gateway token, DB DSNs — are owned by neither branch; `prerequisites.sh` creates them all before either stage, and both stages read them.)

### What's inside each branch

**`agent_os_openclaw`** (gateway)
- `src/`, `apps/`, `extensions/` — the OpenClaw gateway source (built on the VM)
- `infra/config/`, `infra/hooks/` — synthetic model registry, CLI wrappers, Claude Code hooks
- `scripts/gateway-provision.sh` — one script that provisions the VM on boot and on redeploy
- `terraform/` — the VM, VPC, archive bucket, firewall, and the secrets it reads
- `deploy/` — the Stage ① deployment guide

**`agent_os_platform`** (control plane)
- `app/` — the FastAPI application (routers, services, task controller, gateway client, ledger, channels)
- `migrations/` + `seed/` — schema migrations and the built-in agent/template/tool/skill seed
- `terraform/` — Cloud Run (split **or** single profile), VPC connector, buckets, secret wiring
- `prerequisites.sh` — one-time setup: service accounts, IAM, all secrets, and the Cloud SQL instance
- `cloudbuild.yaml` — build → migrate → deploy pipeline
- `deploy/` — the Stage ② deployment guide

---

## Deploying

Deployment is a guided, ordered process across both branches. **Start with [`DEPLOYMENT.md`](DEPLOYMENT.md)** for the map, then follow the detailed, step-by-step guide in each branch:

- **Stage ① — gateway:** [`agent_os_openclaw/deploy/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_openclaw/deploy/README.md)
- **Stage ② — control plane:** [`agent_os_platform/deploy/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/README.md)

You'll need a Google Cloud project (billing + owner), a GitHub org + App, a Firebase project, and LLM API keys (Anthropic + Gemini). Everything deploys to Google Cloud (Cloud Run, Cloud Build, Cloud SQL, a single gateway VM).

**Expect ~2 hours** for a first-time setup. Running cost is a few dollars/day when idle and more while agents are working — you're paying for one always-on gateway VM plus Cloud SQL and Cloud Run.

Each organization runs AgentOS from its **own fork** of this repository. The Stage ① / Stage ② links above point at `github.com/Pilot-Wave-Technology/AgentOS`, the canonical upstream; if you're working from a fork, browse the same paths on your own fork.

---

## License

Apache License 2.0 — see [`LICENSE`](LICENSE). Copyright 2026 Port33, inc.
