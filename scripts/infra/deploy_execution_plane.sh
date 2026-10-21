#!/usr/bin/env bash
#
# deploy_execution_plane.sh <env> — put the conductor and the egress
# proxy on the cluster, at a named image.
#
# THIS IS THE STEP THAT DID NOT EXIST. The execution plane — admission,
# dispatch, legs, verification, the ledger drain — was started only by
# `docker exec -d` from infra/e0/console.sh. No Cloud Build step
# deployed it and no AGENTOS_* variable appeared in any build config:
# Cloud Run shipped the kernel's schema and its tool surface, and
# nothing that runs a story.
#
# ── the secret, and why it is synced rather than mounted ────────────────
#
# The conductor reads its DSN and model keys from the environment. On
# Cloud Run that is `--set-secrets`; on GKE the equivalent is a Secret in
# the namespace, and something has to put it there.
#
# It is READ FROM SECRET MANAGER AT DEPLOY TIME by the build's own
# identity and written into the namespace, rather than baked into the
# manifest or the image. The values never enter git, never enter a layer,
# and rotating one is a redeploy rather than an image rebuild.
#
# The alternative — the app calling Secret Manager itself through
# Workload Identity — is better and is not what the app does today: it
# reads os.environ. Changing that is a change to the app, not to a
# deploy script, so this matches the shape that exists.
set -euo pipefail

ENV_NAME="${1:?environment name required}"
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
: "${IMAGE:?IMAGE env var required — the execution plane image to run}"

# THE SANDBOX'S IMAGE IS NOT THE KERNEL'S. The worker runs the kernel's
# image because it serves workspace operations; the sandbox runs the
# image the work and its checks happen in. Defaulted from the build's
# tag so a deploy names a specific, pullable build rather than a moving
# tag -- `render.py` refuses an unset variable, which is the point.
SANDBOX_BASE_IMAGE="${SANDBOX_BASE_IMAGE:-}"

# WHICH BUILD IS RUNNING, as the logs see it. `obs/fields.process_fields`
# reads BUILD_SHA and logs the string "unset" without it -- which is what
# every line from both Deployments said, so no log line could be
# attributed to a build and "is the fix live?" had no answer but a
# rollout timestamp beside a push time.
#
# DERIVED FROM THE IMAGE, not passed alongside it. Two variables that can
# disagree eventually do, and the image tag IS what is running -- a
# separately-passed sha would be the one thing in the deploy that could
# be stale while looking right. Handles both `repo:tag` and
# `repo@sha256:hex`; `##*:` takes what follows the last colon either way.
BUILD_SHA="${BUILD_SHA:-${IMAGE##*:}}"

PROXY_IMAGE="${PROXY_IMAGE:-ubuntu/squid:latest}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# The same names gke_cluster.sh created things under. Sourced rather
# than restated: the bucket in particular is read by the conductor and
# written into every criterion's initContainer, and two files
# disagreeing about it fails as "every criterion could not start".
# shellcheck source=scripts/infra/names.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/names.sh"
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


echo "deploying ${IMAGE} to ${NAMESPACE} on ${CLUSTER}"

gcloud container clusters get-credentials "$CLUSTER" \
  --region="$REGION" --project="$PROJECT"

# ── ARE THIS CLUSTER'S POOLS MIGRATED? ASK, DO NOT BE TOLD ─────────────
#
# The two halves of the environment selector ship on different
# schedules. The SELECTOR is in the kernel image. The LABEL is on the
# nodes, put there by a person running `node-pools update`, which rolls
# a pool and therefore waits for a quiet moment.
#
# WHAT THIS PROTECTS. Deploy an image that always emits the selector
# into a cluster whose nodes predate the label and nothing errors: pods
# are admitted, scheduled against a label no node carries, and sit
# Pending. Meanwhile the rollout has begun retiring the pods that were
# working. An environment serving real stories would lose them to a
# deploy that reported no fault.
#
# SO THE DEPLOY LOOKS, rather than taking a flag. A flag can be set
# wrong, and the cost of setting it wrong falls on whichever
# environment is live. The cluster already knows the answer, it cannot
# disagree with itself, and it flips over on its own the moment someone
# labels the pools -- there is no second step to forget.
#
# THE PLATFORM POOL IS WHAT IS ASKED ABOUT, not the story pools.
# `medium` and `large` sit at zero nodes most of the time, so an empty
# answer from those means nothing. The platform pool has a floor
# precisely because the conductor has to live somewhere.
# WHICH ENVIRONMENTS MAY ANSWER "no". Exactly one: `next`, and
# PERMANENTLY rather than until someone gets round to it. It is the
# production instance of this architecture, its four pools predate
# `agentos.io/env`, and bringing them into the scheme means rolling
# live pools and evicting running stories. It stays outside; the
# isolation is carried by a taint on the new pools instead.
#
# Every other environment's pools are created by gke_standard_pools.sh
# carrying both labels, so "no" from one of those is a broken question
# rather than an answer -- see the refusal below.
# DERIVED FROM THE SCOPE, NOT FROM A NAME. An environment with a BLANK
# AGENTOS_POOL_SCOPE is declaring that its pools are unlabelled -- it is
# either the only environment in its cluster, or (like `next`) the one
# that predates the scheme. "No" is then a true answer and the deploy
# proceeds unscoped.
#
# An environment that NAMED a scope has pools created with both labels
# by gke_standard_pools.sh, so "no" from one of those is a broken
# question rather than an answer, and the refusal below fires.
if [ -z "${AGENTOS_POOL_SCOPE-}" ]; then
  SCOPE_MAY_BE_OFF="yes"
