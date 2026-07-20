#!/usr/bin/env bash
# Tear down the OpenClaw gateway stack (STAGE 1) for repeatable testing.
#
# ORDER MATTERS: destroy the PLATFORM stack FIRST (its VPC connector lives on
# this stack's VPC), THEN run this. So:
#   1. cd ../agentos-platform && ./reset.sh
#   2. cd ../agentos-openclaw  && ./reset.sh   (this script)
#
# Requires force_destroy_buckets=true in terraform.tfvars so the (non-empty)
# archive bucket can be deleted.
set -euo pipefail
cd "$(dirname "$0")/terraform"
echo "[reset] destroying gateway stack (VM, VPC, subnet, archive bucket, firewalls; secrets + SA persist — they're created by the platform prerequisites.sh)..."
terraform destroy -auto-approve
echo "[reset] gateway stack destroyed. Re-deploy with: terraform apply"
