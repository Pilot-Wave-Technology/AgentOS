#!/usr/bin/env bash
#
# gke_standard_pools.sh <env> — the Standard cluster and its three node
# pools, for an execution plane that runs one story per machine.
#
# ── WHY THIS EXISTS AT ALL ──────────────────────────────────────────────
#
# `gke_cluster.sh` builds the same environment on AUTOPILOT, with
# `clusters create-auto`, and everything it does after the cluster — four
# service accounts, two custom roles, the artifact bucket, the secrets —
# is identical on either mode. This script replaces ONE step of it and
# adds the pools; run this first, then `gke_cluster.sh`, which finds the
# cluster already there and carries on. It is not a fork of that file.
#
# ONE MEASUREMENT DROVE THE WHOLE DECISION. Autopilot refuses any pod
# asking for more than 10 GiB of ephemeral storage -- flat, enforced by
# the GKE Warden webhook, and NOT scaled by pod or machine size: a
# 7 vCPU / 26 GiB pod gets the identical refusal as a 1 vCPU one. Worse,
# when nothing asks, Autopilot injects a default of 1 GiB per container
# as both request AND limit, and a container over its ephemeral limit is
# EVICTED rather than told. Every package download and every `pip`
# unpack lands there. On Standard the ceiling comes from the node's boot
# disk, which is a number set below.
#
# ── WHY THE POOLS ARE LABELLED `agentos.io/node-class` ──────────────────
#
# Because `kernel/drivers/k8s_spec.py` already emits exactly that
# nodeSelector. That code has been correct and inert: no Autopilot node
# carries the label, so any spec choosing a non-default class produced a
# pod that stayed Pending for ever with no error. The pools below give
# the label something to match, and the kernel needs no change for it.
#
# ── WHY THESE THREE MACHINES ────────────────────────────────────────────
#
# Measured on the live cluster: the DaemonSets every node carries
# (Dataplane V2, fluentbit, the CSI drivers, the metadata server) request
# ~550m CPU and ~1.45 GB before any workload, and GKE's kubelet reserve
# takes its own cut on top. A pod also carries two fixed sidecars -- the
# worker at 500m/2Gi and cloud-sql-proxy at 250m/512Mi -- whatever tier
# it is.
#
#   small   e2-standard-4    3.37 vCPU / 11.6 GB usable
#   medium  e2-standard-8    7.36 vCPU / 26.1 GB usable
#   large   e2-standard-16  15.3  vCPU / 56.6 GB usable
#
# AN `xsmall` ON e2-standard-2 WAS TRIED AND REJECTED. Two vCPU leaves
# 1.93 allocatable; the DaemonSets take 550m and the two sidecars 750m,
# so 630m remains for the sandbox and the pod does not fit at all. Four
# vCPU is the floor for this pod shape, and the saving was ~$49/node.
#
# ── DISK ────────────────────────────────────────────────────────────────
#
# 100 GB on every pool, which yields ~44 GiB of ALLOCATABLE ephemeral
# storage (GKE reserves the rest for images, logs and the eviction
# threshold; measured 94.25 GiB capacity -> 43.8 GiB allocatable). That
# is four times what Autopilot permitted, on the cheapest node, and it
# is the point of the migration.
#
# ── IDEMPOTENT ──────────────────────────────────────────────────────────
#
# Every step checks before it creates, like `gke_cluster.sh`, so
# re-running is a no-op. Nothing here deletes anything.

set -euo pipefail

ENV_NAME="${1:?environment name required}"
case "$ENV_NAME" in
  "")  echo "ERROR: environment name must not be empty" >&2; exit 1 ;;
  *[!a-z0-9-]*)
    echo "ERROR: environment name '${ENV_NAME}' -- it becomes a Kubernetes" >&2
    echo "       namespace, a Postgres database name and a Secret Manager" >&2
    echo "       suffix, so it must be lowercase letters, digits and hyphens." >&2
    exit 1 ;;
esac

: "${PROJECT:?PROJECT env var required}"
: "${REGION:?REGION env var required}"

