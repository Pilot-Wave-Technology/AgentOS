# 7 · Build the app, load the database, go live

This is **one command**. Cloud Build reads `cloudbuild.yaml` and does the whole
rollout, in this order (step ids in brackets, as the log shows them):

1. **images** — build and push the app image, the task-runner image and the
   **sandbox** image every story pod runs (`build`, `build-task-runner`,
   `build-sandbox`, `push-*`),
2. **four guards** that refuse a bad tree before anything changes
   (`check-sequence`, `check-expand-contract`, `check-ledger-actors`,
   `check-unbound-names`),
3. **tests** — a test image, a throwaway Postgres, and the **full test suite**
   (`test-image`, `test-db`, `test-suite`). Nothing is deployed until it passes,
4. **migrate** — runs `migrations/apply.py` as a one-off job (as `migrate_user`,
   the DDL role) and applies every migration listed in `migrations/SEQUENCE`
   (`0001`–`0009`): the schema, the built-in templates/prompts/skills/tools, the
   roles and grants (`migrate-deploy`, `migrate-execute`),
5. **deploy** — points each Cloud Run service at the new image and sets its
   `uvicorn` entrypoint (replacing the placeholder Terraform created) — **five**
   services: `-web` (`app.main`), `-council`, `-git`, `-management` and
   `-scheduler` (the background loops, one instance) — plus the task-runner job
   (`deploy-*`),
6. **`sync-url`** — writes the web URL into `control-panel-url` for the gateway,
7. **`smoke`** — all five services answer, on this build's image,
8. **`prune-revisions`** — deletes old Cloud Run revisions,
9. **`execution-plane`** — on the GKE cluster: the `agentos` namespace, its
   in-cluster secrets, and the conductor (with verifier and egress proxy) at
   this build's image. It ends with its own preflight, which must be green,
10. **`preflight`** — the final check of the whole installation.

Terraform (file 05) owns the service *config* (env vars, secrets, SA, connector,
Cloud SQL, sizing, the cluster); this pipeline owns the *images, migrations and
in-cluster objects*, so re-running it never clobbers your Terraform-managed
settings.

## Before you run it — preconditions
- [ ] **File 05 done** — `terraform apply` created the Cloud Run services (hello
      placeholders — **five**: `-web`, `-council`, `-git`, `-management`,
      `-scheduler`), the task-runner job, the VPC connector, and the secret
      wiring. (The pipeline
      *updates* those; they must already exist.)
- [ ] **File 06 done** — the LLM keys (Anthropic + Gemini required) and any other
      external secrets are filled in Secret Manager. The service reads them at
      deploy; blank required keys mean a booting-but-broken app.
- [ ] **Stage ① gateway is up** (from `agent_os_openclaw`).
- [ ] **The pre-build gateway gate below (7.0) is green** — `control-panel-url`
      set, gateway `active`, Anthropic key present. **Do not skip this.**

## 7.0 ⛔ Before you build: point the gateway at the platform, and confirm it's READY

**Do this BEFORE the build in 7.1 — not after.** When the build deploys the web
service, the platform boots and, once per build, **recreates the platform agents**
(the **Steward** and the **Platform Assistant**) from scratch on the gateway. That
recreate is the *only* moment their model + tool scope get set — an existing
agent's model/tools can't be changed afterwards. So at that moment the gateway must
already be able to **(a)** authenticate the Steward's Anthropic model and **(b)**
serve the platform's tools. If it isn't ready, the Steward comes up on the
**fallback gemini model with no platform tools**, and only another deploy fixes it.

You don't need the build to get the URL — Terraform already created the Cloud Run
placeholder (file 05), so `agentos-web` has its final, stable URL now.

### 7.0.a Tell the gateway the platform's URL
```bash
export PROJECT_ID=your-project-id
export REGION=us-central1                 # the ONE region you've used everywhere
gcloud config set project "$PROJECT_ID"
# FROM TERRAFORM, NOT FROM gcloud. Cloud Run answers to two URLs --
# the deterministic `<service>-<projectnumber>.<region>.run.app` and
# a legacy hash form. `status.url` returns the hash one, while
# terraform already computed the deterministic one INTO the
# service's own CONTROL_PANEL_URL env var. Both route, so taking
# either appears to work -- and you end up with two spellings of one
# fact: the gateway calling back on one, the app describing itself
# with the other. Read the value terraform derived.
URL=$(terraform -chdir=terraform output -raw service_url)
test -n "$URL" || echo "⚠️ service URL empty — is file 05 (terraform apply) done?"
printf %s "$URL" | ./set-secret.sh control-panel-url    # from the agentos-platform root
echo "set control-panel-url = $URL"
```
Within ~60s the gateway's refresh timer re-reads the secret and restarts the
gateway process (no VM reboot needed).

### 7.0.b Confirm the gateway is READY — all three must be true before you build

