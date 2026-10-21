# `app/` — the control plane

A FastAPI application: the web UI, the HTTP API, the board, and the background
loops that keep the rest of the system moving. It is **stateless** — everything
durable lives in PostgreSQL — and it holds no long-running agent and no
execution. It decides *what* exists and *who* may do what; it does not run work.

If you have not read [`../HOW_A_STORY_EXECUTES.md`](../HOW_A_STORY_EXECUTES.md),
read it first.

---

## One image, five services

The same container runs under five different entrypoints. Terraform creates all
five; Cloud Build points each at the right one.

| Service | Entrypoint | Runs |
|---|---|---|
| `<prefix>-web` | `app.main` | the UI, chat, the API, agent callbacks |
| `<prefix>-council` | `app.council_worker` | the review-council dispatchers |
| `<prefix>-git` | `app.git_engine_worker` | the git dependency-graph engine |
| `<prefix>-management` | `app.management_worker` | settings, dashboard, admin backends |
| `<prefix>-scheduler` | `app.scheduler_worker` | the background loops, behind one leader |

The background loops run in the scheduler service, one instance with its CPU
always on, and only the leader runs them. The web tier is deployed with
`AGENTOS_SCHEDULER_LOOPS=0` so it leaves them alone and can scale freely.

Locally, `app.main` runs the loops too — `uvicorn app.main:app` is the whole
system in one process.

---

## The shape of the package

| | |
|---|---|
| `routers/` | Every HTTP surface, one module per area: goals, stories, chat, resources, admin, and the `/internal/*` endpoints that pods and agents call. |
| `services/` | The business logic the routers call into. The largest directory, and the one to read. |
| `channels/` | Chat integrations. Three platforms are registered — Slack, Notion, and a Notion bot — behind one adapter/provisioner/relay interface, so adding a fourth is three registry entries and no changes to the goal routes. |
| `models/` | Pydantic models for the domain objects. |
| `ledger/` | The append-only record of who did what. Story-state changes are written through it so the board's history is auditable rather than reconstructed. |
| `memory_v2/` | Turning ledger events into the memory files agents read, and flushing them at the right moments. |
| `chat_module/` | The durable chat queue and its dispatcher. |
| `resource_kinds/` | What kinds of thing a goal can own — repositories, documents, cloud targets — and how each is validated. |
| `templates/` | Jinja templates for the UI. |

### Inside `services/`

The packages worth knowing by name:

- `council/`, `security/`, `platform_council/` — three review councils. The goal
  review council reads finished stories and reports to the orchestrator; the
  security council reviews a branch; the platform council researches
  improvements to AgentOS itself.
- `git_graph/`, `merge_orchestrator/`, `git_providers/` — the dependency-graph
  engine: cutting a story's branch, tracking what depends on what, and running
  the merge queue.
- `model_registry/` — which models exist, what context windows they really
  have, and the synthetic smaller-window variants. It also emits the provider
  config the gateway reads.
- `credential_brokers/`, `tool_registry/`, `custom_tools/` — the broker that
  lets a custom tool fetch only the credentials it declared.
- `taximeter/` — cost accounting, per goal and per agent.
- `scheduler/` — the background loops, in one list.
- `transitions/`, `agent_registry/`, `agent_refresh/`, `template_authoring/`,
  `goal_summary/`, `webhooks/`, `egress_audit/`, `local_prompts/`.

---

## The background loops

Eight, declared in one place (`services/scheduler/registry.py`) rather than
scattered through a startup block:

`park_cards` · `dag_janitor` · `awaiting_merge_janitor` · `token_refresh` ·
`slack_membership_reconcile` · `story_review_backstop` · `credential_watchdog` ·
`dashboard_refresh`

**The test for whether a loop belongs there:** does running two of them do harm?
Every instance used to run every loop with `maxScale` at 100 and no
coordination. The list exists so that question has one answer in one file.

---

## Feature flags

Most subsystems can be switched in `terraform.tfvars`, and most ship **on** —
the goal review council, the git engine, memory v2, the chat queue, the security
flags. The ones to leave alone unless you know why are the three security flags:
per-agent tokens, cross-goal authorization enforcement, and the custom-tool
credential broker.

The exceptions ship `false`:

- the **security council** and the **platform research council** — only the goal
  review council is on out of the box; turn the other two on when you want them;
- `memory_v2_user_memory_enabled` — per-user memory stays off;
- `task_dag_ceiling_enforce` — the concurrency ceiling is measured and logged
  but not applied, until you decide you want it applied.

---

## Conventions

**Raw SQL, no ORM.** Around 5,700 table references across the codebase, written
as SQL. That is a deliberate choice, and it is held up by build-time sweeps
rather than by a type system: the test suite PREPAREs every SQL literal it can
find against a real schema, so a column that does not exist fails the build
instead of a request. See [`../tests/README.md`](../tests/README.md).

**`kernel.`-qualified names.** Code in `app/` reaches kernel tables with an
explicit `kernel.` prefix. Code in `kernel/` uses bare names on a
`search_path=kernel,public` connection. Mixing the two is the most common
review comment.

**Comments carry the incident.** Where something looks over-careful, the comment
usually says which outage made it that way. When a comment and this README
disagree, trust the comment and send a pull request for the README.