else
  SCOPE_MAY_BE_OFF="no"
fi

echo "asking ${CLUSTER} whether ${ENV_NAME}'s pools carry the env label"
PLATFORM_NODES="$(kubectl get nodes \
  -l "agentos.io/env=${ENV_NAME},agentos.io/node-class=platform" \
  -o name 2>/dev/null | grep -c . || true)"
if [ "${PLATFORM_NODES:-0}" -ge 1 ]; then
  NODE_POOL_SCOPED="on"
  PLATFORM_POOL_SELECTOR="{agentos.io/node-class: platform, agentos.io/env: ${ENV_NAME}}"
  # THE OTHER HALF OF THE PAIR. The pool is tainted so the other
  # environment's pods -- which carry no env selector and therefore
  # match every pool of their class -- cannot land on it. Our own
  # control-plane pods need to be let through.
  PLATFORM_POOL_TOLERATIONS="tolerations: [{key: agentos.io/env, operator: Equal, value: ${ENV_NAME}, effect: NoSchedule}]"
  echo "  yes -- ${PLATFORM_NODES} platform node(s). Pods will select"
  echo "  agentos.io/env=${ENV_NAME} as well as their class."
elif [ "$SCOPE_MAY_BE_OFF" = "yes" ]; then
  # NOT AN ERROR, AND THAT IS THE POINT. This is `next` before its
  # pools are migrated, and the behaviour is exactly what it was before
  # this feature existed: one label, the class.
  # `scripts/infra/gke_standard_pools.sh ${ENV_NAME}` prints the two
  # commands that migrate a pool when somebody wants to.
  NODE_POOL_SCOPED="off"
  PLATFORM_POOL_SELECTOR="agentos.io/node-class: platform"
  # A YAML COMMENT, so the line renders to nothing. An empty
  # `tolerations: []` would behave identically to Kubernetes, and the
  # point is that the manifest is byte-for-byte what it was before any
  # of this existed.
  PLATFORM_POOL_TOLERATIONS="# untainted pools: no toleration needed"
  echo "  no -- this environment's pools are not migrated, so pods"
  echo "  select their class only, exactly as they did before."
else
  # ── THE FALLBACK IS SAFE IN ONE DIRECTION ONLY ──────────────────────
  #
  # Everything about the detection above fails to `off`: no matches, no
  # credentials, an RBAC refusal, a transient API error -- `grep -c` on
  # empty input prints 0 and `|| true` swallows the rest. For `next`
  # that is exactly right, because `off` IS next's correct answer and
  # the worst case is the behaviour it already has.
  #
  # FOR EVERY OTHER ENVIRONMENT IT IS THE WRONG WAY ROUND. An
  # environment whose pools were built by gke_standard_pools.sh carries
  # the label by construction, so a `no` here is never the truth -- it
  # is a broken query. And deploying unscoped on that answer is the
  # precise failure this label exists to prevent: a pod selecting only
  # `node-class=small` matches `small` AND `small-dev`, so it lands on
  # whichever has room, which is usually the other environment's
  # machine. Nothing fails. A story runs on hardware somebody else is
  # paying for, and the only trace is an invoice.
  #
  # So the safe default is inverted here: refuse. A blocked deploy is
  # visible in thirty seconds; the other outcome is visible in a month.
  echo "ERROR: ${ENV_NAME}'s pools should carry agentos.io/env=${ENV_NAME}" >&2
  echo "       and no platform node reports it." >&2
  echo >&2
  echo "       This is not the 'not migrated yet' case -- only next may" >&2
  echo "       be unscoped, because only next's pools predate the" >&2
  echo "       label. For ${ENV_NAME} the pools are created labelled," >&2
  echo "       so a 'no' here means the QUESTION failed, not that the" >&2
  echo "       answer is no: missing credentials, an RBAC refusal, or" >&2
  echo "       the pools were never created." >&2
  echo >&2
  echo "       Deploying anyway would emit pods selecting their class" >&2
  echo "       alone, which matches EVERY environment's pool of that" >&2
  echo "       class -- so ${ENV_NAME}'s stories would run on another" >&2
  echo "       environment's machines, silently." >&2
  echo >&2
  echo "       kubectl get nodes -L agentos.io/env -L agentos.io/node-class" >&2
  echo "       CLUSTER=${CLUSTER} scripts/infra/gke_standard_pools.sh ${ENV_NAME}" >&2
  exit 1
