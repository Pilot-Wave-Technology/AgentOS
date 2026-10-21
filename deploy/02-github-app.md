# 2 · GitHub App (Git integration)

AgentOS works on your code by cutting branches, committing, opening pull
requests, and merging. It does this through a **GitHub App** you create and
install on the repositories it's allowed to touch. The App gives AgentOS
short-lived, tightly-scoped tokens — no personal access token, no password.

You will come away with **five values**:
- `GITHUB_APP_ID`
- `GITHUB_APP_INSTALLATION_ID`
- a **private key `.pem` file** (secret)
- a **webhook secret** you make up (secret — **required if you want webhook
  events at all**; see 2.4)
- plus your `GITHUB_ORG` (the org or username that owns the work repos)

## 2.1 Create the App
Create the App under the **organization that owns the work repositories** (the
`github_org` you set in the tfvars), so it can be installed there. If you create
it on a personal account instead, you must choose **Any account** in 2.3 — an
"Only on this account" App can never be installed on the org.

1. Go to:
   - Personal account: **Settings → Developer settings → GitHub Apps → New GitHub App**
     (<https://github.com/settings/apps/new>)
   - Or an org: **Org → Settings → Developer settings → GitHub Apps → New GitHub App**
2. Fill in:
   - **GitHub App name**: e.g. `AgentOS <yourname>` (must be globally unique)
   - **Homepage URL**: your future app URL is fine, or any placeholder for now
     (e.g. `https://example.com`)
   - **Webhook**: leave **“Active”** unchecked for now. The URL is your
     deployed service, which does not exist yet; you come back for it in 2.4.

## 2.2 Set the permissions (IMPORTANT — set each exactly)
Scroll to **Permissions**. Under **Repository permissions**, set these **six**
(five you select + Metadata, which is mandatory):

| Permission | Access | Why AgentOS needs it |
|---|---|---|
| **Contents** | **Read and write** | Clone repos, create branches, commit, push, merge |
| **Pull requests** | **Read and write** | Open, update, and merge PRs |
| **Workflows** | **Read and write** | Only if agents will edit `.github/workflows/*`. Set **No access** otherwise |
| **Checks** | **Read-only** | Read check-run results when deciding if a branch is mergeable |
| **Commit statuses** | **Read-only** | Read CI status, for the same reason |
| **Metadata** | **Read-only** | Mandatory base permission (auto-selected) |

Leave **Organization permissions** and **Account permissions** all at **No access**.

> **Do NOT grant Administration.** An earlier version of this guide asked for
> **Administration: Read and write**, on the grounds that AgentOS registers a
> webhook on each repository itself. It does not: `register_webhook` exists in
> `app/services/git_providers/github.py` and **nothing calls it**. Administration
> is the permission that allows deleting a repository, changing its settings and
> managing its collaborators — a large grant for a code path that does not run.
> (Creating a repository webhook would not need it in any case; GitHub has a
> separate **Webhooks** permission for that.)
>
> AgentOS does **not create repositories** either, which is why no Organization
> permission is needed. You register an existing repo on a goal yourself.

## 2.3 Where can this App be installed?
Under **“Where can this app be installed?”**:
- App created **under the org that owns the work repos** → **Only on this account**.
- App created on a **personal account** but the work repos belong to an org →
  **Any account** (otherwise the org's install page will not offer it).

Click **Create GitHub App**.

## 2.4 Webhooks (optional, and you finish this after deploy)

Webhooks make AgentOS react to a push or a merge **immediately**. Without them
it still works: the git engine runs on `LISTEN/NOTIFY` with a polling backstop,
so a merge is noticed on the next poll instead of instantly.

**You cannot finish this step now** — the URL is your deployed service, which
does not exist until file 07. Do the secret now, come back for the URL.

1. **Generate a webhook secret now.**
   ```bash
   openssl rand -hex 32
   ```
   Put it in `secrets.env` as `GITHUB_WEBHOOK_SECRET` (file 04). If you leave
   it blank, `prerequisites.sh` **generates one for you** — read it back with
   `gcloud secrets versions access latest --secret=github-webhook-secret`.

   > **A blank secret does not mean "no verification".** The receiver logs
   > *"no webhook secret configured — rejecting by default"* and returns 403 to
   > every delivery. There is a secret either way; the only question is whether
   > you chose it. Whatever value ends up in `github-webhook-secret` is the
   > value GitHub must be configured with.

2. **After file 07, come back and activate the webhook.** App settings →
   **General → Webhook**: tick **Active**, set
   **Payload URL** to `https://<your-web-service-url>/webhooks/github/<label>`,
   **Content type** `application/json`, and paste the same secret.

   Then **subscribe the App to events** (GitHub only offers the event list once
   the webhook is active). Under **Permissions & events → Subscribe to
   events**: **Push**, **Pull request** and **Pull request review**, plus —
   recommended — **Check run**, **Check suite** and **Status**, so CI-driven
   merge decisions update instantly.

   `<label>` is any word you like (your `name_prefix` is a good choice). It
   is recorded with each delivery and nothing else: the receiver finds the
   repository from the payload, so one App-level webhook covers every
   repository the App is installed on. The receiver is
   `POST /webhooks/github/{label}` in `app/routers/git_graph.py`; it stores
   the delivery and wakes the git engine.

   > **Not `/webhooks/git`.** That route feeds an older integration flow and
   > never wakes the git engine, so merges would still wait for the poll.

   The web service URL is
   `https://<name_prefix>-web-<project-number>.<region>.run.app`.

## 2.5 Generate the private key (secret)
1. App settings → **General** → scroll to **Private keys** → **Generate a private key**.
2. A `.pem` file downloads.

> **Save it with this exact name and location:**
> ```
> ~/agentos-platform/github-app-key.pem
> ```
> (i.e. the **root of your `agent_os_platform` checkout**, named
> **`github-app-key.pem`**.) That name matches `*.pem` in `.gitignore`, so it can
> never be committed. It's loaded **automatically by `prerequisites.sh`** from the
> `GITHUB_APP_KEY_FILE` pointer you set in `secrets.env` (file **04.1**) — there's
> no separate `set-secret.sh` step for it.

## 2.6 Note the App ID
On the App's **General** page, near the top, copy the **App ID** (a number).
That's your **`GITHUB_APP_ID`**.

## 2.7 Install the App on your repositories
1. App settings → **Install App** (left menu) → click **Install** next to your
   account/org.
2. Choose **All repositories** or **Only select repositories** (pick the repos
   AgentOS will work on). Click **Install**.
3. After installing, look at the browser URL. It ends with a number:
   - on an org: `https://github.com/organizations/<org>/settings/installations/12345678`
   - on a personal account: `https://github.com/settings/installations/12345678`

   → **`12345678`** is your **`GITHUB_APP_INSTALLATION_ID`**. Install it on the
   **org that owns the work repos** (`github_org`), not on your personal account.

## 2.8 Note your org / owner
`GITHUB_ORG` is the account that owns the work repositories — your org name
(e.g. `my-company`) or your GitHub username if they're personal repos.

---

### ✅ Write these into your worksheet (file 04)
- [ ] `GITHUB_ORG`
- [ ] `GITHUB_APP_ID` (from 2.6)
- [ ] `GITHUB_APP_INSTALLATION_ID` (from 2.7)
- [ ] The **private key** saved as `~/agentos-platform/github-app-key.pem` (for file 04.3)
- [ ] The **webhook secret** → `GITHUB_WEBHOOK_SECRET` in `secrets.env` (file 04), or left blank to have one generated
- [ ] **Administration left at No access** (2.2)
- [ ] Noted that the webhook **URL** and the **event subscriptions** are set
      after file 07, not now (2.4)

Next: **`04-collect-your-values.md`**. (Slack — file `03` — is optional and set up
in the admin UI *after* deploy; nothing to collect for it now.)
