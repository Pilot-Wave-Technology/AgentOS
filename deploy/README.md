# Deploying AgentOS — start here

> **AgentOS deploys in TWO stages, in this order:**
> **① the OpenClaw gateway (the `agent_os_openclaw` branch) — FIRST**, then
> **② this platform / control plane — SECOND.**
> The gateway creates the shared VPC and archive bucket (the gateway + LLM secrets come earlier, from `prerequisites.sh`);
> the platform reuses them and connects to the gateway. If you deploy the
> platform first, it has nothing to talk to and no shared infra to attach to.
>
> **Before file 05 here, you must have completed Stage ① — see
> `../agentos-openclaw/deploy/README.md`.**

This folder walks you through the whole thing, in order. **Do the files one at a
time, top to bottom.** Each file is one step and tells you exactly what to do and
why. No prior AgentOS knowledge is assumed.

> ⚠️ **Use ONE region everywhere.** The gateway VM, the Serverless connector,
> Cloud Run, and Cloud SQL must all be in the **same region** (and the gateway's
> zone must be inside it), or the control plane can't reach the gateway. Pick a
> region now (e.g. `us-central1`) and use it in `prerequisites.sh` and in **both**
> `terraform.tfvars` files.

## What you're building

```
   Your browser
        │  (login via Firebase)
        ▼
  ┌─────────────────────┐        ┌──────────────────────┐
  │  Control plane      │  talks │  Gateway VM          │
  │  (Cloud Run web app)│◄──────►│  (OpenClaw, runs the │
  │  goals, stories, UI │   to   │   AI agents)         │
  └─────────┬───────────┘        └──────────────────────┘
            │
     ┌──────┴───────┬───────────────┬──────────────┐
     ▼              ▼               ▼              ▼
  Postgres      Secret Mgr       Buckets       GitHub / Slack / LLM APIs
 (Cloud SQL)    (passwords)   (files/archive)     (integrations)
```

- **Control plane** — the web app you log into (Cloud Run). Manages goals,
  stories, agents, and talks to the gateway.
- **Gateway VM** — a small virtual machine that actually runs the AI agents
  (the "OpenClaw" gateway).
- Plus the supporting pieces: a **Postgres** database, **Secret Manager** for
  passwords/keys, **storage buckets**, and outside integrations
  (**GitHub**, **Firebase** for login, optional **Slack**, and **LLM API keys**).

Almost everything is created for you by **Terraform** (infrastructure-as-code).
Your job is mostly: create a few accounts/apps, paste some IDs and keys into a
config file, and run two `terraform apply` commands.

## The order (follow it exactly)

| # | File | What you do | ~Time |
|---|------|-------------|-------|
| ▶ | `warmup.md` | **Read first** — manifest of tools, accounts, and every file you'll download (exact names + paths) | 5 min |
| 0 | `00-prerequisites.md` | Create the GCP project, install tools | 15 min |
| 1 | `01-firebase-auth.md` | Set up login (Firebase) | 10 min |
| 2 | `02-github-app.md` | Set up Git integration (GitHub App) | 15 min |
| 3 | `03-slack-app.md` | **Optional** — Slack integration, set up *after* deploy in the admin UI (no deploy-time config) | 10 min |
| 4 | `04-collect-your-values.md` | Fill `secrets.env` with everything you gathered, **then run `./prerequisites.sh`** (this is file 04.2) | 10 min |
| **0️⃣** | **`./prerequisites.sh <PROJECT> <REGION>`** | **The run you did in file 04.2 — not a separate step.** ONE-TIME setup: creates service accounts + IAM + ALL secrets + the Cloud SQL instance, `control_panel` DB, and `app_user`/`migrate_user`. Run FIRST, repeatable. | ~10 min (SQL create) |
| **①** | **`../agentos-openclaw/deploy/README.md`** | **STAGE ① — deploy the OpenClaw gateway FIRST** | 20 min |
| 5 | `05-provision-infrastructure.md` | STAGE ② — Terraform: reuse the gateway VPC, create connector/bucket/Cloud-Run placeholders (DB + secrets already made by `prerequisites.sh`) | 5 min |
| 6 | `06-fill-in-secrets.md` | Put the **platform** keys/passwords into Secret Manager | 15 min |
| 7 | `07-build-and-deploy.md` | **Point the gateway at the platform + confirm it's READY (7.0)**, then build the app, load the database, go live | 20 min |
| 8 | `08-deploy-gateway-vm.md` | Verify the gateway link (URL was set pre-build in 7.0; gateway itself was Stage ①) | 5 min |
| 9 | `09-first-login-and-verify.md` | Log in as admin, run a test | 15 min |
| 10 | `10-operations-and-troubleshooting.md` | Day-2: updates, changing settings, repeatable teardown | reference |

## Things you'll need before you start
- A **Google Cloud** account where you can create a project and enable billing.
- A **GitHub** account **and an org** — agents create and work on goal repos
  there via a **GitHub App** you'll set up (`02-github-app.md`). Required.
- A credit/debit card on the Google Cloud account (there's a free tier, but a
  card is required).
- ~2 hours for a first-time setup.
- API keys for **both** LLM providers: **Anthropic (Claude)** and **Google
  Gemini**. (OpenAI is optional.)

> Tip: keep a scratch text file open. Every stage produces a value or two
> (an ID, a key, an email). File 04 is a worksheet to collect them all in one place.

When you're ready, open **`warmup.md`**.
