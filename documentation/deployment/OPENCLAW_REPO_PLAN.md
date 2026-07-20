# pwt-openclaw repo plan

> Goal: stand up the openclaw repo with a Terraform-provisioned VM, in
> phases. Each phase is independently runnable and validates one layer
> before adding the next.

## Approach

Five phases of increasing complexity:

| Phase | Goal | What we validate |
|---|---|---|
| 0 | A bare VM provisioned by Terraform | Terraform → GCP plumbing works |
| 1 | + Service account with the right IAM | Secret Manager and Cloud SQL access work |
| 2 | + Startup script that fetches openclaw code | Boot flow installs deps and clones repo |
| 3 | + The gateway actually runs and accepts WS connections | End-to-end: control panel can talk to it |
| 4 | + MIG (managed instance group) replaces the standalone VM | Production shape: rolling replace, auto-heal |

Each phase is small enough to debug if something breaks. Don't skip to phase 4
until 0–3 work.

---

## Prerequisites

Before you start, install these locally:

- `gcloud` CLI authenticated as you (or as a deploy SA)
- `terraform` v1.6+
- Optional: `tflint` for catching config errors before apply

GCP-side prerequisites (one-time):

- Project `agentic-teams` exists
- Required APIs enabled:
  ```bash
  gcloud services enable \
    compute.googleapis.com \
    secretmanager.googleapis.com \
    sqladmin.googleapis.com \
    iam.googleapis.com \
    cloudbuild.googleapis.com \
    --project=agentic-teams
  ```
- A GCS bucket for Terraform state:
  ```bash
  gcloud storage buckets create gs://agentic-teams-tfstate \
    --location=${GCP_REGION} \
    --uniform-bucket-level-access
  ```

---

## Repo skeleton

Initial layout for `pwt-openclaw`:

```
pwt-openclaw/
├── README.md
├── CLAUDE.md
├── .gitignore
├── .github/
│   └── CODEOWNERS
├── src/                          # the openclaw fork code (move from openclaw_fork_03122026/)
├── tests/
├── package.json
├── tsconfig.json
├── start-openclaw.sh             # boot script (added in Phase 2)
├── run-gateway.sh                # registration + drain wrapper (added in Phase 4)
└── infra/
    └── terraform/
        ├── modules/
        │   └── openclaw-vm/      # the module we're building, phase by phase
        │       ├── main.tf
        │       ├── variables.tf
        │       └── outputs.tf
        └── envs/
            └── prod/
                ├── main.tf
                ├── backend.tf
                ├── variables.tf
                └── terraform.tfvars
```

---

# Phase 0 — bare VM via Terraform

**Goal:** prove that `terraform apply` from your laptop produces a running GCE
VM in `agentic-teams`. Nothing fancy, no openclaw code yet. We're validating
auth, state backend, and the basic provisioning loop.

### Files

`pwt-openclaw/infra/terraform/envs/prod/backend.tf`:

```hcl
terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
  }

  backend "gcs" {
    bucket = "agentic-teams-tfstate"
    prefix = "openclaw/prod"
  }
}

provider "google" {
  project = "agentic-teams"
  region  = "${GCP_REGION}"
  zone    = "${GCP_REGION}-a"
}
```

`pwt-openclaw/infra/terraform/envs/prod/variables.tf`:

```hcl
variable "machine_type" {
  type    = string
  default = "e2-standard-4"
}

variable "zone" {
  type    = string
  default = "${GCP_REGION}-a"
}
```

`pwt-openclaw/infra/terraform/envs/prod/main.tf`:

```hcl
module "openclaw_vm" {
  source = "../../modules/openclaw-vm"

  name         = "openclaw-prod"
  machine_type = var.machine_type
  zone         = var.zone
  env          = "prod"
}

output "vm_external_ip" {
  value = module.openclaw_vm.external_ip
}

output "vm_internal_ip" {
  value = module.openclaw_vm.internal_ip
}
```

`pwt-openclaw/infra/terraform/modules/openclaw-vm/variables.tf`:

```hcl
variable "name"         { type = string }
variable "machine_type" { type = string }
variable "zone"         { type = string }
variable "env"          { type = string }
```

`pwt-openclaw/infra/terraform/modules/openclaw-vm/main.tf` (Phase 0 version):