> **These checks use `gcloud compute ssh … --tunnel-through-iap`, which needs IAP
> SSH.** Stage ① provisions this for you (its IAP-SSH firewall rule), and the
> project must have `iap.googleapis.com` enabled. If you can't SSH (org policy, no
> IAP), use the **non-SSH fallback** below instead of checks 1–2.
>
> The first `gcloud compute ssh` on a machine creates an SSH key and may ask
> for a passphrase: press Enter to accept the defaults.
```bash
ZONE=<your-gateway-zone>                   # e.g. us-central1-a (must be inside $REGION)

# 1) Gateway process is up
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone "$ZONE" \
  --command "sudo systemctl is-active 'openclaw-gateway@18789'"          # want: active

# 2) Gateway actually picked up the platform URL (refresh timer wrote it in)
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone "$ZONE" \
  --command "sudo grep -o 'CONTROL_PANEL_URL=.*' /etc/openclaw/env"      # want: your agentos-web URL

# 3) Gateway can authenticate the Steward's model — Anthropic key is present
test "$(gcloud secrets versions access latest --secret=anthropic-api-key \
         --project "$PROJECT_ID" | wc -c)" -gt 20 \
  && echo "anthropic key present" \
  || echo "⚠️ anthropic-api-key looks empty — set it with set-secret.sh (file 06) before building"
```
> Check 3 reads the **secret**, not the VM. The gateway only reads its keys at
> boot, so if you set or changed a key **after** the VM booted, restart it before
> you build: `gcloud compute instances reset agentos-gateway --zone "$ZONE"`
> (then re-run checks 1–2).

