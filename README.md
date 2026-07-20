# AgentOS — Gateway branch (`agent_os_openclaw`)

This branch is **Stage ① of an AgentOS deployment: the gateway**. It is the
runtime that actually executes the AI agents — the OpenClaw gateway plus the
Terraform and provisioning scripts that stand it up on a Google Cloud VM.

AgentOS ships as **two branches of this repo**, deployed in order:

| Branch | Role | Deploy |
|--------|------|--------|
| **`agent_os_openclaw`** ← *you are here* | **Gateway** — runs the agents; creates the shared VPC and archive bucket (secrets + SA come from the platform branch's `prerequisites.sh`, run first) | **Stage ①** (first) |
| `agent_os_platform` | **Control plane** — the web app: goals, stories, agents, and the UI that drives the gateway | Stage ② |

The control plane (`agent_os_platform`) talks to the gateway this branch deploys.
Deploy the gateway **first** — the platform reuses its VPC/secrets and connects to it.

## Deploy
Everything is step-by-step in **[`deploy/README.md`](deploy/README.md)** — start
there. In short: run the platform branch's `prerequisites.sh` once, then from this
branch `terraform apply` to create the VM (it self-provisions on boot), then
proceed to the platform branch for Stage ②.

## What's in here
- `src/`, `apps/`, `extensions/`, … — the OpenClaw gateway source (built on the VM).
- `terraform/` — the VM, VPC, archive bucket, firewall, and secrets it reads.
- `scripts/gateway-provision.sh` — the single provisioning script the VM runs on
  boot (build → write `openclaw.json` → install wrappers/hooks → start the service).
- `cloudbuild.yaml` — the update loop: push to this branch → the VM pulls, rebuilds,
  and restarts.
- `deploy/` — the deployment guide.

## Forking
Each organization runs AgentOS from **its own fork** — the gateway VM clones your
fork on boot, so point `repo_url` (in `terraform.tfvars`) at your fork. Private
fork? See the platform branch's `FORKING.md` and `deploy/warmup.md` §E.

## Credits & license
The gateway is derived from **[OpenClaw](https://github.com/openclaw/openclaw)**
(© Peter Steinberger), originally under the MIT License. This distribution —
AgentOS's infrastructure, provisioning, and integration on top — is released under
the **Apache License 2.0** (see [`LICENSE`](LICENSE)). OpenClaw's original MIT
copyright and permission notice is retained in [`NOTICE.md`](NOTICE.md).
