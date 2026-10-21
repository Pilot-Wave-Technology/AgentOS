# AgentOS gateway (OpenClaw VM) — Terraform

**Stage ① of an AgentOS deployment — deployed FIRST.** Provisions the OpenClaw
gateway the control plane talks to: a GCE VM that clones the public
`agent_os_openclaw` branch, provisions itself on boot, and runs the gateway
process(es).

This stack **creates** the shared infrastructure the platform (Stage ②) later
reuses:
- the VPC (`network_name`) + its primary subnet,
- the `archive_bucket` (archived agent memory/transcripts),
- the firewalls (gateway port from the VPC/connector ranges; SSH via IAP),
- the gateway VM.

It does **not** create the service account or the Secret Manager secrets — it
only **references** the ones the platform branch's `prerequisites.sh` already
created. `main.tf` has no `google_service_account` and no
`google_secret_manager_secret` resources.

## Prereqs
Run the platform branch's **`prerequisites.sh`** first (from your
`agentos-platform` clone). It creates the scoped gateway service account and the
Secret Manager secrets (`openclaw-gateway-token`, `anthropic-api-key`,
`gemini-api-key`, `control-panel-url`, optional `openai-api-key`) and populates
them from `secrets.env`. The VM reads those secrets on boot.

## Apply
```bash
cp terraform.tfvars.example terraform.tfvars   # edit: project_id, region/zone, name_prefix, repo_url
terraform init
terraform apply
```
`name_prefix` must match the value `prerequisites.sh` used (default `agentos`) —
the gateway SA and secret names derive from it. `archive_bucket` is a computed
default (`<name_prefix>-agent-data-<project>`); leave it unset unless you need a
specific name.

## Wire it to the control plane
After apply, take `network_name` and `archive_bucket` into the platform (Stage ②)
`terraform.tfvars`. The URL needs no copying: the VM's internal address is
reserved as `<name_prefix>-gateway-ip` (it survives recreation) and the
platform stack derives `ws://<address>:18789` from it. The `control-panel-url`
secret is **not** set by the platform stack: you set it in the platform guide's
step 7.0.a (before the first build), and the platform build's sync-url step keeps
it current after that.

> **Only if you are NOT using the platform Stage ②** do you need to write the
> control-panel URL yourself so the gateway can call back:
> ```bash
> gcloud secrets versions add control-panel-url --data-file=- <<< "<control-panel run.app URL>"
> ```
> The VM polls that secret every 60s and restarts the gateway on change (no reboot).

## How it works
- **Self-clones the public repo** on boot (no GitHub creds for a public fork) →
  runs `scripts/gateway-provision.sh` → systemd `openclaw-gateway@<port>.service`
  per process.
- **Least-privilege SA** (created by `prerequisites.sh`): logging + monitoring +
  `secretAccessor` on ONLY the boot secrets + `objectAdmin` on the archive
  bucket. No project-wide secret access.
- **Reachability**: firewall allows the gateway port only from the control-plane
  VPC/connector ranges; SSH only via IAP.
- **Outbound**: needs internet (repo clone + LLM APIs). Default gives an external
  IP; set `ssh_iap_only=true` only if you add Cloud NAT.

## Updating the gateway
A redeploy **must** re-run `scripts/gateway-provision.sh` — it regenerates
`openclaw.json` (LAN bind, compaction/memory, model registry, claude-cli
backend), refreshes secrets, installs the scanners (rg, gitleaks,
trivy, semgrep), and installs the CLI wrappers + hooks. Do **not** shortcut it
with a bare `git pull && pnpm install && pnpm build && restart` — that skips
provisioning and leaves the gateway misconfigured.

Two correct ways:
- **Push to your fork** — Cloud Build (`cloudbuild.yaml`) SSHes in and re-runs
  `gateway-provision.sh`. This is the normal update loop.
- **On the VM directly:**
  ```bash
  cd /opt/agentos-gateway
  sudo git pull --ff-only
  sudo bash scripts/gateway-provision.sh
  ```

Full step-by-step lives in **[`../deploy/README.md`](../deploy/README.md)**.
