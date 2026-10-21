# AgentOS — configuration guide

Everything configurable, and where you set it. There are three surfaces:

| Surface | What | When you edit it |
|---|---|---|
| `secrets.env` (from `secrets.env.example`) | **Secret values** — read by `prerequisites.sh` when it creates the secrets | Once, before `prerequisites.sh` |
| `terraform/terraform.tfvars` | **All non-secret config + every feature flag** | Deploying / changing behaviour |
| Secret Manager | **All secret values** live here (created + filled by `prerequisites.sh`); overwrite with `./set-secret.sh` | Whenever a secret rotates |
| `.env` (from `.env.example`) | Same settings, for **local development** only | Running locally without terraform |

For a Cloud Run deployment you touch **secrets.env** (secret values, once, for
`prerequisites.sh`) and **terraform.tfvars** (config + flags). `.env.example` is
the full human-readable reference for what every setting does; you only create a
real `.env` for local dev.

## 1. Config + flags → `terraform/terraform.tfvars`
Copy `terraform/terraform.tfvars.example` and edit it. It has:
- **Inputs**: project, region, Firebase, GitHub App, admin email, DB size, buckets, `openclaw_ws_url`, sizing, LLM provider toggles.
- **`flags { … }`**: every service behaviour (councils, security, platform research, goal summaries, git-graph, memory-v2, auth enforcement, chat, DAG ceiling, …). Defaults = the recommended defaults.

Terraform turns these into the service's env vars automatically.

## 2. Secret values → Secret Manager (created by `prerequisites.sh`)
You do **not** create secrets by hand. `prerequisites.sh` creates **every**
secret with a version in one pass — internal keys are auto-generated, and your
external values come from `secrets.env` (a single-space placeholder is written
for anything you left blank, so nothing is ever missing -- the app reads a
whitespace-only value as *not configured*, so the feature behind it stays off
rather than half-working). The exception is the DB DSNs (`database-url`,
`database-url-migrate`, `database-url-conductor`): `terraform apply` writes
those, with the logins they describe.

**To change a secret afterwards**, overwrite it with the repo-root helper —
**never** raw `gcloud secrets versions add` (it can write an empty value and
leaves stale versions live behind `:latest`):

```bash
printf %s "<value>" | ./set-secret.sh <secret-name>
```

`set-secret.sh` refuses an empty value and disables every older version.

**Multi-line secrets** (the Firebase service-account JSON and the GitHub App
`.pem`) are not pasted anywhere. Drop each file in the repo root and point a
`*_FILE` variable at it in `secrets.env` — `prerequisites.sh` reads the file and
creates the secret:

```
FIREBASE_SERVICE_ACCOUNT_JSON_FILE=firebase-sa.json
GITHUB_APP_KEY_FILE=github-app-key.pem
```

Secrets (`prerequisites.sh` is the full list; the `secret_names_created_here`
terraform output lists only the DSN secrets terraform writes):

| Secret | Source (all created by `prerequisites.sh`) |
|---|---|
| `app-secret-key` | auto-generated (or `openssl rand -hex 32` in `secrets.env`) |
| `internal-api-key` | auto-generated |
| `agent-token-signing-key` | auto-generated |
| `credential-encryption-key` | auto-generated (Fernet key; see `.env.example`) |
| `openclaw-gateway-token` | auto-generated; must match the gateway VM's token |
| `firebase-service-account` | from `FIREBASE_SERVICE_ACCOUNT_JSON_FILE` (`firebase-sa.json`) |
| `gemini-api-key` | `GEMINI_API_KEY` in `secrets.env` (Google AI Studio) |
| `anthropic-api-key` | `ANTHROPIC_API_KEY` in `secrets.env` (Anthropic console) |
| `openai-api-key` | `OPENAI_API_KEY` in `secrets.env` (optional — **wired, no shipped consumer**: see below) |
| `github-app-key` | from `GITHUB_APP_KEY_FILE` (`github-app-key.pem`) |
| `github-webhook-secret` | `GITHUB_WEBHOOK_SECRET` in `secrets.env`, else **generated** — paste the value into the GitHub App (file 02) |
| `slack-client-secret`, `slack-signing-secret` | normally set via the admin UI after deploy (Settings → Integrations → Slack) |
| `control-panel-url` | created blank; Cloud Build writes the web service's URL after each deploy (read by the gateway and the conductor) |
| `gateway-repo-token` | `GATEWAY_REPO_TOKEN` in `secrets.env` — a token to clone a **private** fork; blank for a public one |
| `database-url`, `database-url-migrate`, `database-url-conductor` | Cloud SQL DSNs written by `terraform apply` |

### On `openai-api-key`
It is carried end to end — into the Cloud Run service and into the gateway's
provider config — and **nothing in AgentOS calls OpenAI**. Every shipped
agent, council and executor runs on Gemini or Anthropic. The slot exists so
that a model you register yourself has somewhere to read a key from; until
you register one, leaving it blank changes nothing. `openai_enabled = false`
in `terraform.tfvars` is the honest default.

## 3. Per-goal executor settings → the goal's Model Configuration page
Not files: a goal admin sets these in the UI, per goal, and they apply to that
goal's executor runs —

| Setting | Range | Blank means |
|---|---|---|
| Executor model, verifier model | any model in the registry | the default for the work |
| Machine size | `small`, `medium`, `large` | the profile's size, else `small` |
| Tokens per leg | 20,000 – 1,000,000, capped to what the model can hold with room for its reply | the model's default |
| Max legs | 1 – 200, or `-1` for no limit | 12 |

They are frozen onto a run at dispatch (`executions.leg_budget`, migration
`0008_goal_leg_budget`), so a change affects the next run, not a running one,
and they govern the work phase, not the review. See
[`HOW_A_STORY_EXECUTES.md`](HOW_A_STORY_EXECUTES.md) §10.

## 4. Local development → `.env`
`cp .env.example .env` and fill it in (points at a local/proxied Postgres and a
gateway). `.env.example` documents every knob with defaults + how-to-obtain.

## Source of truth
`app/config.py` is the code-level source of truth for setting names/defaults.
`terraform.tfvars.example` and `.env.example` mirror it; if you add a setting to
`config.py`, add it to both.
