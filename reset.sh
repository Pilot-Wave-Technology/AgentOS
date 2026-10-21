#!/usr/bin/env bash
# Tear down the gateway stack (stage ①): the VM, its reserved address, the
# network, subnet, firewalls and the archive bucket.
#
# ORDER MATTERS: destroy the PLATFORM stack FIRST (its VPC connector lives
# on this stack's network). The platform's ./teardown.sh runs both, in order.
#
# Secrets and service accounts are NOT removed here -- the platform's
# prerequisites.sh created them; `./teardown.sh --everything` removes them.
#
# NOTHING TO EDIT FIRST: a non-empty archive bucket is allowed to go by
# this script (on resources already in state only, so a re-run after a
# partial teardown never recreates anything).
set -euo pipefail
cd "$(dirname "$0")/terraform"

targets=()
while IFS= read -r addr; do
  case "$addr" in
    *google_storage_bucket.*) targets+=("-target=$addr") ;;
  esac
done < <(terraform state list 2>/dev/null || true)
if [ "${#targets[@]}" -gt 0 ]; then
  echo "[reset] allowing the non-empty archive bucket to be deleted..."
  terraform apply -auto-approve -input=false -var force_destroy_buckets=true "${targets[@]}"
fi

echo "[reset] destroying the gateway stack (VM, reserved address, network, bucket, firewalls)..."
terraform destroy -auto-approve -input=false -var force_destroy_buckets=true
echo "[reset] gateway stack destroyed."
