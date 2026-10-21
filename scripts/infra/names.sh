#!/usr/bin/env bash
#
# names.sh <env> — every name the execution plane is addressed by, in
# one place. Sourced, not executed.
#
# WHY THIS FILE EXISTS. `agentos-staging` was already spelled in
# gke_cluster.sh and again in deploy_execution_plane.sh, and the
# artifacts bucket was spelled in deploy/k8s/conductor.yaml and nowhere
# that creates it. Adding a third identity would have made three
# scripts agree by coincidence.
#
# The failure mode is specific and quiet: the conductor writes the tree
# to one bucket and the verifier's initContainer reads from another, so
# every criterion fails to start and the Job reports only that its init
# container exited 1. Nothing is misconfigured in a way a person can
# see — two files simply disagree.
#
#   source "$(dirname "$0")/names.sh" next
#
# ── TWO SHAPES, ONE DERIVATION ────────────────────────────────────────
#
# By default every name carries the environment, because we run several
# side by side on one project and a name that does not say which one it
# belongs to is a name two environments can collide on.
#
# A SINGLE-ENVIRONMENT INSTALL HAS NO SUCH PROBLEM, and paying the
# suffix anyway is not merely untidy -- it is the thing that made the
# public repo undeployable. `prerequisites.sh` creates `database-url`
# and `control_panel`; this file asked for `<env>-database-url` and
# `control_panel_<env>`; nothing connected the two, so every resource
# the execution plane wanted was absent and the preflight reported a
# correct install as almost entirely MISSING.
#
# So: set AGENTOS_SINGLE_ENV to any non-empty value and the suffixes
# disappear. Leave it unset and every name below is byte-identical to
# what it has always been -- pinned, for all three of our
# environments, by app/services/tests/test_infra_names_are_stable.py.
#
#   AGENTOS_SINGLE_ENV=    (unset)  agentos-staging, control_panel_next,
#                                   agentos-exec-next@, <env>-database-url
#   AGENTOS_SINGLE_ENV=1            agentos, control_panel,
#                                   agentos-exec@, database-url
#
# IT IS A SEPARATE KNOB FROM AGENTOS_POOL_SCOPE on purpose. That one
# says whether the node pools are labelled, and `next` runs with it
# BLANK while its secrets are suffixed -- so the two decisions are
# genuinely independent and folding them together would have renamed
# every secret `next` uses.

