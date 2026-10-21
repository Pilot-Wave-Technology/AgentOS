#!/usr/bin/env bash
#
# cluster_objects.sh <name_prefix> — the Kubernetes objects the
# execution plane needs before anything is deployed onto it: the
# namespace, its ResourceQuota, the five service accounts annotated
# onto their Google accounts, and a default-deny NetworkPolicy.
#
# THE KUBERNETES HALF OF gke_cluster.sh, AND ONLY THAT HALF. That script
# also creates the Google service accounts, custom roles, bucket, and
# every project, bucket, secret and Workload Identity binding. In this
# install terraform owns all of those (terraform/modules/execution_plane),
# and the build that runs this has no business holding the IAM-admin
# rights to make them a second time. These objects stay out of terraform
# for one reason: they need cluster credentials, which do not exist until
# the cluster does.
#
# Pinned to gke_cluster.sh in publish/overlay_sources.json, so a change to
# the quota or the accounts there is flagged here.
#
# Sourced names come from names.sh, exactly as the scripts beside it use.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=names.sh
. "${HERE}/names.sh"
PREFIX="${1:?name prefix required}"
agentos_names "$PREFIX" || exit 1

: "${PROJECT:?set PROJECT}" "${REGION:?set REGION}"
CLUSTER="${CLUSTER:-$PREFIX}"

gcloud container clusters get-credentials "$CLUSTER" \
  --region "$REGION" --project "$PROJECT" >/dev/null

# Unquoted heredoc: the ${...} below must expand. Keep backticks out of
# it, comments included -- an unquoted heredoc EXECUTES them.
kubectl apply -f - <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    agentos.io/environment: ${ENV_NAME}
---
# A BACKSTOP against a bug (a dispatch loop, a reconcile that forgets to
# delete), not the capacity limit: the node pools are that. Sized above
# the worst legitimate load -- twenty executions on the large tier --
# because a backstop below what the pools can produce is an outage that
# shows up as pods left Pending.
apiVersion: v1
kind: ResourceQuota
metadata:
  name: ${NAMESPACE}-quota
  namespace: ${NAMESPACE}
spec:
  hard:
    requests.cpu: "320"
    requests.memory: 1200Gi
    persistentvolumeclaims: "120"
    count/jobs.batch: "150"
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${KSA}
  namespace: ${NAMESPACE}
  annotations:
    iam.gke.io/gcp-service-account: ${GSA}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${COND_KSA}
  namespace: ${NAMESPACE}
  annotations:
    iam.gke.io/gcp-service-account: ${COND_GSA}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${VERIFY_KSA}
  namespace: ${NAMESPACE}
  annotations:
    iam.gke.io/gcp-service-account: ${VERIFY_GSA}
---
# The verifier DEPLOYMENT (which creates criterion Jobs), annotated to
# the CONDUCTOR's Google account: only the Kubernetes Role split.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${VERIFIER_CONTROL_KSA}
  namespace: ${NAMESPACE}
  annotations:
    iam.gke.io/gcp-service-account: ${COND_GSA}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${CAPTURE_KSA}
  namespace: ${NAMESPACE}
  annotations:
    iam.gke.io/gcp-service-account: ${CAPTURE_GSA}
---
# DEFAULT DENY, both directions. Nothing egresses until the proxy that
# mediates it exists, which is the state this design wants.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: ${NAMESPACE}
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
YAML

echo "── ${NAMESPACE}: namespace, quota, 5 service accounts, default-deny ──"
