# Warm-up — everything to gather before you start

Read this first. It's the **manifest**: the tools to install, the accounts to
create, and — most importantly — **every file you'll download, its exact name,
and exactly where to save it.** Using these exact names/paths keeps the later
copy-paste commands working and guarantees nothing secret is ever committed
(each name is already in `.gitignore`).

## A. Install these on your computer
| Tool | For | Install |
|------|-----|---------|
| **gcloud** | Google Cloud CLI | <https://cloud.google.com/sdk/docs/install> |
| **terraform** | Create cloud infra | <https://developer.hashicorp.com/terraform/install> |
| **git** | Get the code | <https://git-scm.com/downloads> |

(Full step-by-step, incl. `gcloud auth login`, is in `00-prerequisites.md`.)

## B. Accounts / access to have ready
- A **Google Cloud** project with **billing** + **Owner** (file 00).
- A **GitHub org** + a **GitHub App** you'll create (file 02).
- A **Firebase** project attached to the GCP project (file 01).
- **LLM API keys**: **Anthropic (Claude)** and **Google Gemini** — both required.
  OpenAI optional.
- *(Optional)* a **Slack** app (file 03).

## C. Get the code — two branches, two folders
Each organization runs AgentOS from its **own fork** — why, and the
`repo_url`-vs-`github_org` distinction, are covered in [`../FORKING.md`](../FORKING.md).
Clone **your fork** (see §E for private forks). Keep the folder names below —
the guide's commands assume them:
```bash
git clone -b agent_os_openclaw --single-branch https://github.com/<your-org>/AgentOS.git agentos-openclaw
git clone -b agent_os_platform --single-branch https://github.com/<your-org>/AgentOS.git agentos-platform
```
`agentos-platform/` is your working root — it holds `prerequisites.sh`,
`set-secret.sh`, this `deploy/` guide, and (below) the files you download.

## D. Files you will DOWNLOAD or CREATE — exact names + locations
Save **all** of these into the **root of your `agentos-platform` checkout**, with
**exactly** these names. Every name is gitignored, so they can't be committed.

| File | What it is | Where you get it | Save as (in `agentos-platform/`) |
|------|-----------|------------------|-----------------------------------|
| `secrets.env` | Local inputs for `prerequisites.sh` (LLM keys, optional tokens) | `cp secrets.env.example secrets.env`, then edit | `agentos-platform/secrets.env` |
| `firebase-sa.json` | Firebase service-account key (login) | File 01.4 (Firebase console) | `agentos-platform/firebase-sa.json` |
| `github-app-key.pem` | GitHub App private key (agent git work) | File 02.5 (GitHub App settings) | `agentos-platform/github-app-key.pem` |

You'll also **create** (not download) `terraform.tfvars` in each stack's
`terraform/` folder from the provided `terraform.tfvars.example`. Those are
gitignored too. **Never rename** any of these — the commands in files 06–08 refer
to them by exact name.

> Rule of thumb: **secrets and downloads live in the checkout root, never
> elsewhere, never renamed.** If a command says `firebase-sa.json`, it means the
> one in `agentos-platform/`.

## E. Private fork? Set up git auth (3 places)
If your AgentOS fork is **public**, skip this. If it's **private**, three
different things need to read it — give each the right credential:

1. **Your laptop (to clone in §C).** Authenticate git once:
   ```bash
   gh auth login              # GitHub CLI — easiest; or:
   git config --global credential.helper store   # then clone; git prompts for a PAT once
   ```
   Use a token with **Contents: Read** on the fork.
2. **The gateway VM (clones the fork on boot).** Put a **fine-grained** GitHub
   token (scoped to only your fork, **Contents: Read-only**) in `secrets.env` as
   `GATEWAY_REPO_TOKEN` **before** running `prerequisites.sh`. See file 04 for how
   to mint it. (Public fork → leave blank.)
3. **Cloud Build (auto-deploy on push).** When you create the triggers (files 07 /
   gateway README), use **Console → Cloud Build → Repositories → Connect** and
   authorize the GitHub App for your fork. That connection is what lets Cloud
   Build read a private repo — no token needed in config.

## F. Handy: how to watch things while deploying
- **Cloud Build** (platform build, gateway rebuild): `gcloud builds submit …`
  streams live; otherwise
  `gcloud builds list --ongoing --region <REGION>` then
  `gcloud builds log <BUILD_ID> --region <REGION> --stream`.
- **Gateway first boot**: `gcloud compute instances tail-serial-port-output
  agentos-gateway --zone <zone>`; it's **ready** when you see
  `openclaw-gateway@18789: active` and `gateway provision complete … (failed=0)`.

---

### ✅ Ready to start when
- [ ] `gcloud`, `terraform`, `git` installed
- [ ] Both branches cloned as `agentos-openclaw/` and `agentos-platform/`
- [ ] You know where `firebase-sa.json`, `github-app-key.pem`, `secrets.env` will go
- [ ] (Private fork) git auth sorted for laptop + `GATEWAY_REPO_TOKEN` + Cloud Build

Next: **`00-prerequisites.md`**
