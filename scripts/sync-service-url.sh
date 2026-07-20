#!/usr/bin/env bash
# Capture the Cloud Run service URL for a given env and store it in the
# control-panel-url-{env} secret. Called from cloudbuild.yaml /
# cloudbuild-dev.yaml as a post-deploy step.
#
# Idempotent + version-thrifty: only adds a new secret version if the URL
# differs from the latest stored value (URLs are stable for the lifetime of
# a Cloud Run service, so on every build after the first this is a no-op).
#
# Usage (from cloudbuild step):
#   ./scripts/sync-service-url.sh prod
#
# Required env vars (passed from cloudbuild substitution → step env):
#   PROJECT   GCP project ID
#   REGION    Cloud Run region (e.g. ${GCP_REGION})
#   SERVICE   Cloud Run service name (e.g. control-panel-prod)

set -euo pipefail

ENV_NAME="${1:?env name (prod|dev|feature) required as first arg}"
case "$ENV_NAME" in
  prod|dev|feature) ;;
  *) echo "ERROR: env must be 'prod', 'dev', or 'feature', got '$ENV_NAME'" >&2; exit 1 ;;
esac

: "${PROJECT:?PROJECT env var required}"
: "${REGION:?REGION env var required}"
: "${SERVICE:?SERVICE env var required}"

SECRET_NAME="control-panel-url-${ENV_NAME}"

echo "── Capturing service URL ───────────────────────────────────────────"
# Cloud Run exposes two URL formats per service:
#   • modern (deterministic): https://<service>-<project-number>.<region>.run.app
#   • legacy (hash-based):    https://<service>-<hash>-<region-code>.a.run.app
# Both route to the same service, but the modern form is what the console
# shows and is the one we want stored. `status.url` historically returns the
# legacy form, so we construct the modern form ourselves from values we
# already have. Project number is stable for the lifetime of the project.

# First verify the service actually exists (sanity check before constructing
# a URL that may not point at a real service yet).
gcloud run services describe "$SERVICE" \
  --region="$REGION" \
  --project="$PROJECT" \
  --format='value(metadata.name)' >/dev/null

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
if [ -z "$PROJECT_NUMBER" ]; then
  echo "ERROR: could not resolve project number for $PROJECT" >&2
  exit 1
fi

URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
echo "Service URL: $URL"

echo
echo "── Comparing with current secret value ─────────────────────────────"
# The secret resource is created by bootstrap-cloudrun-env.sh — it always
# exists by this step. The latest version may be the placeholder (first
# deploy) or a previous URL (re-deploy).
CURRENT=$(gcloud secrets versions access latest \
  --secret="$SECRET_NAME" \
  --project="$PROJECT" 2>/dev/null || echo "")

if [ "$URL" = "$CURRENT" ]; then
  echo "URL unchanged — secret $SECRET_NAME already has this value at :latest"
  echo "No new version added."
  exit 0
fi

echo "URL changed (was: ${CURRENT:0:40}…)  →  adding new version"
printf '%s' "$URL" | gcloud secrets versions add "$SECRET_NAME" \
  --data-file=- --project="$PROJECT" >/dev/null

echo
echo "── Verify ──────────────────────────────────────────────────────────"
NEW=$(gcloud secrets versions access latest --secret="$SECRET_NAME" --project="$PROJECT")
if [ "$NEW" = "$URL" ]; then
  echo "OK   $SECRET_NAME:latest now holds the deployed service URL"
else
  echo "ERROR: verification failed — secret latest is '$NEW', expected '$URL'" >&2
  exit 1
fi
