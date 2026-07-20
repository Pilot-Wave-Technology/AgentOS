# Runbook — provisioning the openclaw VM

Step-by-step to go from "fresh repo" to "VM running the gateway and reachable."
Written for project `${GCP_PROJECT_ID}`, region `${GCP_REGION}`.

---

## Step 0 — One-time GCP prerequisites

Run these once per project. If you've already run them, skip ahead.

### 0.1 Authenticate gcloud

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project ${GCP_PROJECT_ID}
```

### 0.2 Enable required APIs

```bash
gcloud services enable \
  compute.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iap.googleapis.com \
  --project=${GCP_PROJECT_ID}
```

### 0.3 Create the Terraform state bucket

```bash
gcloud storage buckets create gs://${GCP_PROJECT_ID}-tfstate \
  --location=${GCP_REGION} \
  --uniform-bucket-level-access \
  --project=${GCP_PROJECT_ID}
```

Enable versioning so we can recover from accidental state corruption:

```bash
gcloud storage buckets update gs://${GCP_PROJECT_ID}-tfstate \
  --versioning
```

### 0.4 Verify the secrets the VM expects

The VM startup script reads three secrets. They must exist before the VM
boots, or the startup will fail.

```bash
# These should all return values:
gcloud secrets versions access latest --secret=gemini-api-key --project=${GCP_PROJECT_ID}
gcloud secrets versions access latest --secret=Claude_Key_1 --project=${GCP_PROJECT_ID}
gcloud secrets versions access latest --secret=openclaw-gateway-token --project=${GCP_PROJECT_ID}
```

If `openclaw-gateway-token` doesn't exist yet, **store the existing token
that the current VM and control panel already use** — this avoids a
synchronized rotation:

```bash
# Pull the existing token from the current start-openclaw.sh
echo -n "${OPENCLAW_GATEWAY_TOKEN}" \
  | gcloud secrets create openclaw-gateway-token \
    --data-file=- \
    --project=${GCP_PROJECT_ID} \
    --labels=component=openclaw
```

Result: new VM and old VM and control panel all use the same token. No
rotation needed for this migration.

Want a fresh token instead? Generate one and update both the secret AND
the control panel's `OPENCLAW_GATEWAY_TOKEN` env var:

```bash
TOKEN=$(openssl rand -base64 32)
echo -n "$TOKEN" | gcloud secrets create openclaw-gateway-token \
  --data-file=- --project=${GCP_PROJECT_ID} --labels=component=openclaw

gcloud run services update control-panel-acp \
  --region=${GCP_REGION} --project=${GCP_PROJECT_ID} \
  --update-env-vars="OPENCLAW_GATEWAY_TOKEN=$TOKEN"

# Old VM (openclaw-v1-test) will then fail auth — stop or delete it.
```

### 0.5 Grant yourself IAP access (for tunneled SSH)

```bash
USER=$(gcloud config get-value account)
gcloud projects add-iam-policy-binding ${GCP_PROJECT_ID} \
  --member="user:${USER}" \
  --role="roles/iap.tunnelResourceAccessor"
```

---

## Step 1 — Apply Terraform

```bash
cd "c:/Project collaborator all/agent_os_openclaw/infra/terraform/envs/prod"
terraform init
terraform plan -out=tfplan
```

Review the plan output. You should see roughly:

- `module.openclaw_vm.google_service_account.openclaw` — the SA
- `module.openclaw_vm.google_project_iam_member.*` — 3 IAM bindings
- `module.openclaw_vm.google_compute_instance.openclaw` — the VM
- `module.openclaw_vm.google_compute_firewall.openclaw_iap_ssh` — IAP-SSH firewall

Apply:

```bash
terraform apply tfplan
```

Wait ~3 minutes. Terraform creates the resources, then the VM boots and
runs the startup script. The startup script takes 5-10 more minutes
(installing Node, building openclaw, optionally installing CRD).

Outputs after apply:

```bash
terraform output
# vm_name = "openclaw-prod"
# vm_external_ip = "34.x.x.x"
# vm_internal_ip = "10.x.x.x"
# vm_service_account = "sa-openclaw@..."
# ssh_command = "gcloud compute ssh openclaw-prod ..."
```

---

## Step 2 — Watch the boot complete

```bash
# Tail the startup log via SSH (will retry until SSH is ready)
gcloud compute ssh openclaw-prod \
  --zone ${GCP_REGION}-a \
  --project=${GCP_PROJECT_ID} \
  --command="sudo tail -f /var/log/openclaw-startup.log"
