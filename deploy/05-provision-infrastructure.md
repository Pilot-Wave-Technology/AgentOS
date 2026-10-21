# 5 · Provision the platform infrastructure (Terraform) — STAGE ②

> **STOP — do these first:**
> 1. **Stage ①** — the gateway (`agent_os_openclaw` branch) must already be
>    deployed (its `deploy/README.md`). It created the shared **VPC** and
>    **archive bucket** this stack reuses (the gateway + LLM secrets come from
>    `prerequisites.sh`). Have its two
>    outputs ready: `network_name`, `archive_bucket`.
> 2. **`prerequisites.sh`** (step 0️⃣) — it already created the service accounts
>    and every secret **except** the database ones.

This stack creates:
- the **Cloud SQL** instance (`agentos-db`), the `control_panel` database, the
  `app_user` and `migrate_user` logins, and their DSN secrets (`database-url`,
  `database-url-migrate`, `database-url-conductor`);
- the **image registry** the build pushes to;
- the Serverless VPC connector (on the gateway's VPC), the resource bucket and
  permissions;
- the Cloud Run **services + task-runner job as placeholders** (a Google
  "hello" image);
- **the execution plane**, where every executor runs: a GKE Standard cluster
  with four node pools, its service accounts and Workload Identity bindings,
  the password for the execution pods' own database login, and the sqlproxy
  key. (The login itself, `agentos_leg`, and exactly what it may touch are
  migration `0004`'s; the build's migrate step sets its password from this
  secret every run.)

The table *grants* inside the database are not Terraform's: they need a
database connection, and the migration step in file 07 applies them. You fill
the remaining secrets in file 06 and Cloud Build swaps in the real image in
file 07. **The build never creates the cluster.**

> Why this order? The web app can't start healthily until its database and
> secrets exist and it has a gateway to talk to. So: gateway (Stage ①) →
> prerequisites (accounts + secrets) → this apply (database, plumbing and the
> cluster) → fill secrets → switch the app on.

## 5.1 Open the terraform folder and make your config
```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```
Fill in the **non-secret** values from your worksheet (file 04): `project_id`,
`region`, `firebase_project_id`, `firebase_web_api_key`, `bootstrap_admin_email`,
`github_org`, `github_app_id`, `github_app_installation_id`. Also check:
- `name_prefix` (default `agentos`) — **must match** Stage ① and the
  `NAME_PREFIX` you gave `prerequisites.sh`; every resource name derives from it.
- `repo_url` — your AgentOS **fork** URL, the same as Stage ①'s.
- `connector_cidr` (default `10.8.0.0/28`) — change it only if that `/28`
  collides with something already on the VPC.

`anthropic_enabled` / `gemini_enabled` / `openai_enabled` only choose which LLM
key secrets are mounted into the control plane (defaults: Anthropic and Gemini
on, OpenAI off).

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

**Paste the two gateway outputs from Stage ①** (keep `name_prefix` identical to
Stage ① and to what you passed `prerequisites.sh`):
```hcl
network_name    = "agentos-vpc"                          # gateway output: network_name
archive_bucket  = "agentos-agent-data-YOUR_PROJECT"      # gateway output: archive_bucket
```
**The gateway's URL is not copied.** Stage ① reserves the gateway VM's internal
address as `<name_prefix>-gateway-ip`; this stack looks it up and builds
`ws://<that address>:18789` itself. The address is reserved, so it survives the
VM being recreated. (`openclaw_ws_url` exists only as an override.)

Leave `db_deletion_protection` and `force_destroy_buckets` at their defaults,
even on a test project: `./teardown.sh` lifts both itself when you remove
everything (file 12).

**Database sizing** (optional — defaults shown):
```hcl
db_tier    = "db-custom-2-8192"
db_version = "POSTGRES_17"
db_disk_gb = 250
```

**The execution plane** is always created — every executor runs on it. It
costs money continuously: the platform pool keeps two `e2-standard-4` nodes up
whether or not a story runs. The cost knob is `execution_small_min_nodes`
(default `0`): at 0 the `small` pool scales to zero when idle, and the first
story of the day waits for a node; `1` keeps one warm.

Leave the `flags { ... }` block as-is unless you want to change behaviour — the
defaults are a sensible, secure, full-featured setup. See `../CONFIG.md`.

> You put **no** passwords/keys in this file. Service accounts and the non-database
> secrets come from `prerequisites.sh` (step 0️⃣); Terraform generates the
> database passwords itself and writes them only into the DSN secrets.

## 5.2 Initialize Terraform
```bash
terraform init
```

## 5.3 Create the infrastructure
One apply creates everything except the real app image.
```bash
terraform apply
```
Review the plan, type **`yes`**. The Cloud SQL instance takes several minutes,
and the GKE cluster about ten more.

> **If the apply is interrupted while the instance is still building** (a
> dropped connection, or an org policy cutting the API poll short), the
> instance keeps building without Terraform. Wait until
> `gcloud sql instances describe agentos-db --format='value(state)'` says
> `RUNNABLE`, then run `./adopt-existing.sh` in this folder and apply again.
> The same script adopts a database made by an older `prerequisites.sh`.

> The placeholder service is intentionally the Google "hello" image. Cloud Build
> replaces it with the real image (and the real `uvicorn` command) in file 07;
> Terraform ignores image changes after this, so re-applying never reverts it.

## 5.4 See what got created
```bash
terraform output
```
Note **`cloudsql_connection_name`** and **`runtime_service_account`**.
`secret_names_created_here` lists the three DSN secrets this apply wrote, and
`execution_plane_cluster` names the cluster.

The build in file 07 finishes the execution plane: the
Kubernetes namespace and service accounts, and the conductor at the build's
image. Those need cluster credentials, which do not exist until the cluster
does, so they are not Terraform's.

---

> ✈ **Run `./preflight.sh` now.** Terraform returning 0 means the API
> accepted your plan, not that everything exists. Checking here is how
> you avoid discovering it in file 07, after a build.
> ```bash
> REGION=<REGION> ./preflight.sh
> ```
> **Expected here: `23 ok, 5 missing, 1 undetermined`, exit code 1.** `MISS`
> for the `agentos` namespace and its three in-cluster secrets
> (`conductor-secrets`, `execution-db`, `execution-sqlproxy-key`), and `??` for
> the `database` check (it runs inside the conductor, so it is counted in both
> totals). The build in file 07 creates all of them. Everything else must be
> `ok` — anything else missing, fix it before going on.
>
> **Do not run the `fix:` lines printed for those five** (`kubectl create
> namespace`, `./execution-plane.sh`): the build does exactly that, in order.
> The preflight fetches the cluster's credentials itself, into a private
> kubeconfig; your own `kubectl` context is not changed.

---

### ✅ Done when
- [ ] `terraform apply` finished without errors
- [ ] `terraform output cloudsql_connection_name` shows your instance
- [ ] `terraform output execution_plane_cluster` names a cluster
- [ ] `gcloud run services list --region <REGION>` shows the five
      hello-placeholder services (`agentos-web`, `-council`, `-git`,
      `-management`, `-scheduler`) — Cloud Build makes them real in file 07

Next: **`06-fill-in-secrets.md`**
