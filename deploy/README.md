# Deploying AgentOS — start here

> **AgentOS deploys in TWO stages, in this order:**
> **① the OpenClaw gateway (the `agent_os_openclaw` branch) — FIRST**, then
> **② this platform / control plane — SECOND.**
> The gateway creates the shared VPC and archive bucket (the gateway + LLM secrets come earlier, from `prerequisites.sh`);
> the platform reuses them and connects to the gateway. If you deploy the
> platform first, it has nothing to talk to and no shared infra to attach to.
>
> **Before file 05 here, you must have completed Stage ① — see
> `../agentos-openclaw/deploy/README.md`** (online:
> <https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_openclaw/deploy/README.md>).

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
  ┌──────────────────────────┐        ┌──────────────────────┐
  │ Control plane (Cloud Run)│  talks │  Gateway VM          │
  │ 5 services: web, council,│◄──────►│  (OpenClaw, runs the │
  │ git, management,scheduler│   to   │   AI agents)         │
  └──┬──────────────┬────────┘        └──────────────────────┘
     │              │ starts work on
     │              ▼
     │   ┌─────────────────────────────────┐
     │   │ Execution plane (GKE cluster)   │
     │   │ conductor + one executor pod    │
     │   │ per story — the work runs here  │
     │   └─────────────────────────────────┘
     │
     ├──────────────┬───────────────┬──────────────┐
     ▼              ▼               ▼              ▼
  Postgres      Secret Mgr       Buckets       GitHub / Slack / LLM APIs
 (Cloud SQL)    (passwords)   (files/archive)     (integrations)
```

- **Control plane** — five Cloud Run services: **web** (the app you log into),
  **council**, **git**, **management** and **scheduler** (background loops).
  Manages goals, stories, agents, and talks to the gateway.
- **Gateway VM** — a small virtual machine that actually runs the AI agents
  (the "OpenClaw" gateway).
- **Execution plane** — a GKE cluster (required): a **conductor** plus one
  **executor pod per story**. Story work (code, tests, commits) happens there.
- Plus the supporting pieces: a **Postgres** database (**Cloud SQL**),
  **Secret Manager** for passwords/keys, **storage buckets**, and outside
  integrations (**GitHub**, **Firebase** for login, optional **Slack**, and
  **LLM API keys**).

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
| 4 | `04-collect-your-values.md` | Fill `secrets.env` with everything you gathered, **then run `./prerequisites.sh`** (this is file 04.3) | 10 min |
| **0️⃣** | **`./prerequisites.sh <PROJECT> <REGION>`** | **The run you did in file 04.3 — not a separate step.** ONE-TIME setup: creates service accounts + IAM + every non-database secret. Cloud SQL and the GKE cluster are Terraform's (file 05). Run FIRST, repeatable. | ~2 min |
| **①** | **`../agentos-openclaw/deploy/README.md`** | **STAGE ① — deploy the OpenClaw gateway FIRST** | 20 min |
| 5 | `05-provision-infrastructure.md` | STAGE ② — Terraform: Cloud SQL + DSN secrets, registry, connector, buckets, 5 Cloud Run placeholders, GKE cluster (reuses the gateway VPC) | ~15 min |
| 6 | `06-fill-in-secrets.md` | Put the **platform** keys/passwords into Secret Manager | 15 min |
| 7 | `07-build-and-deploy.md` | **Point the gateway at the platform + confirm it's READY (7.0)**, then build (tests included), load the database, start the execution plane, go live | 20–40 min |
| 7.5 | `07-build-and-deploy.md` §7.5 | Publish the VS Code extension: `./publish-extension.sh agentos-vscode-<version>.vsix` (needs Python) | 2 min |
| 8 | `08-deploy-gateway-vm.md` | **Verify only** — prove the control plane and the gateway talk (nothing to deploy; the gateway was Stage ①, its URL set in 7.0) | 5 min |
| 9 | `09-first-login-and-verify.md` | Log in as admin, run a test | 15 min |
| 10 | `10-operations-and-troubleshooting.md` | Day-2: updates, changing settings, repeatable teardown | reference |
| 11 | `11-your-first-story.md` | **The product.** A goal, a repo, a story, a pull request you accept | 30 min |
| 12 | `12-teardown.md` | Remove it: `./teardown.sh` (or `--everything`); 12.6 = deploying again into the same project, which skips most of the above | 20 min |
| **✈** | **`./preflight.sh`** | **Not a step — run it between every pair of steps above.** Checks that what the last stage was supposed to create actually exists. See below. | 20 sec |

## `./preflight.sh` — run it between stages, not once at the end

```bash
gcloud container clusters get-credentials agentos --region <REGION>   # <prefix>; once, after file 05
PROJECT=my-project REGION=<REGION> ./preflight.sh
```
Pass `REGION=` (it defaults to `us-central1`) and fetch the cluster credentials
first, or every cluster check reports `??`.

> **Expected before the first build: `23 ok, 5 missing, 1 undetermined`, exit 1.**
> Until file 07 has run, `MISS` for the `agentos` namespace and its three
> in-cluster secrets (`conductor-secrets`, `execution-db`,
> `execution-sqlproxy-key`), and `??` for the `database` check (it runs inside
> the conductor; it is counted in both totals). The build in file 07 creates
> all of them. Everything else must already be `ok`.

Exit 0 means every secret, bucket, service account, IAM binding,
namespace, node pool and database grant AgentOS needs is present. Exit 1
means something is missing **or could not be determined**, and it prints
which, why it matters, and the command that fixes it.

**Why it is here at all.** Standing up a second environment of AgentOS
found eleven things that were not in the repository: they had been done
by hand, once, and nothing noticed they were missing because nothing
ever asked. Every one of them shipped a **green build**. A test suite
passes identically against an installation that cannot serve a request,
because none of this is in the code.

The failures are quiet and specific:

| What is missing | What you actually see |
|---|---|
| `openclaw-gateway-token` | every agent action fails as `E_KERNEL_UNREACHABLE` — reads as a network problem |
| a default-privileges grant | one page 500s, days later, after some unrelated deploy adds a table |
| the artifact bucket | criteria never start; the Job reports only that its init container exited 1 |
| a node taint | things get slower, and nothing reports anything |

**"Could not ask" is a failure here, not a pass.** Every check has three
outcomes — `ok`, `MISS`, and `??`. If `gcloud` cannot read a secret
because of an IAM binding, that is reported as *undetermined*, not as
missing — because the two have opposite fixes, and an operator told a
secret is missing will create a second one and make it worse.

Run it **after** each of the steps above and **before** the next. It
takes seconds; a deploy into a half-provisioned project is the expensive
case, because it comes up, serves some requests, and fails on the ones
you did not test.

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