fi
export NODE_POOL_SCOPED

# ── the secret ──────────────────────────────────────────────────────────
# `create --dry-run | apply` rather than `create`: idempotent, and it
# updates the values on a rotation instead of failing because the Secret
# already exists.
#
# One name per variable, because the mapping is not mechanical: `next`
# borrows prod's model keys and gateway token while its DSN and its
# crypto keys are its own. Spelling it out is what makes that visible.
# `|| true` SO A MISSING SECRET IS EMPTY, NOT A SILENT ABORT.
#
# Without it, `gcloud` exits 1 on a secret that does not exist, the
# command substitution around this call fails, and `set -euo pipefail`
# ends the script THERE -- with no message, because the failure is an
# assignment rather than a command. Build f999acc9 died exactly that
# way, one line after "secret/conductor-secrets unchanged", and the log
# said only `step exited with non-zero status: 1`.
#
# It also made this file's own error messages unreachable. Every caller
# below that tests `[ -z "$X" ]` and prints a sentence naming the repair
# could never run: the abort came first. The check is the thing that
# should speak, which is the argument the CLOUDSQL_IP block already
# makes a hundred lines down.
#
# The first caller to read a secret that MIGHT be absent is D1's
# sqlproxy pair, whose whole design is "absent means off". Every earlier
# caller reads one that must exist and now fails with its own words.
secret_value() {
  gcloud secrets versions access latest --secret="$1" \
    --project="$PROJECT" 2>/dev/null || true
}

echo "reading secrets"
# ── A DSN OF ITS OWN, BECAUSE THE SHAPE DIFFERS BY SUBSTRATE ────────────
#
# `next-database-url` is the CLOUD RUN form — a Unix socket under
# /cloudsql, which is what `--add-cloudsql-instances` provides and what
# the control panel, council worker, git engine and management service
# all need. There is no such socket on GKE: the conductor reaches Cloud
# SQL through the proxy sidecar in its own pod, on 127.0.0.1:5432.
#
# One secret cannot be both. It briefly was, holding the sidecar form,
# and the first Cloud Run service to start said:
#
#   RuntimeError: Could not connect to database after 5 attempts:
#   [Errno 111] Connection refused
#
# — dialling 127.0.0.1 inside a container that has no proxy beside it.
# dev never meets this because dev has no conductor on Kubernetes;
# `next` is the first environment to run both shapes at once.
#
# So the conductor gets its own name, beside the migrate job's, which
# has needed a different shape all along for the same reason.
DATABASE_URL="$(secret_value "$(agentos_secret database-url-conductor)")"
# ── NOT PROD'S GATEWAY TOKEN ────────────────────────────────────────────
# This read `openclaw-gateway-token-prod`, on the same reasoning that
# lets `next` borrow prod's MODEL keys. The two are not alike. A model
# key buys quota against a metered API; a gateway token is a credential
# to a STATEFUL service that production depends on -- holding it means
# being able to create and delete agents, and push files into agent
# workspaces, on the gateway prod is running on.
#
# `next` HAS ITS OWN GATEWAY NOW -- openclaw-next at ${GATEWAY_VM_IP}, built
# from agent_os_openclaw infra/terraform/envs/next -- so this resolves to a
# real token rather than to nothing. An earlier version of this comment
# said next had no gateway and did not need one; that was true when it
# was written and stopped being true the moment the VM came up, which is
# exactly how a comment starts lying.
OPENCLAW_AUTH_TOKEN="$(secret_value "$(agentos_secret openclaw-gateway-token)")"
# `-next` COPIES, not prod's directly. seed-next-secrets.sh copies
# the values so the quota is shared, and reading prod's name here
# instead would mean a rotation there changes what this
# environment runs against mid-test.
GEMINI_API_KEY="$(secret_value "$(agentos_secret gemini-api-key)")"
CLAUDE_API_KEY="$(secret_value "$(agentos_secret anthropic-api-key)")"
CREDENTIAL_ENCRYPTION_KEY="$(secret_value "$(agentos_secret credential-encryption-key)")"
AGENT_TOKEN_SIGNING_KEY="$(secret_value "$(agentos_secret agent-token-signing-key)")"

