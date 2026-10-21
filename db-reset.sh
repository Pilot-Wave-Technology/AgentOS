#!/usr/bin/env bash
# Fast DB reset for repeatable testing (STAGE ② / platform).
#
# If the Cloud SQL INSTANCE already exists, this wipes the schema and re-applies
# migrations cleanly (schema + seed) WITHOUT recreating the instance — ~1 min
# instead of the ~10 min a full destroy+recreate costs. If the instance is
# missing, it tells you to run the full deploy first.
#
# DESTRUCTIVE to all DB data. Test/dev projects only — never production.
#
# Prereq: you have run the full deploy at least once (Cloud SQL + the runtime SA
# + the image + secrets all exist).
set -euo pipefail
cd "$(dirname "$0")/terraform"

PROJECT=$(terraform output -raw project_id)
REGION=$(terraform output -raw region)
INSTANCE=$(terraform output -raw cloudsql_instance)
SERVICE=$(terraform output -raw service_name)
CONNECTOR="$(terraform output -raw network_name)-conn"
SA="${SERVICE}-runtime@${PROJECT}.iam.gserviceaccount.com"
IMAGE="gcr.io/${PROJECT}/${SERVICE}:latest"

# 1. Does the instance exist? If not, a full deploy is needed.
if ! gcloud sql instances describe "$INSTANCE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "[db-reset] Cloud SQL instance '$INSTANCE' not found."
  echo "           Run the full deploy first (terraform apply + build + migrate)."
  exit 1
fi
echo "[db-reset] instance '$INSTANCE' exists → wiping schema + re-applying migrations cleanly..."

# 2. Run reset_db.py as a one-off job (same wiring as the migrate job).
gcloud run jobs deploy "${SERVICE}-dbreset" \
  --image="$IMAGE" \
  --region="$REGION" \
  --vpc-connector="$CONNECTOR" --vpc-egress=private-ranges-only \
  --service-account="$SA" \
  --set-cloudsql-instances="${PROJECT}:${REGION}:${INSTANCE}" \
  --set-secrets=DATABASE_URL=database-url-migrate:latest,AGENTOS_LEG_PASSWORD=execution-db-password:latest \
  --set-env-vars=APP_ENV=production \
  --command=python --args=migrations/reset_db.py \
  --max-retries=0 --task-timeout=600 --quiet

gcloud run jobs execute "${SERVICE}-dbreset" --region="$REGION" --wait

echo "[db-reset] done — fresh schema + seed applied."
echo "[db-reset] restart the service(s) so they reconnect cleanly:"
echo "           for s in ${SERVICE}-web ${SERVICE}-council ${SERVICE}-git ${SERVICE}-management ${SERVICE}-scheduler; do gcloud run services update \"\$s\" --region ${REGION}; done"
