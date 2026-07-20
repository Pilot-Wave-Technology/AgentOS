# Deploying AgentOS — STAGE 1: the OpenClaw gateway (DO THIS FIRST)

AgentOS deploys in **two stages, in this order**:

1. **STAGE 1 — the OpenClaw gateway** ← *you are here* (`agent_os_openclaw` branch)
2. **STAGE 2 — the platform / control plane** (`agent_os_platform` branch)

**Deploy the gateway first.** It creates the shared pieces the platform needs
(the VPC and the archive bucket; the gateway + LLM secrets are created earlier by
`prerequisites.sh` in Step 0) and comes up as a standalone VM. Then the platform
stack reuses those and connects to it.

```
   STAGE 1 (this branch)                 STAGE 2 (platform branch)
   ┌─────────────────────────┐          ┌──────────────────────────┐
   │ OpenClaw gateway VM      │          │ Control plane (Cloud Run)│
   │  + shared VPC            │  reused  │  + Cloud SQL, buckets,    │
   │  + archive bucket        │─────────▶│    connector on the VPC   │
   │  (reads prereq secrets)  │  outputs │  + the web app            │
   └─────────────────────────┘          └──────────────────────────┘
```

## Prerequisites
- A GCP project with **billing** and **owner** access (e.g. `my-agentos-472913`).
- `gcloud` + `terraform` installed; `gcloud auth login` and
  `gcloud auth application-default login` done.
- Both branches checked out (this one + `agent_os_platform`).

> ⚠️ **Pick ONE region and use it everywhere** — here, in `prerequisites.sh`, and
> in the platform stack. The VM, connector, Cloud Run, and Cloud SQL must share it.

## Step 0 — Run the one-time prerequisites (from the platform folder)
This creates the service accounts **and all secrets** (including the gateway/LLM
secrets this VM reads) before any terraform. It's idempotent — safe to re-run.
Run it from your **platform-branch** clone (the `agentos-platform` folder from
prerequisite 0.5):
```bash
cd ../agentos-platform               # your agent_os_platform clone
cp secrets.env.example secrets.env   # fill in LLM keys (+ GATEWAY_REPO_TOKEN if private)
./prerequisites.sh <PROJECT_ID> <REGION>
cd -                                 # back to this gateway folder
```
> **Private fork?** The gateway VM clones your fork on boot, so if it's private
> it needs a read-only clone token in `secrets.env` as `GATEWAY_REPO_TOKEN`
> **before** you run `prerequisites.sh`. The step-by-step for generating that
> fine-grained GitHub token lives in the **platform** guide,
> `../agentos-platform/deploy/04-collect-your-values.md` (that's where
> `secrets.env` and `prerequisites.sh` live). Public fork = leave it blank.

