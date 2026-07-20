#!/usr/bin/env bash
#
# Ledger provenance guard — prevents regressions of the actor-attribution work
# (see LEDGER_PROVENANCE_REVIEW.md + app/ledger/README.md).
#
# Fails the build if application code (app/**.py) reintroduces:
#   1. a hardcoded canonical system-actor UUID literal (…0001) — everything must
#      import SYSTEM_ACTOR_ID from app.ledger.actors, the ONE source of truth; or
#   2. an all-zero placeholder actor (uuid.UUID(int=0) / …0000) — these resolve
#      to no principal, so the event's provenance is lost.
#
# Requires only bash + grep (available via Git Bash on Windows). Mirrors the
# migrations/check_sequence.sh convention so all three Cloud Build pipelines can
# run it as an early, cheap gate.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# The single file allowed to define the canonical literal.
CANON="app/ledger/actors.py"
fail=0

echo "check_ledger_actors: scanning app/ for ledger-actor regressions…"

# 1. Hardcoded canonical system UUID literal outside the canonical module.
hits=$(grep -rnE 'UUID\("00000000-0000-0000-0000-000000000001"\)' app --include=*.py \
        | grep -vE "^${CANON}:" || true)
if [ -n "$hits" ]; then
  echo ""
  echo "ERROR: hardcoded system-actor UUID literal (…0001)."
  echo "       Import it instead:  from app.ledger.actors import SYSTEM_ACTOR_ID"
  echo "$hits"
  fail=1
fi

# 2. All-zero placeholder actor id (resolves to no principal).
hits=$(grep -rnE 'UUID\(int=0\)|UUID\("00000000-0000-0000-0000-000000000000"\)' app --include=*.py \
        | grep -vE "^${CANON}:" || true)
if [ -n "$hits" ]; then
  echo ""
  echo "ERROR: all-zero placeholder actor id."
  echo "       Use a resolver: system_actor()/human_actor()/agent_actor()/instance_actor()."
  echo "$hits"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "check_ledger_actors: FAILED — see app/ledger/README.md for the attribution model."
  exit 1
fi

echo "check_ledger_actors: OK — no hardcoded system/placeholder actor ids."
