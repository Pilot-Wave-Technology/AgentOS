# 6 · Fill in any remaining secret values

**Most of this is already done.** `prerequisites.sh` (step 0️⃣) created *every*
secret with a version:
- **internal keys** (`app-secret-key`, `internal-api-key`, `agent-token-signing-key`,
  `openclaw-gateway-token`, `credential-encryption-key`) → auto-generated. Nothing to do.
- **external secrets** (LLM keys, Firebase, GitHub, Slack) → the value from your
  `secrets.env`, or a **placeholder** (a single space — Secret Manager rejects a
  truly empty value) if you left it blank.
- **DB DSNs** (`database-url`, `database-url-migrate`) → created by
  `prerequisites.sh`. Nothing to do.

So this step is only: **overwrite any external secret you left blank** with its
real value.

## Always set secrets with `./set-secret.sh`
Use the helper in the repo root — **never** raw `gcloud secrets versions add`. It:
- **refuses an empty value** (an empty secret is rejected by GCP and breaks the deploy), and
- **disables every older version** after adding the new one, so only `:latest` is
  live (no stale key lingering behind `:latest`).

```bash
cd ~/agentos-platform          # the agent_os_platform checkout (holds set-secret.sh)
gcloud config set project YOUR_PROJECT_ID   # so the helper targets the right project
```

> **Already loaded by `prerequisites.sh`:** the multi-line file-based secrets —
> `firebase-service-account` (login) and `github-app-key` (git work) — via their
> `*_FILE` pointers in `secrets.env`. No need to redo them here; the check at the
> bottom confirms they're set.

### LLM keys (required — agents can't run without them)
Skip any you already put in `secrets.env` (prerequisites.sh set those). Fill the rest:
```bash
printf %s 'REAL_GEMINI_KEY'    | ./set-secret.sh gemini-api-key
printf %s 'REAL_ANTHROPIC_KEY' | ./set-secret.sh claude-api-key
```

### GitHub webhook secret (recommended, optional — skip if already in `secrets.env`)
This should already be set from `secrets.env` (`GITHUB_WEBHOOK_SECRET`, file 04). It
signs/validates the per-repo webhooks AgentOS registers (file 02.4); leaving it blank
disables webhook signature verification. To set it:
```bash
printf %s 'YOUR_WEBHOOK_SECRET' | ./set-secret.sh github-webhook-secret
```

### Slack — Legacy only (file 03 is the normal path)
You do **not** normally set these here. The normal path is to configure Slack in
the admin UI after deploy (Settings → Integrations → Slack; see file 03). These
`set-secret.sh` lines are a legacy fallback only, for the deploy-time
`slack_enabled` path:
```bash
printf %s 'REAL_SLACK_CLIENT_SECRET'  | ./set-secret.sh slack-client-secret
printf %s 'REAL_SLACK_SIGNING_SECRET' | ./set-secret.sh slack-signing-secret
```

> Leaving an *unused* external secret as its placeholder is fine — the service
> still deploys. Only the features you use need real values: at minimum **both
> LLM keys** + **Firebase** + the **GitHub App key**.

## Check what's set vs. still a placeholder
```bash
for s in gemini-api-key claude-api-key firebase-service-account github-app-key \
         openclaw-gateway-token app-secret-key credential-encryption-key; do
  v=$(gcloud secrets versions access latest --secret="$s" 2>/dev/null)
  # a placeholder is whitespace-only
  if printf %s "$v" | grep -q '[^[:space:]]'; then echo "$s: SET"; else echo "$s: PLACEHOLDER"; fi
done
```

---

### ✅ Done when
- [ ] Both LLM keys, `firebase-service-account`, and `github-app-key` show **SET**
- [ ] You used `./set-secret.sh` (so old placeholder versions are disabled)

Next: **`07-build-and-deploy.md`**
