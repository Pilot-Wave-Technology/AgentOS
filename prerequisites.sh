#!/usr/bin/env bash
###############################################################################
# AgentOS — ONE-TIME (repeatable) prerequisites. Run this FIRST, before any
# terraform, from the platform folder.
#
# Creates the identity + secret layer so the terraform stacks are pure infra:
#   • enables required APIs
#   • creates both service accounts (gateway + runtime) + their IAM roles
#   • grants the Cloud Build SA the roles needed for the auto-deploy trigger
#   • creates EVERY secret with a version:
#       - internal keys      -> your value, else AUTO-GENERATED
#       - external secrets   -> your value, else a BLANK version (overwrite later)
#     (a blank version — never "no version" — so Cloud Run always deploys)
#   • creates the Cloud SQL instance + control_panel db + app_user/migrate_user
#     + the DB DSN secrets. The slow instance-create (several minutes) lives HERE
#     behind a RUNNABLE poll — NOT in terraform — so a `terraform apply` is fast
#     and can't bail on a mid-create API poll.
#
# Idempotent: re-running does NOT rotate existing secrets/passwords or duplicate
# roles, so it's safe to run again when retesting on the same project.
#
# Usage:
#   cp secrets.env.example secrets.env      # optional: fill in what you have
#   ./prerequisites.sh <PROJECT_ID> [REGION]
#   # sizing knobs (optional): DB_TIER=... DB_DISK_GB=... ./prerequisites.sh ...
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

echo "== [1/6] enabling APIs =="
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  vpcaccess.googleapis.com compute.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com iam.googleapis.com \
  bigquery.googleapis.com monitoring.googleapis.com --project "$PROJECT"

GW_EMAIL="${NAME_PREFIX}-gateway@${PROJECT}.iam.gserviceaccount.com"
RT_EMAIL="${NAME_PREFIX}-runtime@${PROJECT}.iam.gserviceaccount.com"

echo "== [2/6] service accounts =="
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

grant_project() {
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:$1" --role "$2" --condition=None >/dev/null
}

echo "== [3/6] IAM roles =="
for r in roles/logging.logWriter roles/monitoring.metricWriter; do grant_project "$GW_EMAIL" "$r"; done
for r in roles/cloudsql.client roles/storage.objectAdmin roles/logging.logWriter \
         roles/bigquery.dataEditor roles/bigquery.jobUser roles/run.invoker; do grant_project "$RT_EMAIL" "$r"; done

# Cloud Build SA — for the optional auto-deploy trigger (build + deploy as runtime SA).
PNUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
CB_EMAIL="${PNUM}@cloudbuild.gserviceaccount.com"
for r in roles/run.admin roles/artifactregistry.writer roles/logging.logWriter roles/cloudsql.client \
         roles/iap.tunnelResourceAccessor roles/compute.osAdminLogin roles/compute.viewer; do grant_project "$CB_EMAIL" "$r"; done
gcloud iam service-accounts add-iam-policy-binding "$RT_EMAIL" \
  --member "serviceAccount:$CB_EMAIL" --role roles/iam.serviceAccountUser --project "$PROJECT" >/dev/null