agentos_names() {
  local env_name="${1:?env name required}"
  # EVERY NAME BELOW IS DERIVED FROM THIS ARGUMENT, which is why
  # opening the list is the whole of the change: the database, the
  # bucket, the four service accounts and the sandbox repository all
  # spell the environment rather than the word `next`. The guard was a
  # refusal to be called with an argument nobody had built for, not a
  # statement that anything here is next-specific.
  # SHAPE, NOT AN ALLOWLIST. This used to name the two environments we
  # happen to run, which is a sentence only this deployment can write.
  # Somebody standing up their own production from a clone has an
  # environment we have never heard of, and refusing it would make the
  # repo unusable for exactly the case it is published for.
  #
  # The name still has to be checked, because it becomes a Kubernetes
  # namespace, a Postgres database name, a GCP secret suffix and part
  # of a service-account address -- four grammars whose intersection is
  # lowercase letters, digits and hyphens. A name that violates it
  # fails somewhere downstream with a message about none of this.
  case "$env_name" in
    "") echo "ERROR: environment name must not be empty" >&2; return 1 ;;
    *[!a-z0-9-]*)
      echo "ERROR: environment name '${env_name}' must be lowercase letters," >&2
      echo "       digits and hyphens: it becomes a namespace, a database" >&2
      echo "       name, a secret suffix and a service-account address." >&2
      return 1 ;;
  esac

  ENV_NAME="$env_name"

  # THE SUFFIXES, DERIVED ONCE. Everything below spells one of these
  # rather than `${env_name}` directly, so the two shapes cannot drift
  # apart: there is no name that remembers to carry the environment in
  # one mode and forgets in the other.
  #
  # Three forms, because three grammars. `-next` for anything
  # hyphen-separated (namespaces, service accounts, buckets, secrets),
  # `_next` for the Postgres database, and the bare word where a path
  # segment wants it.
  if [ -n "${AGENTOS_SINGLE_ENV-}" ]; then
    ENV_DASH=""; ENV_UNDER=""; ENV_SEG="agentos"
  else
    ENV_DASH="-${env_name}"; ENV_UNDER="_${env_name}"
    ENV_SEG="agentos-${env_name}"
  fi

  NAMESPACE="agentos${ENV_DASH}"
  CLUSTER="${CLUSTER:-agentos}"

  # Kubernetes service accounts. Namespaced, so they do not carry the
  # environment in their names.
  KSA="agentos-execution"
  COND_KSA="agentos-conductor"
  # A THIRD IDENTITY, and the tree is the reason. A criterion's pod has
  # to read one object out of the artifact store, so it needs
  # storage.objectViewer. Giving that to agentos-execution would hand
  # the EXECUTOR bucket read through the same Workload Identity, and
  # "execution holds nothing" is what the pod design rests on.
  VERIFY_KSA="agentos-verifier"
  # A FOURTH, and the direction of the grant is why. A criterion READS
  # one tree; the environment replay WRITES the directory it captured,
  # because a Job's pod cannot be read after it exits. Read and write
  # are different capabilities and this is where they stay apart — a
  # criterion that could write to the store is one that can put things
  # where the control panel and the `artifact` criterion will read them.
  CAPTURE_KSA="agentos-capture"
  # THE FIFTH, AND IT HAS NO GSA OF ITS OWN ON PURPOSE. The RBAC split
  # gave the verifier Deployment a Kubernetes identity separate from the
  # conductor's, so a compromised verifier cannot delete execution pods
  # (E1) or the claim a workspace lives on. It points at COND_GSA: only
  # the KUBERNETES half split, so no GCP capability moved and the
  # Workload Identity stays byte-identical rather than copied and
  # drifting -- which is the objection the old test raised, answered.
  VERIFIER_CONTROL_KSA="agentos-verifier-control"

  # Google service accounts. Project-wide, so these do.
  GSA="agentos-exec${ENV_DASH}@${PROJECT}.iam.gserviceaccount.com"
  COND_GSA="agentos-cond${ENV_DASH}@${PROJECT}.iam.gserviceaccount.com"
  VERIFY_GSA="agentos-verify${ENV_DASH}@${PROJECT}.iam.gserviceaccount.com"
  CAPTURE_GSA="agentos-capture${ENV_DASH}@${PROJECT}.iam.gserviceaccount.com"

  # CUSTOM ROLES, and not for taste. `roles/storage.objectViewer`
  # includes `storage.objects.list`, so granting it would have made the
  # sentence this design rests on false: with list, object names are
  # enumerable and the hash stops being the capability — a criterion
  # could walk the bucket and read every other story's tree, held-out
  # checks included. These carry `get` and `create` and no `list`.
  # Ids must be alphanumeric; hyphens are rejected.
  ROLE_READ="agentosArtifactRead"
  ROLE_WRITE="agentosArtifactWrite"

  # The artifact store. Read by the conductor, the control panel and now
  # a criterion's initContainer, so its name is the one that most needed
  # a single home.
  # A BUCKET NAME IS GLOBAL ACROSS ALL OF GOOGLE CLOUD, not scoped to
  # the project, so the single-environment form carries the project id.
  # `agentos-artifacts` is a name somebody already owns; creating it
  # fails with 409 and the message does not say that the owner is a
  # stranger, so it reads as a permissions problem on your own project.
  # Our multi-environment names get away without it only because
  # `agentos-staging-artifacts` happened to be free.
  if [ -n "${AGENTOS_SINGLE_ENV-}" ]; then
    ARTIFACT_BUCKET="agentos-artifacts-${PROJECT}"
  else
    ARTIFACT_BUCKET="agentos-${env_name}-artifacts"
  fi

  # THE IMAGE A STORY'S WORK AND ITS CHECKS RUN IN, and it belongs here
  # for exactly the reason this file exists. It was a literal in
  # kernel/worker/contracts.py and again in kernel/console/contract.py --
  # `agentos-kernel/fixture:dev`, which exists in one laptop's Docker
  # cache and in no registry. In the cloud the name resolved to
  # docker.io and 404'd, so the pod never became ready, dispatch timed
  # out at 120s, the reconciler recovered it, and it dispatched again:
  # seven pods in ninety minutes for NCAL3's exec-190, and the only
  # symptom a person saw was "the stories keep re-dispatching".
  #
  # Built from infra/e0/fixtures/Dockerfile, which is the same recipe E0
  # builds locally, so the two rooms stay the same room.
  SANDBOX_BASE_REPO="${REGION:-us-central1}-docker.pkg.dev/${PROJECT}/${ENV_SEG}/sandbox-base"

  # The database, and the instance the proxy sidecar dials. `next` gets
  # a DATABASE on the shared instance rather than an instance of its
  # own -- creating a database does not modify the instance, which is
  # what makes sharing it safe.
  DATABASE="control_panel${ENV_UNDER}"

  # THE INSTANCE NAME IS NOT DERIVED FROM THE ENVIRONMENT -- several
  # environments share one instance, which is safe because creating a
  # DATABASE on an instance does not modify the instance. It is still
  # not a constant: a single-environment install creates its own with
  # `prerequisites.sh`, which names it `${NAME_PREFIX}-db`, and ours is
  # called `control-panel-db` for historical reasons no outsider
  # shares. Overridable either way, so neither side has to patch this
  # file.
  if [ -n "${AGENTOS_SQL_INSTANCE_NAME-}" ]; then
    _sql_instance="$AGENTOS_SQL_INSTANCE_NAME"
  elif [ -n "${AGENTOS_SINGLE_ENV-}" ]; then
    _sql_instance="${NAME_PREFIX:-agentos}-db"
  else
    _sql_instance="control-panel-db"
  fi
  CLOUDSQL_INSTANCE="${PROJECT}:${SQL_REGION:-us-central1}:${_sql_instance}"
}

