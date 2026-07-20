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
   AgentOS uses paid services (Cloud Run, Cloud SQL, a small VM); a typical idle
   setup is a few dollars a day, more when agents are working.

## 0.3 Install the command-line tools
You'll run commands on your own computer. Install these:

| Tool | What it's for | Install |
|------|---------------|---------|
| **gcloud** | Talk to Google Cloud | <https://cloud.google.com/sdk/docs/install> |
| **terraform** | Create the cloud infrastructure | <https://developer.hashicorp.com/terraform/install> |
| **git** | Get the AgentOS code | <https://git-scm.com/downloads> |

Verify each works (any recent version is fine):
```bash
gcloud --version
terraform -version
git --version
```

## 0.4 Log in to gcloud
```bash
gcloud auth login                       # opens a browser to sign in
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default login    # lets terraform use your credentials
```
Replace `YOUR_PROJECT_ID` with the Project ID from step 0.1.

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

---

### ✅ Before continuing, you should have
- [ ] A GCP **Project ID** written down
- [ ] Billing enabled
- [ ] `gcloud`, `terraform`, `git` installed and working
- [ ] Logged in with `gcloud auth login` **and** `application-default login`
- [ ] The AgentOS repo cloned, on the `agent_os_platform` branch

Next: **`01-firebase-auth.md`**
