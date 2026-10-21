#!/usr/bin/env bash
#
# gke_cluster.sh <env> — the identities, the namespace and the secrets an
# environment's executions run with. Idempotent: every step asks before
# it creates, so re-running is a no-op.
#
# IT NO LONGER CREATES THE CLUSTER, and that is the point of this note.
# It used to, with `create-auto`, and the argument for Autopilot was a
# real one at the time: E2 Spot is 40 % off where Autopilot Spot is a
# flat 70 %, so Autopilot's pod rate beat the raw VM rate for the same
# pod, and there was no pool to size.
#
# WHAT RETIRED THE ARGUMENT WAS NOT PRICE. It was that Autopilot decides
# placement and will not be told otherwise. One story per machine needs
# a nodeSelector on `agentos.io/node-class`, and Autopilot's Warden
# refuses any selector key outside its own allowlist -- at ADMISSION, so
# the manifest is not applied at all. That is the product working as
# designed, and it is exactly the decision this architecture has to make
# for itself.
#
# It cost three red deploys to learn on Deployments, and would have cost
# far more on execution pods: the selector is emitted for every pod now,
# so a deploy to Autopilot would go GREEN and then fail every story at
# dispatch.
#
# So cluster creation belongs to scripts/infra/gke_standard_pools.sh --
# ONE script, creating ONE kind of cluster. Two scripts that both create
# a cluster, and create different kinds, is how a deleted cluster comes
# back as the kind that cannot run anything.
#
# ── what this creates ───────────────────────────────────────────────────
#   the regional Autopilot cluster (one per project, shared by envs)
#   a namespace per environment
#   a ResourceQuota on it
#   a Kubernetes service account, bound to a Google one by Workload
#     Identity
#
# THE QUOTA IS THE SAFETY ARGUMENT for sharing a cluster between
# environments, and the deck says so plainly: without it a runaway dev
# story starves production of pod capacity. It is not a tidiness measure
# and must not be skipped to save five minutes.
#
# WORKLOAD IDENTITY IS THE METADATA ANSWER. On a VM the guard was an
# iptables DROP on 169.254.169.254; Autopilot forbids NET_ADMIN, so that
# tool is gone. Instead the pod's KSA maps to a Google service account
# bound NOTHING worth having, so reaching the metadata server returns an
# identity that cannot do anything — the same "bind it nothing worth
# having" rule the worker's grants already follow.
set -euo pipefail

ENV_NAME="${1:?environment name required}"
# THE FIFTH GUARD, opened last because it was the one not in the list.
# names.sh, gke_standard_pools.sh, deploy_execution_plane.sh and
# check_infra_scope.py were opened together; this one was missed, and it
# is the script that creates the four service accounts, the artifact
# bucket, the namespace and its quota -- so dev had pools it could not
# put an identity on.
#
# Nothing below is next-specific: every name comes from names.sh, which
# derives all of them from this argument.
case "$ENV_NAME" in
  "")  echo "ERROR: environment name must not be empty" >&2; exit 1 ;;
  *[!a-z0-9-]*)
    echo "ERROR: environment name '${ENV_NAME}' must be lowercase letters," >&2
    echo "       digits and hyphens: it becomes a namespace, a database" >&2
    echo "       name, a secret suffix and a service-account address." >&2
    exit 1 ;;
esac

: "${PROJECT:?PROJECT env var required}"
: "${REGION:?REGION env var required}"

# Every name in one place, so this script and the deploy cannot
# disagree about what the namespace or the bucket is called.
# shellcheck source=scripts/infra/names.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/names.sh"
agentos_names "$ENV_NAME"

# FOUR IDENTITIES, NOT ONE, and the split is the whole point.
#
# The first version of this file gave the namespace a single service
# account "holding nothing" — correct for the thing that runs
# model-authored code, and impossible for the conductor, which needs the
# database and the secrets to do its job. One identity would have forced
# a choice between a conductor that cannot start and an execution pod
# holding production credentials.
#
#   agentos-execution   execution pods and verifier Jobs. NO project
#                       roles, ever. This is the identity reachable from
#                       the metadata server by a process running a
#                       model's bash, so it must be worth nothing.
#
#   agentos-conductor   the conductor Deployment. Holds cloudsql.client
#                       and secretAccessor on `next`'s own secrets. It
#                       runs no model output and creates no sandbox.
#
#   agentos-verifier    a criterion's Job. `storage.objects.get` on the
#                       artifacts bucket and nothing else — it reads the
#                       committed tree, which arrives as an object
#                       because a Job's pod cannot be WRITTEN to before
#                       it starts. NOT agentos-execution: that identity
#                       is reachable from a model's bash, and bucket
#                       read on it would let the executor fetch trees.
#
#   agentos-capture     the environment replay's Job, which WRITES the
#                       directory it captured because a Job's pod cannot
#                       be READ after it exits. get and create. Kept
#                       apart from the verifier because read and write
#                       are different capabilities, and a criterion able
#                       to write is one that can put objects where the
#                       control panel and the `artifact` criterion will
#                       read them.
#
# `list` IS WITHHELD FROM BOTH, which is why they are custom roles:
# `roles/storage.objectViewer` includes `storage.objects.list`, and with
# list the names are enumerable and the hash stops being the capability.
# Object names are the sha256 of their contents, so without list holding
# one id gets you exactly one object and no way to discover a second.
#
# The names live in names.sh, sourced above.

