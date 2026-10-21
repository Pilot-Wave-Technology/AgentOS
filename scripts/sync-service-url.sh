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
#   REGION    Cloud Run region (e.g. us-central1)
#   SERVICE   Cloud Run service name (e.g. control-panel-prod)

set -euo pipefail

ENV_NAME="${1:?env name (prod|dev|feature|next) required as first arg}"
case "$ENV_NAME" in
  prod|dev|feature|next) ;;
  *) echo "ERROR: env must be 'prod', 'dev', 'feature' or 'next', got '$ENV_NAME'" >&2; exit 1 ;;
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

# -- AND THEN ACTUALLY CONTACT IT ------------------------------------
#
# Constructing the modern form is a choice, not a shortcut -- see the
# note above. What was missing is any check that the address we invent
# is one that answers. The existence check above asks whether the
# SERVICE is there; it says nothing about whether this HOSTNAME routes
# to it, and those are different questions the moment we stop reading
# `status.url`.
#
# A wrong URL here is silent and lands far away: the gateway polls this
# secret, every kernel tool builds its endpoint from it, and a fetch to
# a host that does not resolve surfaces to the agent as
# E_KERNEL_UNREACHABLE -- which reads as "the service is down" rather
# than "we stored an address nobody can reach".
#
# ANY HTTP RESPONSE COUNTS, including 401, 403 and 404. We are testing
# DNS and routing, not authorisation -- a service deployed with
# --no-allow-unauthenticated answers 403 and that is a pass. curl exits
# non-zero only when it could not get a response at all.
if curl -sS -o /dev/null -m 20 "$URL" 2>/dev/null; then
  echo "Service URL: $URL (verified reachable)"
else
  REPORTED=$(gcloud run services describe "$SERVICE" \
    --region="$REGION" --project="$PROJECT" \
    --format="value(status.url)" 2>/dev/null || true)
  if [ -n "$REPORTED" ] && curl -sS -o /dev/null -m 20 "$REPORTED" 2>/dev/null; then
    echo "WARNING: the deterministic URL did not answer:" >&2
    echo "           $URL" >&2
    echo "         falling back to the address the service reports:" >&2
    echo "           $REPORTED" >&2
    URL="$REPORTED"
  else
    echo "ERROR: neither URL for ${SERVICE} answers." >&2
    echo "         constructed: $URL" >&2
    echo "         reported:    ${REPORTED:-<none>}" >&2
    echo "       Storing an unreachable address would break every" >&2
    echo "       kernel tool call as E_KERNEL_UNREACHABLE." >&2
    exit 1
  fi
fi

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
