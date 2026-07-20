# Changelog

All notable changes to AgentOS are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
AgentOS is pre-1.0 (MAJOR `0`): per SemVer §4, the public API and internal
interfaces may change between minor versions without a MAJOR bump. Expect breaking
changes at any `0.MINOR` boundary until `1.0.0` is cut.

## [Unreleased]

Nothing yet. Changes land here before a version is cut.

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
- **Split and single deployment profiles** — the control plane runs either as
  four independently scalable Cloud Run services (web, council, git engine,
  management) or as a single always-on service, selected by configuration.
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
