#!/usr/bin/env bash
#
# ./execution-plane.sh — stand up the plane that runs the work.
#
#   ./execution-plane.sh              # create or update everything
#   ./execution-plane.sh --check      # just tell me what is missing
#
# REQUIRED: every executor runs on this plane. Each leg of a story's
# work is a pod of its own -- two containers, two users, a default-deny
# network and no cloud credential in the one the model drives. The
# build runs this script on every push; run it by hand when debugging.
#
# ── TERRAFORM CREATES THE INFRASTRUCTURE. THIS FINISHES THE JOB. ─────
#
# Run `terraform apply` FIRST. That creates the cluster, the four
# node pools, the four service accounts, the two custom roles, the
# artifact bucket, the Workload Identity bindings, the password for
# the execution pods' database login, the sqlproxy key, and every secret grant the
# conductor needs -- declaratively, in a plan you can read, and
# removable with `terraform destroy`. The same apply creates the
# database, so the two are set up together.
#
# This script does the rest, which needs cluster credentials (the
# pods' database login and its grants are migration 0004's):
#
#   the Kubernetes namespace, its ResourceQuota and the service
#     accounts annotated onto the Google ones (they need cluster
#     credentials, which do not exist until the cluster does)
#   the conductor, the verifier and the egress proxy, at a particular
#     image
#
# Every create below is describe-or-create, so running it against
# infrastructure terraform already made is a no-op. It REFUSES if the
# cluster is absent rather than building one a second way: two
# implementations of a provisioning sequence is how the two come to
# disagree.
#
# ── WHY STANDARD AND NOT AUTOPILOT ───────────────────────────────────
#
# Autopilot is cheaper per pod and needs no pool sizing, and that was
# the original choice. What retired it was not price: one story to a
# machine needs a nodeSelector on `agentos.io/node-class`, and
# Autopilot's admission controller refuses any selector key outside its
# own allowlist — at ADMISSION, so the manifest is never applied. The
# selector is emitted for every execution pod, so an Autopilot cluster
# would deploy GREEN and then fail every story at dispatch.
#
# ── ORDER MATTERS ─────────────────────────────────────────────────────
#
# The namespace and its service accounts come before the deploy, which
# runs pods as them. Each step is idempotent -- re-running is a no-op --
# so the honest way to use this is to run it again after fixing
# whatever it reported.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK_ONLY=0
[ "${1-}" = "--check" ] && CHECK_ONLY=1

# ONE ENVIRONMENT. The same flag preflight.sh sets, and for the same
# reason: it is what makes the scripts ask for `database-url` and
# `control_panel` rather than `<env>-database-url` and
# `control_panel_<env>`.
export AGENTOS_SINGLE_ENV="${AGENTOS_SINGLE_ENV-1}"

# UNLABELLED POOLS. A named scope labels and TAINTS each pool with the
# environment, which is how several environments share one cluster
# without stealing each other's machines. With one environment there is
# nobody to steal from, and a taint nothing tolerates is a cluster that
# schedules nothing.
export AGENTOS_POOL_SCOPE="${AGENTOS_POOL_SCOPE-}"

NAME_PREFIX="${NAME_PREFIX:-agentos}"
export NAME_PREFIX
export CLUSTER="${CLUSTER:-$NAME_PREFIX}"
export PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
export REGION="${REGION:-us-central1}"

# THE IMAGE THE CONDUCTOR, VERIFIER AND EGRESS PROXY RUN: the control
# plane's own. The build pins it to its commit; run by hand, this takes
# the latest one the build pushed.
export IMAGE="${IMAGE:-gcr.io/${PROJECT}/${NAME_PREFIX}:latest}"

# THE SERVERLESS VPC CONNECTOR'S RANGE, and it has to match
# `connector_cidr` in terraform.tfvars. The control plane is not a pod,
# so one NetworkPolicy rule naming this range is the only thing that
# lets the file explorer read a workspace. The policy is default-deny:
# a wrong value does not refuse, it HANGS.
export CONNECTOR_CIDR="${CONNECTOR_CIDR:-10.8.0.0/28}"

if [ -z "${PROJECT:-}" ] || [ "$PROJECT" = "(unset)" ]; then
  echo "ERROR: no project. Run 'gcloud config set project YOUR_PROJECT'," >&2
  echo "       or pass PROJECT=YOUR_PROJECT." >&2
  exit 1
fi

I="${HERE}/scripts/infra"
for f in names.sh cluster_objects.sh deploy_execution_plane.sh env_preflight.sh; do
  [ -f "${I}/${f}" ] || { echo "ERROR: ${I}/${f} not found. Run this from" \
                               "the root of your AgentOS checkout." >&2
                          exit 1; }
done

# shellcheck source=scripts/infra/names.sh
. "${I}/names.sh"
agentos_names "$NAME_PREFIX" || exit 1

echo "execution plane: project=${PROJECT} region=${REGION}"
echo "                 cluster=${CLUSTER} namespace=${NAMESPACE}"
echo "                 bucket=${ARTIFACT_BUCKET}"
echo

if [ "$CHECK_ONLY" = "1" ]; then
  exec bash "${I}/env_preflight.sh" "$NAME_PREFIX"
fi

step() {
  echo
  echo "══ $1 ═════════════════════════════════════════════════"
  shift
  if ! "$@"; then
    echo
    echo "STOPPED. The step above failed; nothing after it has run." >&2
    echo "Every step here is idempotent, so fix the cause and run" >&2
    echo "./execution-plane.sh again -- it will skip what already" >&2
    echo "exists rather than duplicating it." >&2
    exit 1
  fi
}

# THE CLUSTER IS TERRAFORM'S. Refuse rather than create a second one.
if ! gcloud container clusters describe "$CLUSTER" --region "$REGION" \
       --project "$PROJECT" >/dev/null 2>&1; then
  echo "ERROR: no cluster '${CLUSTER}' in ${PROJECT}/${REGION}." >&2
  echo "" >&2
  echo "       The execution plane's infrastructure is terraform's:" >&2
  echo "       run \`terraform apply\` and then this again. It creates" >&2
  echo "       the cluster, the node pools, the service accounts, the" >&2
  echo "       custom roles, the bucket and the registry -- in a plan" >&2
  echo "       you can read first, and removable with destroy." >&2
  echo "" >&2
  echo "       This script deliberately will not build one itself:" >&2
  echo "       two ways to create the same cluster is how the two come" >&2
  echo "       to disagree about what it is." >&2
  exit 1
fi
echo "cluster ${CLUSTER}: present (terraform's)"

# NOT gke_cluster.sh or db_bootstrap.sh, which the multi-environment
# installs run here. The first re-creates terraform's accounts, roles
# and IAM bindings, so the build would need IAM-admin to run it; the
# second needs an owner login. Here terraform owns the IAM, migration
# 0004 owns the pods' login and grants, and the build holds neither
# right.
step "1/2  namespace, quota, service accounts, default-deny" \
     bash "${I}/cluster_objects.sh" "$NAME_PREFIX"

step "2/2  conductor, verifier, egress proxy" \
     bash "${I}/deploy_execution_plane.sh" "$NAME_PREFIX"

echo
echo "══ verifying ══════════════════════════════════════════"
bash "${I}/env_preflight.sh" "$NAME_PREFIX"