```hcl
resource "google_compute_instance" "openclaw" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    network = "default"
    access_config {
      # Empty block = ephemeral external IP. Remove for internal-only later.
    }
  }

  labels = {
    environment = var.env
    component   = "openclaw"
  }

  tags = ["openclaw-gateway"]
}
```

`pwt-openclaw/infra/terraform/modules/openclaw-vm/outputs.tf`:

```hcl
output "external_ip" {
  value = google_compute_instance.openclaw.network_interface[0].access_config[0].nat_ip
}

output "internal_ip" {
  value = google_compute_instance.openclaw.network_interface[0].network_ip
}
```

### Run it

```bash
cd pwt-openclaw/infra/terraform/envs/prod

terraform init
terraform plan       # review what it will create
terraform apply      # type 'yes' when prompted
```

### Verify

```bash
# 1. VM exists
gcloud compute instances list --project=agentic-teams \
  --filter="name=openclaw-prod"

# 2. SSH in and confirm it's a stock Debian box
gcloud compute ssh openclaw-prod --zone ${GCP_REGION}-a --project=agentic-teams
> uname -a
> exit
```

### Common failure modes

- **403 on apply** → your `gcloud` user doesn't have `roles/compute.admin` and
  `roles/iam.serviceAccountUser` on the project. Check `gcloud projects
  get-iam-policy agentic-teams`.
- **GCS bucket not found** → state bucket wasn't created, or the prefix is
  wrong. Recheck `backend.tf`.
- **API not enabled** → `gcloud services enable compute.googleapis.com
  --project=agentic-teams`.

### Phase 0 done when

You can SSH into the VM. That's it. We haven't done anything openclaw-specific
yet — that's intentional.

---

# Phase 1 — add service account with IAM

**Goal:** the VM runs as a dedicated service account that can read secrets and
connect to Cloud SQL. We'll use this in Phase 2's startup script.

### What changes

Extend the module to:

- Create `sa-openclaw@agentic-teams.iam.gserviceaccount.com`
- Bind it to: `roles/secretmanager.secretAccessor` (project-level for now;
  scope down later), `roles/cloudsql.client`, `roles/logging.logWriter`,
  `roles/monitoring.metricWriter`
- Attach the SA to the VM

### Updated module `main.tf`

```hcl
# --- service account ---

resource "google_service_account" "openclaw" {
  account_id   = "sa-openclaw"
  display_name = "openclaw VMs (all envs)"
  description  = "Used by openclaw gateway VMs to access Secret Manager + Cloud SQL"
}

resource "google_project_iam_member" "openclaw_secrets" {
  project = "agentic-teams"
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.openclaw.email}"
}

resource "google_project_iam_member" "openclaw_sql" {
  project = "agentic-teams"
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.openclaw.email}"
}

resource "google_project_iam_member" "openclaw_logging" {
  project = "agentic-teams"
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.openclaw.email}"
}

resource "google_project_iam_member" "openclaw_monitoring" {
  project = "agentic-teams"
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.openclaw.email}"
}

# --- VM ---

resource "google_compute_instance" "openclaw" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    network = "default"
    access_config {}
  }

  service_account {
    email  = google_service_account.openclaw.email
    scopes = ["cloud-platform"]
  }

  labels = {
    environment = var.env
    component   = "openclaw"
  }

  tags = ["openclaw-gateway"]
}
```

### Run

```bash
terraform plan       # should show: 1 SA + 4 IAM bindings + VM update
terraform apply
```

### Verify

```bash
# SA exists
gcloud iam service-accounts list --project=agentic-teams \
  | grep sa-openclaw

# VM is running as the SA
gcloud compute instances describe openclaw-prod \
  --zone ${GCP_REGION}-a --project=agentic-teams \
  --format="value(serviceAccounts[0].email)"
# → should print: sa-openclaw@agentic-teams.iam.gserviceaccount.com

# SSH and confirm metadata server reports the SA
gcloud compute ssh openclaw-prod --zone ${GCP_REGION}-a --project=agentic-teams
> curl -H "Metadata-Flavor: Google" \
    http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email
sa-openclaw@agentic-teams.iam.gserviceaccount.com
> exit
```

### Phase 1 done when

The VM authenticates as `sa-openclaw`, and from inside the VM you can list
secrets:

```bash
# inside the VM
gcloud secrets list --project=agentic-teams
# (will be empty until Phase 2 creates them, but the call itself should succeed)
```

---

# Phase 2 — startup script that fetches openclaw

