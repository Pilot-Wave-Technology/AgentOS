# app/

The FastAPI application. Every HTTP endpoint, WebSocket bridge, service, and platform integration lives here.

---

## Entry point

`main.py` — registers all routers, starts background workers (task controller, Assistant cleanup watchdog), and owns the application lifespan.

Startup sequence:
1. Database pool created (`database.py`)
2. Firebase Admin SDK initialised (`auth.py`)
3. Gateway WebSocket client started (`openclaw.py`)
4. Task controller started (`task_controller.py`)
5. Assistant cleanup watchdog started (60-second loop in `main.py`)
6. All routers registered

Router registration order matters in two places:
- `assistant` before `chat` — `/pick-assistant` would otherwise be shadowed by `/{agent_id}`
- `goal_resources` before `resources` — `/browser` shadowing

---

## Directory map

```
app/
│
├── main.py                    Entry point, router registration, startup hooks
├── config.py                  Environment settings (pydantic-settings)
├── database.py                asyncpg connection pool — get_db() dependency
├── auth.py                    Firebase JWT verification, signup gate, role resolution
│
├── openclaw.py                Gateway WebSocket client
│                              SessionManager — one _AgentSession per (goal_id, agent_id)
│                              gateway_admin  — HTTP client for admin RPCs
│
├── task_controller.py         Task state machine and autonomous dispatch loop
│                              Watches for ready tasks, dispatches IAs, handles approvals
│
├── memory.py                  Assistant memory V2 — save, search, compact
│                              Three scopes: global / goal / conversation
│                              FTS via tsvector; auto-expands to all goals on no-match
│
├── resource_provisioning.py   Generates RESOURCES.md for agent workspaces
│                              Embeds short-lived GitHub credentials
│                              Refreshes tokens at the 50-minute mark
│
├── assistant_provisioning.py  Assistant template lookup, per-user instance creation
│                              Workspace file upload (SOUL.md, MEMORY.md, IDENTITY.md)
│
├── llm_models.py              Model allowlist — validates model names agents may use
│
├── gemini_client.py           google-genai wrapper (memory summarization)
│
├── github_tokens.py           GitHub App token mint and refresh
│
├── notifications.py           In-app notification creation and delivery
│
├── storage.py                 GCS upload / download wrappers
│
├── routers/                   HTTP and WebSocket endpoints — see Routers below
│
├── services/                  Business logic — see Services below
│
├── channels/                  Platform integration framework — see channels/README.md
│   ├── base.py                Shared logic: dedup, actor resolution, base types
│   ├── registry.py            Platform registration maps (adapters, provisioners, relays)
│   ├── router.py              FastAPI router: /channels/{platform}/*
│   ├── slack/                 Slack — OAuth, events, commands, Block Kit actions
│   ├── notion/                Notion — OAuth, webhooks, provisioner, page management
│   └── notion_bot/            Notion internal-token variant (no signature verification)
│
├── ledger/                    Append-only event store — see ledger/README.md
│   ├── journal.py             SHA-256 hash chaining, event persistence
│   ├── emit.py                emit() helper — call from anywhere
│   ├── schema.py              Event type registry
│   └── payloads/              Event payload builders per event type
│
├── models/                    Pydantic data models (request/response shapes)
│
├── resource_kinds/            Single source of truth for goal_resources.kind values
│   ├── __init__.py            RESOURCE_KINDS catalog — ResourceKindSpec per kind
│   └── README.md              How to add a new resource kind
│
├── slack/                     DEPRECATED — legacy /slack/* routes kept for URL compat
│                              Remove after Slack app config is updated to /channels/slack/*
│
└── templates/                 Jinja2 HTML templates (HTMX-driven UI)
    ├── base.html              Layout wrapper, navigation
    ├── goals.html             Goal list and sidebar
    ├── dashboard.html         Goal dashboard
    ├── chat.html              Conversation UI (WebSocket-driven)
    ├── stories.html           Story Kanban board
    ├── tasks.html             Task board
    └── dashboard/             Admin monitoring dashboards
```