# SOURCED AND THEN CALLED, and it was only sourced. `names.sh` DEFINES
# `agentos_names`; passing the environment to the `.` does nothing but
# set $1 inside a file that never reads it. So every name the function
# exports -- NAMESPACE above all -- stayed unset here, while
# gke_cluster.sh and deploy_execution_plane.sh both call it properly.
#
# `set -u` turned that into an abort on the LAST line of the script,
# after all four pools were created: "NAMESPACE: unbound variable",
# exit 1, from a run that had done its whole job. A failure that
# reports nothing wrong and leaves everything correct is the kind that
# gets ignored the second time.
# shellcheck source=names.sh
. "$(dirname "$0")/names.sh"
agentos_names "$ENV_NAME"

# ── A NAMED SCOPE IS THE ENVIRONMENT'S OWN NAME ───────────────────────
#
# AGENTOS_POOL_SCOPE says whether this environment's pools are labelled;
# it does not get to say WHAT they are labelled. The label value is
# derived from the namespace by kernel/drivers/k8s_spec.node_environment,
# whose docstring gives the reason: "two values that must agree are two
# values that eventually do not", and the disagreement is silent -- the
# pod is admitted, scheduled against a label no node carries, and stays
# Pending for ever.
#
# So the scope is a BOOLEAN wearing the environment's name, and the only
# thing to check is that it is wearing the right one.
if [ -n "${AGENTOS_POOL_SCOPE-}" ] && [ "${AGENTOS_POOL_SCOPE}" != "$ENV_NAME" ]; then
  echo "ERROR: AGENTOS_POOL_SCOPE='${AGENTOS_POOL_SCOPE}' but this is" >&2
  echo "       environment '${ENV_NAME}'. A named scope must be the" >&2
  echo "       environment's own name -- pods derive the label they" >&2
  echo "       select from their namespace, so a different value here" >&2
  echo "       would leave every pod Pending against a label no node" >&2
  echo "       carries. Leave it blank for unlabelled pools." >&2
  exit 1
fi


DISK_GB="${DISK_GB:-100}"

# ── NEXT IS NOT TOUCHED BY THIS SCRIPT, AND THAT IS THE DESIGN ─────────
#
# `next` is the production instance of this architecture -- real people
# have real stories running on it -- and its four pools predate
# `agentos.io/env`. Every way of bringing them into the scheme modifies
# production: relabelling rolls the pool, and rolling evicts a running
# story, which costs a leg off the twelve-leg convergence ceiling for
# work that may be hours in.
#
# So it is not brought into the scheme. next keeps its bare pool names,
# no env label, no taint, and its pods keep emitting exactly the
# selector they emit today. The isolation is carried entirely by the
# NEW pools:
#
#   dev's pods   select node-class AND env=dev  -> only dev's pools
#   next's pods  select node-class only         -> would match dev's
#                                                  pools too, so dev's
#                                                  pools are TAINTED
#                                                  and repel them
#
# Asymmetric, and strictly better than symmetry here: it achieves the
# same isolation in both directions while changing nothing that is
# already running. The cost is that the taint is load-bearing -- remove
# it and next's pods can drift onto dev's machines, silently.
# ── ONE OPTION: A SCOPE NAME, BLANK BY DEFAULT ────────────────────────
#
# Everything above describes how the SECOND environment coexists with
# the first. A clean-room deployment -- somebody who cloned this repo
# and is standing up their own production -- has no first environment
# to coexist with, and should not have to understand any of it.
#
# So the whole arrangement collapses to one name:
#
#   AGENTOS_POOL_SCOPE=""      pools are `small`, `medium`, ... with no
#                              env label and no taint. Pods select on
#                              node-class alone. This is the default,
#                              and it is what a first deployment wants.
#
#   AGENTOS_POOL_SCOPE=dev     pools are `small-dev`, ... labelled
#                              agentos.io/env=dev and TAINTED with it.
#                              Pods carry the selector and a matching
#                              toleration. This is what a SECOND
#                              environment on a shared cluster needs.
#
# `next` is simply the blank case and `dev` the named one; neither is
# special-cased anywhere any more.
POOL_SCOPE="${AGENTOS_POOL_SCOPE-}"
case "$POOL_SCOPE" in
  "")  POOL_SUFFIX="" ;;
  *[!a-z0-9-]*)
    echo "ERROR: AGENTOS_POOL_SCOPE='${POOL_SCOPE}' -- it becomes a Kubernetes" >&2
    echo "       label value and a node-pool name suffix, so it must be" >&2
    echo "       lowercase letters, digits and hyphens." >&2
    exit 1 ;;
  *)   POOL_SUFFIX="-${POOL_SCOPE}" ;;
