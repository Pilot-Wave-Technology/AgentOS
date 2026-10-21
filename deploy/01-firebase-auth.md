# 1 · Firebase (login / authentication)

AgentOS uses **Firebase Authentication** for login. Firebase attaches to the
**same** GCP project you already created — do not make a new one.

## What you produce here (and where it goes later)
You'll come away with **three values**. Nothing here goes into `secrets.env`;
here's exactly where each one lands and when:

| Value | Where it goes | When (which step) |
|---|---|---|
| **`FIREBASE_PROJECT_ID`** (= your project, e.g. `my-agentos-472913`) | platform **`terraform.tfvars`** → `firebase_project_id` | file **05** |
| **`FIREBASE_WEB_API_KEY`** (a public `apiKey`) | platform **`terraform.tfvars`** → `firebase_web_api_key` | file **05** |
| **service-account JSON file** | **Secret Manager** secret `firebase-service-account` (loaded automatically by `prerequisites.sh` from the `*_FILE` pointer) | file **04** |

> Keep the project id + web key in your notes for file 05; keep the JSON file on
> disk. (It's multi-line, so it's **not** pasted into `secrets.env` — instead you
> set `FIREBASE_SERVICE_ACCOUNT_JSON_FILE=<that file>` in `secrets.env` and
> `prerequisites.sh` loads it into Secret Manager for you.)

---

## 1.1 Attach Firebase to your project
1. Go to <https://console.firebase.google.com> (the **Firebase** console — not
   `console.cloud.google.com`).
2. **Add project** → **"Add Firebase to an existing Google Cloud project"** →
   pick your project → finish (skip Analytics).

## 1.2 Turn on Email/Password login

> ⚠️ The left-menu path **Build → Authentication** is frequently **not visible**
> (the **Build** group is collapsed by default and may not appear right after you
> add Firebase). **Don't hunt for it — open the Authentication page directly by
> URL.** This is the reliable way:

**Open this URL** (replace `YOUR_PROJECT_ID`, e.g. `my-agentos-472913`):
```
https://console.firebase.google.com/project/YOUR_PROJECT_ID/authentication/providers
```
Then:
1. Click **Get started** (first visit only).
2. Open the **Sign-in method** tab.
3. Click **Email/Password** → toggle **Enable** → **Save**.

(Only if you prefer the menu: click the **Build** group in the left sidebar to
expand it, then **Authentication**. Optional: enable **Google** sign-in too —
Email/Password alone is enough.)

## 1.3 Get the Web API key
1. Gear icon → **Project settings** → scroll to **Your apps**.
2. Click the **`</>` (Web)** icon → nickname `agentos-web` → **Register app**
   (leave Firebase Hosting unchecked).
3. Copy the **`apiKey`** from the shown `firebaseConfig`. That is your
   **`FIREBASE_WEB_API_KEY`** (it's a public client key — fine to keep in tfvars).

> **Hit `CreateWebApp … error code 6` or an XHR error?** Code 6 = *already exists*
> (a retry created it). Just refresh **Your apps** and copy the `apiKey` from the
> app that's now listed. If the console keeps failing (often an ad-blocker), get
> the key with gcloud instead:
> ```bash
> gcloud services api-keys list --project YOUR_PROJECT_ID --format="table(displayName,name)"
> gcloud services api-keys get-key-string <KEY_ID> --project YOUR_PROJECT_ID
> ```
> (`<KEY_ID>` = the last segment of the `name` column.) The `AIza…` string is the key.

## 1.4 Download the service-account JSON
Project settings → **Service accounts** tab → **Generate new private key** →
**Generate key**. A `.json` downloads.

> **Save it with this exact name and location:**
> ```
> ~/agentos-platform/firebase-sa.json
> ```
> (i.e. the **root of your `agent_os_platform` checkout**, named
> **`firebase-sa.json`**.) That exact name is already in `.gitignore`, so it can
> never be committed. File **04, step 4.3** loads it from there. Don't rename it.

## 1.5 Authorized domains — do it now
Login only works from domains Firebase trusts. Your app's hostname is
deterministic (`<prefix>-web-<project-number>.<region>.run.app`), so you can add
it before it exists. Print it (`<prefix>` is `agentos` unless you change
`name_prefix`):
```bash
echo "<prefix>-web-$(gcloud projects describe <PROJECT_ID> --format='value(projectNumber)').<REGION>.run.app"
```
- **Authentication → Settings → Authorized domains → Add domain** → paste that
  hostname (no `https://`).

File 07 reminds you to check this — skipping it is the #1 cause of login failing.

---

### ✅ Before continuing
- [ ] Firebase attached to your project
- [ ] Email/Password enabled
- [ ] `FIREBASE_WEB_API_KEY` saved (for file 05)
- [ ] service-account JSON saved as `~/agentos-platform/firebase-sa.json` (for file 04.3)

Next: **`02-github-app.md`** (the GitHub App is required — see the prerequisites).
