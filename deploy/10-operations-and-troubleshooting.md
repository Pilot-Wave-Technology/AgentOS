# 10 · Operations & troubleshooting

Day-2 reference: how to update, change settings, and fix common problems.

## Updating the app code
Push your change to the `agent_os_platform` branch, then re-run the same pipeline
from file 07 (from the `agent_os_platform` checkout root):
```bash
gcloud builds submit \
  --project "$PROJECT_ID" \
  --config=cloudbuild.yaml \
  --service-account="projects/$PROJECT_ID/serviceAccounts/agentos-build@$PROJECT_ID.iam.gserviceaccount.com" \
  --substitutions=COMMIT_SHA=$(git rev-parse --short HEAD),_REGION=$REGION \
  .
```
`--substitutions=COMMIT_SHA=…` is **required**: a manual `gcloud builds submit`
does not populate `$COMMIT_SHA` (only trigger builds do), and omitting it fails
immediately with `invalid image name "…/agentos:"` (empty tag).

> **Auto-deploy on push?** Only if you did **file 07, step 7.4**, which sets up
> a Cloud Build trigger and is recommended. Without that trigger a `git push`
> does **not** redeploy, and the command above is how you deploy. If you are not
> sure which applies to you, look for a trigger on the `agent_os_platform`
> branch in the Cloud Build console.

## Changing behaviour (flags) or config
1. Edit `terraform/terraform.tfvars` (the `flags { ... }` block or any input).
2. `terraform apply`.
Flags take effect on the next revision. See `../CONFIG.md` for what each does.

## Updating the gateway
Push to `agent_os_openclaw`, then run the gateway's own build — it pulls the
branch on the VM and re-runs the full provisioning script (install, build,
config, restart), which a bare `git pull && pnpm build` would skip. From the
`agentos-openclaw` checkout root:
```bash
gcloud builds submit --config=cloudbuild.yaml \
  --project "$PROJECT_ID" \
  --service-account="projects/$PROJECT_ID/serviceAccounts/agentos-build@$PROJECT_ID.iam.gserviceaccount.com" \
  --substitutions=_ZONE=<zone>,_VM_NAME=agentos-gateway \
  .
```
(Or a Cloud Build trigger on `agent_os_openclaw` running the same file.) The
other supported path is a VM restart: every boot re-fetches the branch, re-reads
the secrets and re-runs provisioning.
```bash
gcloud compute instances reset agentos-gateway --zone <zone>
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
<service> --region <region>` (`agentos-web`/`-council`/`-git`/`-management`/`-scheduler`
as relevant). If you rotate `openclaw-gateway-token` or an LLM key, also restart
the gateway VM (`gcloud compute instances reset agentos-gateway --zone <zone>`) —
it reads its secrets at boot.

## Where to look when something's wrong

| Symptom | Look here |
|---|---|
| App won't load / 500s | Cloud Run logs: Console → Cloud Run → `agentos-web` → **Logs** |
| Login fails | Firebase **Authorized domains** includes your URL? (file 01.5) `FIREBASE_*` correct? |
| Migration failed | Re-run the pipeline (file 07, step 7.1); read the job log for the failing statement |
| GO agent never replies | Gateway address right? Compare the control panel's `OPENCLAW_WS_URL` with the reserved `<name_prefix>-gateway-ip` (`gcloud compute addresses list`) -- a re-apply of the platform stack re-derives it. `openclaw-gateway-token` **same** on both sides? Gateway service running? |
| Gateway not starting | SSH in: `sudo tail -100 /var/log/openclaw-startup.log` |
| Git actions fail | GitHub App permissions (file 02.2), App installed on the repo, `github-app-key` secret set |
| Slack commands do nothing | Request URLs point at your live URL? signing secret correct? command names match the suffix? |
| Templates/skills missing | The seed migration didn't run — re-run file 07, step 7.1 |

## Useful commands
```bash
# tail the web app logs
gcloud run services logs read agentos-web --region <region> --limit 100

# list your secrets
gcloud secrets list

# see current service env/flags (what Terraform set)
gcloud run services describe agentos-web --region <region> --format='value(spec.template.spec.containers[0].env)'

# re-run migrations by hand
gcloud run jobs execute agentos-migrate --region <region> --wait
```

## Costs
Expect roughly **$25+/day idle** (GKE ~3 nodes + cluster fee, Cloud SQL, gateway
VM, 5 warm Cloud Run services) before LLM usage, which comes on top when agents
work. To pause spend on a test deployment: stop the gateway VM and scale the
Cloud Run services' `min-instances` to 0 (note: some background loops won't run
at 0). To tear everything down: `./teardown.sh` — see
[file 12](12-teardown.md).

## Repeatable testing on the same project

Two reset paths, fast vs. full:

### Fast: re-apply migrations cleanly (keeps Cloud SQL up) — ~1 min
Use this between test runs. It checks the instance exists, wipes the schema, and
re-runs migrations (schema + seed) — no ~10-min instance recreate:
```bash
cd agentos-platform && ./db-reset.sh
# restart all five services so the app reconnects
for s in agentos-web agentos-council agentos-git agentos-management agentos-scheduler; do
  gcloud run services update "$s" --region <region> 2>/dev/null
done
```
The gateway keeps running; only the platform DB is reset.

### Full: destroy everything and redeploy from scratch
```bash
cd agentos-platform && ./teardown.sh   # platform, then gateway; asks you to type the project id
```
Nothing to edit first — it lifts Cloud SQL deletion protection and allows
non-empty buckets to go by itself. Secrets and service accounts are **kept**, so
the redeploy starts at **gateway (stage ①) → platform (stage ②)**.
`./teardown.sh --everything` also removes the secrets, service accounts and
images. Details, what is left on purpose, and recovery: [file 12](12-teardown.md).
A database made by an older `prerequisites.sh` is not in Terraform's state: run
`terraform/adopt-existing.sh` first, or the destroy leaves it running.

### Idempotency
`terraform apply` is a no-op on infra that already exists, so re-running it after
a `db-reset` is safe — it won't recreate the instance. The seed migration uses
`ON CONFLICT DO NOTHING`, so re-seeding never duplicates.
