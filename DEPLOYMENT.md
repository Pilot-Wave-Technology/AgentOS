# Deploying AgentOS — the map

This is the **high-level map** of an AgentOS deployment. It shows the order and how
the two branches fit together, then hands you off to the detailed, step-by-step
guide inside each branch. Do the real work from those guides — this page is just so
you always know where you are.

> **AgentOS deploys in TWO stages, gateway first:**
> **① the gateway (`agent_os_openclaw`)**, then **② the control plane (`agent_os_platform`)**.
> The gateway creates the shared VPC and archive bucket; the control plane reuses them
> and connects to it. Deploy the platform first and it has nothing to talk to.

Everything runs on **Google Cloud** (Cloud Run, Cloud Build, Cloud SQL, one gateway VM),
all in **one region**. Almost everything is created for you by Terraform — your job is
mostly to gather a few accounts/keys and run a handful of commands in order.

**Expect ~2 hours** for a first-time setup. Running cost is a few dollars/day when idle
and more while agents are working — you're paying for one always-on gateway VM plus
Cloud SQL and Cloud Run.

The two detailed guides:
- **Stage ① gateway** → [`agent_os_openclaw/deploy/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_openclaw/deploy/README.md)
- **Stage ② control plane** → [`agent_os_platform/deploy/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/README.md)

The Stage ② guide is the **spine of the process** — it owns the shared prerequisites
and the one-time setup that both stages depend on, and it hands off to Stage ① at the
right moment (you still deploy the gateway first; the platform guide just drives the
whole sequence). Start there.

---

## The order at a glance

### 0 · Before you start — accounts, keys, and one-time setup
*(Stage ② guide, files `00`–`04` + `prerequisites.sh`)*

- A **Google Cloud** project (billing enabled, you're owner) and the `gcloud` + `terraform` CLIs.
- **Firebase** for login, a **GitHub org + App** for agent repo work, LLM API keys (**Anthropic + Gemini**; OpenAI optional). **Slack** is optional and needs *nothing* pre-deploy — you wire it up in the admin UI (Settings → Integrations → Slack) after first login.
- Collect all of it into `secrets.env`, then run the one-time, repeatable
  **`prerequisites.sh <PROJECT> <REGION>`** — it creates the service accounts, IAM,
  **all Secret Manager secrets**, and the **Cloud SQL** instance + database + users.

### 1 · Stage ① — deploy the gateway
*(`agent_os_openclaw/deploy/`)*

- `terraform apply` in the gateway branch creates the **VM** (plus the shared **VPC**,
  **archive bucket**, firewall). The VM **self-provisions on boot**: it clones the
  branch, builds the gateway, installs the security scanners, writes its config, and
  starts the service on port **18789**.
- Confirm it's up (`systemctl is-active openclaw-gateway@18789` → `active`) and note the
  VM's internal IP — you'll need `ws://<ip>:18789` next.

### 2 · Stage ② — deploy the control plane
*(`agent_os_platform/deploy/`, files `05`–`09`)*

1. **Provision infra** — set `openclaw_ws_url = ws://<gateway-ip>:18789`, then
   `terraform apply` creates the VPC connector, buckets, and the Cloud Run **placeholder**
   services (four in the default split profile, or one in `single`).
2. **Gateway-ready gate (do this before building)** — set the `control-panel-url` secret
   to the web service URL, then confirm the gateway is **ready**: service `active`, it has
   `CONTROL_PANEL_URL` in its env, and the **Anthropic key is present**. This gate matters:
   the build is when the platform agents are (re)created against the gateway, and if the
   gateway can't resolve their model yet they fall back to a default. *Only build once it's green.*
3. **Build & deploy** — `gcloud builds submit … cloudbuild.yaml` runs the pipeline:
   **build** the image → **migrate** (create the schema + seed the built-in
   agents/templates/tools) → **deploy** the real image to every service.

### 3 · First login
*(Stage ② guide, file `09`)*

- Open the web URL, log in as the bootstrap admin, add the hostname to Firebase
  **Authorized domains**, and **create a goal**. That first goal is the end-to-end test:
  the Goal Orchestrator provisions, responds, and you're live.

---

## Tearing it down (repeatable testing)

Each branch ships a `reset.sh`. Run them in this order — **platform first**, because its
VPC connector lives on the gateway's VPC:

```bash
cd agentos-platform  && ./reset.sh      # Cloud Run, buckets, connector, Cloud SQL, DSN secrets
cd ../agentos-openclaw && ./reset.sh    # gateway VM, VPC, archive bucket
```

Your accounts, LLM keys, and other Secret Manager entries survive teardown, so a
re-deploy on the same project reuses them. Then redeploy in the same order: Stage ① → Stage ②.

---

## Where the detail lives

| You want to… | Go to |
|---|---|
| See the full ordered checklist + prerequisites | `agent_os_platform/deploy/README.md` (+ `warmup.md`) |
| Stand up the gateway VM | `agent_os_openclaw/deploy/README.md` |
| Fork + configure for your org | `agent_os_platform/FORKING.md`, `CONFIG.md` |
| Day-2 ops: updates, settings, teardown | `agent_os_platform/deploy/10-operations-and-troubleshooting.md` |

Each organization runs AgentOS from its **own fork** of this repository.
