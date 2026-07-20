# 3 · Slack app (OPTIONAL — set up after deploy, in the admin UI)

Slack lets your team create/track goals and stories with slash commands and get
notifications. **It's optional, and it needs nothing at deploy time** — no
`secrets.env` entries, no Terraform variables. You configure it entirely in the
AgentOS **admin UI** *after* the platform is live, because it needs your real
control-panel URL (for the Slack Request URLs) and an admin login.

> **When to do this:** come back here **after file 09** (first login), once you
> can log in as an admin. You can also skip Slack entirely and add it any time
> later — nothing else depends on it.
>
> You do **not** set `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` /
> `SLACK_SIGNING_SECRET` anywhere in the deploy. AgentOS reads Slack credentials
> from what you enter in the admin UI. (A legacy env/Terraform path still exists
> behind `slack_enabled = true`, but you don't need it — leave `slack_enabled`
> off and use the UI.)

## 3.1 Open the Slack setup in AgentOS
As an admin: **Settings → Integrations → Slack**. This page:
- Generates a ready-to-paste **app manifest** with the scopes, the six slash
  commands, and your **live** Request URLs already filled in — the fastest way to
  create the app correctly.
- Is where you'll paste the app's credentials and install to your workspace (3.4).

## 3.2 Create the Slack app
1. Go to <https://api.slack.com/apps> → **Create New App**.
2. Choose **From an app manifest**, pick your workspace, and paste the manifest
   from the Integrations page (3.1). That sets the bot scopes, the slash commands,
   and the Interactivity/Events Request URLs for you.

<details><summary>Prefer to configure by hand instead of the manifest?</summary>

**Bot Token Scopes** (OAuth & Permissions → Scopes):

| Scope | Why |
|---|---|
| `commands` | Run the slash commands (`/goal`, `/story`, `/task`, `/resources`, `/memory`, `/help`) |
| `chat:write` | Post replies and notifications back into Slack |
| `chat:write.public` | Post in channels the bot isn't a member of |
| `users:read` | Map a Slack user to their AgentOS account |
| `users:read.email` | Match the Slack user by email to the AgentOS login |
| `im:write` | Send direct messages (private notifications) |
| `app_mentions:read` | (Optional) let people @-mention the bot |

**Slash commands** — Request URL `https://<YOUR_CONTROL_PANEL_URL>/channels/slack/commands`
for each of: `/goal`, `/story`, `/task`, `/resources`, `/memory`, `/help`. Actions
are subcommands parsed from the arguments (`/goal status`, `/story list`) — don't
register them separately.

**Interactivity & Shortcuts** → On → `https://<YOUR_CONTROL_PANEL_URL>/channels/slack/actions`
**Event Subscriptions** → On → `https://<YOUR_CONTROL_PANEL_URL>/channels/slack/events`
</details>

> **Command suffix:** if you want suffixed commands (`/goal-prod` instead of
> `/goal`), name them that way in Slack and set `slack_command_suffix` to match —
> optional, and only relevant if you run more than one AgentOS against one workspace.

## 3.3 Grab the app's credentials
On the Slack app's **Basic Information** page:
- **App Credentials → Client ID**
- **Client Secret** (click *Show*)
- **Signing Secret**

## 3.4 Enter them in AgentOS + install
Back in **Settings → Integrations → Slack**:
1. Paste the **Client ID**, **Client Secret**, and **Signing Secret** → **Save**.
   (AgentOS stores these in its own database — they never go in `secrets.env`.)
2. **Install to Workspace** / **Connect** → **Allow**.

That's it — the slash commands and notifications are live. To connect a specific
goal to a Slack channel, use `/goal` from that channel (or the goal's settings).

---

### ✅ Done when
- [ ] Slack app created (manifest or manual) with your live Request URLs
- [ ] Client ID / Secret / Signing Secret saved in **Settings → Integrations → Slack**
- [ ] Installed to your workspace; `/help` responds in Slack

Next (if you skipped here pre-deploy): **`04-collect-your-values.md`**