esac

# ── THE GUARD THAT MAKES THE BLANK DEFAULT SAFE ───────────────────────
#
# A blank scope means "these are the only pools in this cluster". On a
# fresh cluster that is true and needs no thought. On a cluster that
# ALREADY has pools it is false, and acting on it would create
# unsuffixed pools beside somebody else's and let two environments
# schedule onto each other -- the exact failure the taint exists to
# prevent, reintroduced by a default.
#
# Defaulting is only safe where the default can be CHECKED. So it is:
# blank plus an empty cluster proceeds silently, blank plus an occupied
# one refuses and says what to pass. Nothing is inferred.
#
# NARROWED TO THE CASE THAT IS ACTUALLY DANGEROUS. Re-running this
# script for an environment whose pools already exist creates nothing:
# `pool()` describes each one, finds it, and returns. Refusing that
# would break the one environment this change must not touch. So the
# guard fires only when a blank scope would genuinely ADD an unsuffixed
# pool to a cluster that already has others.
if [ -z "$POOL_SCOPE" ]; then
  existing=$(gcloud container node-pools list --cluster="$CLUSTER"                --region="$REGION" --project="$PROJECT"                --format="value(name)" 2>/dev/null | grep -c . || true)
  would_create=0
  for _c in platform small medium large; do
    gcloud container node-pools describe "$_c" --cluster="$CLUSTER"       --region="$REGION" --project="$PROJECT" >/dev/null 2>&1       || would_create=$((would_create + 1))
  done
  if [ "${existing:-0}" -gt 0 ] && [ "$would_create" -gt 0 ]; then
    echo "REFUSING: cluster '${CLUSTER}' already has ${existing} node pool(s)," >&2
    echo "          and AGENTOS_POOL_SCOPE is blank." >&2
    echo >&2
    echo "  A blank scope creates UNLABELLED, UNTAINTED pools, which any" >&2
    echo "  environment's pods can schedule onto. That is correct for the" >&2
    echo "  first environment in a cluster and wrong for every one after." >&2
    echo >&2
    echo "  If this is an additional environment, name it:" >&2
    echo "      AGENTOS_POOL_SCOPE=<name> $0 $*" >&2
    echo "  If you really are re-running the first one, pass" >&2
    echo "      AGENTOS_POOL_SCOPE= AGENTOS_POOL_SCOPE_BLANK_OK=1 $0 $*" >&2
    [ "${AGENTOS_POOL_SCOPE_BLANK_OK:-0}" = "1" ] || exit 1
    echo "  (AGENTOS_POOL_SCOPE_BLANK_OK=1 -- proceeding)" >&2
  fi
fi

# ── ONE WARM NODE IN NEXT, NONE ANYWHERE ELSE ──────────────────────────
#
# next's `small` floor of 1 buys latency: the common story does not wait
# for a machine to come up. That is worth ~$98/month in an environment
# people are waiting on and is not worth it in one where a slower first
# story is simply a slower first story.
#
# The pause placeholders in deploy/k8s/warm-pool.yaml are the same
# decision one level up, and dev does not apply that manifest at all.
case "$ENV_NAME" in
  next) SMALL_MIN=1 ;;
  *)    SMALL_MIN=0 ;;
esac

# ── the cluster ─────────────────────────────────────────────────────────
# REGIONAL, for the reason `gke_cluster.sh` gives: the control plane
# survives a zone. The default pool is created at the minimum size and
# then deleted at the end -- every workload targets one of the pools
# below by label, and a default pool left with nodes in it quietly hosts
# the pods nobody placed anywhere.
#
# WORKLOAD IDENTITY IS NOT OPTIONAL. Autopilot enables it for you;
# Standard does not, and every service account binding `gke_cluster.sh`
# creates depends on it. Omitting it here is a cluster whose pods cannot
# authenticate and whose failures all look like IAM.
#
# ── NOT THE AUTOPILOT ONE ───────────────────────────────────────────────
#
# `names.sh` defaults CLUSTER to `agentos`, and `agentos` IS the live
# Autopilot cluster. Without this guard the flow is: describe succeeds,
# creation is skipped as "already exists", and `node-pools create` then
# fails -- because an Autopilot cluster has no node pools to add to, and
# gcloud's error does not mention Autopilot.
#
# It must also be a DIFFERENT cluster, not a converted one: there is no
# Autopilot-to-Standard conversion, and the point of this phase is that
# the new cluster is built and tested while the old one keeps serving
# stories. Set CLUSTER to a name that does not exist yet, and carry the
# same value through every step below.
MODE="$(gcloud container clusters describe "$CLUSTER" \
          --region="$REGION" --project="$PROJECT" \
          --format='value(autopilot.enabled)' 2>/dev/null || true)"
