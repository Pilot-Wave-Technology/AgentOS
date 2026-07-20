#!/usr/bin/env bash
###############################################################################
# set-secret.sh — safely set a Secret Manager secret value.
#
# Does two things the raw `gcloud secrets versions add` does NOT:
#   1. REFUSES an empty value. Secret Manager rejects an empty payload, and a
#      half-written empty secret breaks the deploy — so we fail loudly instead.
#   2. DISABLES every older enabled version after adding the new one, so only
#      `:latest` is ever active (no stale key lingering behind :latest).
#
# Usage:
#   printf %s 'THE_VALUE' | ./set-secret.sh <secret-name>     # value on stdin
#   ./set-secret.sh <secret-name> path/to/file                # value from a file
#
# Uses your active gcloud project (from `gcloud config set project ...`), or
# pass one with:  GCP_PROJECT=my-proj ./set-secret.sh <name> ...
###############################################################################
set -euo pipefail

name="${1:?usage: set-secret.sh <secret-name> [file]   (value on stdin if no file)}"
src="${2:--}"
proj_args=(); [ -n "${GCP_PROJECT:-}" ] && proj_args=(--project "$GCP_PROJECT")

tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
if [ "$src" = "-" ]; then cat > "$tmp"; else cat "$src" > "$tmp"; fi

# Guard: never write an empty (or whitespace-only) value.
if [ ! -s "$tmp" ] || ! grep -q '[^[:space:]]' "$tmp"; then
  echo "ERROR: refusing to set '$name' to an empty value (Secret Manager rejects it)." >&2
  echo "       Pipe a real value:  printf %s 'VALUE' | ./set-secret.sh $name" >&2
  exit 1
fi

# The secret shell already exists (prerequisites.sh created every one). Create it
# on demand anyway, so this script also works standalone.
gcloud secrets describe "$name" "${proj_args[@]}" >/dev/null 2>&1 \
  || gcloud secrets create "$name" --replication-policy=automatic "${proj_args[@]}" >/dev/null

gcloud secrets versions add "$name" --data-file="$tmp" "${proj_args[@]}" >/dev/null

# Disable every enabled version except the newest (= :latest). gcloud may print
# `name` as a full resource path (…/versions/N) and add a trailing CR on Windows;
# `versions disable` wants the bare version number, so normalize to it.
ver() { sed 's#.*/##' | tr -d '\r'; }
newest="$(gcloud secrets versions list "$name" "${proj_args[@]}" \
            --filter=state=ENABLED --sort-by=~createTime --format='value(name)' --limit=1 | ver)"
for v in $(gcloud secrets versions list "$name" "${proj_args[@]}" \
             --filter=state=ENABLED --format='value(name)' | ver); do
  [ -n "$v" ] && [ "$v" != "$newest" ] \
    && gcloud secrets versions disable "$v" --secret="$name" "${proj_args[@]}" --quiet
done

echo "$name: set to version $newest; older versions disabled."
