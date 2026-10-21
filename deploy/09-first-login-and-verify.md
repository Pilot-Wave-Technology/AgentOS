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
  templates: `go-v2` (the Goal Orchestrator), `kernel-assistant` (the assistant),
  the `ra-*` reviewers, `sec-*`, `platform-*`, etc.
- **Settings → Skills** — you should see the active skills (story planning,
  resource management, …).
- **Settings → Tools** — the tool catalog should be populated.
- **Settings → Model Registry** — six models: Claude Haiku 4.5, Opus 5.5 and
  Sonnet 5.5, Claude CLI Opus and Sonnet, Gemini 3.8 Flash.
- **New goal → model picker** — offers **Claude Opus 5.5, Claude Sonnet 5.5
  and Gemini 3.8 Flash** (100K / 200K / 500K each). A 1M window is registered
  for each of the three too, but the goal picker caps at 500K — 1M is offered
  only to agents that allow it, so it is not missing. If it says "No models
  available", or offers something else, the registry did not load.
- **VS Code page** — a download, if you did file 07 step 7.5.

If these are empty, the migrations didn't run — re-check file 07 step 7.1 (the
Cloud Build run must have reached the **migrate** step and applied every
migration listed in `migrations/SEQUENCE` (0001–0009)).

## 9.3 Run a smoke test
1. Create a **Goal** (give it any objective).
2. Confirm a **Goal Orchestrator (GO)** agent spins up and responds in the goal
   chat. This proves the control plane ↔ gateway link works.
   - If the GO never responds, the gateway link is wrong — re-check file 08
     (the gateway address the platform derived from `<name_prefix>-gateway-ip`, the `openclaw-gateway-token` secret matching on both
     sides, and that the gateway service is running).
That is as far as "is it installed?" goes. Doing actual work — registering a
repository, getting a story proposed, accepting it, approving a plan and
merging the pull request that comes back — is
**[`11-your-first-story.md`](11-your-first-story.md)**, and it is worth doing
once before you invite anybody else.

## 9.4 Invite your team (optional)
Other people can sign up at your `service_url`; as admin you approve them and
assign roles from **Settings → Admin**.

---

### ✅ You're live when
- [ ] You logged in as the admin
- [ ] Templates / skills / tools are present
- [ ] A new goal's GO agent responds in chat

If all three pass, your AgentOS deployment is working end-to-end. 🎉

Next: **`10-operations-and-troubleshooting.md`** for day-2 tasks, then
**`11-your-first-story.md`** to actually use it.
