#!/usr/bin/env bash
###############################################################################
# ./adopt-existing.sh — take ownership of things that exist but are not in
# terraform's state, so the next `terraform apply` manages them instead of
# failing with "already exists".
#
# Run it from this folder, after `terraform init`, in two situations:
#
#   1. AN APPLY WAS INTERRUPTED while the Cloud SQL instance was building.
#      The instance keeps building without terraform. Wait for it to reach
#      RUNNABLE, run this, apply again.
#   2. THE DATABASE WAS MADE BY AN OLDER prerequisites.sh, which created the
#      instance, the database, app_user/migrate_user, the DSN secrets and the
#      image registry with gcloud. Terraform owns all of them now.
#
# Each import runs only if the thing exists in GCP and is absent from state,
# so this is safe to re-run.
#
# AFTER ADOPTING, THE NEXT APPLY ROTATES THE DATABASE PASSWORDS. Terraform
# cannot read a password back out of Cloud SQL, so it sets its own and writes
# a new version of each DSN secret. Running services keep the old DSN until
# they restart: push a build (or redeploy) straight after the apply.
###############################################################################
set -uo pipefail
cd "$(dirname "$0")"

tfvar() {  # name default -- read a plain `name = "value"` line from tfvars
  local v
  v="$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\{0,1\}\([^\"#]*\)\"\{0,1\}.*/\1/p" \
        terraform.tfvars 2>/dev/null | head -1 | tr -d '[:space:]\r')"
  printf %s "${v:-$2}"
}

PROJECT="${PROJECT:-$(tfvar project_id "")}"
REGION="${REGION:-$(tfvar region us-central1)}"
PREFIX="${NAME_PREFIX:-$(tfvar name_prefix agentos)}"
INSTANCE="${PREFIX}-db"
DB="control_panel"

[ -n "$PROJECT" ] || { echo "ERROR: no project_id in terraform.tfvars (or set PROJECT=)." >&2; exit 1; }
[ -d .terraform ] || { echo "ERROR: run \`terraform init\` first." >&2; exit 1; }

STATE="$(terraform state list 2>/dev/null | tr -d '\r')"
adopted=0

# adopt ADDRESS IMPORT_ID EXISTS_CMD...
adopt() {
  local addr="$1" id="$2"; shift 2
  if printf '%s\n' "$STATE" | grep -Fxq "$addr"; then
    echo "  in state   $addr"; return 0
  fi
  if ! "$@" >/dev/null 2>&1; then
    echo "  absent     $addr (terraform will create it)"; return 0
  fi
  echo "  ADOPTING   $addr"
  terraform import -input=false "$addr" "$id" >/dev/null \
    || { echo "  FAILED to import $addr -- see: terraform import '$addr' '$id'" >&2; exit 1; }
  adopted=$((adopted + 1))
}

echo "== adopting into state: project=$PROJECT region=$REGION prefix=$PREFIX =="

state="$(gcloud sql instances describe "$INSTANCE" --project "$PROJECT" \
           --format='value(state)' 2>/dev/null | tr -d '\r')"
if [ -n "$state" ] && [ "$state" != "RUNNABLE" ]; then
  echo "ERROR: $INSTANCE is $state, not RUNNABLE. Wait for it, then run this again." >&2
  exit 1
fi

D=module.database
adopt "$D.google_sql_database_instance.main" "projects/$PROJECT/instances/$INSTANCE" \
  gcloud sql instances describe "$INSTANCE" --project "$PROJECT"
adopt "$D.google_sql_database.main" "projects/$PROJECT/instances/$INSTANCE/databases/$DB" \
  gcloud sql databases describe "$DB" --instance "$INSTANCE" --project "$PROJECT"

has_user() {  # name
  gcloud sql users list --instance "$INSTANCE" --project "$PROJECT" \
    --format='value(name)' 2>/dev/null | tr -d '\r' | grep -qx "$1"
}
adopt "$D.google_sql_user.login[\"app\"]"     "$PROJECT/$INSTANCE/app_user"     has_user app_user
adopt "$D.google_sql_user.login[\"migrate\"]" "$PROJECT/$INSTANCE/migrate_user" has_user migrate_user

for s in database-url database-url-migrate database-url-conductor; do
  adopt "$D.google_secret_manager_secret.dsn[\"$s\"]" "projects/$PROJECT/secrets/$s" \
    gcloud secrets describe "$s" --project "$PROJECT"
done

adopt google_artifact_registry_repository.images \
  "projects/$PROJECT/locations/$REGION/repositories/$PREFIX" \
  gcloud artifacts repositories describe "$PREFIX" --location "$REGION" --project "$PROJECT"

# The execution plane's own logins and identities.
E='module.execution_plane'
for s in execution-db-password:leg_password execution-sqlproxy-key:sqlproxy_key; do
  adopt "$E.google_secret_manager_secret.${s#*:}" "projects/$PROJECT/secrets/${s%%:*}" \
    gcloud secrets describe "${s%%:*}" --project "$PROJECT"
done
for a in exec cond verify capture; do
  adopt "$E.google_service_account.$a" \
    "projects/$PROJECT/serviceAccounts/agentos-$a@$PROJECT.iam.gserviceaccount.com" \
    gcloud iam service-accounts describe "agentos-$a@$PROJECT.iam.gserviceaccount.com" --project "$PROJECT"
done

echo
if [ "$adopted" -gt 0 ]; then
  echo "Adopted $adopted. Next: terraform plan (expect password rotation on the"
  echo "adopted logins and new DSN versions), terraform apply, then push a build"
  echo "so the services pick up the new DSNs."
else
  echo "Nothing to adopt."
fi