echo "project=${PROJECT} region=${REGION} cluster=${CLUSTER} ns=${NAMESPACE}"

# ── the cluster ─────────────────────────────────────────────────────────
# REGIONAL, not zonal. The control plane survives a zone, and Autopilot
# is regional by construction anyway. `--release-channel=regular` because
# a channel is required and `rapid` would move the version under a
# running environment.
if gcloud container clusters describe "$CLUSTER" \
     --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  echo "cluster ${CLUSTER} already exists"
else
  # REFUSED, WHERE THIS ONCE CREATED ONE. See the note at the top: the
  # cluster it created was Autopilot, and an Autopilot cluster cannot
  # host one story per machine. Creating the wrong kind here is worse
  # than creating nothing, because everything downstream succeeds until
  # a story tries to dispatch.
  echo "cluster ${CLUSTER} does not exist, and this script no longer" \
       "creates one." >&2
  echo "Cluster creation belongs to gke_standard_pools.sh, which makes a" \
       "STANDARD cluster with the platform, small, medium and large" \
       "pools that one-story-per-machine needs:" >&2
  echo "    CLUSTER=${CLUSTER} scripts/infra/gke_standard_pools.sh ${ENV_NAME}" >&2
  echo "then re-run this to lay down the identities, namespace and" \
       "secrets." >&2
  exit 1
fi

gcloud container clusters get-credentials "$CLUSTER" \
  --region="$REGION" --project="$PROJECT"

# ── the runtime identity ────────────────────────────────────────────────
# CREATED HOLDING NOTHING, and that is the whole design. Every grant it
# ever gets is a deliberate act by a person, one resource at a time —
# which is what makes reaching the metadata server uninteresting.
if gcloud iam service-accounts describe "$GSA" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "service account ${GSA} already exists"
else
  echo "creating ${GSA} — deliberately holding no roles"
  gcloud iam service-accounts create "${GSA%%@*}" \
    --project="$PROJECT" \
    --display-name="AgentOS ${ENV_NAME} execution plane"
fi

if gcloud iam service-accounts describe "$COND_GSA" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "service account ${COND_GSA} already exists"
else
  echo "creating ${COND_GSA} — the conductor's identity"
  gcloud iam service-accounts create "${COND_GSA%%@*}" \
    --project="$PROJECT" \
    --display-name="AgentOS ${ENV_NAME} conductor"
fi

if gcloud iam service-accounts describe "$VERIFY_GSA" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "service account ${VERIFY_GSA} already exists"
else
  echo "creating ${VERIFY_GSA} — reads committed trees, nothing else"
  gcloud iam service-accounts create "${VERIFY_GSA%%@*}" \
    --project="$PROJECT" \
    --display-name="AgentOS ${ENV_NAME} verifier"
fi

if gcloud iam service-accounts describe "$CAPTURE_GSA" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "service account ${CAPTURE_GSA} already exists"
else
  echo "creating ${CAPTURE_GSA} — writes captured environments"
  gcloud iam service-accounts create "${CAPTURE_GSA%%@*}" \
    --project="$PROJECT" \
    --display-name="AgentOS ${ENV_NAME} environment capture"
fi

# ── two roles that withhold `list` ──────────────────────────────────────
# `roles/storage.objectViewer` INCLUDES `storage.objects.list`, which
# would make the sentence this design rests on false: with list, object
# names are enumerable, the hash stops being the capability, and a
# criterion could walk the bucket and read every other story's tree —
# held-out checks included. There is no predefined role for "get one
# object you already know the name of", so these are custom.
#
# `create` implies neither overwrite nor delete, and `put` is
# conditional on the object not existing, so the write role cannot
# replace an object either.
role_upsert() {
  local role_id="$1" perms="$2" title="$3"
  if gcloud iam roles describe "$role_id" --project="$PROJECT" \
       >/dev/null 2>&1; then
    gcloud iam roles update "$role_id" --project="$PROJECT" \
      --permissions="$perms" --quiet >/dev/null
  else
    gcloud iam roles create "$role_id" --project="$PROJECT" \
      --title="$title" --permissions="$perms" --quiet >/dev/null
  fi
}
role_upsert "$ROLE_READ" "storage.objects.get" \
  "AgentOS artifact read (no list)"
