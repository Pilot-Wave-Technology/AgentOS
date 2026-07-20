# AgentOS control plane — Terraform (Stage ②)

This stack is **Stage ②** of the deploy. It provisions the control-plane plumbing
in your GCP project: APIs, the Serverless VPC connector (on the gateway's VPC),
buckets, and the Cloud Run service(s) + task-runner job.

It does **not** create service accounts, secrets, or the database — those are
made **first** by `../prerequisites.sh` (see below). Terraform only **reads** the
Cloud SQL instance (data source) and **references** the secrets/SAs that already
exist.

> Full, step-by-step walkthrough: **[`../deploy/README.md`](../deploy/README.md)**.
> This file is the terraform-specific reference.

## The one file you edit
`terraform.tfvars` (copy from `terraform.tfvars.example`). It holds:
- **Inputs** you provide — project, region, Firebase, GitHub App, admin email, sizes.
- **Feature flags** — every service behaviour, defaulted to the current running config.

You should not need to touch any `.tf` file.

## Layout
```
variables.tf            all inputs + flags (documented)
terraform.tfvars.example the file you copy + edit
locals.tf               derives names, the env-var map, the secret references
main.tf                 enables APIs, wires the modules
outputs.tf              service URL, SA, DB conn, next steps
modules/
  network/              Serverless VPC connector (attaches to the gateway's VPC)
  database/             READS the existing Cloud SQL instance via a data source
                        (the instance + control_panel db + app/migrate users +
                        DSN secrets are created by prerequisites.sh, not here)
  storage/              resource bucket (archive bucket comes from the gateway)
  secrets/              no-op placeholder (all secrets are created by prerequisites.sh)
  cloud_run/            the service(s) + task-runner job
```

## Service profile
- `service_profile = "split"` (default) — separate web / council-worker /
  git-engine / management services (better isolation + independent scaling).
- `service_profile = "single"` — ONE Cloud Run service runs the combined
  `app.single` entrypoint with every worker co-located (dispatchers are DB-lock
  guarded, so it's safe). Cheaper: 1 always-on instance vs 4.

## Apply order
```bash
# 0. FIRST — service accounts + ALL secrets + Cloud SQL (instance, control_panel
#    db, app_user/migrate_user, DSN secrets). Run from the repo root, once.
cd ..
./prerequisites.sh <PROJECT_ID> <REGION>
cd terraform

# then the terraform stack
cp terraform.tfvars.example terraform.tfvars   # edit it
terraform init
terraform apply
```
Then (see [`../deploy/README.md`](../deploy/README.md) for detail):
1. **Secrets are already created + filled** by `prerequisites.sh`. Overwrite any
   external secret you left blank later with
   [`../deploy/06-fill-in-secrets.md`](../deploy/06-fill-in-secrets.md):
   `printf %s "<value>" | ../set-secret.sh <name>` — never raw
   `gcloud secrets versions add`. Multi-line secrets (Firebase JSON, GitHub App
   `.pem`) are loaded by `prerequisites.sh` from the `*_FILE` pointers in
   `secrets.env`.
2. **Build + deploy the image** — Cloud Build (see
   [`../deploy/07-build-and-deploy.md`](../deploy/07-build-and-deploy.md)) builds,
   runs migrations (which seed templates/skills/tools and grant `app_user`), and
   deploys the real image. Terraform ignores image changes so the two don't fight.
3. **Gateway link** — the OpenClaw VM (`agent_os_openclaw` branch) is deployed in
   **Stage ①**, *before* this stack. Paste its `openclaw_ws_url` output into
   `terraform.tfvars` here.
4. Visit the `service_url` output and log in as `bootstrap_admin_email`.

For the GitHub App setup see
[`../deploy/02-github-app.md`](../deploy/02-github-app.md).

## Notes
- **Image ownership:** terraform owns the service *config* (env + flags +
  secret references); Cloud Build owns the *image*. `lifecycle.ignore_changes` on
  the image lets `gcloud run deploy --image` update revisions without terraform
  reverting them. Change flags in tfvars + `terraform apply` any time.
- **DB access:** Cloud Run reaches Cloud SQL over the built-in connector (unix
  socket at `/cloudsql/...`), so no public authorized networks are opened.
- **Workspace buckets:** per-goal (`<prefix>-<goalid>`) — not pre-created; the
  runtime SA has project `storage.objectAdmin`.
- **Cloud Run URL** is the deterministic `*.run.app` (no custom domain).
