# 10 · Operations & troubleshooting

Day-2 reference: how to update, change settings, and fix common problems.

## Updating the app code
Push your change to the `agent_os_platform` branch, then re-run the same pipeline
from file 07 (from the `agent_os_platform` checkout root):
```bash
gcloud builds submit \
  --project "$PROJECT_ID" \
  --config=cloudbuild.yaml \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD),_REGION=$REGION \
  .
```
`--substitutions=COMMIT_SHA=…` is **required**: a manual `gcloud builds submit`
does not populate `$COMMIT_SHA` (only trigger builds do), and omitting it fails
immediately with `invalid image name "…/agentos:"` (empty tag). Use
`cloudbuild-single.yaml` if you deployed the single profile.

> **Auto-deploy on push?** Not set up by this guide — there is no Cloud Build
> trigger by default, so a `git push` alone does **not** redeploy. If you want
> that, create a trigger on the `agent_os_platform` branch pointing at
> `cloudbuild.yaml` in the Cloud Build console.

## Changing behaviour (flags) or config
1. Edit `terraform/terraform.tfvars` (the `flags { ... }` block or any input).
2. `terraform apply`.
Flags take effect on the next revision. See `../CONFIG.md` for what each does.

## Updating the gateway
Push to `agent_os_openclaw`, then either `terraform apply` in the gateway folder,
or SSH in and refresh:
```bash
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone <zone> --command \
  "cd /opt/agentos-gateway && sudo git pull && sudo pnpm install && sudo pnpm build && sudo systemctl restart 'openclaw-gateway@*'"
```

## Rotating a secret
Always use `./set-secret.sh` (from the `agentos-platform` root) — it refuses an
empty value and **disables the previous version**, so a rotated key can't linger
behind `:latest`:
```bash
printf %s 'NEW_VALUE' | ./set-secret.sh <secret-name>
# or from a file:  ./set-secret.sh <secret-name> path/to/file
```
Then redeploy the affected service(s) so they pick up `:latest` — a `git push`
re-runs Cloud Build, or restart directly with `gcloud run services update
<service> --region <region>` (split: `agentos-web`/`-council`/`-git`/`-management`
as relevant; single: `agentos`). If you rotate `openclaw-gateway-token`, redeploy
**both** the platform and the gateway.

## Where to look when something's wrong

| Symptom | Look here |
|---|---|
| App won't load / 500s | Cloud Run logs: Console → Cloud Run → `agentos-web` (split default; `agentos` on single) → **Logs** |
| Login fails | Firebase **Authorized domains** includes your URL? (file 01.5) `FIREBASE_*` correct? |
| Migration failed | Re-run file 07.2; read the job log for the failing statement |
| GO agent never replies | `openclaw_ws_url` correct? `openclaw-gateway-token` **same** on both sides? Gateway service running? |
| Gateway not starting | SSH in: `sudo tail -100 /var/log/openclaw-startup.log` |
| Git actions fail | GitHub App permissions (file 02.2), App installed on the repo, `github-app-key` secret set |
| Slack commands do nothing | Request URLs point at your live URL? signing secret correct? command names match the suffix? |
| Templates/skills missing | The seed migration didn't run — file 07.2 |

## Useful commands
```bash
# tail the web app logs
gcloud run services logs read agentos-web --region <region> --limit 100   # split default; `agentos` on single

# list your secrets
gcloud secrets list

# see current service env/flags (what Terraform set)  — `agentos` on single profile
gcloud run services describe agentos-web --region <region> --format='value(spec.template.spec.containers[0].env)'

# re-run migrations by hand
gcloud run jobs execute agentos-migrate --region <region> --wait
```

## Costs
The main cost drivers are Cloud SQL (always on), the gateway VM (always on), and
LLM API usage (when agents work). To pause spend on a test deployment: stop the
gateway VM and scale the Cloud Run service `min-instances` to 0 (note: some
background loops won't run at 0). To tear everything down, use `./reset.sh` in
each folder (platform first) — see below. Note the Cloud SQL instance is created
by `prerequisites.sh`, not terraform, so `reset.sh` deletes it explicitly (a bare
`terraform destroy` would leave it running as orphaned cost).

## Repeatable testing on the same project

Two reset paths, fast vs. full:

### Fast: re-apply migrations cleanly (keeps Cloud SQL up) — ~1 min
Use this between test runs. It checks the instance exists, wipes the schema, and
re-runs migrations (schema + seed) — no ~10-min instance recreate:
```bash
cd agentos-platform && ./db-reset.sh
# restart so the app reconnects — split (default): all four; single: just `agentos`
for s in agentos-web agentos-council agentos-git agentos-management; do
  gcloud run services update "$s" --region <region> 2>/dev/null
done
```
The gateway keeps running; only the platform DB is reset.

### Full: destroy everything and redeploy from scratch
Set `force_destroy_buckets = true` in both tfvars first (so non-empty buckets can
be deleted). The Cloud SQL instance was created by `prerequisites.sh`, not
Terraform — `reset.sh` deletes it directly (it has no deletion protection).
**Order matters — platform before gateway** (the connector lives on the gateway's
VPC):
```bash
cd agentos-platform && ./reset.sh      # stage ② down (Cloud Run, Cloud SQL, secrets, connector)
cd ../agentos-openclaw && ./reset.sh   # stage ① down (VM, VPC, archive bucket, gateway secrets)
```
Then redeploy in the normal order: **gateway (stage ①) → platform (stage ②)**.

### Idempotency
`terraform apply` is a no-op on infra that already exists, so re-running it after
a `db-reset` is safe — it won't recreate the instance. The seed migration uses
`ON CONFLICT DO NOTHING`, so re-seeding never duplicates.
