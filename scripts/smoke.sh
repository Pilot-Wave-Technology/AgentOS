#!/usr/bin/env bash
#
# smoke.sh <region> <commit-sha> <service> [<service> ...]
#
# After a deploy: is the thing we just built the thing that is running,
# and does it answer?
#
# WHY THE FIRST QUESTION IS THE IMPORTANT ONE. A Cloud Build that goes
# green means the deploy API accepted the request. It does not mean the
# new code is serving. Three separate correct fixes on this system
# appeared not to deploy, because an old revision kept running the
# background loops and kept winning the write; and a revision whose image
# cannot be pulled leaves Cloud Run serving the previous one while the
# build still reports success. Both look identical from the build log.
#
# So this asserts the SHA, not just a 200. A liveness probe on its own
# would have passed happily throughout every one of those incidents.
#
# WHAT A PASS DOES NOT MEAN. This is a smoke test: the process started,
# is reachable, and is the right build. It says nothing about whether the
# background loops are ticking -- `loop_heartbeats` is the surface for
# that, and a stale instance there is the signal.
#
# Usage:
#   scripts/smoke.sh us-central1 "$COMMIT_SHA" control-panel-prod
set -euo pipefail

REGION="${1:?usage: smoke.sh <region> <commit-sha> <service> [<service> ...]}"
SHA="${2:?usage: smoke.sh <region> <commit-sha> <service> [<service> ...]}"
shift 2
[ "$#" -gt 0 ] || { echo "no services named" >&2; exit 2; }

# How long a just-deployed revision may take to become ready. Cloud Run
# reports the deploy done before the first instance has necessarily
# finished starting.
DEADLINE_S="${SMOKE_DEADLINE_S:-120}"

# READINESS VIA REVISION NAMES rather than a condition projection.
# gcloud's --format language is not JMESPath and has no dependable way to
# pick one entry out of status.conditions; these two fields say the same
# thing plainly -- when the newest revision CREATED is also the newest
# revision READY, the deploy has actually come up.
# `| tr -d '\r'` on every gcloud read: gcloud on Windows ends lines with CR,
# and `read` keeps it on the last field, so a created revision never equalled
# its ready one and a healthy service waited out the whole deadline.
DESCRIBE='value(status.url, spec.template.spec.containers[0].image, status.latestCreatedRevisionName)'
READY_FMT='value(status.latestReadyRevisionName)'

ready_revision() {
  gcloud run services describe "$1" --region="$REGION" --format="$READY_FMT" 2>/dev/null | tr -d '\r' || echo ""
}

check_one() {
  local SERVICE="$1"
  local url image created ready code waited=0

  if ! read -r url image created < <(gcloud run services describe "$SERVICE" --region="$REGION" --format="$DESCRIBE" 2>/dev/null | tr -d '\r'); then
    echo "[$SERVICE] could not be described -- does it exist in $REGION?" >&2
    return 1
  fi
  [ -n "$url" ] || { echo "[$SERVICE] has no URL" >&2; return 1; }

  # 1. The right build. An image pinned by digest has no tag to compare,
  #    so accept a digest only when it is what the caller asked for.
  case "$image" in
    *:"$SHA"|*@"$SHA") ;;
    *)
      echo "[$SERVICE] SERVING THE WRONG BUILD" >&2
      echo "           expected a revision at $SHA" >&2
      echo "           serving  $image" >&2
      return 1
      ;;
  esac

  # 2. Ready. Polled, because "deployed" precedes "started".
  ready=$(ready_revision "$SERVICE")
  while [ "$ready" != "$created" ] && [ "$waited" -lt "$DEADLINE_S" ]; do
    sleep 5
    waited=$((waited + 5))
    ready=$(ready_revision "$SERVICE")
  done
  if [ "$ready" != "$created" ]; then
    echo "[$SERVICE] $created never became ready in ${DEADLINE_S}s" >&2
    echo "           newest ready revision is ${ready:-none}" >&2
    gcloud run services describe "$SERVICE" --region="$REGION" --format=json 2>/dev/null | python3 -c 'import json,sys
conds = (json.load(sys.stdin).get("status") or {}).get("conditions") or []
for c in conds:
    print("          ", c.get("type"), c.get("status"), c.get("message", ""))' >&2 || true
    return 1
  fi

  # 3. It answers. Any HTTP status is a pass except 5xx: these services
  #    sit behind auth, so a 302 to a login page and a 401 both mean the
  #    app is up and routing. A connection failure or a 5xx does not.
  # A FAILED curl IS NOT AN ANSWER. curl prints 000 for -w and exits non-zero
  # on no connection; the old `|| echo "000"` then appended a second 000,
  # and "000000" matched no case below -- so an unreachable service passed
  # as healthy. The exit status decides; the code is only read on success.
  if ! code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$url"); then
    code="000"
  fi
  case "$code" in
    000) echo "[$SERVICE] no HTTP response from $url" >&2; return 1 ;;
    5*)  echo "[$SERVICE] $url returned $code" >&2; return 1 ;;
  esac

  echo "[$SERVICE] ok -- $created at $SHA, ready, HTTP $code"
}

rc=0
for SERVICE in "$@"; do
  [ -n "$SERVICE" ] || continue
  check_one "$SERVICE" || rc=1
done

[ "$rc" -eq 0 ] && echo "smoke: all services healthy" || echo "smoke: FAILED" >&2
exit "$rc"
