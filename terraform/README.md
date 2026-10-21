# AgentOS control plane — Terraform (Stage ②)

This stack is **Stage ②** of the deploy. It provisions the control-plane plumbing
in your GCP project: APIs, Cloud SQL (instance, `control_panel` database,
`app_user`/`migrate_user` and their DSN secrets), the image registry, the
Serverless VPC connector (on the gateway's VPC), buckets, the Cloud Run
service(s) + task-runner job, and the GKE cluster every executor runs on, with
its node pools, identities, the pods' login password and every grant the
conductor needs. **One apply sets up the database and the cluster
together.**

It does **not** create service accounts or the non-database secrets — those are
made **first** by `../prerequisites.sh` (see below) and only referenced here.
It does not apply in-database GRANTS either: they need a connection, and the
build's migrate step applies them (migration 0003).

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
adopt-existing.sh       imports a database/registry made outside terraform
                        (an interrupted apply, or an older prerequisites.sh)
modules/
  network/              Serverless VPC connector (attaches to the gateway's VPC)
  database/             Cloud SQL instance + control_panel db + app/migrate
                        logins + the three DSN secrets
  storage/              resource bucket (archive bucket comes from the gateway)
  secrets/              no-op placeholder (non-DB secrets come from prerequisites.sh)
  cloud_run/            the service(s) + task-runner job
  execution_plane/      GKE cluster, pools, identities, the pods'
                        login + sqlproxy key, conductor secret grants
```

## Services
Five Cloud Run services from one image — web, council-worker, git-engine,
management and scheduler (the background loops: one instance, CPU always on) —
each isolated and independently scalable.

## Apply order
```bash
# 0. FIRST — service accounts + every non-database secret. Run from the repo
#    root, once.
cd ..
./prerequisites.sh <PROJECT_ID> <REGION>   # non-default name_prefix: NAME_PREFIX=<prefix> ./prerequisites.sh …
cd terraform

# then the terraform stack
cp terraform.tfvars.example terraform.tfvars   # edit it
terraform init
terraform apply
```
Then (see [`../deploy/README.md`](../deploy/README.md) for detail):
1. **Secrets are already created + filled** by `prerequisites.sh` (it stops with
   exit 2 if a required one — `gemini-api-key`, `anthropic-api-key`,
   `firebase-service-account`, `github-app-key` — is blank). Re-running it never
   replaces an existing version. Overwrite any external secret you left blank
   later with
   [`../deploy/06-fill-in-secrets.md`](../deploy/06-fill-in-secrets.md):
   `printf %s "<value>" | ../set-secret.sh <name>` — never raw
   `gcloud secrets versions add`. Multi-line secrets (Firebase JSON, GitHub App
   `.pem`) are loaded by `prerequisites.sh` from the `*_FILE` pointers in
   `secrets.env`.
2. **Build + deploy the image** — Cloud Build (see
   [`../deploy/07-build-and-deploy.md`](../deploy/07-build-and-deploy.md)) builds,
   runs migrations (which seed templates/skills/tools and grant `app_user`), and
   replaces the placeholder images this apply created with the real one.
   Terraform ignores image changes so the two don't fight. The build also
   creates the execution plane's namespace, service accounts and conductor.
3. **Gateway link** — the OpenClaw VM (`agent_os_openclaw` branch) is deployed in
   **Stage ①**, *before* this stack. Nothing to copy for the link: this stack
   reads the gateway's reserved address (`<name_prefix>-gateway-ip`) and derives
   the URL. `openclaw_ws_url` is an override only.
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
