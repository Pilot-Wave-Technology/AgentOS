#!/usr/bin/env bash
# Tear down the PLATFORM stack (stage ②): Cloud Run, Cloud SQL (instance,
# database, logins, DSN secrets), the image registry, buckets, the VPC
# connector and the GKE cluster.
#
# Most people want ./teardown.sh, which runs this, then the gateway's
# reset.sh, and can also remove what prerequisites.sh created.
#
# Run this BEFORE the gateway reset: the platform's VPC connector lives on
# the gateway's network.
#
# NOTHING TO EDIT FIRST. Cloud SQL deletion protection and non-empty
# buckets used to need `db_deletion_protection = false` and
# `force_destroy_buckets = true` in terraform.tfvars plus an apply. This
# script lifts both itself, on the resources already in state only, so a
# re-run after a partial teardown never recreates anything.
#
# An instance or DSN secret made by an OLDER prerequisites.sh is not in
# state: run terraform/adopt-existing.sh first, or destroy leaves it running.
set -euo pipefail
cd "$(dirname "$0")/terraform"

# Prefer terraform outputs, but fall back to terraform.tfvars / defaults. After a
# partial destroy the outputs come back EMPTY (exit 0 with no value), so a plain
# `|| echo` fallback does NOT fire -- hence the ${VAR:-default} guards.
tfvar() { grep -E "^[[:space:]]*$1[[:space:]]*=" terraform.tfvars 2>/dev/null | head -1 \
            | sed -E 's/.*=[[:space:]]*"?([^"#]+)"?.*/\1/' | tr -d '[:space:]'; }
PREFIX="$(tfvar name_prefix)";                                          PREFIX="${PREFIX:-agentos}"
REGION="$(terraform output -raw region 2>/dev/null || true)";          REGION="${REGION:-$(tfvar region)}";  REGION="${REGION:-us-central1}"
SERVICE="$(terraform output -raw service_name 2>/dev/null || true)";    SERVICE="${SERVICE:-$PREFIX}"
PROJECT="$(tfvar project_id)";                                          PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"

TEARDOWN_VARS=(-var db_deletion_protection=false -var force_destroy_buckets=true)

# An expired login makes gcloud (and terraform's Google provider) wait on a
# re-authentication prompt a script cannot answer. Fail fast instead.
if ! timeout 60 gcloud auth print-access-token >/dev/null 2>&1; then
  echo "[reset] gcloud is not logged in, or the login has expired." >&2
  echo "        Run: gcloud auth login && gcloud auth application-default login" >&2
  exit 2
fi

echo "[reset] deleting the jobs Cloud Build and db-reset.sh create (terraform does not own them)..."
for job in migrate dbreset dbcheck; do
  timeout 300 gcloud run jobs delete "${SERVICE}-${job}" --region "$REGION" --project "$PROJECT" \
    --quiet >/dev/null 2>&1 && echo "  deleted ${SERVICE}-${job}" || true
done

# LIFT THE PROTECTIONS, but only on what still exists. A full apply here
# would also recreate anything an earlier, partial teardown already
# removed -- the opposite of what this script is for.
targets=()
while IFS= read -r addr; do
  case "$addr" in
    *google_sql_database_instance.*|*google_storage_bucket.*) targets+=("-target=$addr") ;;
  esac
done < <(terraform state list 2>/dev/null || true)
if [ "${#targets[@]}" -gt 0 ]; then
  echo "[reset] allowing Cloud SQL and non-empty buckets to be deleted..."
  terraform apply -auto-approve -input=false "${TEARDOWN_VARS[@]}" "${targets[@]}"
fi

echo "[reset] terraform destroy (Cloud Run, Cloud SQL + DSN secrets, registry,"
echo "        buckets, connector, and the GKE cluster -- about 15 minutes)..."
# THE DATABASE AND ITS LOGINS GO WITH THE INSTANCE, so do not ask for them
# one by one. Cloud SQL refuses to drop `control_panel` while anything is
# still connected, and refuses to drop app_user/migrate_user while that
# database holds privileges granted to them -- so a plain destroy stopped
# halfway with the instance still running. Deleting the instance removes
# all three; forgetting them first lets destroy go straight to it.
for r in module.database.google_sql_database.main \
         'module.database.google_sql_user.login["app"]' \
         'module.database.google_sql_user.login["migrate"]'; do
  terraform state rm "$r" >/dev/null 2>&1 || true
done
terraform destroy -auto-approve -input=false "${TEARDOWN_VARS[@]}"

echo "[reset] platform stack destroyed. Next: the gateway's reset.sh (./teardown.sh does both)."
