#!/usr/bin/env bash
#
# kube_bind.sh — point every kubectl call in this preflight at THE
# cluster it is checking, not at whatever context is current.
#
# WHY. The namespace, in-cluster-secret and database checks used to run
# against the CURRENT kubectl context. On a fresh machine after file 05
# there is none for the new cluster, so all four came back `[ ?? ]` with
# "the server has asked for the client to provide credentials" -- an
# operator stopped at a failure that was only a missing login. Worse, a
# context left pointing at ANOTHER cluster with an `agentos` namespace
# would have reported `ok` about the wrong cluster entirely.
#
# HOW. The credentials go into a PRIVATE kubeconfig for this run only
# (KUBECONFIG is exported for the rest of the preflight and removed on
# exit), so the operator's own context is never touched.
#
# NOT INSIDE A POD. Run in the conductor, the in-cluster service account
# already IS this cluster, and the pod has no gke-gcloud-auth-plugin.
#
# A BIND THAT FAILS IS REPORTED, never skipped: KUBE_BIND_ERR holds the
# reason and KUBE_BIND_ABSENT=1 means the cluster is not there (a fact,
# so MISS); anything else could not be asked (so `??`).
#
# Sourced by env_preflight.sh, after report.sh and before the checks.

KUBE_BIND_ERR=""
KUBE_BIND_ABSENT=0
PF_KUBECONFIG=""

kube_bind() {
  [ -n "${KUBERNETES_SERVICE_HOST-}" ] && return 0
  [ -n "${AGENTOS_PREFLIGHT_KEEP_CONTEXT-}" ] && return 0

  PF_KUBECONFIG="$(mktemp "${TMPDIR:-/tmp}/agentos-preflight-kube.XXXXXX")" || {
    KUBE_BIND_ERR="cannot create a temporary kubeconfig"; return 0; }
  trap 'rm -f "$PF_KUBECONFIG"' EXIT
  # Git Bash: kubectl and gcloud are native Windows programs and want a
  # Windows path, not /tmp/...
  if command -v cygpath >/dev/null 2>&1; then
    export KUBECONFIG="$(cygpath -w "$PF_KUBECONFIG")"
  else
    export KUBECONFIG="$PF_KUBECONFIG"
  fi

  local out
  if out=$(gcloud container clusters get-credentials "$CLUSTER" \
             --region="${REGION:-us-central1}" --project="$PROJECT" 2>&1); then
    return 0
  fi
  case "$out" in
    *"Not found"*|*"not found"*|*NOT_FOUND*|*404*) KUBE_BIND_ABSENT=1 ;;
  esac
  KUBE_BIND_ERR="$(printf '%s' "$out" | grep -v '^$' | tail -1)"
  return 0
}

# kube_unbound WHAT DETAIL FIX — report a check that cannot run because
# the bind failed. Returns 0 when it reported (the caller stops there).
kube_unbound() {
  [ -z "$KUBE_BIND_ERR" ] && return 1
  if [ "$KUBE_BIND_ABSENT" = 1 ]; then
    miss "$1" "$2 (cluster ${CLUSTER} does not exist)" "${3-}"
  else
    unknown "$1" "$2 (cannot get credentials for cluster ${CLUSTER}: ${KUBE_BIND_ERR})"
  fi
  return 0
}