# ── WHERE THE CONTROL PANEL IS ──────────────────────────────────────────
# NOT A SECRET -- a public https URL -- but it lives in Secret Manager
# because `sync-url` already maintains it there: that step reads the
# deployed Cloud Run service's URL after every control-panel deploy and
# writes it to this name, which is how the gateway VM learns the address
# too. Reading the same name here means one source for "where is the
# control panel", not a second copy in a substitution somewhere.
#
# THE CONDUCTOR NEEDS IT TO CLONE A PRIVATE REPOSITORY. The execution
# pod's credential helper asks the control panel's broker for a
# per-story GitHub token, and the broker's address is derived from this
# (kernel/workspace/broker.py). Without it the helper cannot be built,
# the clone runs unauthenticated, and the pod dies in
# `workspace-compose` reporting that git could not read a username from
# a terminal -- which is what executions 6, 7 and 8 did.
#
# Exported below and required by render.py, so a missing value fails the
# DEPLOY rather than producing a conductor that starts, looks healthy,
# and cannot compose a workspace.
CONTROL_PANEL_URL="$(secret_value "$(agentos_secret control-panel-url)")"
if [ -z "$CONTROL_PANEL_URL" ]; then
  echo "ERROR: control-panel-url-${ENV_NAME} is empty." >&2
  echo "       The conductor derives the git credential broker from it," >&2
  echo "       so deploying now gives you an execution plane that runs" >&2
  echo "       and cannot clone a private repository." >&2
  echo "       'sync-url' in cloudbuild-${ENV_NAME}.yaml populates it" >&2
  echo "       after the control panel deploys." >&2
  exit 1
fi

# REFUSED, NOT DEPLOYED WITH A HOLE. bootstrap creates these as
# placeholders and a person fills the DSN in; deploying with an empty one
# produces a conductor that starts, fails every tick on a connection
# error, and logs it — which reads as a broken cluster rather than an
# unfinished setup step.
if [ -z "$DATABASE_URL" ] || [ "$DATABASE_URL" = "PLACEHOLDER" ]; then
  echo "ERROR: ${ENV_NAME}-database-url-conductor is empty or missing." >&2
  echo "       It is the SIDECAR form -- 127.0.0.1:5432 -- and distinct" >&2
  echo "       from ${ENV_NAME}-database-url, which is the Cloud Run" >&2
  echo "       socket form. See the comment above." >&2
  echo "       bootstrap creates it; a person puts the real DSN in." >&2
  echo "       Deploying now would give you a conductor that starts and" >&2
  echo "       fails every tick on a connection error." >&2
  exit 1
fi

# ANTHROPIC_API_KEY, NOT CLAUDE_API_KEY, is the key name inside this
# secret. The execution pod asks for it by the name the harness reads:
# `k8s_spec.MODEL_KEYS` is ("ANTHROPIC_API_KEY", "GEMINI_API_KEY") and
# `model_key_env()` builds a secretKeyRef on exactly those. Written here
# as CLAUDE_API_KEY, that reference resolved to nothing.
#
# AND IT FAILED SILENTLY, because those refs are `optional: True` --
# deliberately, so a deployment holding only one vendor's key still
# starts. The cost of that choice is this: every pod came up with no
# Anthropic key at all, in dev, feature and next alike, and the fault
# surfaced later inside whichever leg used the Anthropic adapter.
# GEMINI_API_KEY matched on both sides, so Gemini legs worked throughout
# and hid it.
#
# The GCP secret is asked for by its CANONICAL name,
# `anthropic-api-key`; names.sh maps that to the legacy
# `claude-api-key-<env>` that exists in our project and leaves it
# unsuffixed for a single-environment install.
# Renaming that touches Secret Manager, terraform and the control plane
# together and is Rule 1 of the OSS release; this line is the functional
# half and is correct on its own.
kubectl create secret generic conductor-secrets \
  --namespace="$NAMESPACE" \
  --from-literal=DATABASE_URL="$DATABASE_URL" \
  --from-literal=OPENCLAW_AUTH_TOKEN="$OPENCLAW_AUTH_TOKEN" \
  --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY" \
  --from-literal=ANTHROPIC_API_KEY="$CLAUDE_API_KEY" \
  --from-literal=CREDENTIAL_ENCRYPTION_KEY="$CREDENTIAL_ENCRYPTION_KEY" \
  --from-literal=AGENT_TOKEN_SIGNING_KEY="$AGENT_TOKEN_SIGNING_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── D1: THE EXECUTION POD'S OWN DATABASE ────────────────────────────────
