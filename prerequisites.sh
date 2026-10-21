#!/usr/bin/env bash
###############################################################################
# AgentOS — ONE-TIME (repeatable) prerequisites. Run this FIRST, before any
# terraform, from the platform folder.
#
# Creates the identity + secret layer so the terraform stacks are pure infra:
#   • enables required APIs
#   • creates the three service accounts (gateway, runtime, build) + their IAM roles
#   • the build account is what BOTH builds run as (gcloud builds submit --service-account)
#   • creates EVERY secret with a version:
#       - internal keys      -> your value, else AUTO-GENERATED
#       - external secrets   -> your value, else a BLANK version (overwrite later)
#     (a blank version — never "no version" — so Cloud Run always deploys)
#   • STOPS (exit 2) if a REQUIRED secret is still blank in Secret Manager:
#       gemini-api-key, anthropic-api-key, firebase-service-account, github-app-key
#   • does NOT create Cloud SQL, the DSN secrets or the image registry: those
#     are terraform's (one apply sets up the database and the cluster together)
#
# Idempotent: re-running does NOT rotate existing secrets/passwords or duplicate
# roles, so it's safe to run again when retesting on the same project.
#
# Usage:
#   cp secrets.env.example secrets.env      # optional: fill in what you have
#   ./prerequisites.sh <PROJECT_ID> [REGION]
#   # database sizing is terraform.tfvars (db_tier, db_disk_gb), not here
###############################################################################
set -euo pipefail

PROJECT="${1:-}"
REGION="${2:-us-central1}"
NAME_PREFIX="${NAME_PREFIX:-agentos}"
if [ -z "$PROJECT" ]; then
  echo "usage: ./prerequisites.sh <PROJECT_ID> [REGION]" >&2; exit 1
fi

# Load optional local secret values (KEY=value lines). Never commit secrets.env.
if [ -f secrets.env ]; then set -a; . ./secrets.env; set +a; fi

gcloud config set project "$PROJECT" >/dev/null
echo "== project=$PROJECT  region=$REGION  name_prefix=$NAME_PREFIX =="

echo "== [1/5] enabling APIs =="
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  vpcaccess.googleapis.com compute.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com iam.googleapis.com \
  bigquery.googleapis.com monitoring.googleapis.com   iap.googleapis.com cloudresourcemanager.googleapis.com   container.googleapis.com --project "$PROJECT"
# iap / cloudresourcemanager / container were MISSING and are not optional:
# file 07's readiness checks SSH over IAP, the execution plane needs GKE, and
# cloudresourcemanager backs the IAM calls below. Without them the guide fails
# at a step that does not say why.

GW_EMAIL="${NAME_PREFIX}-gateway@${PROJECT}.iam.gserviceaccount.com"
RT_EMAIL="${NAME_PREFIX}-runtime@${PROJECT}.iam.gserviceaccount.com"
BD_EMAIL="${NAME_PREFIX}-build@${PROJECT}.iam.gserviceaccount.com"

echo "== [2/5] service accounts =="
# Idempotent + resilient to re-provisioning a project after teardown. A plain
# `create || echo exists` masks the case where the SA was recently DELETED: GCP
# briefly holds the name (deleted SAs are recoverable for 30 days via `undelete`),
# so the create can transiently fail — and silently swallowing it leaves the SA
# missing, which then breaks the IAM grants below with confusing errors. So:
# describe → create → retry the transient window → clear undelete guidance.
ensure_sa() {  # name  display
  local name="$1" display="$2" email="${1}@${PROJECT}.iam.gserviceaccount.com" attempt out
  if gcloud iam service-accounts describe "$email" --project "$PROJECT" >/dev/null 2>&1; then
    echo "  $name SA exists"; return 0
  fi
  for attempt in 1 2 3 4; do
    if out=$(gcloud iam service-accounts create "$name" --display-name "$display" \
               --project "$PROJECT" 2>&1); then
      echo "  $name SA created"; return 0
    fi
    echo "  $name SA not ready (attempt $attempt/4): $(printf %s "$out" | tail -1)"
    sleep 20
  done
  echo "  ⚠ Could not create $name SA (${email})."
  echo "    If it was recently deleted, restore it and re-run this script:"
  echo "      gcloud iam service-accounts undelete <UNIQUE_ID> --project $PROJECT"
  echo "      (find the id: Console → IAM & Admin → Service Accounts → Deleted)"
  return 1
}
ensure_sa "${NAME_PREFIX}-gateway" "AgentOS gateway VM"
ensure_sa "${NAME_PREFIX}-runtime" "AgentOS control-plane runtime"
ensure_sa "${NAME_PREFIX}-build" "AgentOS builds (platform + gateway)"