# `get` as well as `create`: put() checks for an existing object first,
# which is the dedup and also the write barrier.
role_upsert "$ROLE_WRITE" "storage.objects.get,storage.objects.create" \
  "AgentOS artifact write (no list, no overwrite)"

# ── the artifact store ──────────────────────────────────────────────────
# CREATED HERE, and it was not created anywhere before this. The
# conductor's manifest has named `agentos-staging-artifacts` since the
# execution plane was first deployed and no script ever made it, so the
# first `put` would have failed on a bucket that does not exist — and
# `put` is now on the path of every single criterion.
#
# UNIFORM BUCKET-LEVEL ACCESS, so access is IAM and only IAM. Per-object
# ACLs would make "who can read a tree" a question with two answers.
if gcloud storage buckets describe "gs://${ARTIFACT_BUCKET}" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "bucket gs://${ARTIFACT_BUCKET} already exists"
else
  echo "creating gs://${ARTIFACT_BUCKET}"
  gcloud storage buckets create "gs://${ARTIFACT_BUCKET}" \
    --project="$PROJECT" --location="$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

# WHO MAY DO WHAT TO THE STORE.
#
#   conductor   objectUser. It is the trusted component: it writes the
#               trees, reads artifacts for the control panel, and runs
#               no model output. NOT objectAdmin — it has no business
#               changing the bucket's policy.
#   verifier    get, and not even list. A criterion fetches one tree by
#               an id it was handed.
#   capture     get and create. The environment replay writes the
#               directory it captured, because a Job's pod cannot be
#               read after it exits.
#
# The last two are custom roles because no predefined one is this
# narrow. With `list` withheld the hash is the capability: holding one
# id gets you exactly one object and no way to discover a second, which
# is what makes granting a criterion read on the shared bucket a bounded
# decision rather than a broad one.
gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
  --project="$PROJECT" \
  --member="serviceAccount:${COND_GSA}" \
  --role=roles/storage.objectUser --quiet >/dev/null

gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
  --project="$PROJECT" \
  --member="serviceAccount:${VERIFY_GSA}" \
  --role="projects/${PROJECT}/roles/${ROLE_READ}" --quiet >/dev/null

gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
  --project="$PROJECT" \
  --member="serviceAccount:${CAPTURE_GSA}" \
  --role="projects/${PROJECT}/roles/${ROLE_WRITE}" --quiet >/dev/null

# cloudsql.client only. NOT cloudsql.admin: the conductor connects to a
# database, it does not create or drop them, and the migrate Job is a
# separate identity for exactly that reason.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${COND_GSA}" \
  --role=roles/cloudsql.client \
  --condition=None --quiet >/dev/null

# secretAccessor PER SECRET, not project-wide. `next` borrows prod's
# model keys and gateway token, so a project-wide grant here would hand
# the conductor every secret in the project including ones belonging to
# environments it has no business reading.
# ── EVERY NAME IS THIS ENVIRONMENT'S OWN ───────────────────────────────
#
# THREE OF THESE READ `-prod` UNTIL NOW, and deploy_execution_plane.sh
# already stopped doing that with the argument spelled out in full:
# "a model key buys quota against a metered API; a gateway token is a
# credential to a STATEFUL service that production depends on --
# holding it means being able to create and delete agents, and push
# files into agent workspaces, on the gateway prod is running on."
# That file was fixed and this list was not, so the GRANT stayed behind
# the READ.
#
# It mattered little while `next` was the only environment this script
# built. It matters now: running it for `dev` would give a dev identity
# read access to production's gateway credential, which is exactly the
# grant the two-identity split exists to prevent.
#
# THE LIST NOW MIRRORS WHAT THE CONDUCTOR ACTUALLY READS at deploy
# time. Two lists that must agree is the shape that produced this bug;
# they at least now agree, and a mismatch shows up as "COULD NOT BIND"
# below rather than as a conductor that cannot start.
#
# NOTHING IS REVOKED BY THIS. `add-iam-policy-binding` only adds, so
# next's existing bindings are untouched; this changes what a FUTURE
# run grants.
#
# THROUGH agentos_secret, not spelled. These were literals, so on a
# single-environment install every one of them named a secret that
# does not exist -- and the loop below only WARNS on a failed binding,
# so the conductor would have been deployed with no access to its own
# DSN and failed at startup with a message about the database.
SECRETS=(
  "$(agentos_secret database-url)"
  # THE SIDECAR-FORM DSN, which was absent from this list entirely --
  # the one secret the conductor cannot start without.
  "$(agentos_secret database-url-conductor)"
  "$(agentos_secret openclaw-gateway-token)"
  "$(agentos_secret gemini-api-key)"
  "$(agentos_secret anthropic-api-key)"
  "$(agentos_secret credential-encryption-key)"
  "$(agentos_secret agent-token-signing-key)"
)
for SECRET in "${SECRETS[@]}"; do
  if gcloud secrets add-iam-policy-binding "$SECRET" \
       --project="$PROJECT" \
       --member="serviceAccount:${COND_GSA}" \
       --role=roles/secretmanager.secretAccessor \
       --quiet >/dev/null 2>&1; then
    echo "  bound ${SECRET}"
  else
    # NAMED, not swallowed. bootstrap runs before this step and creates
    # them, so a miss here means either bootstrap did not run or the
    # secret is spelled differently — both worth seeing rather than
    # discovering when the conductor cannot start.
    echo "  COULD NOT BIND ${SECRET} — does it exist?" >&2
  fi
