# Deployment overview

This page orients you before you deploy AgentOS. It is a map, **not** the
walkthrough. When you are ready to build something, follow the ordered,
step-by-step guide in [`../deploy/`](../deploy/) — start with
[`../deploy/README.md`](../deploy/README.md).

---

## Two stages, in order

AgentOS is two branches of one repository, and they deploy in a fixed order:

1. **Stage ① — the gateway (`agent_os_openclaw`).** Deploy this **first**. It
   hosts the agent runtime that actually runs the LLM agents, and it creates the
   shared infrastructure the control plane reuses — the VPC and the archive
   bucket (the gateway + LLM secrets are created earlier by `prerequisites.sh`).
2. **Stage ② — the control plane (`agent_os_platform`, this branch).** Deploy
   this **second**. It manages goals, stories, executions, memory, approvals, and
   platform integrations, and it connects to the gateway over a VPC-internal
   WebSocket.

Neither half is useful alone. If you deploy the control plane first, it has no
gateway to talk to and no shared infrastructure to attach to. The `deploy/`
guide sequences both stages for you and tells you exactly where Stage ① fits in.

---

## Prerequisites at a glance

Before you begin, have these ready. The guide's first file,
[`../deploy/warmup.md`](../deploy/warmup.md), is the authoritative
readiness checklist — this is only the short version:

- **A Google Cloud project** with billing enabled and owner-level access.
- **A GitHub organization and a GitHub App** — agents create and work on goal
  repositories through the App.
- **A Firebase project** for user login (JWT verification and the signup gate).
- **LLM API keys** for both providers: **Anthropic (Claude)** and
  **Google Gemini**. (OpenAI is optional.)

Pick **one region** and use it everywhere — the gateway VM, the Serverless VPC
connector, Cloud Run, and Cloud SQL must all share it.

---

## Control-plane services

The control plane ships as one container image, deployed as five Cloud Run
services: the public web app (`app.main`) plus four background workers — the
councils and the research dispatcher (`app.council_worker`), the git
dependency-graph engine (`app.git_engine_worker`), the settings/admin backends
(`app.management_worker`), and the scheduler (`app.scheduler_worker`), which
runs the background loops behind one leader. The web app runs with
`AGENTOS_SCHEDULER_LOOPS=0`, so it leaves those loops to the scheduler.

Story work does not run on Cloud Run: every executor runs in a pod on the GKE
cluster Terraform creates (the execution plane), which Cloud Build deploys.

---

## One fork per organization

Each organization runs AgentOS from **its own fork** of the repository, so your
configuration, branches, and secrets stay yours. See
[`../FORKING.md`](../FORKING.md) for how to fork and keep both branches in sync.

---

## Next: the real walkthrough

This page does not deploy anything. For the ordered, one-step-at-a-time
procedure — gather prerequisites, run the one-time setup, deploy the gateway
(Stage ①), then this control plane (Stage ②), and verify — go to:

- **[`../deploy/README.md`](../deploy/README.md)** — the authoritative,
  ordered deployment guide. Start here.
- **[`../deploy/warmup.md`](../deploy/warmup.md)** — the readiness
  checklist of every tool, account, and value to have on hand first.

The end-to-end order the `deploy/` guide follows is: **`prerequisites.sh`
(service accounts + non-DB secrets) → Stage ① gateway Terraform → Stage ②
platform Terraform (Cloud SQL, DSN secrets, GKE) → readiness gate → Cloud Build
(build / test / migrate / deploy / execution plane) → `publish-extension.sh` →
first login.**

> This page is a duplicate map for orientation. The **canonical** deployment map
> is the repo's main-branch `DEPLOYMENT.md`; if the two ever differ, that one wins.
