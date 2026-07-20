# Security Policy

## Reporting a vulnerability

**Do not open a public issue for security problems.** Report privately through
GitHub [Security Advisories](https://github.com/Pilot-Wave-Technology/AgentOS/security/advisories/new)
so the issue can be triaged and fixed before disclosure.

Please include:
- the affected component — **control plane** (`agent_os_platform`), **gateway** (`agent_os_openclaw`), or **docs / deploy** (`main`),
- a description and the impact,
- steps to reproduce and any relevant logs — **with all credentials redacted**.

We aim to acknowledge reports promptly and will coordinate a fix and a disclosure
timeline with you.

## Scope

AgentOS spans three branches; the security-relevant surface lives in two of them:

- **`agent_os_platform` (control plane)** — a Firebase-authenticated web app. Relevant areas: per-goal authorization, the kill switch, the custom-tools broker and its SSRF guards, and secret handling — all secrets live in Secret Manager, never in the repository.
- **`agent_os_openclaw` (gateway)** — a VPC-internal runtime that hosts agents. It uses per-agent scoped tokens for calls back to the control plane and is never public-facing. It is a fork of [OpenClaw](https://github.com/openclaw/openclaw) (MIT): report issues in the AgentOS additions here; issues in unmodified upstream code should also be reported to OpenClaw.
- **`main`** — documentation and governance only; no runtime.

Each organization runs AgentOS from its **own fork** with its own credentials.

Maintained by Port33, inc. under the Apache License 2.0.
