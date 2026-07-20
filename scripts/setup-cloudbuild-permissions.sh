#!/usr/bin/env bash
# One-time setup: create the user-managed Cloud Build SA `sa-deploy` and
# grant it the roles it needs to run cloudbuild.yaml / cloudbuild-dev.yaml.
#
# Both pipelines run as `sa-deploy` (specified via `serviceAccount:` at the
# top of each yaml). That SA performs every step: bootstrapping runtime SAs,
# creating secrets + IAM, building the image, deploying to Cloud Run.
#
# Run this once per GCP project, as a project owner. Idempotent — re-running
# is a no-op if the SA and bindings already exist.
#
# Usage:
#   ./scripts/setup-cloudbuild-permissions.sh                    # uses current gcloud project
#   ./scripts/setup-cloudbuild-permissions.sh my-project-id      # explicit project

set -euo pipefail

PROJECT="${1:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "$PROJECT" ]; then
  echo "ERROR: no project. Pass as first arg or run 'gcloud config set project PROJECT_ID'." >&2
  exit 1
fi

if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  echo "ERROR: project '$PROJECT' not found or you lack permission to describe it." >&2
  exit 1
fi

SA_NAME="sa-deploy"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "Project:           $PROJECT"
echo "Cloud Build SA:    $SA_EMAIL  (user-managed)"
echo

# ── Enable required APIs ───────────────────────────────────────────────────
echo "── Enabling required APIs ──────────────────────────────────────────"
APIS=(
  cloudbuild.googleapis.com
  run.googleapis.com
  secretmanager.googleapis.com
  iam.googleapis.com
  cloudresourcemanager.googleapis.com
  sqladmin.googleapis.com
  storage.googleapis.com
  vpcaccess.googleapis.com
  artifactregistry.googleapis.com
  bigquery.googleapis.com
)
gcloud services enable "${APIS[@]}" --project="$PROJECT" --quiet
echo "OK   APIs enabled"
echo

# ── Create sa-deploy if missing ────────────────────────────────────────────
echo "── Cloud Build service account ─────────────────────────────────────"
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
  echo "Creating $SA_EMAIL"
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Cloud Build deploy SA" \
    --project="$PROJECT"
else
  echo "OK   $SA_EMAIL"
fi
echo

# ── Grant sa-deploy the bootstrap roles ────────────────────────────────────
echo "── Granting roles to $SA_EMAIL ─────────────────────────────────────"
# Build-time fundamentals
#   cloudbuild.builds.builder       — run builds (required for user-managed SA)
#   logging.logWriter               — write Cloud Logging entries (required when
#                                      using user-managed SA + CLOUD_LOGGING_ONLY)
#   storage.admin                   — push images to gcr.io / AR + create GCS buckets
# Bootstrap (so cloudbuild can create runtime SAs, secrets, IAM, buckets)
#   run.admin                       — deploy / update Cloud Run services
#   iam.serviceAccountUser           — act as runtime SA on deploy
#   iam.serviceAccountAdmin          — create runtime SAs (control-panel-prod / -dev)
#   resourcemanager.projectIamAdmin  — grant runtime SAs project-level roles
#   secretmanager.admin              — create secrets, manage their IAM
ROLES=(
  roles/cloudbuild.builds.builder
  roles/logging.logWriter
  roles/storage.admin
  roles/run.admin
  roles/iam.serviceAccountUser
  roles/iam.serviceAccountAdmin
  roles/resourcemanager.projectIamAdmin
  roles/secretmanager.admin
)

for ROLE in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="$ROLE" \
    --condition=None \
    --quiet >/dev/null
  printf "OK   %s\n" "$ROLE"
done
echo

echo "── Done ────────────────────────────────────────────────────────────"
echo "Cloud Build pipelines (cloudbuild.yaml, cloudbuild-dev.yaml) can now"
echo "run as $SA_EMAIL in project $PROJECT."
echo
echo "Next steps:"
echo "  1. Create Cloud Build triggers — point them at cloudbuild.yaml (main"
echo "     branch → control-panel-prod) and cloudbuild-dev.yaml (dev branch"
echo "     → control-panel-dev). Set the trigger's service account to:"
echo "         $SA_EMAIL"
echo "  2. Push to the trigger branch. The bootstrap step in each yaml will"
echo "     create the env-specific runtime SA (control-panel-prod /"
echo "     control-panel-dev), env-scoped secrets (placeholders), and the"
echo "     env GCS resource bucket."
echo "  3. Populate the placeholder secrets — see README 'Populate placeholder"
echo "     secrets' for each secret's source."
echo "  4. Re-run the trigger; the new revision picks up real values."