**No-SSH fallback (for checks 1–2).** If you can't use IAP SSH, read the gateway's
serial console and the URL secret directly instead. This shows the secret is
set, not that the gateway re-read it, so **wait ~2 minutes after 7.0.a** (the
gateway's refresh timer picks it up within ~60s) before you build:
```bash
# Readiness markers on the serial console (from the gateway's boot/provision)
gcloud compute instances get-serial-port-output agentos-gateway \
  --zone "$ZONE" --project "$PROJECT_ID" \
  | grep -E 'openclaw-gateway@18789: active|gateway provision complete'   # want both

# Confirm the platform URL secret is set (what the gateway refresh timer reads)
gcloud secrets versions access latest --secret=control-panel-url \
  --project "$PROJECT_ID"                                                 # want: your agentos-web URL
```
(Check 3 — the Anthropic key — needs no SSH; run it as-is above.)

> ✅ **Proceed to 7.1 only when all three are green:** `control-panel-url` set,
> gateway `active`, Anthropic key present. This gate is the single most common
> cause of a Steward stuck on the wrong model — a minute here saves a redeploy.

> ✈ **Run `./preflight.sh` before you build, and again after you
> deploy.** A build takes minutes and a preflight takes seconds; the
> missing secret it finds now is the same one that would otherwise
> surface as a pod that will not start.

---

## 7.1 Run the pipeline
From the repo root (the `agent_os_platform` checkout):
```bash
export PROJECT_ID=your-project-id
export REGION=us-central1          # the ONE region you've used everywhere

gcloud builds submit \
  --project "$PROJECT_ID" \
  --config=cloudbuild.yaml \
  --service-account="projects/$PROJECT_ID/serviceAccounts/agentos-build@$PROJECT_ID.iam.gserviceaccount.com" \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD),_REGION=$REGION \
  .
```
`--service-account` runs the build as `agentos-build`, the account
`prerequisites.sh` made for it and Terraform granted exactly the secrets it
reads. Leave it out and the build runs as the project's default account, which
holds none of that, and fails at the execution-plane step on "permission denied".

This takes **~10–30 min** (the test suite and the execution plane are most of
it) — **don't cancel it** because it looks quiet. `gcloud builds submit`
**streams the log to your terminal** by default — watch it pass `test-suite`,
reach **migrate** (applies every migration in `SEQUENCE`, `0001`–`0009`), then
**deploy**, `smoke`, `execution-plane`, `preflight`, and finish `SUCCESS`. If
the tests or migrate fail, nothing is deployed (by design).

> **If the build fails** — every step is re-runnable, so fix the cause and
> re-run the **same command**. If `smoke` passed, the control plane is already
> live; only the later steps need another go. Find the failing step's log:
> ```bash
> gcloud builds log <BUILD_ID> | grep 'Step #<N>'
> ```
> A `[ ?? ]` (undetermined) in the `execution-plane` or `preflight` step is a
> failure there, not a pass — read the line, fix it, re-run.

> **Which config file?** `cloudbuild.yaml` — it builds once and deploys all
> five services (`-web`, `-council`, `-git`, `-management`, `-scheduler`). There is one
> control-plane build config; there is no second profile to match.

> **Lost the stream / ran it from a trigger?** Follow any in-flight build:
> ```bash
> gcloud builds list --ongoing           # find the BUILD_ID
> gcloud builds log <BUILD_ID> --stream  # live log
> ```
> No `--region`: `gcloud builds submit` without `--region` runs a **global** build,
> and `--region` would filter it out of the list.
> Or open **Cloud Build → History** in the console. A failed step shows there with
> its log; the whole build is `SUCCESS` only if every step, through the final
> `preflight`, passed.

> **Non-default names?** If you changed `name_prefix`/`network_name`/region in
> `terraform.tfvars`, pass the matching value for **every** name the build
> takes (defaults assume `name_prefix="agentos"`, `network_name="agentos-vpc"`):
>
> | Substitution | Default | Set it to |
> |---|---|---|
> | `_REGION` | `us-central1` | your region |
> | `_SERVICE` | `agentos` | `<name_prefix>` |
> | `_DB_INSTANCE` | `agentos-db` | `<name_prefix>-db` |
> | `_VPC_CONNECTOR` | `agentos-vpc-conn` | `<network_name>-conn` |
> | `_RUNTIME_SA_NAME` | `agentos-runtime` | `<name_prefix>-runtime` |
> | `_TASK_RUNNER_JOB` | `agentos-task-runner` | `<name_prefix>-task-runner` |
> | `_AR_REPO` | `agentos` | `<name_prefix>` (the Artifact Registry repo) |
>
> e.g. append
> `,_SERVICE=<p>,_DB_INSTANCE=<p>-db,_VPC_CONNECTOR=<network_name>-conn,_RUNTIME_SA_NAME=<p>-runtime,_TASK_RUNNER_JOB=<p>-task-runner,_AR_REPO=<p>`
> to `--substitutions`, and run the build as
> `--service-account=projects/$PROJECT_ID/serviceAccounts/<p>-build@$PROJECT_ID.iam.gserviceaccount.com`.

## 7.2 Get your app URL
The public service is **`agentos-web`**. Take its URL from Terraform (the same
value 7.0.a used — see the note there on why not `status.url`):
```bash
terraform -chdir=terraform output -raw service_url
```
That's your AgentOS address, e.g. `https://agentos-web-123456.us-central1.run.app`.
It matches what you set in 7.0 (the URL is stable across builds). Use this exact
value everywhere else a URL is needed (Firebase, GitHub webhook, Slack).

## 7.3 Check the Firebase authorized domain
Make sure the hostname above (without `https://`) is in Firebase **Authorized
domains** — **file 01, step 1.5**. If you skipped it there, add it now. Without
it, login fails.

## 7.4 Set up automatic deploys for the future (recommended)
So future code changes deploy themselves, connect a Cloud Build trigger:
1. Console → **Cloud Build → Triggers → Create trigger**.
2. **Connect repository** → pick your AgentOS fork → branch `agent_os_platform`.
3. **Configuration**: *Cloud Build configuration file* → `/cloudbuild.yaml`.
4. **Service account**: pick `agentos-build@<project>.iam.gserviceaccount.com`
   (the same account as the manual run above — not the default).
5. Create it. From now on every push to `agent_os_platform` builds, migrates, and
   deploys automatically (image + DB only; Terraform-managed config untouched).

> The trigger supplies `COMMIT_SHA` automatically. Your manual first run above
> already did the full build+migrate+deploy, so you don't need to run the trigger
> now — it's purely for future pushes.

## 7.5 Make the VS Code extension available
AgentOS can run a story in a teammate's own VS Code. Teammates install the
extension from **your** deployment — the in-app **VS Code** page offers the
download, and installed extensions check it for updates daily. Terraform
already created a private bucket for it; you only put the file there.

1. Download the `.vsix` from the repository's **`main` branch**, folder
   `extension/` (for a fork, your fork's `main`). Use the version listed there
   (and in `extension/README.md`):
   ```bash
   curl -L -o ~/Downloads/agentos-vscode-<version>.vsix \
     https://github.com/Pilot-Wave-Technology/AgentOS/raw/main/extension/agentos-vscode-<version>.vsix
   ```
   **Private fork?** `curl` gets a 404 without your GitHub login. Use the
   browser instead: branch `main` → `extension/` → the `.vsix` → **Download
   raw file**. Keep the name exactly `agentos-vscode-<version>.vsix` (a
   browser's ` (1)` suffix makes `publish-extension.sh` refuse it).
2. From the `agent_os_platform` checkout root:
   ```bash
   ./publish-extension.sh ~/Downloads/agentos-vscode-<version>.vsix "Initial release"
   ```
   It uploads the file and writes the `latest.json` the page reads. No
   redeploy needed. A newer version later is the same command. It needs a
   working **Python** (`python3`, `python` or `py`) on your PATH.

Skip this and the VS Code page simply shows no download — nothing else is
affected.

---

### ✅ Done when
- [ ] The Cloud Build run finished `SUCCESS` (tests → migrate → deploy → smoke →
      execution-plane → preflight all passed)
- [ ] `terraform -chdir=terraform output -raw service_url` shows a URL that loads
- [ ] The migrate step applied every migration in `SEQUENCE` (`0001`–`0009`)
- [ ] `./preflight.sh` now exits 0 (the namespace, in-cluster secrets and
      database checks that were `MISS`/`??` after file 05 are `ok`)
- [ ] You added the URL to Firebase Authorized domains (7.3)
- [ ] `./publish-extension.sh agentos-vscode-<version>.vsix` run; the `/extension`
      page offers a download (7.5)

Next: **`08-deploy-gateway-vm.md`**
