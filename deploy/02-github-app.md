# 2 · GitHub App (Git integration)

AgentOS works on your code by cutting branches, committing, opening pull
requests, and merging. It does this through a **GitHub App** you create and
install on the repositories it's allowed to touch. The App gives AgentOS
short-lived, tightly-scoped tokens — no personal access token, no password.

You will come away with **four values**:
- `GITHUB_APP_ID`
- `GITHUB_APP_INSTALLATION_ID`
- a **private key `.pem` file** (secret)
- a **webhook secret** you make up (secret, **recommended** — optional; empty
  disables webhook signature verification)
- plus your `GITHUB_ORG` (the org or username that owns the work repos)

## 2.1 Create the App
1. Go to:
   - Personal account: **Settings → Developer settings → GitHub Apps → New GitHub App**
     (<https://github.com/settings/apps/new>)
   - Or an org: **Org → Settings → Developer settings → GitHub Apps → New GitHub App**
2. Fill in:
   - **GitHub App name**: e.g. `AgentOS <yourname>` (must be globally unique)
   - **Homepage URL**: your future app URL is fine, or any placeholder for now
     (e.g. `https://example.com`)
   - **Webhook**: you can leave **“Active”** unchecked — AgentOS registers a
     webhook on each repo itself (see 2.4). A webhook **secret** is recommended
     (optional) — see 2.4.

## 2.2 Set the permissions (IMPORTANT — set each exactly)
Scroll to **Permissions**. Under **Repository permissions**, set these **seven**
(six you select + Metadata, which is mandatory):

| Permission | Access | Why AgentOS needs it |
|---|---|---|
| **Contents** | **Read and write** | Clone repos, create branches, commit, push, merge |
| **Pull requests** | **Read and write** | Open, update, and merge PRs |
| **Workflows** | **Read and write** | Commit changes that touch `.github/workflows/*` |
| **Administration** | **Read and write** | Register the per-repo webhook AgentOS auto-creates on each repo it works on (see 2.4) |
| **Checks** | **Read-only** | Read check-run results when deciding if a branch is mergeable |
| **Commit statuses** | **Read-only** | Read CI status for the same reason |
| **Metadata** | **Read-only** | Mandatory base permission (auto-selected) |

Leave **Organization permissions** and **Account permissions** all at **No access**.

> These are exactly the permissions the reference deployment uses — no more, no less.

## 2.3 Where can this App be installed?
Under **“Where can this app be installed?”** choose **Only on this account**
(simplest). Click **Create GitHub App**.

## 2.4 Webhooks (recommended)
AgentOS reacts to pushes, PRs, and merges through webhooks. It **registers a
webhook on each repository automatically** when you add that repo to a goal — which
is why **Administration: Read & write** (2.2) is required. You do **not** paste a
webhook URL into the App yourself; AgentOS creates the real per-repo URLs. You just
need two things:

1. **Make a webhook secret** (recommended, optional). Generate a long random string:
   ```bash
   openssl rand -hex 32
   ```
   **Save it** — it becomes `GITHUB_WEBHOOK_SECRET` in `secrets.env` (file 04) and
   the `github-webhook-secret` secret. AgentOS signs and validates every webhook
   delivery with it. If you leave it **empty**, webhook signature verification is
   disabled (the events still work) — set it later with `./set-secret.sh` to turn
   verification on.
2. **Subscribe the App to events.** Under **Permissions & events → Subscribe to
   events**, check **Push** and **Pull request** (required), plus — recommended —
   **Check run**, **Check suite**, and **Status** so CI-driven merge decisions
   update instantly.

(The App-level **General → Webhook → Webhook URL** field can stay blank; the
per-repo hooks AgentOS registers are what deliver the events.)

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
   `https://github.com/settings/installations/12345678` → **`12345678`** is your
   **`GITHUB_APP_INSTALLATION_ID`**.

## 2.8 Note your org / owner
`GITHUB_ORG` is the account that owns the work repositories — your org name
(e.g. `my-company`) or your GitHub username if they're personal repos.

---

### ✅ Write these into your worksheet (file 04)
- [ ] `GITHUB_ORG`
- [ ] `GITHUB_APP_ID` (from 2.6)
- [ ] `GITHUB_APP_INSTALLATION_ID` (from 2.7)
- [ ] The **private key** saved as `~/agentos-platform/github-app-key.pem` (for file 04.3)
- [ ] The **webhook secret** you generated → `GITHUB_WEBHOOK_SECRET` in `secrets.env` (file 04)
- [ ] Subscribed the App to **Push** + **Pull request** events (2.4)

Next: **`04-collect-your-values.md`**. (Slack — file `03` — is optional and set up
in the admin UI *after* deploy; nothing to collect for it now.)