#
# Phase D moves the model leg into the execution pod, and `leg_lock` is a
# Postgres advisory lock held for the whole leg -- it dies with its
# connection, which is what makes a crashed worker release a run instead
# of stranding it. So the pod needs a connection of its own, and it gets
# one through a `cloud-sql-proxy` sidecar carrying a dedicated key.
#
# TWO SECRETS, SYNCED THE SAME WAY conductor-secrets is, and read by the
# build's own identity: the GSA key the proxy authenticates with, and the
# password of the NARROW Postgres role -- advisory locks, its own
# execution row, step_journal, and not the ledger. Neither value enters
# git, a layer, or a manifest.
#
# ABSENT MEANS OFF, NOT BROKEN, and that is the difference between this
# and the DSN check above. A conductor with no database is a conductor
# that cannot work; an execution pod with no database is the pod that has
# been running stories all along, with the leg still driven from the
# conductor. Refusing the deploy would block every unrelated change until
# a person had minted a key. D2, which needs the connection, is where
# this becomes required.
SQLPROXY_KEY="$(secret_value "$(agentos_secret execution-sqlproxy-key)")"
LEG_DB_PASSWORD="$(secret_value "$(agentos_secret execution-db-password)")"
if [ -n "$SQLPROXY_KEY" ] && [ -n "$LEG_DB_PASSWORD" ]; then
  kubectl create secret generic execution-sqlproxy-key \
    --namespace="$NAMESPACE" \
    --from-literal=key.json="$SQLPROXY_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
  # NOT `agentos_leg_${ENV_NAME}`, and the suffix was here until
  # D2-prep measured it. Every OTHER ${ENV_NAME} in this script names
  # a GCP SECRET, which is environment-scoped because one project
  # holds every environment. A DATABASE ROLE is not: each environment
  # has its own database, `kernel_roles_grants_seed.sql:32` creates
  # the sibling `agentos_worker` unsuffixed, and
  # `kernel_execution_pod_leg_role.sql:75` creates this one the same
  # way. The suffix was copied off the secret names one line up.
  #
  # IT WOULD NOT HAVE FAILED HERE. `k8s_sqlproxy.DB_KEYS` maps this
  # key to AGENTOS_LEG_DB_USER and the pod builds its DSN from it, so
  # the first symptom would have been every execution pod failing to
  # authenticate against a role that does not exist -- at D2, in the
  # cluster, looking like a broken sqlproxy.
  # -- THE LEG LOGIN IS PER ENVIRONMENT --------------------------------
  #
  # This said `agentos_leg`, and the migration creating that role argues
  # for the bare name: "each environment has its own database". THAT
  # ASSUMED ONE ENVIRONMENT PER INSTANCE. A Postgres LOGIN role is
  # CLUSTER-wide with ONE password, so two environments on one instance
  # sharing `agentos_leg` share the credential their execution pods
  # authenticate with -- the one credential that would have stayed
  # shared by omission after every other was separated on purpose.
  #
  # `next` KEEPS THE BARE NAME: its execution-db secret already holds
  # it, and rotating a live environment's leg credential to tidy a name
  # is not worth the risk. Everywhere else gets `agentos_leg_<env>`,
  # which migration kernel_leg_role_is_per_environment.sql creates as a
  # MEMBER of `agentos_leg` -- inheriting every grant rather than
  # keeping a second copy of them to drift.
  # THE BARE NAME IS CORRECT FOR A LONE ENVIRONMENT and wrong for any
  # environment sharing a Postgres INSTANCE with another, because a
  # LOGIN role is cluster-wide and carries one password. A blank pool
  # scope is the same declaration in both places -- "nobody else is
  # here" -- so it decides this too, and an operator who needs to say
  # otherwise says it outright.
  LEG_ROLE="${AGENTOS_LEG_ROLE:-}"
  if [ -z "$LEG_ROLE" ]; then
    if [ -z "${AGENTOS_POOL_SCOPE-}" ]; then
      LEG_ROLE="agentos_leg"
    else
      LEG_ROLE="agentos_leg_${ENV_NAME}"
    fi
  fi
  echo "execution pods log in as ${LEG_ROLE}"
  kubectl create secret generic execution-db \
    --namespace="$NAMESPACE" \
    --from-literal=username="$LEG_ROLE" \
    --from-literal=password="$LEG_DB_PASSWORD" \
    --from-literal=dbname="$DATABASE" \
    --dry-run=client -o yaml | kubectl apply -f -
  EXECUTION_SQL_INSTANCE="$CLOUDSQL_INSTANCE"
  echo "execution pods get their own database connection"
else
  # A WORD AND NOT A BLANK: render.py refuses an empty substitution,
  # because an empty value is indistinguishable from the rendering bug it
  # exists to catch. kernel/drivers/k8s_sqlproxy.from_env reads this.
  EXECUTION_SQL_INSTANCE="off"
  echo "execution-sqlproxy-key-${ENV_NAME} or" \
       "execution-db-password-${ENV_NAME} is unset:" \
       "execution pods get no database (the leg stays in the conductor)"
fi

