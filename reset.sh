#!/usr/bin/env bash
# Tear down the PLATFORM stack (STAGE 2) for repeatable testing.
#
# Run this BEFORE the gateway reset (the platform's VPC connector lives on the
# gateway's VPC). Full re-test cycle on the same project:
#   1. cd agentos-platform && ./reset.sh          # this script
#   2. cd agentos-openclaw && ./reset.sh          # then the gateway
#   3. redeploy: gateway (stage 1) → platform (stage 2)
#
# Set in terraform.tfvars for a clean destroy:
#   force_destroy_buckets = true   # so non-empty buckets can be deleted
set -euo pipefail
cd "$(dirname "$0")/terraform"

# Prefer terraform outputs, but fall back to terraform.tfvars / defaults. After a
# partial destroy the outputs come back EMPTY (exit 0 with no value), so a plain
# `|| echo` fallback does NOT fire — that would leave INSTANCE="" and silently
# skip the Cloud SQL delete, orphaning the instance as ongoing cost.
tfvar() { grep -E "^[[:space:]]*$1[[:space:]]*=" terraform.tfvars 2>/dev/null | head -1 \
            | sed -E 's/.*=[[:space:]]*"?([^"#]+)"?.*/\1/' | tr -d '[:space:]'; }
PREFIX="$(tfvar name_prefix)";                                          PREFIX="${PREFIX:-agentos}"
REGION="$(terraform output -raw region 2>/dev/null || true)";          REGION="${REGION:-$(tfvar region)}";  REGION="${REGION:-us-central1}"
SERVICE="$(terraform output -raw service_name 2>/dev/null || true)";    SERVICE="${SERVICE:-$PREFIX}"
INSTANCE="$(terraform output -raw cloudsql_instance 2>/dev/null || true)"; INSTANCE="${INSTANCE:-${PREFIX}-db}"
PROJECT="$(tfvar project_id)";                                          PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"

echo "[reset] deleting the migrate job (created by Cloud Build, not terraform)..."
gcloud run jobs delete "${SERVICE}-migrate" --region "$REGION" --quiet 2>/dev/null || true

echo "[reset] terraform destroy (Cloud Run, buckets, secrets, connector)..."
terraform destroy -auto-approve

# Cloud SQL was created by prerequisites.sh — terraform only reads it via a data
# source — so terraform destroy leaves it running. Delete it explicitly, or it
# lingers as orphaned cost. Disable deletion protection first (prerequisites.sh
# enables it), and guard against an empty name.
if [ -n "$INSTANCE" ]; then
  echo "[reset] deleting Cloud SQL instance '$INSTANCE' (created by prerequisites.sh)..."
  gcloud sql instances patch  "$INSTANCE" --no-deletion-protection --quiet 2>/dev/null || true
  gcloud sql instances delete "$INSTANCE" --quiet 2>/dev/null || true
else
  echo "[reset] WARNING: could not resolve the Cloud SQL instance name — delete it manually."
fi

# The app_user/migrate_user DSN secrets (database-url*) are bound to the users on
# THIS Cloud SQL instance. Deleting the instance orphans them: a later
# prerequisites.sh re-run sees the surviving secret, says "keep existing", and
# never (re)sets the recreated user's password — so the stored DSN points at a
# password the fresh instance's user doesn't have, and the migrate job dies with
# "password authentication failed". Drop them here so re-provision regenerates the
# users + passwords + DSNs in sync. (terraform destroy already removed the Cloud
# Run services that referenced them, so nothing live depends on them anymore.)
if [ -n "${PROJECT:-}" ]; then
  for s in database-url database-url-migrate; do
    gcloud secrets delete "$s" --project "$PROJECT" --quiet 2>/dev/null \
      && echo "[reset] deleted DSN secret $s (re-provision regenerates it in sync)" || true
  done
else
  echo "[reset] WARNING: project unresolved — delete database-url + database-url-migrate secrets manually."
fi

echo "[reset] platform stack destroyed. Now reset the gateway: cd ../agentos-openclaw && ./reset.sh"
