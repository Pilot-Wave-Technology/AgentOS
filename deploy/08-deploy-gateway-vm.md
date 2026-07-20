# 8 · Verify the gateway link

The gateway VM itself was deployed back in **Stage ①** (the `agent_os_openclaw`
branch), and you already pointed it at the platform and confirmed it was ready in
**file 07, step 7.0** (before the build). This file is a final confirmation that
the two are actually talking now that the real app is live.

## 8.1 Re-confirm the gateway has the platform URL
You set this in **7.0** before building. Confirm it's still in place (the refresh
timer keeps it in sync from the `control-panel-url` secret):
```bash
export PROJECT_ID=your-project-id
export REGION=us-central1
gcloud config set project "$PROJECT_ID"
gcloud secrets versions access latest --secret=control-panel-url --project "$PROJECT_ID"
# → should print your agentos-web URL. If it's empty/placeholder, re-run 7.0.a.
```
(`set-secret.sh` refuses an empty URL and disables the old placeholder version.)
Within ~60 seconds of any change the gateway picks it up (its timer re-reads the
secret and restarts the gateway process — no VM reboot needed).

## 8.2 Confirm the gateway token matches
The platform authenticates to the gateway with `openclaw-gateway-token` — the
**same** secret you set in Stage ①. Both sides read the same Secret Manager
entry, so they match by construction. (If you ever rotate it, redeploy both.)

## 8.3 Check the gateway is up
```bash
gcloud compute ssh agentos-gateway --tunnel-through-iap --zone <your-zone> \
  --command "sudo systemctl status 'openclaw-gateway@18789' --no-pager | head -5"
```
Want `active (running)`. Boot/first-build logs if needed:
`sudo tail -50 /var/log/openclaw-startup.log`.

---

### ✅ Done when
- [ ] `control-panel-url` secret set to your `service_url`
- [ ] Gateway service is `active (running)`

Next: **`09-first-login-and-verify.md`**