# agentos_secret <base> -- the Secret Manager id for this environment.
#
# THE SECRETS WERE THE HALF NOBODY CENTRALISED. Every caller spelled
# `"gemini-api-key-${ENV_NAME}"` inline, so the suffix rule lived in
# four scripts and in none of them on purpose -- and the public
# `prerequisites.sh`, which creates them without a suffix, could not
# agree with any of them. Call this instead of interpolating.
#
#   agentos_secret database-url        -> next-database-url  (prefix!)
#   agentos_secret gemini-api-key      -> gemini-api-key-next (suffix!)
#
# AND YES, THE TWO FAMILIES DISAGREE ABOUT WHICH END. The DSN secrets
# are `<env>-database-url` and the key secrets are `<name>-<env>`; that
# is how they were created years ago and renaming live secrets is not
# this change. The inconsistency is now in one function instead of
# scattered across the scripts that have to reproduce it.
# ALL THREE DSN SECRETS, and the third was nearly missed. The list
# is matched on the WHOLE name, not a prefix, so
# `database-url-conductor` does not inherit `database-url`'s rule
# -- leaving it out produced `database-url-conductor-next`, a
# secret that does not exist, which would have failed the
# conductor deploy on every one of our environments. Caught by
# diffing the resolved names against the literals they replaced.
AGENTOS_SECRETS_PREFIXED="database-url database-url-migrate database-url-conductor"

# SECRETS WE CREATED UNDER AN OLDER NAME. The canonical name is the one
# on the left -- it is what the published `prerequisites.sh` creates,
# what the gateway reads as an environment variable, and what every
# document now says. The name on the right is what actually exists in
# OUR project, because the secret was created before the rename and
# renaming a live secret is a separate operation with its own cutover:
# a new secret, both read for a while, then the old one deleted.
#
# Putting the map here rather than leaving `claude-api-key` spelled in
# the callers is the difference between one line to delete when that
# cutover happens and a search across four scripts. Callers ask for the
# canonical name and never learn this exists.
#
# To retire it: create `anthropic-api-key-<env>` in each environment
# with the same value, confirm, then delete this map and that case.
agentos_legacy_secret() {
  case "$1" in
    anthropic-api-key) printf '%s' "claude-api-key" ;;
    *)                 printf '%s' "$1" ;;
  esac
}

agentos_secret() {
  local base="${1:?secret base name required}"
  if [ -n "${AGENTOS_SINGLE_ENV-}" ]; then
    # No legacy map here, deliberately. A single-environment install is
    # new, so its secrets were created by today's prerequisites.sh
    # under today's names; applying our history to a stranger's project
    # would ask for a secret that has never existed there.
    printf '%s' "$base"
    return 0
  fi
  base="$(agentos_legacy_secret "$base")"
  case " $AGENTOS_SECRETS_PREFIXED " in
    *" $base "*) printf '%s' "${ENV_NAME}-${base}" ;;
    *)           printf '%s' "${base}-${ENV_NAME}" ;;
  esac
}