```

Wait for the line:
```
openclaw startup complete at <timestamp>
```

Then check the gateway is running:

```bash
gcloud compute ssh openclaw-prod \
  --zone ${GCP_REGION}-a \
  --project=${GCP_PROJECT_ID} \
  --command="sudo systemctl status openclaw-gateway@18789.service && sudo tail -20 /var/log/openclaw-18789.log"
```

Expected: `Active: active (running)` and gateway log lines about port 18789.

### If the startup script failed

Check the serial console for boot errors:

```bash
gcloud compute instances get-serial-port-output openclaw-prod \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID} \
  | tail -200
```

Common failures:
- **"Permission denied" on Secret Manager** → SA didn't get IAM binding. Run
  `terraform apply` again.
- **Build fails (`pnpm install`)** → check the lockfile is committed and
  matches package.json.
- **systemctl can't find service** → check `/etc/systemd/system/openclaw-gateway.service` exists.

---

## Step 3 — Pair Chrome Remote Desktop (one-time)

Skip this section if `enable_chrome_remote_desktop = false` in your tfvars.

### 3.1 Get the pairing command

1. On your laptop browser, go to https://remotedesktop.google.com/headless
2. Sign in with the same Google account that has GCP access.
3. Click **Begin** → **Next** → **Authorize**.
4. Pick **Debian Linux** when asked for OS.
5. Copy the long bash command shown. It starts with:
   ```
   DISPLAY= /opt/google/chrome-remote-desktop/start-host \
     --code="..." --redirect-url="..." --name=$(hostname)
   ```

### 3.2 Run it on the VM

```bash
# SSH into the VM
gcloud compute ssh openclaw-prod \
  --zone ${GCP_REGION}-a \
  --project=${GCP_PROJECT_ID}

# On the VM, switch to the openclaw user (CRD must run under a real user)
sudo su - openclaw

# Paste the bash command from step 3.1. It will:
#  - Prompt for a 6-digit PIN (set anything you'll remember)
#  - Enroll the VM with your Google account
#  - Start the chrome-remote-desktop service

# Verify CRD is running
sudo systemctl status chrome-remote-desktop@openclaw
```

### 3.3 Connect from your laptop

1. Visit https://remotedesktop.google.com/access
2. You'll see "openclaw-prod" listed.
3. Click it, enter the PIN you set, and you're in.

You're now looking at an XFCE desktop running on the VM. Chromium is installed —
you can use it to view local URLs (e.g. `http://localhost:18789`) or watch the
agent's browser tooling in real time.

### 3.4 Stop CRD when not needed

CRD is a privileged service; stop it when you don't need it.

```bash
sudo systemctl stop chrome-remote-desktop@openclaw
```

To re-enable: `sudo systemctl start chrome-remote-desktop@openclaw`.

---

## Step 4 — Allow the control panel to reach the gateway

By default, no firewall rule allows port 18789 traffic. You need to set
`allowed_source_ranges_gateway` to permit Cloud Run egress.

### Find your Cloud Run egress IP

If your Cloud Run service uses a static egress IP via VPC connector + NAT:
- Check Cloud Console → VPC → Cloud NAT → see the assigned IP.

If it doesn't have one yet (egress is pooled):
- Open `0.0.0.0/0` temporarily for initial testing **with the gateway token
  acting as the only auth**, OR
- Set up a VPC connector + Cloud NAT in the control panel project to get a
  predictable IP, then restrict.

For initial testing (quickest):

```hcl
# in envs/prod/terraform.tfvars
allowed_source_ranges_gateway = ["0.0.0.0/0"]
```

`terraform apply` again to add the firewall rule.

**Production-grade** later: replace with the specific Cloud Run egress IP.

---

## Step 5 — Connect the control panel to the new VM

Update the control panel's env var to point at the new VM:

```bash
# Get the VM's external IP
VM_IP=$(cd "c:/Project collaborator all/agent_os_openclaw/infra/terraform/envs/prod" \
  && terraform output -raw vm_external_ip)

# Update Cloud Run env
gcloud run services update control-panel-acp \
  --region=${GCP_REGION} \
  --project=${GCP_PROJECT_ID} \
  --update-env-vars="OPENCLAW_WS_URL=ws://${VM_IP}:18789"
```

The control panel will reconnect on its next request and start using the new
gateway.

---

