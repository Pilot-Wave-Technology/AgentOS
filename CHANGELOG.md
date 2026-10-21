# Changelog

All notable changes to AgentOS are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
AgentOS is pre-1.0 (MAJOR `0`): per SemVer §4, the public API and internal
interfaces may change between minor versions without a MAJOR bump. Expect breaking
changes at any `0.MINOR` boundary until `1.0.0` is cut.

## [Unreleased]

Nothing yet. Changes land here before a version is cut.

## [0.2.0] - 2026-10-21

**The execution model changed.** The 0.1.0 notes below describe
`goal -> story -> task`, dispatched to a per-task Independent Agent and
reviewed by a Reviewer Agent. That is no longer how AgentOS works, and those
notes are kept because a changelog records what was true at the time, not
because they describe the system you are running.

### Changed

- **`goal -> story -> execution -> leg`.** A story is the unit a person
  accepts; an *execution* is one attempt at running it; a *leg* is one
  conversation with a model, starting from an empty context and leaving a brief
  for the next. See `HOW_A_STORY_EXECUTES.md`.
- **Work runs in its own pod**, not in the control plane: two containers
  running as different users, a default-deny network, an egress proxy, and no
  cloud credential inside. The execution plane is required: every executor
  runs on it, and one `terraform apply` creates it with the database.
- **A kernel plane** (`kernel/`) decides *when* work runs: admission ceilings,
  dispatch, leases, reconciliation, and a verifier that re-runs a completion
  claim's checks in a clean container. It contains no model call.
- **Executor profiles** replace per-task agent types. A profile is data — a
  TOML file, a prompt, optional skills — and its tool surface is fail-closed.
- **IA is vestigial; RA was repurposed.** The standard-IA list was emptied in
  earlier versions and nothing dispatches one. `RA` is now the type the review
  councils provision their reviewers as.
- **The model set** is Claude Opus 5.5, Claude Sonnet 5.5 and Gemini 3.8 Flash,
  each registered at 100K, 200K, 500K and 1M context windows (migration
  `0007_model_windows_tags_and_defaults`).
- **Legacy templates retired.** Migration `0009_retire_legacy_templates`
  removes the `go`, `default-shca` and IA templates (and `task-reviewer-ia`)
  where no live agent still uses them. The current templates are `go-v2` for
  the orchestrator and `kernel-assistant` for the assistant.

### Added

- **A fifth Cloud Run service, `<prefix>-scheduler`** (`app.scheduler_worker`).
  The background loops run there, one instance behind one leader; the web
  service runs with `AGENTOS_SCHEDULER_LOOPS=0`.
- **Per-goal executor settings** on the goal's Model Configuration page:
  executor model, verifier model, machine size (`small` / `medium` / `large`),
  tokens per leg and max legs. They are frozen onto the run at dispatch
  (`executions.leg_budget`, migration `0008_goal_leg_budget`) and govern the
  work phase, not the review.
- **The gateway's compaction summarizer** is chosen on the Model Registry page,
  under Utility Models.
- `preflight.sh` — checks that an installation is actually provisioned, and
  treats "could not determine" as a failure rather than a pass.
- `execution-plane.sh` — stands up the cluster, identities, pools and
  conductor for a single-environment install.
- A documentation set written from the source: `HOW_A_STORY_EXECUTES.md`, a
  rewritten `ARCHITECTURE.md`, and a README in every published folder.

### Removed

- The single-service deployment profile. It existed only in the published
  overlay, so no test could import it, and it had drifted into importing a
  router that no longer exists.

## [0.1.0] - 2026-07-19

Initial public release. This section documents the state of AgentOS at the
open-source cut. AgentOS ships as two branches of one repository — the
`agent_os_platform` control plane (this branch) and the `agent_os_openclaw`
agent gateway.

### Added

- **Goal → Story → Task orchestration** with a human-in-the-loop approval gate at
  every layer. Goals decompose into stories; stories decompose into tasks.
- **Four core agent types** — GO (Goal Orchestrator), Assistant, IA (Independent
  Agent), and RA (Reviewer Agent) — with the Reviewer Agent auto-reviewing task
  output (verdict, findings, quality score) before it reaches the human gate.