if [ "$MODE" = "True" ]; then
  echo "ERROR: cluster '${CLUSTER}' is an AUTOPILOT cluster." >&2
  echo "       Node pools cannot be added to it, and Autopilot cannot" >&2
  echo "       be converted. This script builds a SECOND, Standard" >&2
  echo "       cluster beside it." >&2
  echo >&2
  echo "       Re-run naming the new cluster:" >&2
  echo "         CLUSTER=${CLUSTER}-std PROJECT=${PROJECT} REGION=${REGION} \\" >&2
  echo "           $0 ${ENV_NAME}" >&2
  echo >&2
  echo "       Carry the SAME CLUSTER through gke_cluster.sh and" >&2
  echo "       deploy_execution_plane.sh, or they configure the OLD" >&2
  echo "       cluster and report success." >&2
  exit 1
fi

if gcloud container clusters describe "$CLUSTER" \
     --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  echo "cluster ${CLUSTER} already exists"
else
  echo "creating Standard cluster ${CLUSTER} (this takes several minutes)"
  # `--num-nodes=0` IS REFUSED -- "Value must be greater than or equal
  # to 1". Every cluster is born with a default pool and GKE will not
  # make an empty one, so the smallest node is created here and the pool
  # is DELETED once the labelled pools exist, at the end of this script.
  # Leaving it is worse than its cost: a pod whose nodeSelector is
  # missing or misspelled lands there and RUNS, so one story per node
  # would hold everywhere except the one place nobody is looking.
  #
  # IMAGE STREAMING IS NOT ON BY DEFAULT HERE, and Autopilot had it on
  # -- verified, the `agentos` cluster reports gcfsConfig enabled true.
  # Standard does not, and the sandbox image is ~900 MB: the toolchain
  # that makes a brownfield repo buildable. Without streaming every cold
  # node pulls all of it before the story starts, which is exactly the
  # cost the image decision assumed away. Set on the cluster AND on each
  # pool below, because a pool does not inherit it.
  gcloud container clusters create "$CLUSTER" \
    --region="$REGION" \
    --project="$PROJECT" \
    --release-channel=regular \
    --enable-dataplane-v2 \
    --workload-pool="${PROJECT}.svc.id.goog" \
    --enable-ip-alias \
    --enable-image-streaming \
    --num-nodes=1 \
    --machine-type=e2-small \
    --disk-size=50
fi

gcloud container clusters get-credentials "$CLUSTER" \
  --region="$REGION" --project="$PROJECT"