**Goal:** boot the VM with a script that installs Node, fetches the openclaw
code, and prepares it to run. We won't actually start the gateway yet — just
get it ready to start.

### What changes

- Add a startup script attached to the VM via metadata
- Script installs Node 20, jq, git
- Script fetches the openclaw fork (initially via tarball from GCS; later via
  GitHub deploy key)

### Decision: where does openclaw source code come from at boot?

Two options:

**Option A: Tarball in GCS** (simpler for first test)

- Build a tarball locally: `tar -czf openclaw.tar.gz src/ package.json
  tsconfig.json start-openclaw.sh`
- Upload to `gs://agentic-teams-openclaw-prod/code/openclaw.tar.gz`
- Boot script downloads with `gsutil cp` (uses the SA, no auth fuss)

**Option B: Git clone from GitHub** (closer to production)

- Need a GitHub deploy key (or use a public repo)
- Boot script does `git clone`

For this phase, use **Option A** — it sidesteps the deploy-key dance. We'll
move to Option B in Phase 4 once Packer is in place (Packer will bake the code
into the image at build time, so neither tarball nor git clone is needed at
boot).

### Files to add

`pwt-openclaw/start-openclaw.sh` (Phase 2 version — minimal):

```bash
#!/bin/bash
set -euo pipefail

# Read instance metadata
ENV=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/env)
PROJECT=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/project/project-id)

echo "Boot at $(date) — env=$ENV project=$PROJECT"

# Install deps
apt-get update
apt-get install -y curl ca-certificates gnupg jq

# Node 20
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g pnpm

# Fetch openclaw code from GCS
mkdir -p /opt/openclaw
gsutil cp "gs://agentic-teams-openclaw-${ENV}/code/openclaw.tar.gz" /tmp/oc.tar.gz
tar -xzf /tmp/oc.tar.gz -C /opt/openclaw

cd /opt/openclaw
pnpm install --frozen-lockfile
pnpm build

echo "Build complete at $(date). Gateway not started yet (Phase 2)."
```

### Update the module to attach the startup script

Add to `modules/openclaw-vm/main.tf`:

```hcl
resource "google_compute_instance" "openclaw" {
  # ... existing fields ...

  metadata = {
    env             = var.env
    startup-script  = file("${path.module}/../../../../start-openclaw.sh")
  }
}
```

