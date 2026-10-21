#!/usr/bin/env bash
#
# rollback.sh <region> <commit-sha> <service> [<service> ...]
#
# Put a service back on a previous build by REDEPLOYING that build's image,
# creating a new revision from it.
#
# WHY NOT `update-traffic --to-revision`, which is the usual answer.
#
#   Every deploy here runs prune-revisions.sh, which DELETES superseded
#   revisions, so by the time anyone wants to roll back the revision they
#   would roll back to does not exist. That is not an oversight to correct:
#   these services run with --min-instances=1 and --no-cpu-throttling, and
#   app/main.py starts background loops (branch_ready_listener, the
#   provision-retry backstop, the reapers) that keep running in ANY live
#   instance regardless of the traffic split. A retained previous revision
#   is not a dormant rollback target -- it is a second copy of the system
#   doing the same work, and on 2026-08-25 the STALE copy was observed
#   finishing last and therefore winning:
#
#     09:14:22  control-panel-prod-00075-dtk  recorded story=a2a1b473  <- new
#     09:09:22  control-panel-prod-00074-v9n  recorded story=a2a1b473  <- OLD, won
#
#   So keeping a revision around to roll back to would reintroduce the
#   exact defect the pruning exists to prevent. The image is the durable
#   artefact instead: every build tags ${IMAGE}:${COMMIT_SHA} and nothing
#   deletes those, so any past build is redeployable by name for as long
#   as the registry keeps it.
#
# THE SCHEMA IS NOT ROLLED BACK, and cannot be. Migrations are forward-only
# -- apply.py records what it ran and never re-runs it, and there are no
# down-migrations. Rolling code back over a migrated database is safe only
# where the migration was EXPAND-then-contract: the new schema still serves
# the old code. A migration that renamed or dropped something the previous
# build reads will fail after this script succeeds, and it will fail at
# request time rather than here. Check what shipped between the two SHAs
# before reaching for this.
#
# Usage:
#   scripts/rollback.sh us-central1 a1b2c3d control-panel-prod
#   scripts/rollback.sh us-central1 a1b2c3d control-panel-prod council-worker-prod
set -euo pipefail

REGION="${1:?usage: rollback.sh <region> <commit-sha> <service> [<service> ...]}"
SHA="${2:?usage: rollback.sh <region> <commit-sha> <service> [<service> ...]}"
shift 2
[ "$#" -gt 0 ] || { echo "no services named" >&2; exit 2; }

# The image comes from the service itself rather than a substitution, so
# this stays correct as services are added and never names the wrong
# repository for one of them.
image_for() {
  gcloud run services describe "$1" --region="$REGION" \
    --format='value(spec.template.spec.containers[0].image)'
}

rc=0
for SERVICE in "$@"; do
  if ! CURRENT=$(image_for "$SERVICE" 2>/dev/null) || [ -z "$CURRENT" ]; then
    echo "[$SERVICE] does not exist in $REGION -- skipping" >&2
    rc=1
    continue
  fi

  # Strip the tag or digest, keep the repository. A digest-pinned image
  # (repo@sha256:...) has no tag to replace, so split on '@' first.
  REPO="${CURRENT%%@*}"
  REPO="${REPO%:*}"
  TARGET="${REPO}:${SHA}"

  if [ "$CURRENT" = "$TARGET" ]; then
    echo "[$SERVICE] already on ${SHA} -- nothing to do"
    continue
  fi

  # Fail before touching the service if the tag was never pushed --
  # `gcloud run services update` would otherwise create a revision that
  # cannot pull, and Cloud Run keeps serving the old one while reporting
  # a failed deploy, which reads like the rollback did nothing.
  if ! gcloud container images describe "$TARGET" >/dev/null 2>&1; then
    echo "[$SERVICE] $TARGET is not in the registry -- refusing" >&2
    echo "           (list what is: gcloud container images list-tags $REPO)" >&2
    rc=1
    continue
  fi

  echo "[$SERVICE] $CURRENT"
  echo "        -> $TARGET"
  if gcloud run services update "$SERVICE" --region="$REGION" \
       --image="$TARGET" --quiet >/dev/null; then
    echo "[$SERVICE] rolled back; now serving $(image_for "$SERVICE")"
  else
    echo "[$SERVICE] FAILED to roll back" >&2
    rc=1
  fi
done

[ "$rc" -eq 0 ] && echo "rollback: done" || echo "rollback: FAILED" >&2
exit "$rc"
