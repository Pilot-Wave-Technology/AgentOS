#!/usr/bin/env bash
set -euo pipefail
[ -d .git ] && mkdir -p .git/info && echo -e ".venv/\n" >> .git/info/exclude || true
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --no-input -q -r requirements.txt -r requirements-dev.txt
python3 -m pip install --no-input -q -e packages/agentos-ledger-client
