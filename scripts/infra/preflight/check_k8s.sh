#!/usr/bin/env bash
#
# check_k8s.sh — the cluster-side facts: the namespace, its service
# accounts and their annotations, the synced secrets, and the node
# pools this environment is allowed to schedule on.
#
# THE NODE-POOL CHECK IS THE ONE THAT MATTERS MOST. `dev` and `next`
# share a cluster, and the only thing keeping dev's executor pods off
# next's nodes is a label plus a taint. If the label is absent the
# selector matches nothing (a pod that never schedules -- loud), but
# if the TAINT is absent, dev's pods land on next's nodes and next's
# work slows down with nothing anywhere saying why. Quiet, and in
# production. So both halves are checked, separately.
#
# Sourced by env_preflight.sh.

_kube() { kubectl --namespace "$NAMESPACE" "$@" 2>&1; }

check_namespace() {
  section "namespace"
  local out rc
  kube_unbound "namespace" "$NAMESPACE" && return
  out=$(kubectl get namespace "$NAMESPACE" -o name 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then ok "namespace" "$NAMESPACE"
  elif printf '%s' "$out" | grep -qi 'not found'; then
    miss "namespace" "$NAMESPACE does not exist" \
         "kubectl create namespace ${NAMESPACE}"
    return
  else unknown "namespace" "$NAMESPACE ($out)"; return; fi

  # KSAs, and the annotation that ties each to its Google account.
  # An unannotated KSA gets no token and the pod fails at first API
  # call, far from here.
  local pair ksa gsa ann
  for pair in "$KSA:$GSA" "$COND_KSA:$COND_GSA" "$VERIFY_KSA:$VERIFY_GSA" \
              "$CAPTURE_KSA:$CAPTURE_GSA" "$VERIFIER_CONTROL_KSA:$COND_GSA"; do
    ksa="${pair%%:*}"; gsa="${pair##*:}"
    ann=$(_kube get serviceaccount "$ksa" \
            -o jsonpath='{.metadata.annotations.iam\.gke\.io/gcp-service-account}')
    if printf '%s' "$ann" | grep -qi 'not found'; then
      miss "ksa" "$ksa is absent" "$(fix_setup "scripts/infra/gke_cluster.sh ${ENV_NAME}")"
    elif [ "$ann" = "$gsa" ]; then
      ok "ksa" "$ksa -> ${gsa%%@*}"
    elif [ -z "$ann" ]; then
      miss "ksa" "$ksa has no gcp-service-account annotation" \
           "kubectl annotate sa ${ksa} -n ${NAMESPACE} iam.gke.io/gcp-service-account=${gsa}"
    else
      miss "ksa" "$ksa points at ${ann}, expected ${gsa}"
    fi
  done
}

# ── secrets synced into the namespace ────────────────────────────────
#
# THE LIST IS READ OUT OF THE SCRIPT THAT CREATES THEM, not written
# here. The first version of this check named `conductor-db`; the
# secret is called `conductor-secrets`, so it reported a correctly
# provisioned environment as broken. That is precisely the failure
# names.sh exists to prevent -- two files agreeing by coincidence
# until one of them is edited -- reproduced inside the checker meant
# to catch it.
#
# Derived, there is exactly one place a secret's name is written, and
# a rename cannot make this check wrong.
check_kube_secrets() {
  section "in-cluster secrets"
  local deploy="${HERE}/deploy_execution_plane.sh" name out wanted
  wanted=$(grep -oE 'create secret generic [a-z0-9-]+' "$deploy"              | awk '{print $NF}' | sort -u)
  if [ -z "$wanted" ]; then
    unknown "k8s-secret" "cannot read the secret names out of deploy_execution_plane.sh"
    return
  fi
  for name in $wanted; do
    kube_unbound "k8s-secret" "$name" && continue
    out=$(_kube get secret "$name" -o name)
    if printf '%s' "$out" | grep -q '^secret/'; then ok "k8s-secret" "$name"
    elif printf '%s' "$out" | grep -qi 'not found'; then
      miss "k8s-secret" "$name is not in ${NAMESPACE}"            "$(fix_setup "AGENTOS_POOL_SCOPE=${AGENTOS_POOL_SCOPE-} scripts/infra/deploy_execution_plane.sh ${ENV_NAME}")"
    else unknown "k8s-secret" "$name ($out)"; fi
  done
}

# ── node pools ───────────────────────────────────────────────────────
#
# WHAT IS CHECKED DEPENDS ON ONE OPTION. AGENTOS_POOL_SCOPE is blank by
# default, and blank means "these pools are the only ones in this
# cluster": unsuffixed names, no agentos.io/env label, no taint, pods
# selecting on node-class alone. That is what a first deployment gets
# without configuring anything, and what `next` has always had.
#
# A NAMED scope means this environment shares a cluster with another,
# and then three things must hold together: the pool is suffixed, it
# carries the label its pods select, and it carries the matching taint.
#
# THE TAINT IS THE HALF THAT FAILS QUIETLY, which is why it is checked
# separately rather than assumed to follow the label. Without the LABEL
# a pod matches nothing and never schedules -- loud and immediate.
# Without the TAINT everything still runs: the other environment's
# pods, which carry no env selector and so match every pool of their
# class, simply start landing here. The only symptom is that somebody
# else's work got slower.
#
# ASKED WITH gcloud's OWN PROJECTION, not piped through anything. The
# first version of this question piped gcloud into `python3`; on a
# machine where python3 is the Windows Store stub it read empty for
# every pool, empty was taken for "unlabelled", and four correctly
# configured pools were reported broken.
# ONE CALL, NOT EIGHT. The first version asked gcloud to describe each
# pool twice -- once for the label, once for the taint -- which is 8
# round trips for 4 classes, and gcloud costs a second or two per
# invocation before it does anything. `list` returns every pool with
# both fields, so the whole section is one request.
_pools_csv() {
  gcloud container node-pools list     --cluster="$CLUSTER" --region="${REGION:-us-central1}" --project="$PROJECT"     --format="csv[no-heading](name,config.labels,config.taints)" 2>&1
}

check_node_pools() {
  local scope="${AGENTOS_POOL_SCOPE-}" suffix="" csv row name cls
  [ -n "$scope" ] && suffix="-${scope}"
  section "node pools (${CLUSTER}${scope:+, scope=${scope}})"

  csv="$(_pools_csv)"
  # A LISTING THAT DID NOT HAPPEN IS NOT AN EMPTY CLUSTER. Without
  # this, a failed call would make every pool below read as missing --
  # four confident findings, each with a fix to run, about an
  # environment that is fine.
  if printf '%s' "$csv" | grep -qiE "ERROR:|denied|Reauth|can.t open file|Traceback"; then
    # A CLUSTER THAT IS NOT THERE IS A FINDING, NOT A MYSTERY. `404
    # Not found: ... clusters/<name>` answers the question: there is no
    # cluster, so there are no pools. Everything else here -- a denied
    # call, a reauth prompt, a broken interpreter -- genuinely could
    # not ask, and stays undetermined.
    case "$csv" in
      *"Not found"*cluster*|*"not found"*cluster*|*404*cluster*)
        miss "pool" "cluster ${CLUSTER} does not exist, so it has no node pools" \
             "$(fix_setup "CLUSTER=${CLUSTER} scripts/infra/gke_standard_pools.sh ${ENV_NAME}")" ;;
      *)
        unknown "pool" "cannot list node pools: $(printf '%s' "$csv" | head -1)" ;;
    esac
    return
  fi

  for cls in platform small medium large; do
    name="${cls}${suffix}"
    row="$(printf '%s