## Step 6 — Verify end-to-end

1. Open the control panel UI in a browser.
2. Send a chat message in any goal.
3. Watch `/var/log/openclaw-18789.log` on the VM:
   ```bash
   gcloud compute ssh openclaw-prod --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID} \
     --command="sudo tail -f /var/log/openclaw-18789.log"
   ```
4. You should see the message arrive at the gateway and a response stream back.

---

## Step 7 — Decommission the old VM (when ready)

The original `openclaw-v1-test` VM is still running. Once you've confirmed
the new `openclaw-prod` VM works:

```bash
# Optionally save its current state for comparison
gcloud compute instances describe openclaw-v1-test \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID} \
  > /tmp/old-vm-spec.txt

# Delete it
gcloud compute instances delete openclaw-v1-test \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID}
```

If you don't want to delete it yet, just stop it — saves money but keeps it
recoverable:

```bash
gcloud compute instances stop openclaw-v1-test \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID}
```

---

## Future: scaling up to multiple VMs (Phase 4)

The current setup is one VM. When you want more:

1. Build a Packer image:
   ```bash
   cd c:/Project\ collaborator\ all/agent_os_openclaw/infra/packer
   packer init openclaw.pkr.hcl
   packer build -var "git_sha=$(git rev-parse --short HEAD)" -var "project=${GCP_PROJECT_ID}" openclaw.pkr.hcl
   ```
2. Switch `envs/prod/main.tf` from the `openclaw-vm` module to the
   `openclaw-fleet` module.
3. Pass in the existing SA email so the SA isn't recreated.
4. `terraform apply` — provisions the MIG with the baked image.
5. Old single VM can be destroyed.

Detailed steps in `docs/deployment/OPENCLAW_REPO_PLAN.md` (Phase 4).

---

## Common ops cheat sheet

```bash
# SSH (with external IP)
gcloud compute ssh openclaw-prod \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID}

# SSH (via IAP tunnel — when ssh_iap_only=true)
gcloud compute ssh openclaw-prod \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID} \
  --tunnel-through-iap

# Tail gateway log
gcloud compute ssh openclaw-prod \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID} \
  --command="sudo tail -f /var/log/openclaw-18789.log"

# Restart gateway (e.g. after pulling new code) — normally scripts/redeploy.sh
# does this for you via Cloud Build; this is the manual equivalent.
gcloud compute ssh openclaw-prod \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID} \
  --command="cd /opt/agent_os_openclaw && sudo git pull && sudo pnpm install --frozen-lockfile && sudo pnpm build && sudo systemctl restart openclaw-gateway@18789"

# Reboot VM (re-runs full startup script — useful if you've changed the script in Terraform)
gcloud compute instances reset openclaw-prod \
  --zone ${GCP_REGION}-a --project=${GCP_PROJECT_ID}

# Destroy the entire prod env (CAREFUL — only for testing/teardown)
cd "c:/Project collaborator all/agent_os_openclaw/infra/terraform/envs/prod"
terraform destroy
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `terraform apply` fails with `Error 403: Required 'compute.googleapis.com'` | API not enabled | `gcloud services enable compute.googleapis.com` |
| Startup script silent | Boot is slow on first run (5-10 min) | Wait, or `gcloud compute instances get-serial-port-output` |
| Gateway log shows `Cannot find module` | Build didn't complete | SSH in, run `cd /opt/agent_os_openclaw && pnpm install --frozen-lockfile && pnpm build` manually |
| WS connect refused | Firewall not allowing the source | Check `allowed_source_ranges_gateway` in tfvars |
| Auth fails on WS | Token mismatch | Compare `gcloud secrets versions access latest --secret=openclaw-gateway-token` against control panel env |
| CRD pairing URL doesn't show the VM | Pairing command never ran on VM | Re-do step 3.2 |
| CRD shows desktop but no browser | google-chrome-stable failed to install | SSH in, run `sudo apt-get install -y google-chrome-stable` |

---

## Open question — confirm before running

- **Confirm the existing `openclaw-gateway-token` secret value matches the
  token your existing Cloud Run control panel uses** (`OPENCLAW_GATEWAY_TOKEN`
  env var on the control panel service). If they differ, the new VM and
  the control panel won't authenticate to each other.
- **Confirm `control-panel-acp-1097695349140.${GCP_REGION}.run.app` is your
  prod control panel URL.** If not, update `control_panel_url` in
  `terraform.tfvars` before applying.