# ── WHERE CLOUD SQL ACTUALLY IS ─────────────────────────────────────────
# The namespace is default-deny egress and the execution pod's only hole
# is squid on 3128. The proxy's ADMIN calls go through squid --
# .googleapis.com is on the allowlist -- but the data connection is a raw
# TLS dial to the instance on 3307, which no HTTP proxy can carry, so
# `execution-egress-to-sql` admits that one address and that one port.
#
# THE PRIMARY ADDRESS, because the proxy dials the primary unless told
# `--private-ip`. The two are one decision: changing the sidecar's flag
# without changing this leaves a proxy that hangs rather than fails.
#
# Rendered ALWAYS, even when the connection is off, because render.py
# requires every name in the manifest -- and a policy admitting one port
# to one address grants nothing while no container dials it.
CLOUDSQL_IP="$(gcloud sql instances describe "${CLOUDSQL_INSTANCE##*:}" \
  --project="$PROJECT" --format=json 2>/dev/null |
  python3 -c 'import json,sys
try:
    addresses = json.load(sys.stdin).get("ipAddresses", [])
except ValueError:
    addresses = []
primary = [a["ipAddress"] for a in addresses if a.get("type") == "PRIMARY"]
print(primary[0] if primary else "")' || true)"
# `|| true` because `set -o pipefail` makes a failed `describe` fail the
# whole assignment, and `set -e` would then abort with no message at all
# -- the check below is the one that should speak.
# FATAL ONLY WHEN THE CONNECTION IS ON. The comment above is right that
# the policy is rendered always -- render.py requires every name -- but
# the ERROR was unconditional, so a deployment with the sqlproxy OFF
# failed for an address nothing was going to dial. That is a deploy
# blocked by a permission the running system does not need, which is the
# same shape as the `off` sentinel two blocks up exists to avoid.
#
# With the proxy off there is no container to reach Cloud SQL, so a
# policy naming an unroutable placeholder grants exactly what a policy
# naming the real address grants: nothing. With it ON, a blank or wrong
# address is a HANG rather than a refusal -- default-deny drops -- so it
# stays fatal.
if [ -z "$CLOUDSQL_IP" ]; then
  if [ "$EXECUTION_SQL_INSTANCE" = "off" ]; then
    CLOUDSQL_IP="127.0.0.1"
    echo "cloudsql address: UNREADABLE, and the proxy is off --"
    echo "  the policy names a placeholder no container dials."
    echo "  Needs cloudsql.instances.get before turning it on."
  else
    echo "ERROR: could not read the primary address of ${CLOUDSQL_INSTANCE}." >&2
    echo "       The execution pod's sqlproxy is ON and its NetworkPolicy" >&2
    echo "       pins egress to that address; rendering it blank would" >&2
    echo "       apply a policy naming nothing, and a default-deny drop" >&2
    echo "       HANGS rather than refusing." >&2
    echo "       The deploying identity needs cloudsql.instances.get." >&2
    exit 1
  fi
fi

# ── the manifests ───────────────────────────────────────────────────────
# Falls back to the repo's `:latest` when the caller named no build, so
# a hand-run deploy still resolves to something the cluster can pull.
SANDBOX_BASE_IMAGE="${SANDBOX_BASE_IMAGE:-${SANDBOX_BASE_REPO}:latest}"
echo "sandbox base image: ${SANDBOX_BASE_IMAGE}"

# ── the digest, which only the thing that pushed the image knows ────────
#
# `contracts.base_digest` has existed since F1 and every READER prefers
# it, but no INSERT path ever wrote one -- so every contract named a
# moving tag and every run logged "the contract names an image by TAG,
# not a digest; the Verifier may not have run the image that was
# approved". Executions 205 and 207-210 all carried it.
#
# NOTHING IN `kernel/` CAN ANSWER THIS. There is no registry client in
# the tree, the Kubernetes driver talks to the API server rather than a
# registry, and the Docker route is closed on purpose (preflight.sh:
# "Do NOT add IMAGES=1 to the socket proxy"). So the deployment declares
# it, which is the argument `kernel/base_image.py` already makes about
# the NAME: a fact only the local environment declares is not a fact.
#
# UNRESOLVED TRAVELS AS THE WORD `off`, exactly as EXECUTION_SQL_INSTANCE
# does above and for the same reason: `render.py` refuses a variable that
# is unset OR EMPTY -- `if not env.get(name)` -- because rendering one
# blank would apply a manifest naming nothing. So "no digest" cannot be
# the empty string; it has to be a value.
#
# It must not fail the deploy either. `image_digest.resolve` treats the
# sentinel as "not declared, and here is why": contracts stay unpinned,
# the parity warning keeps firing, and behaviour is exactly today's. A
# missing registry permission blocking every unrelated change to `next`
# is a worse outcome than the warning we have lived with all along.
#
# `resolve` also refuses a digest whose REPOSITORY does not match the
# image, so a stale value cannot silently dispatch the wrong thing.
if [ -z "${SANDBOX_BASE_DIGEST:-}" ]; then
  _digest="$(gcloud artifacts docker images describe \
      "${SANDBOX_BASE_IMAGE}" --format='value(image_summary.digest)' \
      2>/dev/null || true)"
  if [ -n "${_digest}" ]; then
    SANDBOX_BASE_DIGEST="${SANDBOX_BASE_REPO}@${_digest}"
  else
    SANDBOX_BASE_DIGEST="off"
  fi
fi
if [ "${SANDBOX_BASE_DIGEST}" = "off" ]; then
  echo "sandbox base digest: UNRESOLVED -- contracts stay unpinned and" \
       "the parity warning keeps firing. Needs artifactregistry.reader."
else
  echo "sandbox base digest: ${SANDBOX_BASE_DIGEST}"
fi

# -- WHICH KIND OF CLUSTER IS THIS ---------------------------------------
# Autopilot and Standard disagree about exactly one line of
# conductor.yaml, and they disagree at ADMISSION rather than at schedule
# time. Autopilot's Warden refuses a nodeSelector on any key outside its
# own allowlist, so a manifest carrying `agentos.io/node-class` is not
# applied AT ALL: the deploy goes red and the cluster keeps running the
# previous image, having changed nothing. That pin is also what keeps
# control-plane pods off the story pools on Standard, so dropping it is
# not an option either -- see the long note on the conductor Deployment.
#
# ASKED, NOT ASSUMED, and a cluster we cannot ask about stops the
# deploy. Guessing Standard reproduces the red deploy on Autopilot;
# guessing Autopilot puts the conductor on a story node on Standard,
# where it contends with the execution it is serving and evicts the warm
# placeholders. The second is the same defect arriving silently, which
# makes it the worse of the two. Neither is a default worth having.
if _autopilot="$(gcloud container clusters describe "$CLUSTER"       --region="$REGION" --project="$PROJECT"       --format='value(autopilot.enabled)' 2>/dev/null)"; then
  case "${_autopilot}" in
    True|true)
      # REFUSED, WHERE THIS ONCE DEGRADED. The old branch pinned nothing
      # and carried on, which was right while Autopilot was the target
      # and a pool pin was meaningless there. It is wrong now: every
      # EXECUTION pod emits `agentos.io/node-class`, Autopilot's Warden
      # refuses that key at admission, and so a deploy here would go
      # green and then fail every story at dispatch -- the worst of the
      # two failures, because the cluster looks healthy.
      echo "cluster ${CLUSTER} is Autopilot, and this platform cannot run" \
           "on one. One story per machine needs a nodeSelector on" \
           "agentos.io/node-class, and Autopilot's Warden refuses any" \
           "selector key outside its own allowlist. Deploy to a Standard" \
           "cluster -- see scripts/infra/gke_standard_pools.sh." >&2
      exit 1
      ;;
    *)
      # DECIDED ABOVE, by asking the cluster. On a migrated cluster
      # this is a YAML flow mapping carrying both keys -- it
      # substitutes into a single indented line under `nodeSelector:`
      # in three places, and a second block-style key would land at
      # the wrong indent. On an unmigrated one it is the single
      # class key this file has always used.
      PLATFORM_NODE_SELECTOR="${PLATFORM_POOL_SELECTOR}"
      PLATFORM_TOLERATIONS="${PLATFORM_POOL_TOLERATIONS}"
      echo "cluster ${CLUSTER} is Standard: control-plane pods pin to the platform pool"
      ;;
  esac
