# Development guide

This guide takes you from a fresh checkout to a running local instance of the
AgentOS **control plane** (the `agent_os_platform` branch). Read
[../README.md](../README.md) first for the project overview, and
[../CONFIG.md](../CONFIG.md) for the full configuration reference.

The control plane is Python 3.12 / FastAPI / PostgreSQL, with a Jinja2 + HTMX
frontend that has **no build step**. For most control-plane work you can point
at a remote gateway instead of running one locally — the web UI and nearly all
APIs start without a live gateway; only the agents themselves need it.

Everything below is copy-paste runnable from a POSIX shell (bash / zsh on macOS
or Linux, Git Bash on Windows) with obvious placeholder substitutions.

---

## Contents

- [Prerequisites](#prerequisites)
- [Clone and virtualenv](#clone-and-virtualenv)
- [Configuration (`.env`)](#configuration-env)
- [PostgreSQL and migrations](#postgresql-and-migrations)
- [Run it](#run-it)
- [Pointing at a gateway](#pointing-at-a-gateway)
- [Tests and checks](#tests-and-checks)
- [The migration workflow](#the-migration-workflow)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Tool | Why |
|---|---|
| **Python 3.12** | The runtime targets `python:3.12-slim`; match it locally to avoid wheel/type drift. |
| **PostgreSQL** | The control plane stores all state here (asyncpg, no ORM). A local Docker Postgres is the quickest path. |
| **git** | To clone the repo; also invoked at runtime for repository summarisation. |
| **Node.js** | Only if you intend to hack on the gateway (`agent_os_openclaw`). Not needed for control-plane development. |

Optional, only for the code paths that use them:

- A **Firebase** project (email/password sign-in + a service-account key) for
  authentication.
- **LLM API keys** (Anthropic and/or Google Gemini) to exercise agents and
  memory summarisation.
- A **GitHub App** if you touch the code paths that mint per-task repository
  credentials.

A minimal "just run the UI" setup can stub the gateway, GitHub App, and LLM
values and still reach the goals page. A full end-to-end setup needs all of the
above — see [../CONFIG.md](../CONFIG.md).

---

## Clone and virtualenv

Clone the control-plane branch:

```bash
git clone -b agent_os_platform https://github.com/Pilot-Wave-Technology/AgentOS.git
cd AgentOS

python -m venv .venv
source .venv/bin/activate          # macOS / Linux
# source .venv/Scripts/activate     # Windows (Git Bash)
# .venv\Scripts\activate            # Windows (cmd / PowerShell)

pip install --upgrade pip
pip install -r requirements.txt
```

Two dependencies are worth knowing about:

- `asyncpg` builds a C extension. On macOS run `xcode-select --install`; on
  Debian/Ubuntu install `python3-dev` and `libffi-dev` before `pip install` if
  the build fails.
- `websockets` is the client used to reach the gateway.

---

## Configuration (`.env`)

Configuration is loaded from a local `.env` file (via
[`pydantic-settings`](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)).
Every variable maps to a field on the `Settings` class in `app/config.py`,
which is the code-level source of truth for names, types, and defaults.

Copy the template and fill it in:

```bash
cp .env.example .env
```

`.env.example` documents every knob with its default and how to obtain each
value; **[../CONFIG.md](../CONFIG.md) is the full reference** for what each
setting does. The minimum to reach the goals page:

- `DATABASE_URL` — PostgreSQL DSN (see the next section).
- `FIREBASE_PROJECT_ID`, `FIREBASE_WEB_API_KEY`, `FIREBASE_SERVICE_ACCOUNT_JSON`
  — Firebase auth (the service account may be a JSON string or a file path).
- `BOOTSTRAP_ADMIN_EMAIL` — the Firebase account email auto-created as admin on
  first login; it bypasses the signup-approval gate.
- `OPENCLAW_WS_URL` and `OPENCLAW_AUTH_TOKEN` — the gateway WebSocket URL and
  its pre-shared token (see [Pointing at a gateway](#pointing-at-a-gateway)).
- `APP_SECRET_KEY`, `INTERNAL_API_KEY` — random secrets
  (`openssl rand -hex 32`).

Feature flags — councils, memory, git-graph engine, chat queue, task-DAG
ceiling, auth enforcement — default to safe values in `app/config.py`; enable
them as you exercise the corresponding code paths. Never commit `.env`; it is
git-ignored.

---

## PostgreSQL and migrations

AgentOS uses PostgreSQL directly through `asyncpg` (no ORM). Schema is created
and evolved by plain SQL migrations applied **in the order declared in
`migrations/SEQUENCE`** — not the filesystem sort.

### Bring up a local database

The quickest path is Docker:

```bash
docker run --name agentos-pg \
  -e POSTGRES_USER=agentos \
  -e POSTGRES_PASSWORD=agentos \
  -e POSTGRES_DB=agentos \
  -p 5432:5432 \
  -d postgres:17
```

Then set the DSN in `.env`:

```
DATABASE_URL=postgresql://agentos:agentos@localhost:5432/agentos
```

Any reachable PostgreSQL works; Docker is just convenient.

### Apply migrations

`migrations/apply.py` takes a DSN as its first argument, or reads `DATABASE_URL`
from the environment:

```bash
python migrations/apply.py "$DATABASE_URL"
# or, with DATABASE_URL exported:
python migrations/apply.py
```

The runner is idempotent: each migration is applied once inside its own
transaction and recorded in a `_migrations` tracking table, so re-running is
safe and already-applied migrations are skipped. A fresh install applies the
squashed baseline (`0001_baseline_schema`) followed by the baseline seed, then
any later migrations in `SEQUENCE`.

To wipe and re-apply a local/test database from scratch:

```bash
python migrations/reset_db.py "$DATABASE_URL"   # DESTRUCTIVE — dev/test only
```

---

## Run it

`app.main` runs the web app **and**, unless you turn them off, the background
loops in the same process — which is what you want locally:

```bash
uvicorn app.main:app --reload --port 8000
```

Then open `http://localhost:8000` and sign in with the Firebase account whose
email matches `BOOTSTRAP_ADMIN_EMAIL`. That account is auto-created as admin on
first login; any other account lands on `/signup` and waits for admin approval.

If you want the web surface alone, with no loops running under it:

```bash
AGENTOS_SCHEDULER_LOOPS=0 uvicorn app.main:app --reload --port 8000
```

That switch is the same one the deployed split uses: in Cloud Run the web
service sets it to `0`, and a dedicated scheduler service
(`app.scheduler_worker`, one instance) runs the background loops behind one
leader. The council, git-engine and management services each run their own
workers. For how those map onto a deployed
footprint, see the deploy guide
([../deploy/README.md](../deploy/README.md)).

Verbose logs: set `LOG_LEVEL=DEBUG` (respected by `app/config.py`), which also
turns on gateway-frame and database-pool debug output. Logs are emitted as one
JSON object per line; for readable local output, pipe through
[`jq`](https://jqlang.github.io/jq/):

```bash
LOG_LEVEL=DEBUG uvicorn app.main:app --reload --port 8000 2>&1 | jq -R 'fromjson? // .'
```

---

## Pointing at a gateway

Agents run on the **gateway** — the `agent_os_openclaw` branch of this repo, a
separate Node/TypeScript service. The control plane talks to it over a
WebSocket configured by two settings:

- `OPENCLAW_WS_URL` — e.g. `ws://localhost:18789` for a local gateway, or the
  URL of a shared/remote gateway.
- `OPENCLAW_AUTH_TOKEN` — the pre-shared token; it must match the token
  configured on the gateway.

For most control-plane development you do **not** need to run a gateway
locally — point `OPENCLAW_WS_URL` at an existing remote gateway. The web UI and
APIs start fine without one; only actual agent responses require a reachable,
token-matched gateway. If you are changing agent-runtime behaviour, run the
gateway from the `agent_os_openclaw` branch and point `OPENCLAW_WS_URL` at it.

---

## Tests and checks

The suite runs with [pytest](https://docs.pytest.org/) against a real
PostgreSQL database built from `SEQUENCE`. Run it the way Cloud Build does, from
the repo root:

```bash
INTERLOCK_TEST_DSN=postgresql://agentos:agentos@localhost:5432/agentos_test \
  scripts/ci/run_suite.sh
```

The script is more than a pytest call: database-backed tests skip when no
database is reachable, so it fails the run if too many tests skip, rather than
going green on a fraction of the suite. Cloud Build runs it inside the image's
`test` stage, so the packages under test are the packages the app runs with.

The other automated gate is **migration consistency**: every `.sql` and `.py`
migration in `migrations/` must appear in `SEQUENCE` exactly once and vice-versa, and the
sequence must apply cleanly against a fresh database. Before opening a pull
request that touches `migrations/`, apply the full sequence against a throwaway
database (`python migrations/reset_db.py "$DATABASE_URL"`) to confirm it builds
from scratch.

---

## The migration workflow

Migrations are **additive and backwards-compatible** — code from another branch
may run against a database this branch has already migrated. Never edit an
applied migration; its content is immutable once it has run anywhere. Fix
mistakes by adding a **new** sequential migration.

Rules (never break these):

- **Never** drop or rename columns/tables, or change a column type.
- Use `ADD COLUMN IF NOT EXISTS` (with a default or nullable) and
  `CREATE TABLE IF NOT EXISTS`.
- **Never** drop or modify a CHECK constraint — only add new allowed values.
- One migration per concern: split unrelated changes into separate files so a
  future migration can depend on just the part it needs.

To add a migration:

1. Create `migrations/<descriptive_name>.sql` following the rules above (or a
   `.py` migration, for a change that is easier to express in code — a `.py`
   file goes in `SEQUENCE` exactly like a `.sql` one).
2. Append `<descriptive_name>` (without the extension) to `migrations/SEQUENCE`
   at the correct position — after anything it depends on, otherwise at the end.
3. Apply it locally: `python migrations/apply.py "$DATABASE_URL"`.
4. Commit the migration file and the updated `SEQUENCE` **together**.

When merging another branch, insert its new `SEQUENCE` entries at the correct
position (a migration must come after whatever creates the tables/columns it
uses), then re-apply against a fresh database to confirm the order is valid.

---

## Troubleshooting

**`uvicorn app.main:app` fails with `ModuleNotFoundError`.** The virtualenv
is not active, or `pip install -r requirements.txt` did not finish. Reactivate
and re-run pip.

**`asyncpg` install fails with a compiler error.** You need a C toolchain and
the Python headers — `xcode-select --install` on macOS, `python3-dev` +
`libffi-dev` on Debian/Ubuntu.

**Sign-in loops between `/login` and `/signup`.** `BOOTSTRAP_ADMIN_EMAIL` does
not match the account you signed in with. Set it to your Firebase account
email, restart, and sign in again.

**Startup succeeds but agents never respond.** The gateway handshake failed.
Confirm `OPENCLAW_WS_URL` is reachable from the process and `OPENCLAW_AUTH_TOKEN`
matches the gateway's token.

**`apply.py` reports a migration missing from `SEQUENCE` (or vice-versa).**
A `.sql` or `.py` migration and `SEQUENCE` are out of sync — usually after a merge. Add the
missing `SEQUENCE` entry at the correct position.

**`.env` values are not picked up.** `pydantic-settings` loads `.env` from the
current working directory. Start `uvicorn` from the repo root, or export the
variables explicitly.

**Port `8000` is already in use.** Another process holds the port. Kill it or
pass a different `--port`.

---

AgentOS is open source under the Apache 2.0 license, maintained by Port33, inc.
See [../README.md](../README.md) and [../CONFIG.md](../CONFIG.md) for more.
