# 0 · Prerequisites

Set up your machine and a fresh Google Cloud project. Do every step here before
moving on — later stages assume all of this is done.

## 0.1 Create a Google Cloud project
1. Go to <https://console.cloud.google.com>.
2. Top bar → the project dropdown → **New Project**.
3. Name it (e.g. `my-agentos`). Note the **Project ID** it shows (e.g.
   `my-agentos-472913`) — this is different from the name and you'll use it a lot.
4. Click **Create**, then switch into the new project (project dropdown → select it).

## 0.2 Turn on billing
1. Left menu → **Billing**.
2. Link a billing account (add a card if you don't have one).
   AgentOS uses paid services. Expect roughly **$25+/day idle** (GKE ~3 nodes +
   cluster fee, Cloud SQL, gateway VM, 5 warm Cloud Run services) before LLM
   usage, more when agents are working.

## 0.3 Install the command-line tools
You'll run commands on your own computer. Install these:

| Tool | What it's for | Install |
|------|---------------|---------|
| **gcloud** | Talk to Google Cloud | <https://cloud.google.com/sdk/docs/install> |
| **terraform** | Create the cloud infrastructure | <https://developer.hashicorp.com/terraform/install> |
| **git** | Get the AgentOS code | <https://git-scm.com/downloads> |
| **kubectl** + **gke-gcloud-auth-plugin** | Reach the GKE cluster (preflight, readiness checks) | `gcloud components install kubectl gke-gcloud-auth-plugin` |
| **Python 3** | `publish-extension.sh` and a few helper scripts | <https://www.python.org/downloads/> |
| **openssl** | `prerequisites.sh` generates the internal keys with it | Nothing to install: built into macOS/Linux, and on Windows it comes with Git for Windows (Git Bash) |
| **bash** | Every script in this guide is bash | macOS/Linux: built in. Windows: **Git Bash** or **WSL** |

Verify each works (any recent version is fine):
```bash
gcloud --version
terraform -version
git --version
kubectl version --client
gke-gcloud-auth-plugin --version
python3 --version        # `python --version` on Windows
openssl version
```
On Windows, run these (and every later command) in **Git Bash**. PowerShell and
Command Prompt do not see Git Bash's tools, so `openssl version` fails there.

## 0.4 Log in to gcloud
```bash
gcloud auth login                       # opens a browser to sign in
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default login    # lets terraform use your credentials
```
Replace `YOUR_PROJECT_ID` with the Project ID from step 0.1.

> **Logins expire mid-deploy.** If a command fails with `Reauthentication
> failed`, or just hangs, re-run **both** `gcloud auth login` and
> `gcloud auth application-default login`, then re-run the step that stopped.
> Every step in this guide is safe to re-run.

## 0.5 Get the AgentOS code (BOTH branches, in two folders)
AgentOS deploys in two stages from two branches — clone **both** (from your fork;
here we use the upstream). Keep them side by side:
```bash
git clone -b agent_os_openclaw --single-branch https://github.com/YOUR_ORG/AgentOS.git agentos-openclaw
git clone -b agent_os_platform --single-branch https://github.com/YOUR_ORG/AgentOS.git agentos-platform
```
- `agentos-openclaw` = the **gateway** → **STAGE ①** (deploy first)
- `agentos-platform` = the **control plane** → **STAGE ②**, and it holds
  `prerequisites.sh` + this `deploy/` guide.

(Private fork? Authenticate git first — `gh auth login` or a PAT with
**Contents: Read**. Full 3-place setup — laptop, gateway VM, Cloud Build — is in
`warmup.md` §E.)

## 0.6 Make sure you're an Owner
You need the **Owner** role on the project (you are, if you created it). To check:
Left menu → **IAM & Admin → IAM** → find your email → it should say **Owner**.

**Project inside a Google Cloud organisation?** Project Owner alone is not
enough. Organisation policies often block two things AgentOS needs, and only an
**Org Policy Administrator** can allow them for this project:
- **Service-account key creation** (`iam.disableServiceAccountKeyCreation` and
  its managed twin `iam.managed.disableServiceAccountKeyCreation`) — two keys
  need it: the Firebase service-account key you download in file 01, and the
  key Terraform creates in file 05 for the execution plane's database proxy
  (`execution-sqlproxy-key`).
- **An external IP for the gateway VM** (`compute.vmExternalIpAccess`) — the
  gateway VM reaches the LLM providers and GitHub through it.

Check each of the three constraints (enforced = blocked):
```bash
gcloud org-policies describe iam.disableServiceAccountKeyCreation --project <PROJECT_ID> --effective
gcloud org-policies describe iam.managed.disableServiceAccountKeyCreation --project <PROJECT_ID> --effective
gcloud org-policies describe compute.vmExternalIpAccess --project <PROJECT_ID> --effective
```
For the two `iam.*` key constraints, `enforce: true` in the output means
blocked. For `compute.vmExternalIpAccess`, any `allowedValues` or
`denyAll: true` restricts it.
or look under **IAM & Admin → Organization Policies** (with the project
selected), and ask your admin before you start; otherwise file 01, the gateway
stage, or file 05 fails part-way.

---

### ✅ Before continuing, you should have
- [ ] A GCP **Project ID** written down
- [ ] Billing enabled
- [ ] `gcloud`, `terraform`, `git`, `kubectl` + `gke-gcloud-auth-plugin`, Python 3,
      `openssl` and a bash shell installed and working
- [ ] (Organisation projects) SA key creation + gateway external IP allowed
- [ ] Logged in with `gcloud auth login` **and** `application-default login`
- [ ] The AgentOS repo cloned, on the `agent_os_platform` branch

Next: **`01-firebase-auth.md`**
