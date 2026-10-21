#!/usr/bin/env bash
#
# prune-revisions.sh <region> <service> [<service> ...]
#
# Ensure the latest revision serves all traffic, then DELETE the superseded revisions.
#
# Why this exists (proven live on prod, 2026-08-25):
#
#   Cloud Run's traffic split governs HTTP requests ONLY. It does not stop an old
#   revision's instances from running. `app/main.py` starts background asyncio loops
#   (branch_ready_listener, the provision-retry backstop, the reapers), and those keep
#   executing in ANY live instance — with `--no-cpu-throttling` and `--min-instances=1`
#   nothing reaps them, and old instances were observed alive more than an hour after a
#   new revision took 100% of traffic.
#
#   The result is two revisions doing the same work concurrently, with the STALE one
#   frequently finishing last and therefore winning:
#
#     09:14:22  control-panel-prod-00075-dtk  recorded story=a2a1b473 (repos/…)  <- new
#     09:09:22  control-panel-prod-00074-v9n  recorded story=a2a1b473 (repo)     <- OLD, won
#
#   Three separate correct fixes appeared not to deploy because of this. A deploy that
#   does not replace the RUNNING logic makes every subsequent fix unverifiable.
#
# SPEED. The first version took ~10 minutes: it deleted revisions one at a time, each
# call blocking until Cloud Run finished, across five services with months of history.
# Three changes bring it to roughly one round trip per service plus fire-and-forget
# deletes:
#   * `--async` deletes — we need the revision GONE, not the API call watched. The
#     backing instances are torn down either way.
#   * services pruned in PARALLEL, output buffered per service so logs stay readable.
#   * the traffic update is skipped when the latest revision already serves, which is
#     the normal case after `gcloud run deploy`.
#
# CONCURRENT BUILDS. Two builds of the same branch can overlap (seen on dev, 3 minutes
# apart). Two hazards, both handled:
#   1. The other build deletes a revision between our list and our delete. `not found`
#      then means ALREADY DELETED — the end state we wanted — so it is SUCCESS. Same
#      rule the gateway path uses for `agents.delete`. Any OTHER delete error is fatal:
#      silently leaving a stale revision running is what this script exists to prevent.
#   2. The other build deploys a NEWER revision while we prune. Deleting "everything
#      not serving" would then delete the revision that just went live. So we take a
#      cutoff from the serving revision's creation timestamp and never touch anything
#      created at or after it.
set -euo pipefail

REGION="${1:?usage: prune-revisions.sh <region> <service> [<service> ...]}"
shift

prune_one() {
  local SERVICE="$1"
  local SERVING LATEST CUTOFF REV out

  if ! gcloud run services describe "$SERVICE" --region="$REGION" >/dev/null 2>&1; then
    echo "[$SERVICE] does not exist yet — skipping"
    return 0
  fi

  read -r LATEST SERVING < <(gcloud run services describe "$SERVICE" --region="$REGION" \
    --format='value(status.latestReadyRevisionName, status.traffic.revisionName)')
  SERVING=$(tr ';,' '\n' <<<"$SERVING" | sed '/^$/d' | sort -u)

  # Only pay for a traffic update when it is actually needed.
  if [ "$SERVING" != "$LATEST" ]; then
    echo "[$SERVICE] routing all traffic to $LATEST"
    gcloud run services update-traffic "$SERVICE" --region="$REGION" --to-latest --quiet \
      >/dev/null
    SERVING="$LATEST"
  fi

  if [ -z "$SERVING" ]; then
    echo "[$SERVICE] no serving revision — refusing to prune"
    return 0
  fi
  echo "[$SERVICE] serving: $(tr '\n' ' ' <<<"$SERVING")"

  # Oldest creation timestamp among serving revisions. Anything created at or after
  # this may belong to a concurrent, newer build and must be left alone.
  CUTOFF=$(gcloud run revisions list --service="$SERVICE" --region="$REGION" \
             --format='value(metadata.name,metadata.creationTimestamp)' \
           | while read -r name ts; do
               if grep -qx "$name" <<<"$SERVING"; then echo "$ts"; fi
             done | sort | head -1)

  if [ -z "$CUTOFF" ]; then
    echo "[$SERVICE] could not resolve a cutoff — refusing to prune"
    return 0
  fi

  for REV in $(gcloud run revisions list --service="$SERVICE" --region="$REGION" \
                 --filter="metadata.creationTimestamp<'$CUTOFF'" \
                 --format='value(metadata.name)'); do
    if out=$(gcloud run revisions delete "$REV" --region="$REGION" --async --quiet 2>&1); then
      echo "[$SERVICE] deleting $REV"
      continue
    fi
    if grep -qiE 'could not be found|not found|NOT_FOUND' <<<"$out"; then
      echo "[$SERVICE] $REV already gone (concurrent build) — treating as done"
      continue
    fi
    echo "[$SERVICE] FAILED to delete $REV:" >&2
    echo "$out" >&2
    return 1
  done
}

pids=(); names=(); logs=()
for SERVICE in "$@"; do
  [ -n "$SERVICE" ] || continue
  log=$(mktemp)
  prune_one "$SERVICE" >"$log" 2>&1 &
  pids+=("$!"); names+=("$SERVICE"); logs+=("$log")
done

rc=0
for i in "${!pids[@]}"; do
  if ! wait "${pids[$i]}"; then rc=1; fi
  cat "${logs[$i]}"; rm -f "${logs[$i]}"
done

[ "$rc" -eq 0 ] && echo "prune-revisions: done" || echo "prune-revisions: FAILED" >&2
exit "$rc"