done

# ── the namespace, its quota, and the bindings ──────────────────────────
# Applied rather than created imperatively: this is the one part that is
# naturally declarative, and `kubectl apply` is idempotent by design.
# BACKTICKS ESCAPED THROUGHOUT THIS HEREDOC. The delimiter is
# unquoted on purpose -- ${NAMESPACE} and ${ENV_NAME} below have to
# expand -- and an unquoted heredoc also runs command substitution,
# including inside YAML comments. The prose here quotes pool and
# field names in backticks, so every run printed a line like
#   gke_cluster.sh: line 342: persistentvolumeclaims: command not
#   found
# for each one, and substituted the empty string into the comment.
#
# Harmless with these words. Not harmless as a rule: whatever sits
# between backticks in an unquoted heredoc is EXECUTED, so the only
# thing standing between a comment and a command was the vocabulary
# someone happened to choose.
kubectl apply -f - <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    agentos.io/environment: ${ENV_NAME}
---
# WITHOUT THIS, a runaway story in one environment starves the other of
# pod capacity. The numbers are a ceiling on concurrency, not a target:
# admission_slots in Postgres is what actually paces work, and this is
# the backstop for when that is wrong.
#
# THIS IS WHAT "MAKE THE CLUSTER BIGGER" MEANS HERE. \`agentos\` is a GKE
# AUTOPILOT cluster: there are no node pools to resize -- \`gcloud
# container node-pools list\` refuses outright -- and Google adds nodes to
# fit the pods that are admitted. So the only ceiling that is ours is
# this one, and raising it is the whole operation.
#
# RAISED AGAIN 2026-09-19, from 128 / 512Gi, AND THE REASON IS A CHANGE
# OF ROLE RATHER THAN A CHANGE OF APPETITE. Under one-story-per-machine
# the thing that decides how much can run at once is the NODE POOLS in
# scripts/infra/gke_standard_pools.sh -- small maxes at 20 machines,
# medium at 10, large at 4, and a story occupies a whole one. Those
# maxima are also the cost control, because an autoscaler that cannot
# add a 21st small node cannot spend money on one.
#
# That leaves this quota as a BACKSTOP against a bug -- a dispatch loop,
# a reconcile that forgets to delete -- rather than as a second opinion
# about capacity. A backstop set below what the pools can legitimately
# produce is not a backstop; it is an outage with a confusing message,
# because the pods it refuses are refused by being left Pending. The
# worst legitimate load is twenty executions (kernel/conductor/admit.py
# SYSTEM_CEILING) taken from the biggest tiers the pools can serve.
#
# RAISED AGAIN when every pool went to twenty. It used to be four large,
# ten medium and two small -- 138 vCPU and 496 GiB -- because \`large\`
# stopped at four and \`medium\` at ten, and the mix was forced. With all
# three pools able to reach twenty the worst case is TWENTY LARGE:
# 20 x (14.0 sandbox + 0.5 worker + 0.25 sqlproxy) = 295 vCPU and
# 20 x (52 GiB + 2 GiB + 0.5 GiB) = 1090 GiB. That mix is now reachable,
# so the backstop has to sit above it -- a backstop below what the pools
# can legitimately produce is not a backstop, it is an outage with a
# confusing message.
#
# \`test_the_ceiling_is_priced_against_the_real_quota\` parses both this
# file and the pools file and recomputes the number, so a pool maximum
# and this quota can no longer drift apart in silence. It is what
# caught the raise above.
#
# RAISED 2026-09-16, from 64 / 256Gi / 40 / 60. An execution asks for
# about 1 CPU and 4Gi between its worker and its sandbox, so the old
# \`requests.cpu: 64\` was roughly thirty concurrent runs and was never
# what limited anything. \`persistentvolumeclaims\` was the one that
# actually bit: a claim outlives its pod on purpose (preemption), so 40
# was forty executions EVER until the reclaim sweep started releasing
# them -- and a namespace at its claim ceiling cannot start a story at
# all, which reads as "nothing runs" long after the cause.
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
# The verifier DEPLOYMENT, which is not the verifier JOB above.
# ${VERIFY_KSA} is a criterion Job's identity; this is the process that
# CREATES those Jobs, and the RBAC split gave it a Role of its own.
# Annotated to the CONDUCTOR's GSA: only the Kubernetes half split.
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
# DEFAULT DENY, both directions. A NetworkPolicy selects pods, and
# worker and sandbox share one — so this is the coarse boundary, and the
# fine one is credential possession. See
# docs/refactor/decisions/p1-one-pod-per-execution.md.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: ${NAMESPACE}
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
YAML

