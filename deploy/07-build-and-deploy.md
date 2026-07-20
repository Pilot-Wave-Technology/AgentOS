# 7 · Build the app, load the database, go live

This is **one command**. Cloud Build reads `cloudbuild.yaml` and does the whole
control-plane rollout in order:

1. **build** the app image (and the task-runner image),
2. **push** them,
3. **migrate** — runs `migrations/apply.py` as a one-off job (as `migrate_user`,
   the DDL role): creates every table (`0001_baseline_schema`), seeds every
   built-in template, prompt, skill, and tool (`0002_baseline_seed`), and **grants
   `app_user`** (the runtime role) DML access on the schema so the web app can read
   and write it,
4. **deploy** — points each Cloud Run service at the new image and sets its
   `uvicorn` entrypoint (replacing the placeholder Terraform created). The default
   **split** profile deploys **four** services — `-web` (`app.main`), `-council`,
   `-git`, `-management`; the **single** profile deploys one (`app.single`),
5. **update the task-runner** job image.

Terraform (file 05) owns the service *config* (env vars, secrets, SA, connector,
Cloud SQL, sizing); this pipeline owns only the *image + migrations*, so re-running
it never clobbers your Terraform-managed settings.

## Before you run it — preconditions
- [ ] **File 05 done** — `terraform apply` created the Cloud Run service(s) (hello
      placeholders — **four** for the default split profile, one for single), the
      task-runner job, the VPC connector, and the secret wiring. (The pipeline
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
URL=$(gcloud run services describe agentos-web --region "$REGION" \
        --format='value(status.url)')     # single profile: use `agentos`
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
```bash
ZONE=<your-gateway-zone>                   # e.g. us-central1-a (must be inside $REGION)

# 1) Gateway process is up
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone "$ZONE" \
  --command "sudo systemctl is-active 'openclaw-gateway@18789'"          # want: active

# 2) Gateway actually picked up the platform URL (refresh timer wrote it in)
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone "$ZONE" \
  --command "sudo grep -o 'CONTROL_PANEL_URL=.*' /etc/openclaw/env"      # want: your agentos-web URL

# 3) Gateway can authenticate the Steward's model — Anthropic key is present
test "$(gcloud secrets versions access latest --secret=claude-api-key \
         --project "$PROJECT_ID" | wc -c)" -gt 20 \
  && echo "anthropic key present" \
  || echo "⚠️ claude-api-key looks empty — set it (file 04 / prerequisites.sh) before building"
```

**No-SSH fallback (for checks 1–2).** If you can't use IAP SSH, read the gateway's
serial console and the URL secret directly instead:
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

## 7.1 Run the pipeline
From the repo root (the `agent_os_platform` checkout):
```bash
export PROJECT_ID=your-project-id
export REGION=us-central1          # the ONE region you've used everywhere

gcloud builds submit \
  --project "$PROJECT_ID" \
  --config=cloudbuild.yaml \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD),_REGION=$REGION \
  .
```
This takes ~10–15 min. `gcloud builds submit` **streams the log to your terminal**
by default — watch it reach the **migrate** step (applies `0001_baseline_schema` +
`0002_baseline_seed`), then **deploy**, then finish `SUCCESS`. If the migrate step
fails, the deploy is skipped (by design) — fix the cause and re-run the same command.

> **Which config file?** The default is the **split** profile — `cloudbuild.yaml`
> builds once and deploys all four services (`-web`, `-council`, `-git`,
> `-management`). If you set `service_profile = "single"` in `terraform.tfvars`,
> use `--config=cloudbuild-single.yaml` instead (one service). **The config must
> match your tfvars** or the deploy targets service names that don't exist.

> **Lost the stream / ran it from a trigger?** Follow any in-flight build:
> ```bash
> gcloud builds list --ongoing --region "$REGION"          # find the BUILD_ID
> gcloud builds log <BUILD_ID> --region "$REGION" --stream  # live log
> ```
> Or open **Cloud Build → History** in the console. A failed step shows there with
> its log; the whole build is `SUCCESS` only if migrate **and** deploy passed.

> **Non-default names?** If you changed `name_prefix`/`network_name`/region in
> `terraform.tfvars`, pass the matching values, e.g. also add
> `,_SERVICE=<name_prefix>,_DB_INSTANCE=<name_prefix>-db,_VPC_CONNECTOR=<network_name>-conn,_RUNTIME_SA_NAME=<name_prefix>-runtime`
> to `--substitutions`. Defaults assume `name_prefix="agentos"`.

## 7.2 Get your app URL
The public service is **`agentos-web`** on the default split profile (or `agentos`
on single):
```bash
gcloud run services describe agentos-web --project "$PROJECT_ID" --region "$REGION" \
  --format='value(status.url)'          # single profile: use `agentos`
```
That's your AgentOS address, e.g. `https://agentos-web-123456.us-central1.run.app`.
It matches what you set in 7.0 (the URL is stable across builds). Use this exact
value everywhere else a URL is needed (Firebase, GitHub webhook, Slack).

## 7.3 Finish the Firebase step you couldn't do earlier
Go back to **file 01, step 1.5** and add your app hostname (without `https://`) to
Firebase **Authorized domains**. Without this, login fails.

## 7.4 Set up automatic deploys for the future (recommended)
So future code changes deploy themselves, connect a Cloud Build trigger:
1. Console → **Cloud Build → Triggers → Create trigger**.
2. **Connect repository** → pick your AgentOS fork → branch `agent_os_platform`.
3. **Configuration**: *Cloud Build configuration file* → `/cloudbuild.yaml`
   (or `/cloudbuild-single.yaml` if you deploy the single profile).
4. Create it. From now on every push to `agent_os_platform` builds, migrates, and
   deploys automatically (image + DB only; Terraform-managed config untouched).

> The trigger supplies `COMMIT_SHA` automatically. Your manual first run above
> already did the full build+migrate+deploy, so you don't need to run the trigger
> now — it's purely for future pushes.

---

### ✅ Done when
- [ ] The Cloud Build run finished green (build → migrate → deploy all passed)
- [ ] `gcloud run services describe agentos-web …` (or `agentos` on single) shows a URL that loads
- [ ] The migrate step applied `0001_baseline_schema` + `0002_baseline_seed`
- [ ] You added the URL to Firebase Authorized domains (7.3)

Next: **`08-deploy-gateway-vm.md`**
