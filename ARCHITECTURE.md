# Architecture

AgentOS is a multi-agent orchestration platform. It breaks long-running
**goals** into **stories** and **tasks**, dispatches them to a fleet of LLM
agents, and keeps humans in the loop at every decision point. This document is
the map — start here, then follow the code into `app/`, `deploy/`, and
`migrations/` for depth. See [`README.md`](README.md) for the overview and
[`deploy/README.md`](deploy/README.md) for the guided setup.

AgentOS ships as **two branches of one repository**
([github.com/Pilot-Wave-Technology/AgentOS](https://github.com/Pilot-Wave-Technology/AgentOS)):

| Branch | Role | Tech |
|---|---|---|
| **`agent_os_openclaw`** | Agent **gateway** — the agent runtime. Hosts each agent in its own workspace, makes the LLM calls, and executes the work. **Deploy first (Stage ①)**; it creates the shared VPC and the archive bucket (the gateway + LLM secrets are created earlier by `prerequisites.sh`). | Node.js / TypeScript (derived from OpenClaw) |
| **`agent_os_platform`** (this branch) | **Control plane** — goals, stories, tasks, memory, credentials, approvals, and platform integrations (Stage ②). | Python 3.12 · FastAPI · PostgreSQL |

The control plane owns **state and coordination**; the gateway owns **agent
execution and LLM calls**. They communicate over a single VPC-internal
WebSocket. Neither is useful without the other.

```
                             ┌──────────────────────┐
                             │      Browser         │
                             │  (Firebase JWT)      │
                             └──────────┬───────────┘
                                        │ HTTPS + WebSocket
                                        ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Control plane — agent_os_platform (Cloud Run)                          │
│                                                                         │
│  FastAPI + Jinja2 + HTMX (no build step)                                │
│    routers · Firebase auth · chat / WS bridge (SessionManager)          │
│    services: story_transitions · task_controller · dag_* · memory ·     │
│              ledger · execution_control · custom_tools · councils · …    │
│                                                                         │
│    asyncpg │           google-cloud-storage │        google-genai       │
│  ┌─────────▼──────┐  ┌──────────────────────▼┐  ┌───────────────────┐   │
│  │  PostgreSQL    │  │  Cloud Storage         │  │  Anthropic Claude │   │
│  │  (Cloud SQL)   │  │  (workspaces + files)  │  │  Google Gemini    │   │
│  └────────────────┘  └────────────────────────┘  └───────────────────┘  │
└─────────────────────────────┬───────────────────────────────────────────┘
                              │ WebSocket (VPC-internal)
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Agent gateway — agent_os_openclaw (GCP VM)                             │
│                                                                         │
│  Gateway process (Node.js / TypeScript)                                 │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐             │
│  │  GO Agent │  │ assistant │  │    IA     │  │    RA     │             │
│  │ workspace │  │ (per      │  │ workspace │  │ workspace │             │
│  │ (per goal)│  │  goal,    │  │ (per task)│  │ (review)  │             │
│  │           │  │  user)    │  │           │  │           │             │
│  └───────────┘  └───────────┘  └───────────┘  └───────────┘             │
│                                                                         │
│  custom-tools runtime  ← fetches tool definitions from the control plane│
└────────────────────────────────────────────────────────────────────────┘
```

---

## Table of contents

- [Two-branch topology](#two-branch-topology)
- [Gateway protocol](#gateway-protocol)
- [Session model](#session-model)
- [Control-plane services](#control-plane-services)
- [Domain model](#domain-model)
- [Agent types](#agent-types)
- [Task lifecycle and dispatch](#task-lifecycle-and-dispatch)
- [Assistant memory](#assistant-memory)
- [Ledger and projections](#ledger-and-projections)
- [Auth and roles](#auth-and-roles)
- [Kill switch](#kill-switch)
- [Councils](#councils)
- [Custom tools](#custom-tools)
- [Channels: Slack](#channels-slack)
- [Deployment topology](#deployment-topology)
- [Repository layout](#repository-layout)

---

## Two-branch topology

The two branches deploy in order, gateway first:

1. **Stage ① — `agent_os_openclaw`.** The gateway boots on a GCP VM and
   provisions the shared infrastructure both halves rely on: the VPC and the
   archive bucket (the gateway + LLM secrets come from `prerequisites.sh`). It exposes one authenticated
   WebSocket on the private network and never faces the public internet.
2. **Stage ② — `agent_os_platform`.** The control plane deploys to Cloud Run,
   attaches to the same VPC, and dials the gateway's WebSocket over private IP.

The control plane creates and destroys agent instances, sends them messages,
and streams their responses back to browsers and integrations. The gateway runs
each agent in an isolated workspace and makes the actual model calls. Every
durable fact — goals, stories, tasks, memory, the ledger — lives in the control
plane's PostgreSQL database; the gateway is stateless between messages.

---

## Gateway protocol

The gateway exposes a **single WebSocket endpoint** — no path routing;
everything happens at the protocol level. The control-plane side is
`app/openclaw.py`.

### Handshake

```
control plane                gateway
     │                          │
     │◄── event connect.challenge ──│
     │                          │
     │─── req connect ─────────►│
     │      { client.id: "gateway-client",           │
     │        mode: "backend",                        │
     │        auth.token: <gateway token>,            │
     │        scopes: ["operator.admin"] }            │
     │                          │
     │◄── res ok, hello-ok ─────│
```

The client id and mode are pinned constants — the gateway rejects unknown
clients. The auth token is read from configuration (never hard-coded).

### Sending a message

```json
{
  "type": "req",
  "id": "<uuid>",
  "method": "chat.send",
  "params": {
    "sessionKey": "agent:<agentId>:<goalId>-<threadId>",
    "message": "user text",
    "idempotencyKey": "<uuid>"
  }
}
```

Session-key builders live in `app/openclaw.py`. Each `(goal, agent, thread)`
maps to one stable session key so the gateway can resume the right agent
workspace.

### Receiving a response

The gateway streams responses as **cumulative** deltas — each `chat state=delta`
frame carries the full response so far, so a UI must *replace* the bubble text,
not append to it. The final frame is `chat state=final`; failures arrive as
`state=error` or `state=aborted`.

```
res(ack)  →  chat state=delta  →  delta  →  …  →  chat state=final
```

### Admin RPCs

A separate HTTP client handles administrative operations against the gateway:
`agents.create`, `agents.delete`, `agents.files.set`, `config.set`, and
`tools.reload` (which asks the gateway's custom-tools runtime to re-fetch tool
definitions). Agent provisioning, workspace file writes, and tool refreshes all
flow through these calls.

---

## Session model

`SessionManager` (in `app/openclaw.py`) holds one persistent agent session per
`(goal, agent)` pair. Browser WebSocket connections are **ephemeral
subscribers** — they come and go while the gateway session stays open.

```
Browser A ─┐
Browser B ─┤──►  agent session  ──►  gateway WS  ──►  agent workspace
Browser C ─┘
```

- **Browser disconnect** is an unsubscribe only. The agent keeps running,
  writes its output to PostgreSQL, and pushes a notification; the next browser
  re-subscribes and sees the result.
- **Idle timeout** evicts a session after a period with zero subscribers and no
  activity.
- **Auto-reconnect** to the gateway uses exponential backoff.
- **Run tracking** maps a request id to a run id on ack, then routes cumulative
  deltas into the correct thread buffer — so concurrent messages on different
  threads never cross-contaminate.

---

## Control-plane services

The control plane is **one image** that runs in one of two shapes, chosen by the
`service_profile` deploy variable.

**Split profile (default)** — four Cloud Run services, each isolated and
independently scalable:

| Service | Entrypoint | Runs |
|---|---|---|
| **`agentos-web`** | `app.main` | Public web UI, chat, and agent callbacks |
| **`agentos-council`** | `app.council_worker` | Review-council dispatchers and the research dispatcher |
| **`agentos-git`** | `app.git_engine_worker` | The git dependency-graph engine |
| **`agentos-management`** | `app.management_worker` | Settings, dashboard, and admin backends |

**Single profile** — set `service_profile = "single"` and everything runs in
one service (`app.single`) with the worker loops in-process: one always-on
instance instead of four. Both profiles are first-class; pick based on cost vs.
isolation.

Splitting the work matters because the web tier answers user requests on a
request budget, while the council, git, and management loops are long-running
background workers. Keeping them in separate services means a busy background
sweep never starves the UI, and each service scales to its own load. Global
single-run guarantees for the background loops are enforced in the database, so
the split between "who enqueues" and "who drains" can never double-process.

---

## Domain model

Everything in AgentOS is scoped to a **goal**. State values below come from
`CHECK` constraints in `migrations/` — they are the ground truth.

**Goal** — a long-running objective with a natural-language description and a
team of human members. Each goal owns a set of resources (repositories,
documents, files), a GO Agent that decomposes it into stories, and a per-member
assistant. `goals.status` ∈ {`active`, `deleted`}.

**Story** — a proposable unit of work. The GO Agent generates stories; a human
accepts, rejects, or rewrites each one before implementation begins.
`stories.state` ∈ {`proposed`, `accepted`, `in_progress`, `blocked`, `done`,
`rejected`, `cancelled`}. An accepted story's tasks are dispatched
automatically.

**Task** — a concrete step inside a story. The state machine is additive:
`tasks.state` ∈ {`pending`, `ready`, `dispatching`, `in_progress`, `blocked`,
`halted`, `automated_review`, `awaiting_approval`, `approved`, `rejected`,
`done`, `failed`, `cancelled`, …}. Dependencies between tasks are tracked and
enforced — completing one task can unblock others automatically.

**Resources** attach to goals (repositories, uploaded documents, stored files,
cloud credentials). Per-task allocations are tracked so an agent only receives
the resources its task declares. Short-lived repository tokens are refreshed on
a timer and injected into the agent workspace, never persisted in plaintext.

---

## Agent types

Agent definitions are **templates**; running agents are **instances**.

| Type | Name | Scope | Memory | Lifecycle |
|---|---|---|---|---|
| **GO** | Goal Orchestrator | one per goal | persistent goal thread | lives as long as the goal |
| **assistant** | Assistant | one per (goal, user) | persistent, three scopes | lives with the (goal, user) binding; removed when the goal is deleted |
| **IA** | Independent Agent | one per task | none (reports out) | destroyed on completion |
| **RA** | Reviewer Agent | one per review | none | destroyed after its verdict |

- The **GO Agent** reads the goal description and team resources and proposes
  stories. It owns the goal's orchestration thread.
- The **assistant** is a per-(goal, user) agent with persistent memory. It
  surfaces the plan, answers questions, and handles approvals inline — the human
  can drive the whole system through conversation with their assistant.
- An **IA** is a task-scoped worker. It is created automatically when a task
  becomes ready, does the work, reports its output, and is destroyed.
- An **RA** reviews an IA's output before it reaches the human approval gate.

**Claude Code** executes code stories: when a plan declares a DAG of phases,
each phase runs as a fresh turn on the same Claude Code session, preserving plan
and prior-task context across the whole story.

The single source of truth for type constants and their database-string aliases
is `app/agent_types.py`.

---

## Task lifecycle and dispatch

Work flows **goal → story → task**, with a human approval gate at each layer.

```
GO Agent proposes stories
        │
        ▼
  human accepts / rejects / rewrites a story        ← gate 1
        │
        ▼
  tasks become ready as dependencies clear
        │
        ▼
  dispatch: create an IA, send the first message → in_progress
        │
        ▼
  automated review: an RA reviews the output → automated_review   ← automated gate
        │  approved                            rejected
        ▼                                        │
  awaiting_approval                              ▼
        │                                     rejected
        ▼
  human approves → done                             ← gate 2
```

The **task controller** (`app/task_controller.py`) is an asyncio worker started
at boot. It watches for ready tasks, creates the IA, streams the agent's deltas
into the `messages` table, spawns a review task handled by an RA, waits at
`awaiting_approval` for the human gate, and cascades completions to unblock (or
fail) dependents. Every transition writes a ledger event.

For code stories, a **DAG-of-phases** path (`app/services/dag_*.py` +
`phase_dispatcher.py`) parses a plan's task DAG, inserts one parent task and a
subtask per phase, runs each phase as a new turn on the same Claude Code
session, and caps global concurrency with FIFO admission. A periodic janitor
re-dispatches stalled subtasks so a stuck DAG self-heals.

Agents **pause and wait** at every gate — they never proceed without it.

---

## Assistant memory

The assistant builds a **three-scoped** persistent memory
(`app/memory.py`), stored per-instance and retrievable by full-text search:

| Scope | Holds |
|---|---|
| `global` | Preferences and facts about the user, across all goals. |
| `goal` | Decisions, conventions, and context for this goal. |
| `conversation` | Short-term context for the current thread only. |

Memory entries are typed (`preference`, `observation`, `context`, `convention`,
`decision`). Full-text search auto-expands to all of a user's goals when a local
search returns nothing. When a store grows past its threshold, older entries are
consolidated by an LLM summarization pass and marked compacted rather than
deleted. Auto-save hooks fire on message callbacks so nothing is lost even when
a streaming response is retried.

The result: the assistant remembers preferences, past decisions, and
conventions across sessions and across goals.

---

## Ledger and projections

`app/ledger/` is an **append-only event store**. Every state-mutating operation
emits a ledger event before it returns.

- Appends are serialized behind a database advisory lock, and each event
  computes a **SHA-256 hash chain** (`previous_hash + payload → integrity_hash`)
  inside a transaction. Time-sortable event ids give a total order.
- Event types are dotted strings (`task.created`, `task.status_changed`,
  `goal.created`, `execution.*`, …) — adding a new one is a new string value, no
  schema change.
- Each event carries a **correlation id** (the root decision) and **causation
  ids** (its direct causes), so the full "why did this happen?" graph is
  queryable from the journal alone.

**Projection tables are derived from events, not the other way around.** The
readable views (activity feeds, dashboards) are built by querying the ledger.
Retroactive corrections are *additive* — append a compensating event; never
rewrite history. Nothing is deleted, and everything is traceable.

---

## Auth and roles

Browsers authenticate with a **Firebase JWT** (cookie or `Authorization`
header), verified by the Firebase Admin SDK. First-time users pass through a
signup gate:

```
verify token → look up user
     ├─ no user row → check signup request
     │       ├─ pending  → /signup/pending
     │       ├─ rejected → /signup/rejected
     │       ├─ approved → auto-create the user row (one-time)
     │       └─ none     → /signup
     └─ user found → look up the goal role for the requested goal
```

A bootstrap-admin email (set in configuration) bypasses the gate on first login
and creates the initial platform admin. Internal service-to-service calls use a
scoped bearer/JWT mechanism rather than user credentials.

**Platform roles**: `admin` (signup review, kill switch, all goal controls, user
management), `editor` (create goals, edit content), `member` (participate in
goals they are added to). **Goal roles**: `admin` (all operations within a goal)
and `member` (chat, view stories and tasks). Platform admins can act on any goal
without explicit membership.

---

## Kill switch

The kill switch (`app/services/execution_control.py`) is the single source of
truth for "is execution paused?" — every dispatch path calls into it rather than
touching the table directly.

- **`paused`** is reversible: work stops at the next checkpoint; resuming
  removes the pause.
- **`halted`** is terminal: force-killed work cannot be restarted — you create a
  new task to redo it.

Scopes are `system` (freeze everything) and `goal` (freeze one goal). Active pauses are rows in a
table — presence means paused, resume is a delete — while the full history lives
in the ledger. The dispatch guard **fails closed** (a bug blocks rather than
dispatches during a freeze), so the kill switch is safe by construction.

---

## Councils

Beyond the per-task RA review, AgentOS runs standing **review councils** in the
`agentos-council` service. They share one internal shape — a dispatcher, a
scheduler, provisioning, run tracking, and validators — and read/write the same
queue tables that the web tier's submit endpoints feed. A database-enforced
single-active-run rule keeps enqueue and drain from double-processing.

- **Goal Review Council** — automated end-of-story review. On story completion
  the dispatcher fans out into reviewer-agent stages; each RA submits its
  findings, and a convener packet is delivered back to the goal's GO Agent.
- **Security Council** — branch-scoped security review. The review unit is a
  (repository, branch) pair; shell-capable aspect agents scan a server-prepared
  read-only checkout with offline scanners, and results fan out to every active
  goal that references the branch.

Each council's runtime activation is a configuration decision — the tables, the
constraint values, the code constants, and the worker service ship in every
deploy; enabling a council is a flag, not a code change.

---

## Custom tools

Admins define tools in the **web UI** — no gateway restart, no deploy. Tool
definitions live in PostgreSQL; the gateway's custom-tools runtime fetches them
at startup, on the `tools.reload` RPC (fired after every admin save), and on a
periodic safety-net refresh, so agents pick up a new tool in under a minute.

| Tier | How it runs |
|---|---|
| `http_action` | Authenticated HTTP call. |
| `openapi` | An operation from an OpenAPI spec. |
| `typescript` | Sandboxed runtime — network-only, short timeout, hard cap. |
| `mcp_server` | An MCP server proxied through the control plane. |

One OpenAPI spec or MCP server becomes exactly one tool row; agents call it with
`{ operation, arguments }` and per-operation routing happens in the executor.
Security boundaries include an SSRF guard that blocks private and link-local
addresses, encrypted credential storage (encryption key never persisted with the
data), per-session rate limits, and an output-size cap. Execution runs inside
the gateway's capability runtime, never in the control plane.

---

## Channels: Slack

`app/channels/` is a generic platform-channel layer: no router needs to know
which platform is on the other end.

```
inbound (any platform) → signature check → dedup → resolve actor →
    load binding → agent session → agent response → outbound relay
```

Platforms register in a lazy slug → module map, so adding one is a registry
change, not a rewrite. Storage is uniform across platforms: app-level
credentials and config, per-goal opt-in, and channel bindings that map a
`(goal, platform, user)` triple to a channel reference (with a goal-level
binding when there is no specific user).

- **Slack** is a full operational surface: slash commands (`/work`, `/goal`,
  `/story`, `/task`, `/done`, `/pause`, `/resume`, `/kill`, and more), Block Kit
  actions, and an OAuth install flow. Every inbound request is HMAC-verified, and
  because Slack requires a fast acknowledgement, gateway round-trips are pushed to
  background tasks and the command is acked immediately.

Slack is optional — enable it by supplying its credentials.

---

## Deployment topology

The control plane deploys to **Google Cloud Run** by a single Cloud Build
pipeline. One image, deployed as either four services (default) or one
(`service_profile = "single"`).

```
git push  →  Cloud Build
                │
                ├─ 1. bootstrap: runtime service account, secrets, buckets
                ├─ 2. docker build → push to Artifact Registry
                ├─ 3. validate migration order
                ├─ 4. apply migrations via a Cloud Run Job
                └─ 5. deploy the service(s) from the same image
                        agentos-web · agentos-council · agentos-git · agentos-management
                    Each service attaches:
                        ├─ Cloud SQL over a Unix socket (asyncpg)
                        ├─ a Serverless VPC connector → the gateway (private IP)
                        ├─ Secret Manager for every credential
                        └─ Cloud Storage for workspaces and resources
```

The gateway is reached **only over the private VPC** and is never publicly
exposed. Migrations are applied by a Cloud Run Job that reads a `SEQUENCE` file
top-to-bottom; they are **additive-only** (never drop or rename; only add
columns, tables, and new constraint values), because code from one deploy may
run against a database a later deploy has already migrated. The full ordered
setup — gateway first (Stage ①), then this control plane (Stage ②) — is in
[`deploy/README.md`](deploy/README.md).

**Tech stack**

| Layer | Technology |
|---|---|
| API framework | Python 3.12 · FastAPI · uvicorn |
| Database | PostgreSQL (asyncpg) · Cloud SQL |
| Frontend | Jinja2 + HTMX — no build step |
| Auth | Firebase (JWT verification + signup gate) |
| Agent gateway | `agent_os_openclaw` (WebSocket, VPC-internal) |
| LLM APIs | Anthropic Claude · Google Gemini |
| File storage | Google Cloud Storage |
| Integrations | Slack |
| Deployment | Google Cloud Run · Cloud Build · Cloud SQL |

---

## Repository layout

```
agent_os_platform/
├── app/                    FastAPI application
│   ├── main.py             agentos-web entrypoint: routers + startup hooks
│   ├── council_worker.py   agentos-council entrypoint
│   ├── git_engine_worker.py  agentos-git entrypoint
│   ├── management_worker.py  agentos-management entrypoint
│   ├── single.py           single-service profile entrypoint
│   ├── config.py           settings model
│   ├── database.py         asyncpg pool
│   ├── auth.py             Firebase auth + signup gate
│   ├── openclaw.py         gateway WebSocket + SessionManager + admin client
│   ├── task_controller.py  task state machine + dispatch loop
│   ├── agent_types.py      agent type constants
│   ├── memory.py           assistant memory (scopes, FTS, compaction)
│   ├── ledger/             append-only event store (journal/schema/emit/payloads)
│   ├── routers/            HTTP + WebSocket endpoints
│   ├── services/           dag_* · phase_dispatcher · story_transitions ·
│   │                       execution_control · custom_tools · councils ·
│   │                       git_engine · templates · …
│   ├── channels/           generic channel system (+ slack/)
│   ├── models/             data models
│   └── templates/          Jinja2 templates (HTMX UI)
├── static/                 css + js (no build step)
├── migrations/             *.sql (additive only) + apply.py + SEQUENCE
├── deploy/                 ordered deploy guide + infrastructure config
├── Dockerfile              python:3.12-slim, uvicorn
├── README.md · ARCHITECTURE.md · FORKING.md · LICENSE
```

---

AgentOS is open source under the **Apache License 2.0**. Copyright © Port33,
inc. Each organization runs AgentOS from its own fork — see
[`FORKING.md`](FORKING.md).