else
  echo "cannot tell whether ${CLUSTER} is Autopilot or Standard, and the"        "answer decides one line of conductor.yaml. Needs"        "container.clusters.get on ${PROJECT}." >&2
  exit 1
fi

export PLATFORM_NODE_SELECTOR PLATFORM_TOLERATIONS
# -- THE ALLOWLIST'S FINGERPRINT ------------------------------------------
# Squid reads its configuration at start and never again, and the
# ConfigMap holding it is mounted into a pod whose spec is otherwise
# identical from one deploy to the next. So an allowlist change applies
# to the file and not to the process, the deploy reports success, and
# the rule that was added is not in force. That happened once already,
# with .nodejs.org.
#
# Hashing the ALLOWLIST BLOCK rather than the whole manifest is the
# point: the proxy then rolls when the rules change and stays up when
# only an image tag did. Read from the template rather than the rendered
# output because the block contains no substitutions, and because
# rendering it would require this value to already exist.
#
# `sed` and `sha256sum` rather than a YAML parser: the image that runs
# this ships kubectl and gcloud, not PyYAML, and scripts/infra/render.py
# exists because the same image has no envsubst either.
EGRESS_CONFIG_SHA="$(sed -n '/name: egress-allowlist/,/^---$/p' \
  "${HERE}/deploy/k8s/conductor.yaml" | sha256sum | cut -c1-12)"