# The other half of Workload Identity: the Google account must allow the
# Kubernetes one to impersonate it. Without this the annotation above is
# decoration.
gcloud iam service-accounts add-iam-policy-binding "$GSA" \
  --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT}.svc.id.goog[${NAMESPACE}/${KSA}]" \
  --quiet >/dev/null

gcloud iam service-accounts add-iam-policy-binding "$COND_GSA" \
  --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT}.svc.id.goog[${NAMESPACE}/${COND_KSA}]" \
  --quiet >/dev/null

gcloud iam service-accounts add-iam-policy-binding "$VERIFY_GSA" \
  --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT}.svc.id.goog[${NAMESPACE}/${VERIFY_KSA}]" \
  --quiet >/dev/null

# -- THE CAPTURE IDENTITY, WHICH THIS SCRIPT NEVER BOUND --------------
#
# It creates CAPTURE_GSA, grants it agentosArtifactWrite on the bucket,
# and writes the CAPTURE_KSA ServiceAccount carrying the
# `iam.gke.io/gcp-service-account` annotation -- every piece except the
# binding that lets the KSA actually impersonate the GSA. The
# annotation on its own does nothing.
#
# It went unnoticed because `next` was bound BY HAND at some point, so
# the first environment ever built from this script end to end is the
# one that revealed it: `dev`, on 2026-09-29, where agentos-capture-dev
# had no members at all while the other three were correct.
#
# The failure is quiet and late. The environment replay is the only
# thing that uses this identity, so everything looks healthy until a
# capture runs and cannot authenticate to the artifact store.
gcloud iam service-accounts add-iam-policy-binding "$CAPTURE_GSA" \
  --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT}.svc.id.goog[${NAMESPACE}/${CAPTURE_KSA}]" \
  --quiet >/dev/null

# A SECOND MEMBER ON THE CONDUCTOR'S GSA, not a second GSA. Both the
# conductor and the verifier Deployment impersonate it; what the RBAC
# split gave them is a different Kubernetes Role, not a different
# Google identity. E1 found a shared Role let a compromised verifier
# delete execution pods; 5.18 added podfs write. Splitting the GCP half
# too would mean copying the Workload Identity and keeping two copies
# identical, which is the objection the old test raised.
gcloud iam service-accounts add-iam-policy-binding "$COND_GSA" \
  --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT}.svc.id.goog[${NAMESPACE}/${VERIFIER_CONTROL_KSA}]" \
  --quiet >/dev/null

echo "── ${NAMESPACE} ready ──"
echo "  cluster   ${CLUSTER} (${REGION}, Autopilot)"
echo "  namespace ${NAMESPACE} with a ResourceQuota and default-deny"
echo "  identity  ${KSA} -> ${GSA} (no project roles, by design)"
echo "  conductor ${COND_KSA} -> ${COND_GSA} (cloudsql.client + named secrets)"
echo "  verifier  ${VERIFY_KSA} -> ${VERIFY_GSA} (get only, no list)"
echo "  capture   ${CAPTURE_KSA} -> ${CAPTURE_GSA} (get+create, no list)"
echo "  store     gs://${ARTIFACT_BUCKET}"
echo ""
echo "NOTE: default-deny means nothing egresses until the proxy exists."
echo "      That is deliberate — a namespace that can reach the internet"
echo "      before anything mediates it is the state this design avoids."