(The relative path from the module to the script is awkward — see "Improving
the module" note below for a cleaner approach.)

### Create the GCS bucket and upload the tarball

```bash
# Create bucket
gcloud storage buckets create gs://agentic-teams-openclaw-prod \
  --location=${GCP_REGION} \
  --uniform-bucket-level-access \
  --project=agentic-teams

# Grant the SA read access (Terraform will own this once we move it in)
gcloud storage buckets add-iam-policy-binding gs://agentic-teams-openclaw-prod \
  --member='serviceAccount:sa-openclaw@agentic-teams.iam.gserviceaccount.com' \
  --role='roles/storage.objectViewer'

# Build and upload tarball
cd pwt-openclaw
tar -czf /tmp/openclaw.tar.gz \
  --exclude=node_modules \
  --exclude=.git \
  src/ package.json pnpm-lock.yaml tsconfig.json start-openclaw.sh

gsutil cp /tmp/openclaw.tar.gz gs://agentic-teams-openclaw-prod/code/openclaw.tar.gz
```

### Run

```bash
terraform plan
terraform apply
# Note: changing metadata/startup-script triggers VM recreate, not in-place update.
```

### Verify

```bash
# Wait ~3 min for boot script to finish, then check
gcloud compute ssh openclaw-prod --zone ${GCP_REGION}-a --project=agentic-teams

# Inside the VM:
> tail -f /var/log/syslog | grep startup-script
> ls /opt/openclaw                    # should see src/, dist/, node_modules/
> node --version                      # v20.x
> exit
```

If the script failed:
```bash
# Get the boot log
gcloud compute instances get-serial-port-output openclaw-prod \
  --zone ${GCP_REGION}-a --project=agentic-teams | tail -200
```

### Phase 2 done when

`/opt/openclaw/dist/` exists on the VM (the built fork), and the startup
script ran cleanly per the serial console log.

### Improving the module

The relative `file()` path is ugly. Two cleaner alternatives:

1. **Inline the script via a Terraform `template_file`** keyed to module
   variables.
2. **Upload the script to GCS, reference via metadata** — script lives in the
   bucket, VM downloads on boot. This is what we'll do in Phase 4 with Packer.

For Phase 2 the file-path approach is fine — refactor in Phase 4.

---

# Phase 3 — start the gateway and connect

**Goal:** the VM actually runs the openclaw gateway, and we can establish a
WebSocket connection to it from your laptop or from the staging control panel.

### What changes

- Add gateway auth token secret and grant access
- Extend `start-openclaw.sh` to fetch the token and start the gateway
- Add firewall rule allowing port 18789 from your laptop's IP (for testing)
- Optionally: serve a simple `/health` HTTP endpoint to verify reachability

### Create the gateway token secret

One-time setup:

```bash
TOKEN=$(openssl rand -base64 32)
echo -n "$TOKEN" | gcloud secrets create prod-openclaw-gateway-token \
  --data-file=- \
  --project=agentic-teams \
  --labels=environment=prod,component=openclaw

echo "Token: $TOKEN"
# Save this; you'll use it from the control panel side later.
```

The SA already has `roles/secretmanager.secretAccessor` from Phase 1, so it can
read this.

### Update `start-openclaw.sh`

```bash
#!/bin/bash
set -euo pipefail

ENV=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/env)
PROJECT=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/project/project-id)

echo "Boot at $(date) — env=$ENV project=$PROJECT"

apt-get update
apt-get install -y curl ca-certificates gnupg jq
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g pnpm

# Fetch + build
mkdir -p /opt/openclaw /var/openclaw/state
gsutil cp "gs://agentic-teams-openclaw-${ENV}/code/openclaw.tar.gz" /tmp/oc.tar.gz
tar -xzf /tmp/oc.tar.gz -C /opt/openclaw
cd /opt/openclaw
pnpm install --frozen-lockfile
pnpm build

# Fetch gateway auth token
TOKEN=$(gcloud secrets versions access latest \
  --secret="${ENV}-openclaw-gateway-token" --project="$PROJECT")

# Pull config (allowlist) — for now, write a minimal one inline
cat > /var/openclaw/state/openclaw.json <<EOF
{
  "agents": {
    "defaults": {
      "models": {
        "anthropic/claude-haiku-4-5":  {},
        "anthropic/claude-sonnet-4-5": {},
        "anthropic/claude-opus-4-7":   {},
        "google/gemini-2.5-flash":     {},
        "google/gemini-2.5-pro":       {}
      }
    }
  }
}
EOF

# Start gateway
export OPENCLAW_STATE_DIR=/var/openclaw/state
export OPENCLAW_GATEWAY_AUTH_TOKEN="$TOKEN"
export OPENCLAW_GATEWAY_PORT=18789

# Run via systemd-style nohup for now; Phase 4 uses systemd properly
nohup node /opt/openclaw/dist/openclaw.mjs gateway run \
  > /var/log/openclaw.log 2>&1 &

echo "Gateway started (PID $!) at $(date)"
```

Re-upload the tarball and apply Terraform (which will recreate the VM):

```bash
tar -czf /tmp/openclaw.tar.gz \
  --exclude=node_modules --exclude=.git \
  src/ package.json pnpm-lock.yaml tsconfig.json start-openclaw.sh
gsutil cp /tmp/openclaw.tar.gz gs://agentic-teams-openclaw-prod/code/openclaw.tar.gz

cd pwt-openclaw/infra/terraform/envs/prod
terraform apply
```

### Add firewall rule to test from your laptop

Add to `modules/openclaw-vm/main.tf`:

```hcl
resource "google_compute_firewall" "openclaw_gateway" {
  name    = "${var.name}-allow-gateway"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["18789"]
  }

  # Production: lock to control panel's VPC connector source range.
  # For Phase 3 testing, use your laptop's public IP.
  source_ranges = var.allowed_source_ranges
  target_tags   = ["openclaw-gateway"]
}
```

Add to `modules/openclaw-vm/variables.tf`:

```hcl
variable "allowed_source_ranges" {
  type    = list(string)
  default = []
}
```

In `envs/prod/terraform.tfvars`:

```hcl
# replace with your laptop's public IP for now (find via curl ifconfig.me)
allowed_source_ranges = ["YOUR.LAPTOP.IP.HERE/32"]
```

Pass it through in `envs/prod/main.tf`:

```hcl
module "openclaw_vm" {
  source                = "../../modules/openclaw-vm"
  name                  = "openclaw-prod"
  machine_type          = var.machine_type
  zone                  = var.zone
  env                   = "prod"
  allowed_source_ranges = var.allowed_source_ranges
}
```

### Verify

```bash
# 1. Get the VM's external IP
VM_IP=$(terraform output -raw vm_external_ip)

# 2. Tail the gateway log via SSH
gcloud compute ssh openclaw-prod --zone ${GCP_REGION}-a --project=agentic-teams
> tail -f /var/log/openclaw.log
# Should show: gateway listening on :18789

# 3. From your laptop, hit the WS endpoint with auth
TOKEN=$(gcloud secrets versions access latest \
  --secret=prod-openclaw-gateway-token --project=agentic-teams)

# Use websocat or wscat to test:
# wscat -c "ws://${VM_IP}:18789" -H "Authorization: Bearer ${TOKEN}"
# Should connect; type a JSON-RPC request to test:
# {"jsonrpc":"2.0","id":1,"method":"agents.list","params":{}}
```

### Common failure modes

- **Connection refused** → gateway didn't start. SSH in, check
  `/var/log/openclaw.log` for crash details.
- **403 reading secret** → IAM binding missed. Check Phase 1's bindings.
- **WS connect hangs** → firewall rule too restrictive. Verify your IP and
  source_ranges.
- **Auth rejected** → token mismatch. Re-fetch from Secret Manager and confirm
  exact match.

### Phase 3 done when

You can establish an authenticated WebSocket to the VM from your laptop and the
gateway responds to a basic method call.

---

# Phase 4 — replace standalone VM with MIG

**Goal:** move from a single hand-managed VM to a Managed Instance Group with
Packer-baked images. This is the production shape.

### What changes

- Build the openclaw image via Packer (instead of tarball + boot-time install)
- Create an instance template
- Create a regional MIG with `target_size=1`
- Boot script reduces to: read metadata, fetch token, start gateway (no
  install/build at boot — already in image)
- Add health check + auto-heal
- Add SIGTERM trap for graceful shutdown

### Packer config

`pwt-openclaw/infra/packer/openclaw.pkr.hcl`:

```hcl
packer {
  required_plugins {
    googlecompute = {
      source  = "github.com/hashicorp/googlecompute"
      version = "~> 1.1"
    }
  }
}

variable "git_sha" { type = string }
variable "project" { type = string }

source "googlecompute" "openclaw" {
  project_id          = var.project
  source_image_family = "debian-12"
  zone                = "${GCP_REGION}-a"
  image_name          = "openclaw-${var.git_sha}"
  image_family        = "openclaw"
  image_labels = {
    git_sha = var.git_sha
  }
  machine_type = "e2-small"
  ssh_username = "packer"
}

build {
  sources = ["source.googlecompute.openclaw"]

  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      "sudo apt-get install -y curl ca-certificates gnupg jq",
      "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -",
      "sudo apt-get install -y nodejs",
      "sudo npm install -g pnpm",
    ]
  }

  provisioner "file" {
    source      = "../../"
    destination = "/tmp/openclaw-src"
  }

  provisioner "shell" {
    inline = [
      "sudo mkdir -p /opt/openclaw",
      "sudo cp -r /tmp/openclaw-src/. /opt/openclaw/",
      "cd /opt/openclaw && sudo pnpm install --frozen-lockfile && sudo pnpm build",
      "sudo cp /opt/openclaw/start-openclaw.sh /opt/openclaw/run-gateway.sh /usr/local/bin/",
      "sudo chmod +x /usr/local/bin/start-openclaw.sh /usr/local/bin/run-gateway.sh",
    ]
  }
}
```

Build the first image:

```bash
cd pwt-openclaw/infra/packer
packer init openclaw.pkr.hcl
packer build \
  -var "git_sha=$(git rev-parse --short HEAD)" \
  -var "project=agentic-teams" \
  openclaw.pkr.hcl
```

### MIG Terraform module (replaces the standalone VM)

`modules/openclaw-fleet/main.tf`:

```hcl
data "google_compute_image" "openclaw" {
  family  = "openclaw"
  project = var.project
}

resource "google_compute_instance_template" "openclaw" {
  name_prefix  = "openclaw-${var.env}-template-"
  machine_type = var.machine_type
  region       = var.region

  disk {
    source_image = data.google_compute_image.openclaw.self_link
    auto_delete  = true
    boot         = true
    disk_size_gb = 20
  }

  network_interface {
    network = "default"
    access_config {}
  }

  service_account {
    email  = google_service_account.openclaw.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    env        = var.env
    project_id = var.project
  }

  metadata_startup_script = "/usr/local/bin/start-openclaw.sh"

  labels = {
    environment = var.env
    component   = "openclaw"
  }

  tags = ["openclaw-gateway"]

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_health_check" "openclaw" {
  name                = "openclaw-${var.env}-health"
  check_interval_sec  = 30
  timeout_sec         = 10
  healthy_threshold   = 2
  unhealthy_threshold = 3

  tcp_health_check {
    port = 18789
  }
}

resource "google_compute_region_instance_group_manager" "openclaw" {
  name               = "openclaw-${var.env}-mig"
  region             = var.region
  base_instance_name = "openclaw-${var.env}"
  target_size        = var.vm_count

  version {
    instance_template = google_compute_instance_template.openclaw.id
  }

  update_policy {
    type                  = "PROACTIVE"
    minimal_action        = "REPLACE"
    max_surge_fixed       = 1
    max_unavailable_fixed = 0
    replacement_method    = "RECREATE"
  }

  auto_healing_policies {
    health_check      = google_compute_health_check.openclaw.id
    initial_delay_sec = 240
  }
}
```

### Cutover

1. `packer build` produces the first image
2. `terraform apply` provisions the MIG (creates one VM from the new image)
3. Verify it's healthy + accepts WS connections (same checks as Phase 3)
4. `terraform destroy -target=module.openclaw_vm` to remove the standalone VM
   from Phase 0–3

### Phase 4 done when

- The MIG has `target_size=1` and the VM is healthy
- Killing the VM (`gcloud compute instances delete`) → MIG auto-replaces it
- A `packer build` followed by `terraform apply` does a rolling replace
  cleanly

---

## After Phase 4

Subsequent work (out of scope for this initial repo plan):

- Cloud Build trigger for Packer image rebuild on push to `dev`
- Cloud Build trigger for `terraform apply` on image change
- Self-registration in Postgres `gateway_endpoints` table (requires control
  panel's table to exist first)
- Multi-process per VM (start 4 gateways on consecutive ports)
- Drain-on-SIGTERM in `run-gateway.sh`
- Staging environment (mirror of prod, smaller VM)

These are detailed in `OPENCLAW_SCALING_PLAN.md` — they layer onto what we've
built here, they don't replace it.

---

## Quick reference — common commands

```bash
# Apply changes
cd pwt-openclaw/infra/terraform/envs/prod
terraform apply

# SSH to the current VM
gcloud compute ssh openclaw-prod --zone ${GCP_REGION}-a --project=agentic-teams

# View boot log
gcloud compute instances get-serial-port-output openclaw-prod \
  --zone ${GCP_REGION}-a --project=agentic-teams | tail -200

# Tail the gateway log
gcloud compute ssh openclaw-prod --zone ${GCP_REGION}-a --project=agentic-teams \
  --command="tail -f /var/log/openclaw.log"

# Restart the VM (picks up new tarball)
gcloud compute instances reset openclaw-prod \
  --zone ${GCP_REGION}-a --project=agentic-teams

# Destroy everything (careful!)
terraform destroy
```

---

## Open questions to settle as we go

1. **Do you want internal-only IP** (no `access_config{}`)? Requires VPC
   connector for control panel egress and IAP tunneling for SSH. Cleaner but
   adds setup cost. Defer to post-Phase-3 if just testing.
2. **Region** — assumed `${GCP_REGION}`. Confirm.
3. **VM size for prod** — `e2-standard-4` is fine for testing; `c3-standard-16`
   when you go to prod scale per the scaling plan. Confirm starting size.
4. **State bucket location** — assumed `${GCP_REGION}` to match. Confirm.
5. **Secret rotation cadence** — gateway token: every 90 days? More?

Pin these as you hit each phase; they don't block starting.

---

## Recommendation: start at Phase 0 today

Phase 0 is ~30 minutes of work. It validates that your `gcloud` auth, the
state bucket, and Terraform itself all work. If Phase 0 succeeds, Phase 1
takes another ~30 minutes (just IAM additions). Phases 2–3 are the real work
(~half a day each). Phase 4 is a follow-on once the basics are solid.

Don't try to leap to Phase 4 first. The point of the phasing is so each
failure mode is small.
