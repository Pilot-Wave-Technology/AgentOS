# Contributing to AgentOS

Thanks for helping make AgentOS better — it improves one focused change at a time.
This is the complete contributor guide for the whole repository: the branch model,
which branch to target, the conventions each one expects, and how to get a change merged.

## The repository: three branches, separate histories

AgentOS is **one repository with three orphan branches** (they don't share history):

| Branch | What it is | Stack | Target it for |
|---|---|---|---|
| **`main`** (default) | Docs + governance — this guide, the deployment map, license, community-health files. No application code. | — | Docs, the deployment guide, governance |
| **`agent_os_platform`** | The **control plane, kernel, and execution plane** — goals, stories, executions, assistant memory, platform integrations, the web UI, the conductor, and executor profiles. | Python 3.12 · FastAPI · PostgreSQL · Jinja2 + HTMX | API, services, migrations, web UI, Slack channel, `kernel/`, `profiles/` |
| **`agent_os_openclaw`** | The **agent gateway** — the OpenClaw-derived runtime that hosts agents, manages workspaces, and makes LLM calls. | Node.js · TypeScript · pnpm | Gateway behaviour, extensions, agent runtime, workspaces, provisioning |

**Base your feature branch on the branch you're changing, and open the PR against
that same branch.** A control-plane change never belongs on the gateway branch, and
vice-versa. A change that spans both is two coordinated PRs — one per branch, linked.

Each organization runs AgentOS from its **own fork**; if you're standing up a
deployment rather than contributing upstream, start at [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Ground rules (all branches)

- **Small, focused PRs** beat sweeping ones — one concern, one layer. Split large features into a sequence of additive PRs.
- **Business logic lives in services / extensions** — routers, platform adapters, and workers stay thin transport.
- **Human-in-the-loop** — don't build anything that silently advances a goal, story, or execution past a human gate.
- **Never commit secrets** — tokens, service-account JSON, API keys, DB URLs with passwords. If you spot one already committed, report it privately (see Security), not in a public issue.
- **Clarity over cleverness** — explain the *why* in commit messages (imperative subject) and the PR description.

## Control plane — `agent_os_platform`

Local setup (a Postgres, running the server, the pre-push hook) is in that branch's
`documentation/DEVELOPMENT.md`; configuration options in `CONFIG.md`.

- **Python 3.12 / FastAPI.** Business logic in `app/services/`. Routers (`app/routers/`) parse/validate/render; platform adapters (`app/channels/<platform>/`) translate a platform's wire format to/from service calls. Neither carries business logic.
- **Jinja2 + HTMX front end — no build step.** Server-rendered templates + HTMX + plain CSS/JS; don't introduce a bundler or transpile.
- **Kernel and executors.** `kernel/` is the kernel plane — the conductor (admission, dispatch, leases, reconciliation), the executor harness that runs inside each pod, and the verifier. It contains no model call; keep it that way. What an executor may do is defined by **executor profiles** in `profiles/` (a TOML file, a prompt, optional skills) — a profile is data, and its tool surface is fail-closed. Read `HOW_A_STORY_EXECUTES.md` before changing either.
- **Database migrations follow [`migrations/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/migrations/README.md).** Two rules matter most: **never edit a migration that has been applied** (it changes nothing on databases that already ran it — add a new one), and **expand, then contract** (add the new column or table, deploy, backfill, and only later remove the old one) so old and new code can run against the same database during a rolling deploy. Add the file, append its name to `migrations/SEQUENCE` (the source of truth for order), and commit them together; the **pre-push hook validates `SEQUENCE`**.
- **Structured logging** with entity ids (`goal_id`, `story_id`, `execution_id`, `agent_id`) so an operator can reconstruct events from logs alone.

## Agent gateway — `agent_os_openclaw`

The gateway is derived from [OpenClaw](https://github.com/openclaw/openclaw) (MIT);
the AgentOS additions are Apache 2.0 © Port33, inc. **Keep the fork thin — every diff
against upstream is a cost paid on every future rebase:**

- **AgentOS-authored work lives in `extensions/`,** not in patched core `src/`.
- **Prefer consuming an existing interface** (RPC method, HTTP endpoint, broadcast event) over patching core source.
- **Touch core `src/` only when you must** — and only for a genuine upstream contribution or a documented AgentOS core patch (catalogued in `ARCHITECTURE.md`).
- **Don't restructure the tree** (`apps/`, `packages/`, `src/`, `ui/`, `extensions/`, `pnpm-workspace.yaml`) — the build depends on it.
- Node.js / TypeScript via **pnpm**: `pnpm install`, `pnpm build`, `pnpm check` (format/type/lint), `pnpm test`. Keep modules small and cohesive.

A change that belongs in OpenClaw itself is better sent
[upstream](https://github.com/openclaw/openclaw) than carried here as fork weight.

## Test before you open a PR

Run the branch's tests **and** exercise the change end-to-end — drive the affected
page, endpoint, or command; don't stop at green unit tests.

On `agent_os_platform`, the suite runs against a real PostgreSQL built from
`migrations/SEQUENCE`:

```bash
INTERLOCK_TEST_DSN=postgresql://… scripts/ci/run_suite.sh   # inside the test image (docker build --target test)
bash migrations/check_sequence.sh                          # SEQUENCE and the migrations directory agree
```

The suite fails if too many tests skip, because a database that did not start
makes database-backed tests skip rather than fail. For docs-only changes,
confirm every internal link you touched still resolves. If you built the change with
an AI coding agent, say so in the PR and confirm you understand what it does.

## Submitting a pull request

1. **Fork**, and branch off the branch you're targeting (short, descriptive name, e.g. `fix/execution-lease-race` or `feature/insights-token-panel`).
2. Keep it focused; verify migrations (platform) or keep the fork thin (gateway); test end-to-end.
3. Open the PR **against the branch you based it on** and fill in the
   [pull request template](.github/pull_request_template.md) — it prompts for *what*, *why* (link the issue), *how you tested*, and any non-obvious design decisions.

## Issues, discussions, security

- **Bugs / feature requests:** [GitHub Issues](https://github.com/Pilot-Wave-Technology/AgentOS/issues) — pick the matching form and note which component (control plane / gateway / docs & deploy).
- **Questions / design discussion:** [GitHub Discussions](https://github.com/Pilot-Wave-Technology/AgentOS/discussions).
- **Security-sensitive reports:** never a public issue — use private [Security Advisories](https://github.com/Pilot-Wave-Technology/AgentOS/security/advisories). See [SECURITY.md](SECURITY.md).

Be kind and constructive — participation follows our [Code of Conduct](CODE_OF_CONDUCT.md) (Contributor Covenant).

## License

Apache License 2.0 (see [LICENSE](LICENSE)), maintained by Port33, inc. The gateway
branch additionally carries upstream OpenClaw's MIT license for the code it derives
from. By contributing, you agree your contributions are licensed under Apache 2.0.
