# 9 · First login and verify

Everything is deployed. Now become the admin and run a quick end-to-end check.

## 9.1 Log in as the admin
1. Open your `service_url` in a browser.
2. Sign up / log in with the **exact** email you set as `bootstrap_admin_email`.
   The first login with that email is automatically made a **platform admin**
   (no approval needed).
3. If login fails with a domain error, you missed **file 01 step 1.5** — add your
   Cloud Run hostname to Firebase **Authorized domains**, then retry.

## 9.2 Confirm the built-ins loaded
As admin, check the seed data came in (from the migration in file 07):
- **Settings → Agent registry** (or Templates) — you should see the built-in
  templates: `go`, `default-shca`, the `ra-*` reviewers, `sec-*`, `platform-*`, etc.
- **Settings → Skills** — you should see the active skills (story planning,
  resource management, …).
- **Settings → Tools** — the tool catalog should be populated.

If these are empty, the migration didn't run — re-check file 07 step 7.1 (the
Cloud Build run must have reached the **migrate** step and applied
`0001_baseline_schema` + `0002_baseline_seed`).

## 9.3 Run a smoke test
1. Create a **Goal** (give it any objective).
2. Confirm a **Goal Orchestrator (GO)** agent spins up and responds in the goal
   chat. This proves the control plane ↔ gateway link works.
   - If the GO never responds, the gateway link is wrong — re-check file 08
     (`openclaw_ws_url`, the `openclaw-gateway-token` secret matching on both
     sides, and that the gateway service is running).
3. (If you set up GitHub) Register a repo on the goal and create a small story to
   confirm branch/PR actions work.

## 9.4 Invite your team (optional)
Other people can sign up at your `service_url`; as admin you approve them and
assign roles from **Settings → Admin**.

---

### ✅ You're live when
- [ ] You logged in as the admin
- [ ] Templates / skills / tools are present
- [ ] A new goal's GO agent responds in chat

If all three pass, your AgentOS deployment is working end-to-end. 🎉

For day-2 tasks (updating, changing settings, fixing issues) see
**`10-operations-and-troubleshooting.md`**.