grant_project() {
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:$1" --role "$2" --condition=None >/dev/null
}

echo "== [3/5] IAM roles =="
for r in roles/logging.logWriter roles/monitoring.metricWriter; do grant_project "$GW_EMAIL" "$r"; done
for r in roles/cloudsql.client roles/storage.objectAdmin roles/logging.logWriter \
         roles/bigquery.dataEditor roles/bigquery.jobUser roles/run.invoker; do grant_project "$RT_EMAIL" "$r"; done

# ── THE BUILD'S OWN ACCOUNT ──────────────────────────────────────────
#
# Both builds -- this platform's and the gateway's -- run as
# ${NAME_PREFIX}-build (`gcloud builds submit --service-account`, and the
# same setting on a trigger). NOT the project's default Cloud Build
# account: on a project created after 2024 that is the Compute default
# account, shared with anything else that runs without one, so every
# right granted to the build would leak to those too. And terraform can
# name this one -- it is how each secret the build reads gets granted
# ONE BY ONE in terraform/build_access.tf, instead of project-wide.
#
# What each project role is for:
#   run.admin                       deploy the services + the migrate job
#   artifactregistry.createOnPushWriter  push images (and create the gcr.io
#                                   repository on a fresh project's first push)
#   logging.logWriter               the build log (cloudbuild.yaml logs to
#                                   Cloud Logging only, which a custom
#                                   build account requires)
#   cloudsql.client                 read the instance's address for the
#                                   execution plane's egress rules
#   container.admin                 cluster credentials, the conductor's
#                                   Roles/RoleBindings, and preflight's
#                                   database check inside the conductor
#   iam.securityReviewer            preflight READS each Workload Identity
#                                   binding; it changes none
#   iap.tunnelResourceAccessor,
#   compute.osAdminLogin,
#   compute.viewer                  the gateway build's SSH over IAP
for r in roles/run.admin roles/artifactregistry.createOnPushWriter roles/logging.logWriter \
         roles/cloudsql.client roles/container.admin roles/iam.securityReviewer \
         roles/iap.tunnelResourceAccessor roles/compute.osAdminLogin roles/compute.viewer; do
  grant_project "$BD_EMAIL" "$r"; done
# Deploy Cloud Run as the runtime account; SSH (OS Login) to a VM that
# runs as the gateway account. Each needs actAs on that account only.
for sa in "$RT_EMAIL" "$GW_EMAIL"; do
  gcloud iam service-accounts add-iam-policy-binding "$sa" \
    --member "serviceAccount:$BD_EMAIL" --role roles/iam.serviceAccountUser \
    --project "$PROJECT" >/dev/null
done
# `gcloud builds submit` uploads the source here and the build reads it
# back AS THIS ACCOUNT. Created now so the grant has something to bind to.
SRC_BUCKET="gs://${PROJECT}_cloudbuild"
gcloud storage buckets describe "$SRC_BUCKET" >/dev/null 2>&1 \
  || gcloud storage buckets create "$SRC_BUCKET" --project "$PROJECT" \
       --location "$REGION" --uniform-bucket-level-access >/dev/null
gcloud storage buckets add-iam-policy-binding "$SRC_BUCKET" \
  --member "serviceAccount:$BD_EMAIL" --role roles/storage.objectViewer >/dev/null

