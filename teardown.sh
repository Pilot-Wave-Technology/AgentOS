#!/usr/bin/env bash
# Tear AgentOS down: the platform, then the gateway, optionally everything
# prerequisites.sh and the builds created as well.
#
#   ./teardown.sh                 platform stack, then gateway stack
#   ./teardown.sh --everything    ...and the secrets, service accounts,
#                                 build jobs and container images too
#   --yes                         skip the typed confirmation (CI only)
#
# Where the gateway checkout is: GATEWAY_DIR (default ../agentos-openclaw,
# the layout the deploy guide uses). Project, region and name prefix come
# from terraform/terraform.tfvars.
#
# WHAT IS LEFT AFTERWARDS, ON PURPOSE (see deploy/12-teardown.md):
#   - the GCP project itself, its enabled APIs and its Firebase setup;
#   - the Cloud Build source bucket <project>_cloudbuild (Google's, shared);
#   - your GitHub App and its webhook, your Slack app;
#   - custom IAM roles, which Google keeps soft-deleted for 7 days (a
#     re-deploy undeletes them automatically).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GATEWAY_DIR="${GATEWAY_DIR:-$HERE/../agentos-openclaw}"

EVERYTHING=0; YES=0
for a in "$@"; do
  case "$a" in
    --everything) EVERYTHING=1 ;;
    --yes)        YES=1 ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a (try --help)" >&2; exit 2 ;;
  esac
done

tfvar() { grep -E "^[[:space:]]*$1[[:space:]]*=" "$HERE/terraform/terraform.tfvars" 2>/dev/null \
            | head -1 | sed -E 's/.*=[[:space:]]*"?([^"#]+)"?.*/\1/' | tr -d '[:space:]'; }
PROJECT="$(tfvar project_id)"; PREFIX="$(tfvar name_prefix)"; PREFIX="${PREFIX:-agentos}"
REGION="$(tfvar region)"; REGION="${REGION:-us-central1}"
[ -n "$PROJECT" ] || { echo "no project_id in terraform/terraform.tfvars" >&2; exit 2; }

# A LOGIN THAT HAS EXPIRED is the usual way a teardown stops halfway: gcloud
# waits on a re-authentication prompt that a script cannot answer. Ask once,
# up front, with a deadline, and say what to do.
if ! timeout 60 gcloud auth print-access-token >/dev/null 2>&1; then
  echo "gcloud is not logged in, or the login has expired." >&2
  echo "Run: gcloud auth login   (and gcloud auth application-default login)" >&2
  echo "then re-run this command; it continues from where it stopped." >&2
  exit 2
fi

# Delete one thing; "already gone" is success, anything else is reported.
_delete() {  # label, command...
  local label="$1"; shift
  local err
  if err=$(timeout 300 "$@" --quiet 2>&1 >/dev/null); then
    echo "  $label deleted"
  elif printf '%s' "$err" | grep -qiE "NOT_FOUND|not found|does not exist|was not found"; then
    :
  else
    echo "  $label NOT deleted: $(printf '%s' "$err" | head -1)" >&2
    FAILED=1
  fi
}
FAILED=0
[ -f "$GATEWAY_DIR/reset.sh" ] || {
  echo "gateway checkout not found at $GATEWAY_DIR (it needs its reset.sh)." >&2
  echo "Set GATEWAY_DIR=<path to your agent_os_openclaw checkout> and re-run." >&2
  exit 2
}

echo "This DELETES AgentOS (prefix '$PREFIX') in project '$PROJECT':"
echo "  - platform: Cloud Run services, Cloud SQL and ALL its data, GKE, buckets"
echo "  - gateway:  the VM, its address, network and archive bucket"
[ "$EVERYTHING" = 1 ] && \
echo "  - and every secret, the three service accounts, build jobs and images"
if [ "$YES" != 1 ]; then
  read -r -p "Type the project id to confirm: " answer
  [ "$answer" = "$PROJECT" ] || { echo "not confirmed; nothing deleted."; exit 1; }
fi

echo; echo "== 1/3 platform stack"; bash "$HERE/reset.sh"
echo; echo "== 2/3 gateway stack";  bash "$GATEWAY_DIR/reset.sh"

if [ "$EVERYTHING" != 1 ]; then
  echo; echo "Done. Secrets and service accounts were kept, so a re-deploy can"
  echo "start at stage ① (gateway terraform). ./teardown.sh --everything removes them."
  exit 0
fi

echo; echo "== 3/3 what prerequisites.sh and the builds created"
# The secret names prerequisites.sh creates (terraform removed its own DSNs).
for s in app-secret-key internal-api-key agent-token-signing-key openclaw-gateway-token \
         credential-encryption-key gemini-api-key anthropic-api-key openai-api-key \
         firebase-service-account github-app-key github-webhook-secret \
         slack-client-secret slack-signing-secret control-panel-url gateway-repo-token; do
  _delete "secret $s" gcloud secrets delete "$s" --project "$PROJECT"
done
for sa in gateway runtime build; do
  _delete "service account ${PREFIX}-${sa}" gcloud iam service-accounts delete \
    "${PREFIX}-${sa}@${PROJECT}.iam.gserviceaccount.com" --project "$PROJECT"
done
# Control-plane and task-runner images the builds pushed to gcr.io.
for image in $(timeout 120 gcloud container images list --repository="gcr.io/$PROJECT" \
                 --format='value(name)' 2>/dev/null | tr -d '\r' | grep -E "/${PREFIX}(-|$)" || true); do
  for digest in $(timeout 120 gcloud container images list-tags "$image" \
                    --format='get(digest)' 2>/dev/null | tr -d '\r'); do
    _delete "image ${image##*/}@${digest:7:12}" gcloud container images delete \
      "${image}@${digest}" --force-delete-tags
  done
done

echo
if [ "$FAILED" = 1 ]; then
  echo "Some deletions failed (listed above). Re-run ./teardown.sh --everything;" >&2
  echo "it skips what is already gone." >&2
  exit 1
fi
echo "Done. Left on purpose: the project, its APIs and Firebase, the Cloud Build"
echo "bucket ${PROJECT}_cloudbuild, your GitHub/Slack apps, and soft-deleted custom roles."
