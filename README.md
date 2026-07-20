# AgentOS

A multi-agent orchestration platform that breaks down long-running goals into stories and tasks, dispatches them to a fleet of LLM agents, and keeps humans in the loop at every decision point — through a web UI or Slack.

AgentOS is the **control plane**. It manages state, memory, credentials, and approvals. A companion gateway — the **`agent_os_openclaw`** branch of this repo — hosts the agent runtime and executes the work.

---

## How it works

Goals decompose into stories, stories decompose into tasks. Each layer has a dedicated agent type and a human approval gate.

```
Goal  ──────────────────────────────────────────────────────────────────
  │
  ├─ GO Agent (Goal Orchestrator)
  │    Reads the goal description and team resources, proposes stories.
  │
  ├─ Stories  [proposed → accepted → in_progress → blocked → done]
  │    Humans accept, reject, or rewrite each story before work begins.
  │
  │    └─ Tasks  [pending → ready → in_progress → automated_review → awaiting_approval → done]
  │         │
  │         └─ IA (Independent Agent)
  │              Task-scoped agent. Dispatched automatically when a task
  │              is ready. Output reviewed by an RA (Reviewer Agent)
  │              before reaching the human approval gate.
  │
  └─ Assistant
       Per-(goal, user) agent with persistent 3-scope memory (global scope spans goals).
       Surfaces the plan, answers questions, and handles approvals inline.
```

Every state transition writes to an append-only ledger (SHA-256 chained). Nothing is deleted; everything is traceable.

---

## Architecture

AgentOS ships as **two branches of this one repository**, deployed in order (gateway first):

| Branch | Role | Tech |
|---|---|---|
| **`agent_os_openclaw`** | Agent gateway — agent runtime, workspace management, LLM calls. **Deploy first** (Stage ①); it creates the shared VPC and archive bucket. (The gateway + LLM secrets are created earlier by `prerequisites.sh`.) | Node.js / TypeScript |
| **`agent_os_platform`** (this branch) | Control plane — goals, stories, tasks, memory, platform integrations (Stage ②). | Python / FastAPI / PostgreSQL |

The control plane and gateway communicate over a VPC-internal WebSocket. The control plane creates/destroys agent instances and sends messages; the gateway streams responses back. Neither is useful without the other.

### Control-plane services (split by default)

The control plane ships in a **split** deployment by default — one image, four Cloud Run services, each isolated and independently scalable:

| Service | Runs |
|---|---|
| **`agentos-web`** | Public web UI, chat, and agent callbacks (`app.main`) |
| **`agentos-council`** | Goal Review + Security councils and the research dispatcher (`app.council_worker`) |
| **`agentos-git`** | The git dependency-graph engine (`app.git_engine_worker`) |
| **`agentos-management`** | Settings / dashboard / admin backends (`app.management_worker`) |

Prefer a smaller/cheaper footprint? Set `service_profile = "single"` in `terraform.tfvars` and everything runs in **one** service (`app.single`) with the worker loops in-process — one always-on instance instead of four. Both are first-class; see [`deploy/`](deploy/).

---

## Key capabilities

**Goal-oriented orchestration** — every piece of work traces back to a goal. Dependencies between tasks are tracked and enforced; completing one task can unblock others automatically.

**Human-in-the-loop at every layer** — story acceptance, task approval, and plan revision are first-class operations, not afterthoughts. Agents pause and wait; they do not proceed without a gate.

**Persistent agent memory** — the Assistant agent builds a three-scoped memory (global/goal/conversation) with full-text search. It remembers preferences, past decisions, and conventions across sessions.

**Platform integrations** — Slack is a full operational surface. Users can create tasks, approve work, and converse with agents without opening the web UI.

**Custom tools** — admins define tools (HTTP endpoints, OpenAPI specs, TypeScript functions, MCP servers) through the web UI. Agents pick them up in under a minute — no gateway restart, no deploy.

**Kill switch** — operators can pause or force-kill work at system or goal scope. Pause is reversible; force-kill (`halted`) is terminal.

**Append-only ledger** — every state mutation is a ledger event with SHA-256 hash chaining. Projection tables are derived from events, not the other way around.

---

## Tech stack

| Layer | Technology |
|---|---|
| API framework | Python 3.12 · FastAPI · uvicorn |
| Database | PostgreSQL (asyncpg) |
| Frontend | Jinja2 + HTMX — no build step |
| Auth | Firebase (JWT verification + signup gate) |
| Agent gateway | `agent_os_openclaw` — derived from OpenClaw (WebSocket, VPC-internal) |
| LLM APIs | Anthropic Claude · Google Gemini |
| File storage | Google Cloud Storage |
| Platform integrations | Slack SDK |
| Deployment | Google Cloud Run · Cloud Build · Cloud SQL |

---

## Deploy

Deploying is a guided, ordered process — **start with [`deploy/README.md`](deploy/README.md)**. It walks you end-to-end: gather prerequisites, run the one-time setup, deploy the gateway (Stage ①), then this control plane (Stage ②).

You'll need a Google Cloud project (billing + owner), a GitHub org + App, a Firebase project, and LLM API keys (Anthropic + Gemini). The guide's first file, [`deploy/warmup.md`](deploy/warmup.md), is a checklist of everything to have ready.

Each organization runs AgentOS from its **own fork** — see [`FORKING.md`](FORKING.md).