# ── the pools ───────────────────────────────────────────────────────────
#
# `--min-nodes` IS A FLOOR ON TOTAL NODES, NOT ON IDLE ONES, and reading
# it the other way is the mistake this comment exists to prevent. With
# min=1 and one story running, the pool is at its floor and the
# autoscaler adds nothing; story two waits for a machine. Warm capacity
# comes from the pause pods in deploy/k8s/warm-pool.yaml, which reserve
# space a real story can preempt. min=1 here does one thing only: it
# keeps the pool from draining to zero, so the slower scale-from-zero
# path is never on a story's critical path.
#
# `GKE_METADATA` is the Workload Identity node mode. Without it the pods
# read the node's own service account from the metadata server, which is
# the escalation the two-uid boundary exists to make uninteresting.
#
# TOTAL, NOT PER ZONE, AND THE FIRST RUN GOT THIS WRONG. On a REGIONAL
# cluster `--min-nodes` and `--max-nodes` are bounds PER ZONE, and this
# cluster spans three of them. The first run therefore built a platform
# pool with a floor of six e2-standard-4 rather than two, and a small
# pool that could reach sixty. Nothing failed and nothing looked wrong;
# the bill was simply three times the number written here, which is the
# kind of error that gets found a month later on an invoice.
#
# `--total-*` says what it means and reads the same whether the cluster
# is regional or zonal, so the numbers in this file are the numbers that
# exist. The two forms cannot be combined in one command.
#
# `--num-nodes` IS ALSO REQUIRED, for the same family of reason: without
# it a pool is created at GKE's default of THREE PER ZONE and then
# drains back to its floor over the following quarter of an hour. Nine
# machines to arrive at one is only wasteful rather than wrong, but it
# also means the pool briefly holds more capacity than its own stated
# maximum allows.
#
# A POOL MAY START EMPTY where a CLUSTER may not -- the `--num-nodes=0`
# this file already refuses on the cluster is expected to be fine here,
# because the restriction exists so a cluster has somewhere to run its
# system pods and a pool carries no such duty. Expected, not verified:
# the account that wrote this has read-only access to clusters. If
# `medium` or `large` is refused for asking for zero, raise their floor
# to 1 in the calls below rather than dropping the flag -- dropping it
# restores the three-per-zone default this note is about.
# ── THE SECOND LABEL, AND WHY THE CLASS STOPPED BEING ENOUGH ───────────
#
# One cluster runs more than one environment, and each needs a pool of
# every class: the class is a SHAPE, and a dev story needs the same
# shape a next story does. So `agentos.io/node-class=small` names a kind
# of machine, of which there are now two sets, and a pod selecting only
# the class lands on whichever set has room.
#
# NOTHING FAILS WHEN IT PICKS WRONG. Both pods run. A dev story simply
# takes a whole machine that `next` is paying for, and the only trace is
# an invoice -- which is why this is a label the scheduler enforces
# rather than a naming convention nobody checks.
#
# `kernel/drivers/k8s_spec.py::node_selector` emits both keys, derived
# from the pod's namespace.
REPAIRS=""

# -- ASKED OF gcloud DIRECTLY, AND A FAILED QUESTION IS NOT AN ANSWER --
#
# This piped `--format=json` into `python3` and printed the label, with
# `|| true` on the end. Two faults in one line.
#
# `python3` IS NOT ALWAYS PYTHON. On Windows it resolves to the
# Microsoft Store stub, which prints an advert and exits non-zero, so
# the parse produced nothing at all.
#
# AND NOTHING WAS INDISTINGUISHABLE FROM NO LABEL. `|| true` swallowed
# that failure and the empty string read as "this pool is unlabelled",
# so a run which had just created four correctly labelled pools then
# reported all four as needing repair and exited non-zero telling the
# operator to go and fix what was already right. The same shape as the
# detection bug in deploy_execution_plane.sh: a question that cannot
# fail visibly answers "no".
#
# gcloud's own projection reads the key with the dots and the slash in
# it, so there is no parser and no interpreter left to be missing. A
# blank now means the label is genuinely absent, because a describe
# that fails fails the function.
env_label_of() {
  gcloud container node-pools describe "$1"     --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT"     --format="value(config.labels['agentos.io/env'])"
}

# ── AN EXISTING POOL IS CHECKED, NOT SILENTLY ACCEPTED ─────────────────
#
# `already exists` used to end the story, and for a pool whose only
# label was its class that was true. It is not true now: next's four
# pools predate `agentos.io/env`, and a pool without it is one that
# every pod will refuse to schedule on the moment the selector ships.
#
# REPORTED AND NOT REPAIRED HERE. Adding a label to a live pool through
# `node-pools update` ROLLS ITS NODES, and a script somebody ran to
# create pools should not drain production on its way past. The commands
# are printed at the end, in an order that does not interrupt anything.
ensure_env_label() {
  local name="$1" class="$2" have
  # NEXT IS EXEMPT, PERMANENTLY. Its pods do not select the env label,
  # so a pool without one is not a fault there -- it is the whole
  # arrangement. Reporting it as a repair would make this script fail
  # on a correct cluster and teach everyone to ignore the failure.
  if [ -z "$POOL_SCOPE" ]; then
    echo "  agentos.io/env -- not set, and not wanted: next is"
    echo "  deliberately outside the scheme (see the note above)"
    return 0
  fi
  have="$(env_label_of "$name")"
  if [ "$have" = "$POOL_SCOPE" ]; then
    echo "  agentos.io/env=${POOL_SCOPE} -- already set"
    return 0
  fi
  echo "  agentos.io/env is '${have:-unset}', and pods select it"
  REPAIRS="${REPAIRS}${name} ${class}
"
}