echo "== [4/5] secrets (real | generated | blank) =="
_has_version() { gcloud secrets versions list "$1" --project "$PROJECT" --limit 1 --format='value(name)' 2>/dev/null | grep -q .; }
ensure_secret() {  # name value  — creates the secret + ONE version if none exists
  local name="$1" raw="${2-}" val
  # Secret Manager rejects an EMPTY payload, so unprovided secrets get an
  # inert single-space placeholder -- there has to be SOME version or Cloud
  # Run refuses to start a service that references the secret.
  #
  # The app normalises a whitespace-only setting to the empty string
  # (app/config.py::_whitespace_is_unset), so a placeholder reads as NOT
  # CONFIGURED and the feature behind it stays cleanly off instead of
  # failing later against a key that is one space. Overwrite the
  # placeholder with the real value when you want the feature.
  val="$raw"; [ -z "$val" ] && val=" "
  gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create "$name" --replication-policy=automatic --project "$PROJECT" >/dev/null
  if _has_version "$name"; then
    echo "  $name: keep existing version"
  else
    printf %s "$val" | gcloud secrets versions add "$name" --data-file=- --project "$PROJECT" >/dev/null
    echo "  $name: $( [ -n "$raw" ] && echo 'set' || echo 'placeholder (overwrite later)')"
  fi
}
gen()    { openssl rand -hex 32; }
# A Fernet key is 32 random bytes in URL-safe base64 -- openssl, which this
# script already needs for gen(), produces exactly that. It used to shell out
# to `python -c "from cryptography.fernet import Fernet"`, which needs BOTH a
# `python` on PATH (macOS and most current Linux have only `python3`) and the
# `cryptography` package on the operator's laptop. The failure was swallowed:
# the value came back empty, became a placeholder, and surfaced months later
# as credentials that would not decrypt.
fernet() { openssl rand -base64 32 | tr '+/' '-_'; }
# Resolve a secret from an inline <VAR> OR, for multi-line secrets (the Firebase
# JSON key, the GitHub App .pem), a <VAR>_FILE path — drop the file next to
# secrets.env and set <VAR>_FILE=that-file instead of pasting multi-line content
# into secrets.env or running a separate set-secret step. Inline wins if both set;
# path is relative to where you run this script (the platform folder).
secret_val() {  # VAR
  local var="$1" fvar="${1}_FILE"
  if   [ -n "${!var:-}"  ]; then printf %s "${!var}"
  elif [ -n "${!fvar:-}" ] && [ -f "${!fvar}" ]; then cat "${!fvar}"
  elif [ -n "${!fvar:-}" ]; then echo "  [warn] ${fvar}='${!fvar}' not found — leaving blank" >&2
  fi
}

# Internal keys — your value, else auto-generated (stable across re-runs).
ensure_secret app-secret-key            "${APP_SECRET_KEY:-$(gen)}"
ensure_secret internal-api-key          "${INTERNAL_API_KEY:-$(gen)}"
ensure_secret agent-token-signing-key   "${AGENT_TOKEN_SIGNING_KEY:-$(gen)}"
ensure_secret openclaw-gateway-token    "${OPENCLAW_GATEWAY_TOKEN:-$(gen)}"
ensure_secret credential-encryption-key "${CREDENTIAL_ENCRYPTION_KEY:-$(fernet)}"
# External secrets — your value, else a BLANK version (overwrite when you have it).
ensure_secret gemini-api-key            "${GEMINI_API_KEY:-}"
ensure_secret anthropic-api-key            "${ANTHROPIC_API_KEY:-}"
ensure_secret openai-api-key            "${OPENAI_API_KEY:-}"
ensure_secret firebase-service-account  "$(secret_val FIREBASE_SERVICE_ACCOUNT_JSON)"
ensure_secret github-app-key            "$(secret_val GITHUB_APP_KEY)"
# GENERATED, not blank. A blank webhook secret does not disable signature
# verification -- it verifies every delivery against the empty string, so
# either every push is accepted or none is, depending on which side is
# empty. Generating one costs nothing, and you paste the SAME value into
# the GitHub App's webhook settings (file 02); if you already have one,
# put it in secrets.env and this keeps it.
ensure_secret github-webhook-secret     "${GITHUB_WEBHOOK_SECRET:-$(gen)}"
ensure_secret slack-client-secret       "${SLACK_CLIENT_SECRET:-}"
ensure_secret slack-signing-secret      "${SLACK_SIGNING_SECRET:-}"
ensure_secret control-panel-url         "${CONTROL_PANEL_URL:-}"   # filled after platform deploy
ensure_secret gateway-repo-token        "${GATEWAY_REPO_TOKEN:-}"  # PAT to clone a PRIVATE repo; blank = public

# The image registry, the Cloud SQL instance, the control_panel database,
# app_user/migrate_user and the three DSN secrets (database-url,
# database-url-migrate, database-url-conductor) used to be made here. They are
# terraform's now (terraform/main.tf + modules/database), together with the
# execution plane's login and key. An install made by an older version of this
# script is adopted with terraform/adopt-existing.sh.

