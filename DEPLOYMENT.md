# Deploying AgentOS — the map

This page is only the order. Do the real work from the step-by-step files it links;
the `agent_os_platform` guide ([`deploy/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/README.md)) is the spine and drives the whole sequence.
Everything runs on **Google Cloud, in one region**. Expect ~2 hours the first time.

**Run [`./preflight.sh`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/preflight.sh) (platform branch) between every pair of steps** — it checks that what the last step should have created actually exists, and treats "could not determine" as a failure.

1. **Accounts and one-time setup** — platform [`deploy/00`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/00-prerequisites.md)–[`04`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/04-collect-your-values.md): GCP project, Firebase, GitHub App, optional Slack (configured after first login), collect your values into `secrets.env`, then run `./prerequisites.sh <PROJECT> <REGION>`. It creates the service accounts, IAM and every non-database secret, and stops with a list of exact fix commands if a required secret (the Gemini and Anthropic keys, the Firebase service account, the GitHub App key) is empty.
2. **Stage ① — gateway** — [`agent_os_openclaw` `deploy/README.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_openclaw/deploy/README.md): `terraform apply` creates the VM (it self-provisions on boot), VPC, reserved address, archive bucket and firewalls. Note the outputs `network_name` and `archive_bucket`. The gateway URL is derived automatically from the reserved `<prefix>-gateway-ip` address — nothing to copy.
3. **Stage ② — platform infrastructure** — platform [`deploy/05`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/05-provision-infrastructure.md): `terraform apply` creates Cloud SQL, the image registry, the VPC connector, the five Cloud Run services and the GKE execution-plane cluster.
4. **Secrets, readiness, build** — platform [`deploy/06`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/06-fill-in-secrets.md)–[`07`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/07-build-and-deploy.md): check every secret is real, pass the gateway readiness gate, run Cloud Build (build → migrate → deploy), publish the VS Code extension with `./publish-extension.sh`, and point the GitHub App webhook at your URL.
5. **Verify and log in** — platform [`deploy/08`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/08-deploy-gateway-vm.md)–[`09`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/09-first-login-and-verify.md): confirm the gateway link, then log in as the bootstrap admin. Optional channels (Slack, [`deploy/03`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/03-slack-app.md)) are set up now, in the admin UI.
6. **Your first story** — platform [`deploy/11-your-first-story.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/11-your-first-story.md): a goal, a repo, a story, and a pull request you accept.

**Teardown:** `./teardown.sh` in the platform checkout removes the platform (Cloud Run, Cloud SQL, GKE), then the gateway (VM, VPC, archive bucket). Secrets and service accounts persist, so a redeploy on the same project reuses them; `./teardown.sh --everything` removes those too. What is left on purpose, and recovery: platform [`deploy/12-teardown.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/12-teardown.md).

## Where the detail lives

| You want to… | Go to |
|---|---|
| See the full ordered checklist + prerequisites | `agent_os_platform/deploy/README.md` (+ `warmup.md`) |
| Stand up the gateway VM | `agent_os_openclaw/deploy/README.md` |
| Fork + configure for your org | `agent_os_platform/FORKING.md`, `CONFIG.md` |
| Day-2 ops: updates, settings, teardown | `agent_os_platform/deploy/10-operations-and-troubleshooting.md` |

Each organization runs AgentOS from its **own fork** of this repository.