if [ -z "${EGRESS_CONFIG_SHA}" ]; then
  echo "could not fingerprint the egress allowlist. Rendering would fail" \
       "on an empty value, which is the right outcome: a proxy that does" \
       "not restart on a rule change enforces yesterday's rules." >&2
  exit 1
fi
echo "egress allowlist fingerprint: ${EGRESS_CONFIG_SHA}"
export EGRESS_CONFIG_SHA

export IMAGE NAMESPACE PROXY_IMAGE PROJECT ARTIFACT_BUCKET CLOUDSQL_INSTANCE
export BUILD_SHA
export CONTROL_PANEL_URL SANDBOX_BASE_IMAGE SANDBOX_BASE_DIGEST
# THE SERVERLESS VPC CONNECTOR'S RANGE. The control panel is not a pod
# and matches no podSelector, so the one NetworkPolicy rule that lets
# the file explorer read a workspace names this CIDR directly. It was a
# literal in conductor.yaml; terraform lets the deployer choose it
# (`connector_cidr`), and the default below is that variable's default,
# so nothing changes for anyone who left it alone. Anyone who did not
# was silently losing the file explorer -- the policy is default-deny,
# so a wrong range HANGS rather than refusing.
CONNECTOR_CIDR="${CONNECTOR_CIDR:-10.8.0.0/28}"

export EXECUTION_SQL_INSTANCE CLOUDSQL_IP CONNECTOR_CIDR
# NOT envsubst: the cloud-sdk image has no gettext, and envsubst
# renders an unset variable as the empty string rather than failing.
# See scripts/infra/render.py.
python3 "${HERE}/scripts/infra/render.py" \
  "${HERE}/deploy/k8s/conductor.yaml" | kubectl apply -f -

# ── did it actually come up ─────────────────────────────────────────────
# A `kubectl apply` that returns 0 means the API server accepted the
# object, not that anything is running. The same distinction Cloud Run's
# smoke check exists for: green means the request was accepted.
echo "waiting for the rollout"
kubectl rollout status deployment/conductor \
  --namespace="$NAMESPACE" --timeout=300s
kubectl rollout status deployment/egress-proxy \
  --namespace="$NAMESPACE" --timeout=300s
# AND THE VERIFIER (Phase E1). Waited on for the same reason the other
# two are: `kubectl apply` returning 0 means the API server accepted the
# object. A Deployment nobody waits on is one that can CrashLoopBackOff
# through a green deploy -- and this one is the least likely to be
# noticed, because the conductor still runs adjudication itself until
# the cutover, so the platform keeps working while the new pod does not.
kubectl rollout status deployment/verifier \
  --namespace="$NAMESPACE" --timeout=300s

echo ""
echo "── ${NAMESPACE} ──"
kubectl get pods --namespace="$NAMESPACE" -o wide
echo ""
echo "the conductor is running ${IMAGE}"
echo "watch it:  kubectl logs -n ${NAMESPACE} -l app=conductor -f"
# NAMED SEPARATELY, because after E the verifier's output is no longer in
# the conductor's log -- and plan 9.2 records that the diagnosis of every
# fault found on 2026-09-17 depended on it being there.
echo "verifier:  kubectl logs -n ${NAMESPACE} -l app=verifier -f"

# ── WHICH TOPOLOGY YOU ACTUALLY GOT ─────────────────────────────────────
# `off` is a supported mode, not a failure -- with no sqlproxy beside it
# the execution pod cannot reach the database, so the conductor runs the
# legs itself, which is what E0, a laptop and this whole test suite do.
#
# IT IS ALSO INVISIBLE. The decision is announced a few hundred lines up
# by one echo that has long since scrolled away, every rollout above
# still reports success, and pods still appear in `kubectl get pods` --
# they just are not where the work happens. An operator following the
# deploy guide reads all of that as "the execution plane is up", and
# then spends the first real story wondering why nothing lands in a pod.
#
# So the last thing this script prints is which of the two shapes is
# now running, and when it is the degraded one, the two secrets that
# would change it.
echo ""
if [ "$EXECUTION_SQL_INSTANCE" = "off" ]; then
  echo "=============================================================="
  echo " EXECUTION PODS HAVE NO DATABASE -- legs run in the conductor"
  echo "=============================================================="
  echo " This deployment is complete and usable, but it is NOT the"
  echo " isolated-pod topology the architecture describes: a story's"
  echo " work executes inside the conductor process, sharing its pool."
  echo ""
  echo " Why: execution-sqlproxy-key-${ENV_NAME} and"
  echo "      execution-db-password-${ENV_NAME} must BOTH exist."
  echo " Check: gcloud secrets versions access latest \\"
  echo "          --secret=execution-sqlproxy-key-${ENV_NAME}"
  echo " Then re-run this script -- it is idempotent."
  echo "=============================================================="
else
  echo "execution pods run their own legs (sql proxy ->"
  echo "  ${EXECUTION_SQL_INSTANCE})"
fi
