#!/usr/bin/env bash
#
# env_preflight.sh <env> — is this environment fully provisioned?
#
#   scripts/infra/env_preflight.sh dev
#
# Exit 0 = every access, role, grant, secret and label this environment
# needs is present. Exit 1 = something is missing, or could not be
# determined. It prints what, and the command that fixes it.
#
# ── WHY THIS EXISTS ───────────────────────────────────────────────────
#
# Standing up a second environment beside the first found eleven things
# that were never in the repo: they had been done by hand, once, years
# earlier, and nothing noticed they were missing because nothing ever
# asked. Every one of them shipped a GREEN BUILD -- the test suite
# passes identically against an environment that cannot serve a
# request, because none of this is in the code.
#
# THE FAILURES WERE ALL QUIET. A missing default-privileges rule means
# one endpoint 500s after some LATER deploy adds a table. A missing
# ASSISTANT_API_TOKEN reports itself as E_KERNEL_UNREACHABLE, which
# reads as a network problem. A missing taint means another
# environment's pods start landing on your nodes and the only symptom
# is that somebody else's work got slower.
#
# So this runs BEFORE the deploy and REFUSES. A deploy into a
# half-provisioned environment is the expensive case: it comes up, it
# serves some requests, and it fails on the ones nobody tested.
#
# ── COULD-NOT-ASK IS A FAILURE ────────────────────────────────────────
#
# Four separate defects during that build had one shape: a question
# that could not fail visibly answered "no". report.sh has the list.
# Every check here has three outcomes, not two.
set -uo pipefail          # NOT -e: a failing check must be REPORTED,
                          # not abort the run before the others report.

ENV_NAME="${1:?environment name required}"
export PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
export REGION="${REGION:-us-central1}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ -n "${PROJECT:-}" ] || { echo "ERROR: set PROJECT" >&2; exit 1; }

# shellcheck source=names.sh
. "${HERE}/names.sh"
agentos_names "$ENV_NAME" || exit 1
CLUSTER="${CLUSTER:-agentos}"

. "${HERE}/preflight/report.sh"
. "${HERE}/preflight/check_gcp.sh"
. "${HERE}/preflight/kube_bind.sh"
. "${HERE}/preflight/check_k8s.sh"
. "${HERE}/preflight/run_db.sh"

# Derived exactly as deploy_execution_plane.sh derives it, so the
# check asks about the login the pods actually use.
LEG_ROLE="${AGENTOS_LEG_ROLE:-}"
if [ -z "$LEG_ROLE" ]; then
  if [ -z "${AGENTOS_POOL_SCOPE-}" ]; then
    LEG_ROLE="agentos_leg"
  else
    LEG_ROLE="agentos_leg_${ENV_NAME}"
  fi
fi

echo "PREFLIGHT ${ENV_NAME}  (project ${PROJECT}, cluster ${CLUSTER})"
echo "scope: ${AGENTOS_POOL_SCOPE:-<blank -- unlabelled pools>}"

check_secrets
check_buckets
check_identity
check_registry
kube_bind
check_namespace
check_kube_secrets
check_node_pools

# ── the database ──────────────────────────────────────────────────────
#
# RUN AS THE MIGRATE LOGIN, because that is the login whose privileges
# are in question: it is the one that has to CREATE SCHEMA and re-grant
# the three cluster-wide roles. Checking as anybody else would be
# asking about the wrong identity.
section "database"
if DSN=$(gcloud secrets versions access latest \
           --secret="$(agentos_secret database-url-migrate)" \
           --project="$PROJECT" 2>/dev/null); then
  if APP_DSN=$(gcloud secrets versions access latest \
                 --secret="$(agentos_secret database-url)" \
                 --project="$PROJECT" 2>/dev/null); then
    APP_DB_USER=$(printf '%s' "$APP_DSN" | sed -n 's|^[a-z+]*://\([^:/@]*\).*|\1|p')
  else
    APP_DB_USER=""
  fi
  # THE INTERPRETER IS RESOLVED, NOT NAMED. `python3` on Windows is
  # normally the Microsoft Store stub: it prints an advert, exits
  # non-zero and runs nothing. Naming it here would have made the
  # database section report MISSING on a perfectly good environment
  # -- the same defect as `env_label_of` piping gcloud into that stub
  # and calling four correct node pools broken.
  #
  # A TOOL THAT IS ABSENT IS NOT A GRANT THAT IS MISSING, so a missing
  # interpreter is undetermined rather than a finding about the
  # database.
  PY=""
  for _cand in python3 python py; do
    command -v "$_cand" >/dev/null 2>&1 || continue
    "$_cand" -c 'import sys' >/dev/null 2>&1 || continue
    PY="$_cand"; break
  done

  if [ -z "$PY" ]; then
    unknown "database" "no working python on PATH, so the grants cannot be checked"
  elif run_db_check "$DSN"; then
    PF_OK=$((PF_OK + 1))
  else
    PF_MISS=$((PF_MISS + 1))
  fi
else
  unknown "database" "cannot read $(agentos_secret database-url-migrate), so nothing about the database can be checked"
fi

pf_verdict
