# Vision

## Why AgentOS exists

LLM agents are good at doing work and bad at knowing when to stop and ask. Most agent frameworks treat human oversight as an afterthought — a log you can read, a button you can press if something looks wrong. AgentOS treats it as the architecture.

The premise is simple: real work is long-running and goal-shaped. A goal is too big for one prompt, so AgentOS decomposes it into stories, and stories into tasks, and hands each layer to a dedicated agent. But between every layer there is a human gate. No story starts until a person accepts it. No task output ships until a person approves it. The platform's job is to propose; the operator's job is to dispose.

The result is a control plane you can point a fleet of agents at and trust to stop, not just to go fast. State, memory, credentials, and platform integrations live in the control plane. Agent execution — the actual LLM calls and tool use — lives in the companion gateway, the [`agent_os_openclaw`](https://github.com/Pilot-Wave-Technology/AgentOS) branch of this repository. Neither is useful without the other, and that separation is deliberate: the thing that decides is not the thing that acts.

## Principles

**Traceability is not optional.** Every state-mutating operation writes an event to an append-only ledger with SHA-256 hash chaining. Projection tables are derived from events, never the reverse. If you cannot reconstruct what happened and why from the ledger, that is a gap worth fixing before adding new capability. Nothing is deleted; everything is accountable.

**Human approval is a first-class operation.** Story acceptance, task approval, and plan revision are core verbs in the system, not bolt-on confirmations. Agents pause and wait at each gate — they do not advance without an explicit accept, reject, or revise. If a task could slip past its approval gate without a human decision, that is a severity-one bug, not a missing feature.

**Agents should remember.** The per-(goal, user) assistant builds a persistent, three-scoped memory — global, goal, and conversation — with full-text search. It carries preferences, past decisions, and conventions across sessions, so the assistant gets more useful over time rather than merely longer-running.

**Meet operators where they already work.** Slack is a complete operational surface, not a notification firehose. You can create tasks, approve work, and converse with agents from the channel you already live in. The web UI is one surface among several, not the only door in.

**Extensibility without a redeploy.** Admins define custom tools — HTTP endpoints, OpenAPI specs, TypeScript functions, MCP servers — through the UI, and agents pick them up within a minute. No gateway restart, no code deploy. The capability of your fleet is something you shape at runtime, safely, behind SSRF guards and encrypted credentials.

**A real off switch.** Operators can pause or force-kill work at system or goal scope. Pause is reversible; force-kill is terminal and one-way. High autonomy is only responsible when stopping is as easy as starting.

## Where it's headed

The near-term work is about making the guarantees above unbreakable and the operator experience effortless:

- **Deepening the custom-tools system** so teams can extend what their agents can do without ever touching the codebase.
- **Deepening memory** so cross-conversation context compounds and the assistant becomes a genuine collaborator, not a stateless responder.
- **Full support for a wide array of language models** so teams can match the model to each agent role, task, and cost profile across providers.
- **Broadening platform integrations** so that once an integration exists for your channel of choice, you rarely need the web UI at all.
- **Formalizing the review-council and kill-switch model** as the standard safety layer for higher-autonomy work — an explicit, auditable review stage rather than a bolt-on.
- **Hardening the control-plane ↔ gateway connection** — goals stall the moment that link degrades, so observability and graceful degradation there matter as much as any new feature.

## What we will not build

- **Business logic hidden in platform adapters.** Slack handlers and every other adapter stay thin translation layers over shared services, so behavior is defined once and observed everywhere.
- **Destructive database migrations.** The migration system is additive-only by design, because branches and deployments must safely share history.

This list is a guardrail, not a permanent boundary. A strong case can move it — but the case has to address why the guardrail existed in the first place.

## Contributing to the vision

AgentOS is open source under Apache 2.0, stewarded by Port33, inc. It is built to be forked: each organization runs it from its own fork, with its own credentials and integrations. We favor small, additive changes over sweeping rewrites — one pull request, one concern — and we hold the human-gate guarantee above all else. A change that silently lets a goal, story, or task advance past a human gate will not be merged, no matter how good the rest of it is.

If that principle resonates, [`CONTRIBUTING.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/main/CONTRIBUTING.md) is where the engineering values and conventions behind this vision live.