pool() {
  local class="$1" machine="$2" min="$3" max="$4"
  local name="${class}${POOL_SUFFIX}"
  if gcloud container node-pools describe "$name" \
       --cluster="$CLUSTER" --region="$REGION" \
       --project="$PROJECT" >/dev/null 2>&1; then
    echo "node pool ${name} already exists"
    ensure_env_label "$name" "$class"
    return 0
  fi
  echo "creating node pool ${name} (${machine}, ${min}-${max})"
  # ── TAINTED, AND ONLY OUTSIDE NEXT ──────────────────────────────────
  #
  # The label says who MAY use this pool. It cannot say who may not:
  # an environment whose pods carry no env selector matches every pool
  # of its class, this one included, and lands on whichever has room.
  # The taint is the other direction, and without it the isolation is
  # one-way -- dev stays off next's machines while next drifts onto
  # dev's.
  #
  # `next` creates no pools through this script, so the guard below is
  # belt and braces rather than a live branch. It is here because a
  # tainted pool in next would repel next's own pods, which have no
  # toleration, and that failure is silent: Pending, for ever.
  local taint=""
  if [ -n "$POOL_SCOPE" ]; then
    taint="--node-taints=agentos.io/env=${POOL_SCOPE}:NoSchedule"
  fi
  gcloud container node-pools create "$name" \
    ${taint:+"$taint"} \
    --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" \
    --machine-type="$machine" \
    --disk-size="$DISK_GB" --disk-type=pd-balanced \
    --workload-metadata=GKE_METADATA \
    --enable-image-streaming \
    --enable-autoupgrade --enable-autorepair \
    --num-nodes="$min" \
    --enable-autoscaling \
    --total-min-nodes="$min" --total-max-nodes="$max" \
    --location-policy=ANY \
    --node-labels="agentos.io/node-class=${class}${POOL_SCOPE:+,agentos.io/env=${POOL_SCOPE}}"
}

# `platform` FIRST, because nothing else works without it. The
# conductor, the verifier and the egress proxy have to live somewhere,
# and on Autopilot that was invisible -- Google placed them and nobody
# had to think about it. With named pools a control-plane pod carrying
# no selector lands on a STORY pool, where it takes 1 CPU and 4 GiB of a
# node sized for one execution, shares that node with a story (the
# anti-affinity rule excludes other EXECUTION pods, and a conductor is
# not one), and evicts the warm placeholders on its way in because they
# hold negative priority.
#
# TWO NODES, NOT ONE. The conductor runs two replicas and they should
# not share a machine; two e2-standard-4 give 6.74 vCPU / 23.2 GB
# against the 3.0 / 11 the control plane requests today, so one node
# failing still leaves room for both conductors and the proxies.
pool platform e2-standard-4  2  4

# TWENTY EACH, because the system ceiling is twenty and nothing tells a
# pool which size the next twenty runs will ask for. `medium` at 10 and
# `large` at 4 were sized when the ceiling was sixteen and the mix was
# assumed; a burst of large stories then stops at four with the other
# sixteen slots held open and the scheduler reporting nothing — the pods
# are Pending on capacity, which is not a state admission can see.
#
# The FLOORS are what keep this from being expensive. `small` holds one
# warm node so the common story does not wait on a node coming up;
# `medium` and `large` stay at zero and cost nothing until something
# asks for one. A ceiling is permission to grow, not a reservation.
pool small  e2-standard-4  "$SMALL_MIN" 20
pool medium e2-standard-8  0 20
pool large  e2-standard-16 0 20

# ── the default pool goes ───────────────────────────────────────────────
#
# It exists only because GKE refuses to create a cluster without one. It
# carries no `agentos.io/node-class` label, so nothing this platform
# schedules ever asks for it -- and that is the hazard rather than the
# saving: a pod with a missing or misspelled selector lands there and
# runs, sharing a machine with whatever else made the same mistake.
#
# LAST, and only once the real pools are up: deleting it earlier would
# leave the cluster with nowhere to put anything.
if gcloud container node-pools describe default-pool \
     --cluster="$CLUSTER" --region="$REGION" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "deleting the default pool, which nothing selects"
  gcloud container node-pools delete default-pool \
    --cluster="$CLUSTER" --region="$REGION" --project="$PROJECT" --quiet
fi