' "$csv" | grep -E "^${name}," || true)"

    if [ -z "$row" ]; then
      miss "pool" "node pool ${name} does not exist"            "$(fix_setup "CLUSTER=${CLUSTER} AGENTOS_POOL_SCOPE=${scope} scripts/infra/gke_standard_pools.sh ${ENV_NAME}")"
      continue
    fi

    if [ -z "$scope" ]; then
      # Unscoped by choice. A label here means somebody began scoping
      # this environment without giving its pods the selector, which
      # strands every pod that still selects on node-class alone.
      if printf '%s' "$row" | grep -q "agentos.io/env="; then
        miss "pool" "${name} carries an agentos.io/env label but this environment's pods do not select it"              "either clear the label or set AGENTOS_POOL_SCOPE=${ENV_NAME} and redeploy"
      else
        ok "pool" "${name} (unscoped)"
      fi
      continue
    fi

    if printf '%s' "$row" | grep -q "agentos.io/env=${scope}"; then
      ok "pool" "${name} labelled agentos.io/env=${scope}"
    else
      miss "pool" "${name} carries no agentos.io/env=${scope} label"            "$(fix_setup "CLUSTER=${CLUSTER} AGENTOS_POOL_SCOPE=${scope} scripts/infra/gke_standard_pools.sh ${ENV_NAME}")"
    fi

    # THE TAINT IS THE HALF THAT FAILS QUIETLY. Without the LABEL a pod
    # matches nothing and never schedules -- loud and immediate.
    # Without the TAINT everything still runs: the other environment's
    # pods, which carry no env selector and so match every pool of
    # their class, simply start landing here, and the only symptom is
    # that somebody else's work got slower.
    if printf '%s' "$row" | grep -q "'key': 'agentos.io/env'"; then
      ok "taint" "${name} repels pods from other environments"
    else
      miss "taint" "${name} has no agentos.io/env taint -- another environment's pods can land on it"            "$(fix_setup "CLUSTER=${CLUSTER} AGENTOS_POOL_SCOPE=${scope} scripts/infra/gke_standard_pools.sh ${ENV_NAME}")"
    fi
  done
}