- **Persistent Assistant memory** across all goals, with three scopes
  (`global`, `goal`, `conversation`), PostgreSQL full-text search, and
  automatic summarised compaction.
- **Append-only ledger** — every state-mutating action is a SHA-256-chained
  ledger event. Projection tables are rebuilt from ledger events, giving a
  tamper-evident audit trail across operator actions, agent dispatch, and
  human approvals.
- **Kill switch** — pause, resume, and force-kill at system or goal scope.
  Pause is reversible; force-kill (`halted`) is a one-way terminal state.
- **Custom tools** — admin-defined tools across four tiers (`http_action`,
  `openapi`, `typescript` sandbox, `mcp_server`), picked up by the gateway
  without a restart or redeploy.
- **Slack integration** — slash commands, Block Kit actions, an OAuth install
  flow, and lifecycle relays, so users can create tasks, approve work, and
  converse with agents without opening the web UI.
- **Generic platform-channel framework** — inbound, outbound, lifecycle, and
  provisioner slots discovered through a registry, so adding a new platform
  needs registry entries rather than router changes.
- **Agent template registry** — template definitions separated from running
  instances via `templates`, `template_files`, and `template_history`.
- **Skills registry** — reusable, versioned prompt fragments injected into
  agent contexts.
- **Resource kinds registry** — a single source of truth for resource kinds,
  with per-kind validation, format hints, and field requirements. Adding a new
  kind is a one-file change.
- **GitHub App integration** — short-lived, per-task repository credentials that
  refresh before the one-hour token lifetime expires.
- **Resource ingestion** — text and binary document scanning (PDF, spreadsheets,
  Word) plus repository summarisation, with async pre-rendering (OCR, spreadsheet
  parsing) for large uploads.
- **FastAPI + Jinja2 + HTMX web UI** with no frontend build step, including a
  guided-tour engine (platform overview, story creation, resource addition,
  member management) and agent-type explainability.
- **Firebase authentication** with an admin-approval signup gate and a
  first-admin bootstrap bypass.
- **In-app notifications** — durable per-user notifications with unread state
  and read markers.
- **Four-service control plane** — web, council, git engine and management run
  as independently scalable Cloud Run services off one image.
- **Multi-council architecture** — Goal Review, Security, and Platform Research
  councils run as separate workers off the same image.
- **Git dependency-graph engine** — an optional worker that manages per-repo
  branches, sync, and merge queues for operator-story workspaces.
- **PostgreSQL data layer** with an additive-only, sequence-ordered migration
  policy validated in CI before any migration is applied.
- **Structured logging** with entity identifiers (goal, task, agent) on every
  state-mutating operation, so operators can reconstruct sessions from logs.
- **Single-model configuration reference** — every environment variable, feature
  flag, and secret name is defined in one place.

### Security

- Custom-tool credentials are encrypted at rest; plaintext is never persisted
  and never logged.
- SSRF guard on outbound custom-tool URLs — every resolved address is checked and
  the request is blocked if any resolves into a private, link-local, loopback, or
  reserved range.
- The TypeScript custom-tool tier runs in a Deno sandbox with network access only
  (no filesystem, environment, or subprocess), a default per-execution timeout, a
  hard cap, and an output-size cap.
- Per-agent JWT enforcement on internal endpoints, on by default.
- All sensitive credentials are held in a secret manager — never in source.
- Additive-only migration policy — column drops, renames, and type changes are
  prohibited and enforced in CI as the first step of every deploy.
- The append-only, SHA-256-chained ledger provides a tamper-evident audit trail.
- The kill switch's `halted` state is terminal (no restart, no resume),
  preventing silent resumption of aborted work.
- The agent gateway is reachable only over a VPC-internal connection; the public
  internet cannot address it directly.

---

For a deeper walk-through of the subsystems above, see the project
[`README.md`](README.md). Repository:
<https://github.com/Pilot-Wave-Technology/AgentOS>.

Copyright Port33, inc. Licensed under Apache License 2.0.

<!-- Compare links will be added once tags are cut. -->
<!-- [Unreleased]: https://github.com/Pilot-Wave-Technology/AgentOS/compare/v0.1.0...HEAD -->
<!-- [0.1.0]: https://github.com/Pilot-Wave-Technology/AgentOS/releases/tag/v0.1.0 -->