---

## Routers

Registered in `main.py`. Each router is a FastAPI `APIRouter` mounted under a specific prefix.

| Router file | Prefix | Purpose |
|---|---|---|
| `routers/auth.py` | `/` | Login, signup, signup approval |
| `routers/admin.py` | `/admin` | Signup review, user management |
| `routers/goals.py` | `/goals` | Goal CRUD, membership, join requests |
| `routers/agents.py` | `/agents` | Global agent definitions |
| `routers/assistant.py` | `/goals/{gid}/shca` | Assistant (SHCA) picker and instance management |
| `routers/goal_thread.py` | `/goals/{gid}/thread` | GO goal-thread UI |
| `routers/chat.py` | `/goals/{gid}/chat` | Conversation UI + WebSocket bridge |
| `routers/stories.py` | `/goals/{gid}/stories` | Story Kanban, state transitions |
| `routers/tasks.py` | `/goals/{gid}/tasks` | Task board, approval flow |
| `routers/monitor.py` | `/goals/{gid}/monitor` | Audit log, token usage |
| `routers/notifications.py` | `/notifications` | User notification list |
| `routers/goal_resources.py` | `/goals/{gid}/resources` | Per-goal resource browser |
| `routers/resources.py` | `/resources` | Global resource registry |
| `routers/memory.py` | `/goals/{gid}/memory` | Memory search API |
| `routers/admin_controls.py` | `/goals/{gid}/controls` | Kill switch — pause, resume, force-kill |
| `routers/go_internal.py` | `/internal/go` | Internal API for GO/IA tools (Bearer auth) |
| `channels/router.py` | `/channels` | Generic platform router — all registered platforms |
| `routers/custom_tools/` | `/custom-tools` | Custom tool admin and execution |

The legacy `/slack/*` router is also mounted but is deprecated.

---

## Services

`app/services/` is the single home for business logic. Routers call services; services do not call routers.

Key services:

| Path | Responsibility |
|---|---|
| `services/story_transitions.py` | Story state machine — all valid transitions and their guards |
| `services/execution_control.py` | Kill switch — pause/resume checks; single source of truth for `execution_pauses` |
| `services/agent_kill.py` | Force-kill a task and move it to `halted` |
| `services/agent_driver.py` | Session creation, workspace provisioning, agent lifecycle |
| `services/agent_registry/` | Agent template registry — lookup, instantiation, history |
| `services/goal_membership.py` | Add/remove members, trigger channel provisioning |
| `services/plan_approval.py` | Validate GO-generated plans and ingest them as tasks |
| `services/resource_allocation.py` | Grant/revoke resource access for tasks |
| `services/resource_scanner.py` | Scan repos and documents to generate summaries |
| `services/credential_brokers/` | GCP and GitHub credential resolution for agent workspaces |
| `services/custom_tools/` | Custom tool execution — HTTP, OpenAPI, TypeScript, MCP |
| `services/git_providers/` | GitHub and GitLab integrations |
| `services/merge_orchestrator/` | Merge request orchestration across repos |
| `services/webhooks/` | GitHub and GitLab webhook ingestion |
| `services/go_agent.py` | Goal Orchestrator agent logic |

---

## Configuration

All environment variables are declared in `config.py` using pydantic-settings. The `Settings` object is a singleton imported as `from app.config import settings`.

See the [Configuration reference in the README](../README.md#configuration-reference) for the full variable list.

---

## Dependency injection

Two FastAPI dependencies are used throughout the routers:

- `get_db()` — yields an `asyncpg.Connection` from the pool (in `database.py`)
- `get_current_user()` — verifies the Firebase JWT and returns the authenticated user row (in `auth.py`)

Goal-scoped routes also inject a `goal_member` dependency that checks the user's role in the requested goal.