echo "== [5/5] secret access =="
sa_secret() { gcloud secrets add-iam-policy-binding "$1" --member "serviceAccount:$2" \
  --role roles/secretmanager.secretAccessor --project "$PROJECT" >/dev/null; }
for s in openclaw-gateway-token gemini-api-key anthropic-api-key openai-api-key control-panel-url gateway-repo-token; do
  sa_secret "$s" "$GW_EMAIL"; done
for s in firebase-service-account app-secret-key internal-api-key agent-token-signing-key \
         credential-encryption-key github-app-key github-webhook-secret slack-client-secret \
         slack-signing-secret openclaw-gateway-token gemini-api-key anthropic-api-key openai-api-key; do
  sa_secret "$s" "$RT_EMAIL"; done

# ── HARD STOP: required secrets must hold a real value ───────────────
# A blank placeholder lets every later step "succeed" and then fails at
# login (Firebase), on the first agent turn (LLM keys) or on the first git
# action (App key). The check reads Secret Manager's LATEST version, not
# secrets.env, so a value loaded earlier with set-secret.sh passes even when
# its secrets.env line is now blank.
echo "== checking required secrets =="
_sm_nonblank() {  # secret -> 0 if latest version has a non-whitespace char
  local n
  n=$(gcloud secrets versions access latest --secret="$1" --project "$PROJECT" 2>/dev/null \
        | tr -d '[:space:]' | wc -c | tr -d '[:space:]') || n=0
  [ "${n:-0}" -gt 0 ]
}
_fix_cmd() {  # secret -> the exact command that loads it
  case "$1" in
    firebase-service-account) echo "GCP_PROJECT=$PROJECT ./set-secret.sh firebase-service-account <path/to/firebase-sa.json>" ;;
    github-app-key)           echo "GCP_PROJECT=$PROJECT ./set-secret.sh github-app-key <path/to/github-app-key.pem>" ;;
    *)                        echo "printf %s '<value>' | GCP_PROJECT=$PROJECT ./set-secret.sh $1" ;;
  esac
}
MISSING_REQUIRED=()
for s in gemini-api-key anthropic-api-key firebase-service-account github-app-key; do
  if _sm_nonblank "$s"; then echo "  $s: ok"; else MISSING_REQUIRED+=("$s"); fi
done
if [ "${#MISSING_REQUIRED[@]}" -gt 0 ]; then
  echo
  echo "REQUIRED BUT EMPTY:" >&2
  for s in "${MISSING_REQUIRED[@]}"; do
    echo "  $s" >&2
    echo "      fix: $(_fix_cmd "$s")" >&2
  done
  echo "Re-running prerequisites.sh never replaces an existing secret version," >&2
  echo "so filling secrets.env now does nothing: load each one with set-secret.sh" >&2
  echo "as shown, then re-run this script to confirm." >&2
  exit 2
fi
if ! _sm_nonblank gateway-repo-token; then
  echo "  gateway-repo-token: blank — fine for a PUBLIC fork; a PRIVATE fork needs:"
  echo "      $(_fix_cmd gateway-repo-token)"
fi

echo
echo "DONE (repeatable). Service accounts:"
echo "  gateway: $GW_EMAIL"
echo "  runtime: $RT_EMAIL"
echo "  build:   $BD_EMAIL   (pass --service-account to gcloud builds submit)"
echo "Not made here, and made by terraform instead: Cloud SQL (instance,"
echo "  database, logins, DSN secrets), the image registry, and the execution"
echo "  plane's login and key."
echo "Secrets created with versions (blank where you didn't provide a value)."
echo "Re-running this script never replaces an existing secret version; to"
echo "  change one, use: printf %s '<value>' | ./set-secret.sh <secret-name>"
echo "Next: deploy STAGE ① gateway, then STAGE ② platform (same PROJECT + REGION=$REGION)."
echo
# THE LAST LINE IS THE VERDICT. Success used to be inferred from the
# absence of REQUIRED BUT EMPTY, scrolled off above a screen of output.
echo "PREREQUISITES OK -- gemini-api-key, anthropic-api-key, firebase-service-account"
echo "and github-app-key all hold real values."