# ── POOLS THAT PREDATE `agentos.io/env` ────────────────────────────────
#
# Printed rather than applied, and the ORDER is the whole content of
# this block. The selector ships in the kernel image; the labels live on
# the nodes. Deploy the image first and every pod in this environment
# selects a label no node carries -- they are admitted, they schedule
# against nothing, and they sit Pending for ever, which is the one
# failure state that reports nothing on its own.
#
# TWO COMMANDS PER POOL, AND BOTH ARE NEEDED.
#
#   `kubectl label nodes` reaches the nodes that exist RIGHT NOW,
#   immediately, and disturbs nothing -- no drain, no eviction, no
#   restart. A story running on that node keeps running.
#
#   `node-pools update` is what makes it true of nodes that do not
#   exist yet. Without it the pool's own config still says one label,
#   so the next node the autoscaler adds comes up unlabelled and the
#   first story scheduled onto it is Pending. It ROLLS THE POOL, which
#   is why it is second and why it wants an idle moment.
#
# Between the two the environment is fully working, so the gap can be
# as long as it needs to be.
if [ -n "$REPAIRS" ]; then
  echo
  echo "────────────────────────────────────────────────────────────────"
  echo "POOLS WITHOUT agentos.io/env=${POOL_SCOPE}, AND PODS SELECT IT."
  echo
  echo "Run these BEFORE deploying an image built after this change."
  echo "Step 1 disturbs nothing. Step 2 rolls the pool -- do it when"
  echo "that pool is idle. The environment works between them."
  echo
  echo "  ── 1. the nodes that exist now (no disruption) ──"
  printf '%s' "$REPAIRS" | while read -r name class; do
    [ -n "$name" ] || continue
    echo "  kubectl label nodes --overwrite \\"
    echo "    -l cloud.google.com/gke-nodepool=${name} \\"
    echo "    agentos.io/env=${POOL_SCOPE}"
  done
  echo
  echo "  ── 2. the nodes that do not exist yet (rolls the pool) ──"
  printf '%s' "$REPAIRS" | while read -r name class; do
    [ -n "$name" ] || continue
    echo "  gcloud container node-pools update ${name} \\"
    echo "    --cluster=${CLUSTER} --region=${REGION} --project=${PROJECT} \\"
    echo "    --node-labels=agentos.io/node-class=${class},agentos.io/env=${POOL_SCOPE}"
  done
  echo
  echo "Then:  kubectl get nodes -L agentos.io/env -L agentos.io/node-class"
  echo "────────────────────────────────────────────────────────────────"
  echo
  echo "Exiting non-zero: this environment is not ready for an image"
  echo "carrying the environment selector until the above is done."
  exit 1
fi

echo
echo "pools ready on ${CLUSTER}. Carry CLUSTER=${CLUSTER} through every"
echo "step below -- each one defaults to the OLD cluster without it."
echo
echo "  1. CLUSTER=${CLUSTER} scripts/infra/gke_cluster.sh ${ENV_NAME}"
echo "       identities, bucket, secrets. Idempotent, and the IAM half"
echo "       is already done: the Workload Identity members read"
echo "       ${PROJECT}.svc.id.goog[<namespace>/<ksa>] and name no"
echo "       cluster, so the existing bindings cover this one too --"
echo "       provided the namespace and the service account names are"
echo "       unchanged."
echo
echo "  2. NAMESPACE=${NAMESPACE} ENV_NAME=${ENV_NAME} \\"
echo "       python3 scripts/infra/render.py \\"
echo "       deploy/k8s/warm-pool.yaml | kubectl apply -f -"
echo "       NEXT ONLY. The placeholders buy latency by holding a"
echo "       machine open, which is the cost dev deliberately does not"
echo "       pay -- its small pool floors at zero for the same reason."
echo "       ENV_NAME because the placeholders select a pool too, and a"
echo "       placeholder holding the OTHER environment's node open is"
echo "       worse than no placeholder at all."
echo "       the PriorityClass and the two pause Deployments. RENDERED,"
echo "       not applied directly: the manifest carries \${NAMESPACE}"
echo "       and kubectl would send that string to the API verbatim."
echo "       render.py rather than envsubst, which turns an unset"
echo "       variable into an empty one instead of failing."
echo
echo "  3. CLUSTER=${CLUSTER} scripts/infra/deploy_execution_plane.sh ${ENV_NAME}"
echo "       rebuilds the k8s Secrets from Secret Manager. Nothing is"
echo "       re-minted; no secret value is typed by hand."
