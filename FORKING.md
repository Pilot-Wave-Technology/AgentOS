# Forking AgentOS for your own deployment

Each organization runs its **own** copy of AgentOS from a **fork**. The gateway
VM clones your fork's code on boot, and the Platform Research Council reads
platform config from it — so the deploy has to point at *your* fork, not upstream.

## 1. Fork the repo
Fork `AgentOS` into your GitHub org (keep the name `AgentOS`). Your fork has the
same two branches:
- **`agent_os_openclaw`** — the gateway (OpenClaw) code
- **`agent_os_platform`** — the control plane

## 2. Point the deploy at your fork
Set **`repo_url` to your fork in BOTH stacks** (same value):

| File | Setting | Value |
|---|---|---|
| `AgentOS-openclaw/terraform/terraform.tfvars` | `repo_url` | `https://github.com/<your-org>/AgentOS.git` |
| `AgentOS-openclaw/terraform/terraform.tfvars` | `repo_branch` | `agent_os_openclaw` (default) |
| `AgentOS-platform/terraform/terraform.tfvars`  | `repo_url` | `https://github.com/<your-org>/AgentOS.git` (same) |

That single URL drives everything code-related: the gateway clone **and** the
Platform Research Council's clone (it reads the `agent_os_platform` branch).

## 3. Two different "repo" ideas — don't confuse them
- **`repo_url`** = *where the AgentOS code lives* (your fork). Used by the gateway
  + the research council.
- **`github_org`** (platform tfvars) = *where your agents create and work on goal
  repos* via the GitHub App. This is your product's work — often a different org,
  and unrelated to where AgentOS itself is hosted.

## 4. Private fork?
A private fork is read by **three** different things — give each the right auth
(full walkthrough in `deploy/warmup.md` §E):

1. **Your laptop** (to clone): `gh auth login`, or a PAT with **Contents: Read**.
2. **The gateway VM** (clones on boot): a **fine-grained** GitHub token scoped to
   only your fork, **Contents: Read-only**, placed in `secrets.env` as
   `GATEWAY_REPO_TOKEN` **before** running `prerequisites.sh`. Leave blank for a
   public fork.
3. **Cloud Build** (auto-deploy on push): connect the repo via **Console → Cloud
   Build → Repositories → Connect** and authorize the GitHub App for your fork —
   that connection grants read access; no token in config.

## 5. Keeping your fork up to date
Pull upstream changes into your fork's branches as usual
(`git fetch upstream && git merge upstream/agent_os_openclaw`, etc.), then
re-deploy: the gateway `cloudbuild.yaml` pulls + rebuilds on push, and the
platform `cloudbuild.yaml` builds + migrates + deploys on push.

---
TL;DR: **fork → set `repo_url` to your fork in both tfvars → `github_org` is only
your goal-repo org → deploy (gateway first, then platform).**