echo "== [4/6] secrets (real | generated | blank) =="
_has_version() { gcloud secrets versions list "$1" --project "$PROJECT" --limit 1 --format='value(name)' 2>/dev/null | grep -q .; }
ensure_secret() {  # name value  — creates the secret + ONE version if none exists
  local name="$1" raw="${2-}" val
  # Secret Manager rejects an EMPTY payload, so unprovided secrets get an inert
  # single-space placeholder. Consumers treat whitespace-only as "unset", and
  # you overwrite it later with the real value.
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
fernet() { python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"; }
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
ensure_secret claude-api-key            "${CLAUDE_API_KEY:-}"
ensure_secret openai-api-key            "${OPENAI_API_KEY:-}"
ensure_secret firebase-service-account  "$(secret_val FIREBASE_SERVICE_ACCOUNT_JSON)"
ensure_secret github-app-key            "$(secret_val GITHUB_APP_KEY)"
ensure_secret github-webhook-secret     "${GITHUB_WEBHOOK_SECRET:-}"
ensure_secret slack-client-secret       "${SLACK_CLIENT_SECRET:-}"
ensure_secret slack-signing-secret      "${SLACK_SIGNING_SECRET:-}"
ensure_secret control-panel-url         "${CONTROL_PANEL_URL:-}"   # filled after platform deploy
ensure_secret gateway-repo-token        "${GATEWAY_REPO_TOKEN:-}"  # PAT to clone a PRIVATE repo; blank = public

echo "== [5/6] Cloud SQL instance + database + users + DSN secrets =="
DB_INSTANCE="${NAME_PREFIX}-db"
DB_TIER="${DB_TIER:-db-custom-2-8192}"
DB_VERSION="${DB_VERSION:-POSTGRES_17}"
DB_DISK_GB="${DB_DISK_GB:-250}"
DB_NAME="${DB_NAME:-control_panel}"
DB_CONN="${PROJECT}:${REGION}:${DB_INSTANCE}"

# Instance — idempotent. Async create + explicit RUNNABLE poll: the slow part
# (several minutes) lives here in a plain loop, NOT in terraform, so an
# interrupted API poll can never fail a deploy.
if ! gcloud sql instances describe "$DB_INSTANCE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "  creating $DB_INSTANCE ($DB_TIER / $DB_VERSION / ${DB_DISK_GB}GB) — takes several minutes…"
  gcloud sql instances create "$DB_INSTANCE" \
    --project "$PROJECT" --region "$REGION" \
    --edition=ENTERPRISE --tier="$DB_TIER" --database-version="$DB_VERSION" \
    --storage-size="$DB_DISK_GB" --storage-auto-increase \
    --availability-type=ZONAL --backup --enable-point-in-time-recovery \
    --quiet >/dev/null 2>&1 || true
fi
printf "  waiting for %s to reach RUNNABLE" "$DB_INSTANCE"
until [ "$(gcloud sql instances describe "$DB_INSTANCE" --project "$PROJECT" --format='value(state)' 2>/dev/null)" = "RUNNABLE" ]; do
  printf "."; sleep 15
done
echo " ok"

# control_panel database — idempotent.
gcloud sql databases describe "$DB_NAME" --instance "$DB_INSTANCE" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud sql databases create "$DB_NAME" --instance "$DB_INSTANCE" --project "$PROJECT" >/dev/null

# app_user (DML) + migrate_user (DDL) + their DSN secrets. A password is generated
# ONLY when the DSN secret is absent, so the stored DSN and the live user password
# never drift on a re-run.
db_user_secret() {  # role  secret_name
  local role="$1" secret="$2" pw dsn
  gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud secrets create "$secret" --replication-policy=automatic --project "$PROJECT" >/dev/null
  if _has_version "$secret"; then echo "  $secret: keep existing"; return 0; fi
  pw="$(openssl rand -hex 24)"   # hex → URL-safe inside the DSN
  if gcloud sql users list --instance "$DB_INSTANCE" --project "$PROJECT" --format='value(name)' 2>/dev/null | grep -qx "$role"; then
    gcloud sql users set-password "$role" --instance "$DB_INSTANCE" --password="$pw" --project "$PROJECT" >/dev/null
  else
    gcloud sql users create "$role" --instance "$DB_INSTANCE" --password="$pw" --project "$PROJECT" >/dev/null
  fi
  dsn="postgresql://${role}:${pw}@/${DB_NAME}?host=/cloudsql/${DB_CONN}"
  printf %s "$dsn" | gcloud secrets versions add "$secret" --data-file=- --project "$PROJECT" >/dev/null
  echo "  $secret: created (user $role)"
}
db_user_secret app_user     database-url
db_user_secret migrate_user database-url-migrate

echo "== [6/6] secret access =="
sa_secret() { gcloud secrets add-iam-policy-binding "$1" --member "serviceAccount:$2" \
  --role roles/secretmanager.secretAccessor --project "$PROJECT" >/dev/null; }
for s in openclaw-gateway-token gemini-api-key claude-api-key openai-api-key control-panel-url gateway-repo-token; do
  sa_secret "$s" "$GW_EMAIL"; done
for s in firebase-service-account app-secret-key internal-api-key agent-token-signing-key \
         credential-encryption-key github-app-key github-webhook-secret slack-client-secret \
         slack-signing-secret openclaw-gateway-token gemini-api-key claude-api-key openai-api-key \
         database-url database-url-migrate; do
  sa_secret "$s" "$RT_EMAIL"; done

echo
echo "DONE (repeatable). Service accounts:"
echo "  gateway: $GW_EMAIL"
echo "  runtime: $RT_EMAIL"
echo "Cloud SQL: $DB_CONN (db=$DB_NAME, users app_user + migrate_user)"
echo "Secrets created with versions (blank where you didn't provide a value)."
echo "Next: deploy STAGE ① gateway, then STAGE ② platform (same PROJECT + REGION=$REGION)."
