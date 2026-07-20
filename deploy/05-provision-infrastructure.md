# 5 · Provision the platform infrastructure (Terraform) — STAGE ②

> **STOP — do these first:**
> 1. **Stage ①** — the gateway (`agent_os_openclaw` branch) must already be
>    deployed (its `deploy/README.md`). It created the shared **VPC** and
>    **archive bucket** this stack reuses (the gateway + LLM secrets come from
>    `prerequisites.sh`). Have its three
>    outputs ready: `network_name`, `archive_bucket`, `openclaw_ws_url`.
> 2. **`prerequisites.sh`** (step 0️⃣) — it already created the service accounts,
>    **all secrets**, and the **Cloud SQL instance + `control_panel` database +
>    DB users + DSN secrets** (`database-url`, `database-url-migrate`). Terraform
>    does **not** create the database — it just reads it and builds the rest of
>    the plumbing around it.

This stack creates the Serverless VPC connector (on the gateway's VPC), the
resource bucket, permissions, and the Cloud Run **service + task-runner job as
placeholders** (a Google "hello" image). You fill secrets in file 06 and Cloud
Build swaps in the real image in file 07.

> Why this order? The web app can't start healthily until its database and
> secrets exist and it has a gateway to talk to. So: gateway (Stage ①) →
> prerequisites (DB + secrets) → this plumbing → fill secrets → switch the app on.

## 5.1 Open the terraform folder and make your config
```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```
Fill in the **non-secret** values from your worksheet (file 04): `project_id`,
`region`, `firebase_project_id`, `firebase_web_api_key`, `bootstrap_admin_email`,
`github_org`, `github_app_id`, `github_app_installation_id`. Turn LLM providers
on/off with `anthropic_enabled` / `gemini_enabled` / `openai_enabled` (match
Stage ①).

> **Slack:** leave `slack_enabled` **off**. The normal path is to configure Slack
> in the admin UI *after* deploy (Settings → Integrations → Slack; see file 03).
> The `slack_enabled` + slack tfvars fields are a **legacy** deploy-time fallback
> only — you don't need them.

**Reuse the gateway's VPC — do NOT recreate it.** Leave `create_network = false`
(the example default); `true` makes Terraform try to create `agentos-vpc` again
and fail with "already exists".
```hcl
create_network  = false                                  # reuse the gateway's VPC
```

**Paste the three gateway outputs from Stage ①** (keep `name_prefix` identical to
Stage ① and to what you passed `prerequisites.sh`):
```hcl
network_name    = "agentos-vpc"                          # gateway output: network_name
archive_bucket  = "agentos-agent-data-YOUR_PROJECT"      # gateway output: archive_bucket
openclaw_ws_url = "ws://10.20.x.x:18789"                 # gateway output: openclaw_ws_url
```

For **repeatable testing** on the same project, also set:
```hcl
force_destroy_buckets = true
```

Leave the `flags { ... }` block as-is unless you want to change behaviour — the
defaults are a sensible, secure, full-featured setup. See `../CONFIG.md`.

> You put **no** passwords/keys in this file. Service accounts, every secret, and
> the database were all created by `prerequisites.sh` (step 0️⃣); Terraform only
> references them.

## 5.2 Initialize Terraform
```bash
terraform init
```

## 5.3 Create the infrastructure
One apply creates everything except the real app image: the VPC connector, the
resource bucket, IAM, and the Cloud Run **service + task-runner job** (hello
placeholders). It also **reads** your Cloud SQL instance — so if you skipped
`prerequisites.sh`, this fails here with a data-source "not found" error.
```bash
terraform apply
```
Review the plan, type **`yes`**. It takes ~2–3 minutes (no database is created
here — `prerequisites.sh` already did the slow part).

> The placeholder service is intentionally the Google "hello" image. Cloud Build
> replaces it with the real image (and the real `uvicorn` command) in file 07;
> Terraform ignores image changes after this, so re-applying never reverts it.

## 5.4 See what got created
```bash
terraform output
```
Note **`cloudsql_connection_name`** and **`runtime_service_account`**. The DB DSN
secrets (`database-url`, `database-url-migrate`) already exist from
`prerequisites.sh`; `secret_names_created_here` is empty by design (all secrets
live in Secret Manager from step 0️⃣).

---

### ✅ Done when
- [ ] `terraform apply` finished without errors
- [ ] `terraform output cloudsql_connection_name` shows your instance
- [ ] `gcloud run services list --region <REGION>` shows the hello-placeholder
      services (four `agentos-web/-council/-git/-management` on the default split
      profile, or one `agentos` on single — Cloud Build makes them real in file 07)

Next: **`06-fill-in-secrets.md`**
