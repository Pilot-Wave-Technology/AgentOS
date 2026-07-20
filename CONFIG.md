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
- **Inputs**: project, region, Firebase, GitHub App, admin email, DB size, buckets, `openclaw_ws_url`, sizing, `service_profile`, LLM provider toggles.
- **`flags { … }`**: every service behaviour (councils, security, platform research, goal summaries, git-graph, memory-v2, auth enforcement, chat, DAG ceiling, …). Defaults = the current running configuration.

Terraform turns these into the service's env vars automatically.

## 2. Secret values → Secret Manager (created by `prerequisites.sh`)
You do **not** create secrets by hand. `prerequisites.sh` creates **every**
secret with a version in one pass — internal keys are auto-generated, and your
external values come from `secrets.env` (a single-space placeholder is written
for anything you left blank, so nothing is ever missing). The DB DSNs
(`database-url`, `database-url-migrate`) are created there too.

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

Secrets (see the `secret_names` terraform output for the exact list):

| Secret | Source (all created by `prerequisites.sh`) |
|---|---|
| `app-secret-key` | auto-generated (or `openssl rand -hex 32` in `secrets.env`) |
| `internal-api-key` | auto-generated |
| `agent-token-signing-key` | auto-generated |
| `credential-encryption-key` | auto-generated (Fernet key; see `.env.example`) |
| `openclaw-gateway-token` | auto-generated; must match the gateway VM's token |
| `firebase-service-account` | from `FIREBASE_SERVICE_ACCOUNT_JSON_FILE` (`firebase-sa.json`) |
| `gemini-api-key` | `GEMINI_API_KEY` in `secrets.env` (Google AI Studio) |
| `claude-api-key` | `CLAUDE_API_KEY` in `secrets.env` (Anthropic console) |
| `openai-api-key` | `OPENAI_API_KEY` in `secrets.env` (optional) |
| `github-app-key` | from `GITHUB_APP_KEY_FILE` (`github-app-key.pem`) |
| `github-webhook-secret` | `GITHUB_WEBHOOK_SECRET` in `secrets.env` (recommended; empty = webhook signature check disabled) |
| `slack-client-secret`, `slack-signing-secret` | normally set via the admin UI after deploy (Settings → Integrations → Slack) |
| `database-url`, `database-url-migrate` | Cloud SQL DSNs written by `prerequisites.sh` |

## 3. Local development → `.env`
`cp .env.example .env` and fill it in (points at a local/proxied Postgres and a
gateway). `.env.example` documents every knob with defaults + how-to-obtain.

## Source of truth
`app/config.py` is the code-level source of truth for setting names/defaults.
`terraform.tfvars.example` and `.env.example` mirror it; if you add a setting to
`config.py`, add it to both.
