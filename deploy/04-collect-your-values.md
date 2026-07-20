# 4 · Fill in `secrets.env` and run `prerequisites.sh`

Now put your **secret** values into `secrets.env` and run `prerequisites.sh`
**once**. That script creates the service accounts + IAM and **every secret**
(auto-generating internal keys, using your values where you gave them, and
leaving a blank version otherwise so nothing is ever missing). Non-secret values
go into the `terraform.tfvars` files later (file 05 + the gateway stage).

## 4.1 Create `secrets.env`
```bash
cd agentos-platform          # your agent_os_platform clone
cp secrets.env.example secrets.env
```
Open `secrets.env` and fill it in. AgentOS **requires** a Gemini key, an Anthropic
(Claude) key, **Firebase**, and a **GitHub App**. OpenAI is optional. (**Slack**
needs nothing here — it's configured in the admin UI after deploy; see file 03.)

| Key | When | Notes |
|-----|------|-------|
| `GEMINI_API_KEY` | **required** | Google Gemini (agents / summaries) |
| `CLAUDE_API_KEY` | **required** | Anthropic Claude (agents / councils) |
| `OPENAI_API_KEY` | optional | extra provider |
| `FIREBASE_SERVICE_ACCOUNT_JSON_FILE` | **required** (login) | *pointer*, not inline — set to `firebase-sa.json` (the file from 01.4 in the repo root); `prerequisites.sh` reads it |
| `GITHUB_APP_KEY_FILE` | **required** (agent git work) | *pointer*, not inline — set to `github-app-key.pem` (the file from 02.5 in the repo root); `prerequisites.sh` reads it |
| `GITHUB_WEBHOOK_SECRET` | recommended (optional) | single-line, ok here; empty = webhook signature verification disabled |
| **`GATEWAY_REPO_TOKEN`** | **if your fork is PRIVATE** | Fine-grained GitHub PAT, **Contents: Read-only on your fork**, so the gateway VM can clone it. Blank = public fork. **Generation steps in 4.5 below.** |
| `APP_SECRET_KEY`, `INTERNAL_API_KEY`, `AGENT_TOKEN_SIGNING_KEY`, `OPENCLAW_GATEWAY_TOKEN`, `CREDENTIAL_ENCRYPTION_KEY` | leave blank | auto-generated for you |

> The **GitHub App** id + installation id go in `terraform.tfvars` (file 05); its
> private-key `.pem` is loaded automatically by `prerequisites.sh` via the `*_FILE`
> pointer below. Create the App first — see `02-github-app.md`.

> **Multi-line secrets use a file pointer, not inline text.** The **Firebase
> service-account JSON** and the **GitHub App `.pem`** are multi-line — instead of
> pasting them into `secrets.env`, drop each file in the repo root and point its
> `*_FILE` variable at it in `secrets.env`:
> ```
> FIREBASE_SERVICE_ACCOUNT_JSON_FILE=firebase-sa.json
> GITHUB_APP_KEY_FILE=github-app-key.pem
> ```
> `prerequisites.sh` reads those files and creates the secrets for you (step 4.2) —
> no separate load step, no `set-secret.sh` for these.

## 4.2 Run the prerequisites (one-time, repeatable)
```bash
./prerequisites.sh <PROJECT_ID> <REGION>      # e.g. ./prerequisites.sh my-agentos-472913 us-central1
```
This is the one script that stands up the shared foundation. It creates:
- **APIs** enabled on the project.
- **Service accounts** `agentos-gateway` + `agentos-runtime` (+ IAM, including the
  Cloud Build deploy grants).
- **All secrets**, with a version each (your value, or a placeholder to overwrite later).
- **Cloud SQL**: the Postgres **instance** (`agentos-db`, ENTERPRISE, `DB_TIER`/
  `DB_DISK_GB`/`DB_VERSION` — defaults `db-custom-2-8192` / 250 GB / POSTGRES_17),
  the **`control_panel` database**, and two **users** — `app_user` (runtime, DML)
  and `migrate_user` (migrations, DDL) — plus their **DSN secrets** `database-url`
  and `database-url-migrate`.

> ⏱️ **The Cloud SQL instance takes several minutes** to create — the script waits
> for it to reach `RUNNABLE` before finishing. That's expected; let it run. Safe to
> re-run — it never rotates existing secrets/passwords or recreates the instance.

> The table *grants* (which schema privileges `app_user` gets) are applied later,
> automatically, by the migration step in file 07 — you don't do anything for them.

## 4.3 Verify the file-based secrets loaded
The Firebase JSON and the GitHub App `.pem` are loaded **automatically** by
`prerequisites.sh` (4.2) from the `*_FILE` paths you set in `secrets.env` — there's
no separate load step. Confirm both are populated (not the 1-byte placeholder):
```bash
for s in firebase-service-account github-app-key; do
  printf '%s: %s bytes\n' "$s" \
    "$(gcloud secrets versions access latest --secret="$s" --project <PROJECT_ID> | wc -c)"
done
```
Hundreds+ of bytes each = loaded. If either shows ~1 byte, set its `*_FILE` in
`secrets.env` (pointing at the file from 01.4 / 02.5) and re-run `prerequisites.sh`.
Both filenames are gitignored, so keeping them in the repo root is safe.

## 4.4 Keep these NON-secret values for the tfvars files
You'll paste these into `terraform.tfvars` (NOT secrets): `project_id`, `region`,
`firebase_project_id` (= your project), `firebase_web_api_key`,
`bootstrap_admin_email`, `github_org` + App ids, and **`repo_url`** (your fork
URL — same in both stacks). After Stage ① you'll also paste the gateway's
`network_name` / `archive_bucket` / `openclaw_ws_url`.

## 4.5 Private fork? Generate the gateway's clone token (`GATEWAY_REPO_TOKEN`)
Most orgs run AgentOS from a **private** fork. The gateway VM clones it over HTTPS
on boot using the token you put in `secrets.env` as `GATEWAY_REPO_TOKEN` — so set
this **before** you run `prerequisites.sh`. Use a **fine-grained** GitHub token
scoped to just this one repo (least-privilege):

1. GitHub → your avatar → **Settings → Developer settings → Fine-grained tokens**
   → **Generate new token**.
2. **Resource owner** = the org/user that owns your AgentOS fork.
3. **Repository access** → **Only select repositories** → pick **your AgentOS fork**.
4. **Permissions → Repository permissions → Contents → Read-only**. (Nothing else.)
5. **Expiration** — pick a duration and note it. The token is only used when the
   VM clones/redeploys; when it expires, generate a new one and update the secret:
   `printf %s 'NEW_TOKEN' | ./set-secret.sh gateway-repo-token`
6. **Generate token**, copy it (`github_pat_…`), and put it in `secrets.env`:
   `GATEWAY_REPO_TOKEN=github_pat_…`

Leave `GATEWAY_REPO_TOKEN` blank if your fork is **public** (no token needed).

> This is a different credential from the **GitHub App** (§4 / `02-github-app.md`):
> the App lets *agents* create and work on goal repos; this PAT only lets the
> *gateway VM* clone your fork's source.

---

Next: **STAGE ① — deploy the gateway** → `../agentos-openclaw/deploy/README.md`.
Then return here for **`05-provision-infrastructure.md`** (Stage ②).
