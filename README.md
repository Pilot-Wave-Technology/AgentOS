# AgentOS

**AgentOS is an open-source platform for running long-lived software-development goals with a fleet of LLM agents — with a human in the loop at every decision point.**

You give it a goal. An orchestrator agent (the **GO**) proposes **stories**. A person accepts one. An **assistant** scopes it with you and starts it, which creates an **execution** — one attempt at running that story — in **its own pod** on the execution plane, with its own workspace and no cloud credentials. Inside the pod an executor works in **legs** (each one conversation with a model, starting from an empty context and leaving a brief for the next) until it claims it is finished. The work comes back as **a branch and a pull request you merge**. Every state change is written to an append-only, hash-chained ledger, so nothing is lost and everything is traceable.

```
Goal
 ├─ GO (orchestrator) ── proposes stories from the goal + team resources
 ├─ Story ── a piece of work a person accepts
 │   └─ Execution ── one attempt at running it, in its own pod (GKE)
 │        └─ Legs ── one model conversation each, briefing the next
 │             └─ → a branch and a pull request you merge
 └─ Assistant ── per-(goal, user), persistent memory; scopes and starts stories
```

The full walk-through — goal, story, execution, leg, and where each one lives — is [`HOW_A_STORY_EXECUTES.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/HOW_A_STORY_EXECUTES.md) on the `agent_os_platform` branch.

Beyond the core loop, AgentOS runs **councils** (Goal Review, Security, Platform Research), a deterministic **git dependency-graph engine** for cross-goal repo work, **custom tools** admins define through the UI (HTTP / OpenAPI / TypeScript / MCP — live in under a minute, no redeploy), and a **kill switch** to pause or force-kill work at system or goal scope.

Agents run on **Claude Opus 5.5**, **Claude Sonnet 5.5**, and **Gemini 3.8 Flash**, each offered at 100K, 200K, 500K, and 1M context windows. The current release is **v0.2.0** (see `CHANGELOG.md` on `agent_os_platform`).

---

## Demos

*Both videos were recorded on v0.1 — the UI has changed since.*

| Manage AgentOS (recorded on v0.1 — the UI has changed) | AgentOS — how to use (recorded on v0.1 — the UI has changed) |
|---|---|
| [![Manage AgentOS](https://img.youtube.com/vi/aKoZoHrSEeQ/hqdefault.jpg)](https://www.youtube.com/watch?v=aKoZoHrSEeQ) | [![AgentOS — how to use](https://img.youtube.com/vi/hFO1hyO3150/hqdefault.jpg)](https://www.youtube.com/watch?v=hFO1hyO3150) |

---

## How the repository is laid out

AgentOS is **one repository with three branches**. There is no application code on `main` — each branch is self-contained code-wise (no shared code on `main`). Deploy order is strict, though: the gateway stands up first, then the control plane (see [Deploying](#deploying)).

| Branch | Role | Stack | Deploy |
|---|---|---|---|
| **`main`** ← you are here | Front door: license, this overview, the deployment map, and the packaged VS Code extension (`extension/`) | — | — |
| **`agent_os_openclaw`** | **Gateway** — the agent runtime that actually executes the work; creates the shared VPC and archive bucket | Node.js / TypeScript (derived from OpenClaw) | **Stage ①** (first) |
| **`agent_os_platform`** | **Control plane, kernel, and execution plane** — goals, stories, executions, memory, credentials, approvals, integrations, the web UI, and the GKE cluster the work runs on | Python 3.12 / FastAPI / PostgreSQL | **Stage ②** |

The control plane and gateway talk over a VPC-internal WebSocket: the control plane creates and destroys the conversational agents (orchestrator, assistants) and sends them messages; the gateway hosts them and streams responses back. Story work itself runs in executor pods on the platform's GKE execution plane. **Neither branch is useful without the other**, and the gateway must be deployed first because the control plane reuses its VPC and archive bucket. (Service accounts and most secrets — LLM keys, gateway token — are created by the platform branch's `prerequisites.sh` before either stage; the database secrets are created by the platform's Terraform.)

### What's inside each branch

**`agent_os_openclaw`** (gateway)
- `src/`, `apps/` — the OpenClaw gateway source (built on the VM)
- `extensions/` — the AgentOS plugins: `model-registry`, `misc-models`, `custom-tools`, `insights`, `workspace-ops`, plus upstream `memory-core`
- `infra/hooks/`, `scripts/wrappers/` — Claude Code hooks and the CLI wrappers agents use to call back to the control plane
- `scripts/gateway-provision.sh` — one script that provisions the VM on boot and on redeploy
- `terraform/` — the VM, VPC, reserved internal address, archive bucket, and firewalls (it reads the secrets; it creates none)
- `deploy/` — the Stage ① deployment guide

**`agent_os_platform`** (control plane)
- `app/` — the FastAPI application (routers, services, gateway client, ledger, channels)
- `kernel/` — the kernel plane: the conductor (admission, dispatch, leases, reconciliation) and the executor harness that runs inside each pod, plus the verifier
- `profiles/` — executor profiles (TOML + prompt + optional skills) that define what an executor may do
- `migrations/` + `seed/` — schema migrations and the built-in agent/template/tool/skill seed
- `terraform/` — five Cloud Run services (web, council, git, management, scheduler), Cloud SQL, the image registry, VPC connector, buckets, and the GKE (Standard) execution-plane cluster
- `prerequisites.sh` — one-time setup: service accounts, IAM, and every non-database secret
- `preflight.sh` — checks that an installation is actually provisioned; run it between deploy stages
- `publish-extension.sh` — publishes the VS Code extension (the `.vsix` in `main`'s `extension/` folder) for your installation
- `cloudbuild.yaml` — build → migrate → deploy pipeline
- `deploy/` — the step-by-step deployment guide (both stages, in order)

---

## Deploying

Deployment is a guided, ordered process across both branches. **Start with [`DEPLOYMENT.md`](DEPLOYMENT.md)** for the map, then follow the detailed, step-by-step guide in each branch:

- **Stage ① — gateway:** [`agent_os_openclaw/deploy/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_openclaw/deploy/README.md)
- **Stage ② — control plane:** [`agent_os_platform/deploy/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/README.md)

You'll need a Google Cloud project (billing + owner), a GitHub org + App, a Firebase project, and LLM API keys (Anthropic + Gemini). Everything deploys to Google Cloud (Cloud Run, Cloud Build, Cloud SQL, GKE, a single gateway VM).

**Expect ~2 hours** for a first-time setup. Running cost is roughly **$25+/day idle**, before any LLM usage — you're paying for one always-on gateway VM, Cloud SQL, the always-on Cloud Run workers, and the GKE cluster — and more while agents are working.

Each organization runs AgentOS from its **own fork** of this repository. The Stage ① / Stage ② links above point at `github.com/Pilot-Wave-Technology/AgentOS`, the canonical upstream; if you're working from a fork, browse the same paths on your own fork.

---

## License

Apache License 2.0 — see [`LICENSE`](LICENSE). Copyright 2026 Port33, inc.