## Step 1 — Configure
```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```
Edit `terraform.tfvars`:
- `project_id` = your project.
- `region` / `zone` = the ONE region you're using (zone must be inside it).
- `name_prefix` = `agentos` (**must match** what you'll use in Stage 2).
- `repo_url` = your AgentOS fork URL (the VM clones this branch on boot);
  `repo_branch` defaults to `agent_os_openclaw`.
- For repeatable testing leave `force_destroy_buckets = true`.

> You do **not** set LLM/provider flags here — the gateway just reads whatever LLM
> key secrets exist in Secret Manager, and those were already created by
> `prerequisites.sh` (Step 0). There are no `*_enabled` variables in this stack.

## Step 2 — Apply
```bash
terraform init
terraform apply
```
This creates the VPC, subnet, archive bucket, firewalls, and VM; it
**references** the gateway SA + secrets that `prerequisites.sh` already created
(Step 0) — it does not create them (~1–2 min; the VM then spends a few minutes on
first boot cloning + building the gateway).

## Step 3 — Make sure the LLM keys are set
The secrets already exist (created in Step 0). `openclaw-gateway-token` was
auto-generated and is shared with the platform automatically (both read the same
secret). You only need to set real **LLM keys** if you left them blank in `secrets.env`.
Run these **from your `agentos-platform` checkout** (that's where `set-secret.sh`
lives — it refuses empty values and disables older versions so only `:latest` is
live):
```bash
cd ../agentos-platform      # adjust if your platform checkout is elsewhere
printf %s 'REAL_GEMINI_KEY'    | ./set-secret.sh gemini-api-key
printf %s 'REAL_ANTHROPIC_KEY' | ./set-secret.sh claude-api-key
```
If you set keys AFTER the VM booted, reboot it so it re-reads them:
```bash
gcloud compute instances reset agentos-gateway --zone <your-zone>
```
(`control-panel-url` is filled automatically in Stage ②.)

## Step 4 — Grab the outputs for Stage 2
```bash
terraform output
```
Copy these three into the **platform** stack's `terraform.tfvars`:
- `network_name`   → platform `network_name`
- `archive_bucket` → platform `archive_bucket`
- `openclaw_ws_url` → platform `openclaw_ws_url`

> **What happens on boot.** The VM installs deps, clones your fork, then runs
> `scripts/gateway-provision.sh` — the single provisioning script that builds the
> gateway, writes the full `openclaw.json` (LAN bind, compaction/memory-v2, the
> synthetic context-window models every agent needs, model registry), installs
> the CLI wrappers + Claude Code hooks, and starts the service. First boot takes
> ~5–8 min. Watch it with:
> ```bash
> gcloud compute instances get-serial-port-output agentos-gateway \
>   --zone <your-zone> --project <PROJECT> | grep -E "provision|startup complete" | tail
> ```

## Step 5 — Watch first boot + confirm the gateway is ready
First boot clones your fork, builds the gateway, writes `openclaw.json`, and
starts the service (~5–8 min). **You don't need SSH to watch it** — stream the
serial console (works even while OS Login/IAP isn't set up yet):
```bash
# Live-follow boot + provisioning (Ctrl-C to stop):
gcloud compute instances tail-serial-port-output agentos-gateway \
  --zone <your-zone> --project <PROJECT>

# …or just check the finish markers so far:
gcloud compute instances get-serial-port-output agentos-gateway \
  --zone <your-zone> --project <PROJECT> \
  | grep -E "\[scanners\] OK|openclaw-gateway@18789:|gateway provision complete" | tail
```
**Ready when you see all three:**
- `[scanners] OK: rg gitleaks trivy semgrep all present`   ← Security Council scanner coverage installed
- `openclaw-gateway@18789: active`
- `gateway provision complete … (failed=0)`   ← `failed=0` means every step passed

Once it's up, confirm the service directly (SSH is IAP-only + OS Login):
```bash
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone <your-zone> \
  --command "sudo systemctl status 'openclaw-gateway@18789' --no-pager | head -5"
# live gateway logs (listening / agent traffic):
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone <your-zone> \
  --command "sudo journalctl -u 'openclaw-gateway@18789' -n 50 -f"
# full provisioning log if a step failed:
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone <your-zone> \
  --command "sudo tail -80 /var/log/openclaw-provision.log"
```
Want `active (running)`. If your human account can't IAP-SSH, read everything via
`get-serial-port-output` instead — the provision log is echoed to the console.

---

➡️ **Now go to the `agent_os_platform` branch and follow its `deploy/` guide (STAGE 2).**

## Redeploying — edit your fork → push → rebuild
Both branches share the same model: **Terraform builds infra once; a Cloud Build
run does the full provisioning, and it's also your update loop.**

- **Gateway:** the `cloudbuild.yaml` in this branch SSHes into the VM and re-runs
  the *same* `scripts/gateway-provision.sh` (pull → build → reconfigure → restart).
  Point a Cloud Build trigger at your fork's `agent_os_openclaw` branch (or run it
  manually with `gcloud builds submit --config cloudbuild.yaml .`). The Cloud Build
  service account already has the IAP-SSH roles it needs (granted by
  `prerequisites.sh`). So: **edit fork → push → gateway reconfigures + rebuilds.**
  - **Watch the run.** A manual `gcloud builds submit` streams the log to your
    terminal. For a trigger-started run, follow it with:
    ```bash
    gcloud builds list --ongoing --region <REGION>           # find the BUILD_ID
    gcloud builds log <BUILD_ID> --region <REGION> --stream   # live log
    ```
    The build ends `SUCCESS` only if the on-VM `gateway-provision.sh` finished
    with `failed=0` (it propagates the exit code back).
- **Config/secret changes** (e.g. a new model in `models-synthetic.json`, or a
  rotated key in Secret Manager) are picked up by that same redeploy — no VM
  recreate needed.
- **Tear down to re-test from scratch:** `./reset.sh` (see the repo root).
