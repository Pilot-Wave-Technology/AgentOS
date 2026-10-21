# 12 — Tear it down

One command removes a deployment. Nothing to edit first: Cloud SQL deletion
protection and non-empty buckets are handled for you.

## 12.1 Before you start

- Run it from your **platform** checkout (`agentos-platform/`), the folder
  whose `terraform/terraform.tfvars` names the project.
- The gateway checkout is expected beside it at `../agentos-openclaw`. If yours
  is elsewhere, prefix the command with `GATEWAY_DIR=<path>`.
- You need the same access you deployed with (project Owner, or the roles in
  file 00).
- **Everything in Cloud SQL is deleted** — goals, stories, history, memory.
  There is no undo. Export anything you want to keep first.

## 12.2 Remove the deployment, keep the setup (re-deploy later)

```bash
./teardown.sh
```

It asks you to type the project id, then:

1. **Platform** (`reset.sh`, ~15 min): deletes the build's jobs, then Cloud Run
   services, Cloud SQL and its data, the image registry, buckets, the VPC
   connector and the GKE cluster.
2. **Gateway** (`../agentos-openclaw/reset.sh`, ~3 min): the VM, its reserved
   address, the network, firewalls and the archive bucket.

Secrets and the three service accounts are **kept**, so a re-deploy starts at
stage ① (the gateway's `terraform apply`) and skips `prerequisites.sh`.

## 12.3 Remove everything AgentOS created

```bash
./teardown.sh --everything
```

Does 12.2, then also deletes what `prerequisites.sh` and the builds made: every
AgentOS secret (your API keys, Firebase key, GitHub App key, gateway token, …),
the `<prefix>-gateway`, `<prefix>-runtime` and `<prefix>-build` service
accounts, and the control-plane and task-runner images in `gcr.io/<project>`.
A re-deploy after this starts from file 04 (`./prerequisites.sh`); see 12.6.

## 12.4 What is left on purpose

| Left | Why | Remove it yourself if you want |
|---|---|---|
| The GCP project, its enabled APIs | Yours, and other things may use them | Delete the project (removes everything below too) |
| Firebase (Authentication users, web app) | Created by hand in file 01 | Firebase console → Project settings |
| `<project>_cloudbuild` bucket | Google's shared Cloud Build source bucket | `gcloud storage rm -r gs://<project>_cloudbuild` |
| Your GitHub App, its webhook and installation | Lives on GitHub | GitHub → Settings → Developer settings → GitHub Apps |
| Your Slack app (if you made one) | Lives on Slack | api.slack.com/apps |
| Custom IAM roles (`agentos…`) | Google keeps deleted roles for 7 days; a re-deploy undeletes them | Nothing to do — they expire |
| Local files (`secrets.env`, `*.pem`, `firebase-sa.json`, terraform state) | On your machine | Delete them; they hold keys |

The quickest way to be sure nothing is billed: delete the whole project
(`gcloud projects delete <project>`), which removes all of the above that
lives in Google Cloud.

## 12.5 If something stops halfway

Re-run the same command. Each step only acts on what still exists, so a
second run finishes the job instead of recreating anything. Two messages you
may see:

- **`database "control_panel" is being accessed by other users`** — a service
  was still connected. Re-run; the instance is deleted as a whole.
- **`gateway checkout not found`** — set `GATEWAY_DIR` to your
  `agent_os_openclaw` checkout.

If the platform's Terraform state was lost (for example you deleted the
`terraform/` folder), the scripts cannot see what exists: delete the project,
or remove the resources in the console using the list in file 05.

## 12.6 Deploying again into the same project

Even after `--everything`, the slow, by-hand parts survive: the project and
its APIs, Firebase (and the `firebase-sa.json` key you downloaded, because
its service account belongs to Firebase), the authorised domain, and your
GitHub App with its webhook. The web URL is the same too, since it is built
from the project number. So a re-deploy, by you or by a teammate you hand it
to, needs only these files:

| File | Goes in |
|---|---|
| `secrets.env`, `firebase-sa.json`, `github-app-key.pem` | the `agentos-platform/` checkout root |
| `terraform/terraform.tfvars` (platform) | `agentos-platform/terraform/` |
| `terraform/terraform.tfvars` (gateway) | `agentos-openclaw/terraform/` |

Then follow the order table **skipping** 01, 02, 03 and 4.1, 4.2, 4.5:
`warmup.md` and 00 (tools and both logins), 4.3–4.4 (`./prerequisites.sh`),
stage ①, 05, 07, 08, 09. Handing it to someone else? Set
`bootstrap_admin_email` to **their** email, give them Owner on the project,
and send the files through a secure channel: they hold live keys.
After a plain `./teardown.sh` (12.2) the secrets still exist, so skip 4.3 too.

✅ **Done when:** `./teardown.sh` printed `Done.` and
`gcloud run services list --project <project>` shows no `<prefix>-` services.
