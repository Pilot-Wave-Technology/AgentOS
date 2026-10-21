#!/usr/bin/env bash
#
# ./publish-extension.sh <agentos-vscode-X.Y.Z.vsix> [notes]
#
# Make a VS Code extension build available from THIS deployment: the
# in-app page (Settings -> VS Code, /extension) offers it for download, and
# installed extensions find it on their daily update check.
#
# Where the .vsix comes from: the `extension/` folder of the AgentOS `main`
# branch (deploy/07-build-and-deploy.md, 7.5). Download it and pass its path here.
#
# What it does, and nothing else:
#   1. uploads the file to the extension bucket terraform created
#      (`terraform output extension_bucket`), private -- the control plane
#      serves it, so nobody needs bucket access;
#   2. writes latest.json naming it, which is what the page and the
#      extension read. Publishing a new version is the same command again;
#      older .vsix files stay in the bucket for rollback.
#
# No redeploy needed: the control plane reads latest.json on each request.
set -euo pipefail

VSIX="${1:?usage: ./publish-extension.sh <agentos-vscode-X.Y.Z.vsix> [notes]}"
NOTES="${2:-}"
[ -f "$VSIX" ] || { echo "ERROR: $VSIX not found" >&2; exit 1; }

NAME="$(basename "$VSIX")"
VERSION="$(printf '%s' "$NAME" | sed -n 's/^agentos-vscode-\([0-9][0-9.]*[0-9]\)\.vsix$/\1/p')"
[ -n "$VERSION" ] || {
  echo "ERROR: expected a file named agentos-vscode-<version>.vsix, got $NAME" >&2
  exit 1
}

HERE="$(cd "$(dirname "$0")" && pwd)"
BUCKET="${EXTENSION_BUCKET:-$(cd "$HERE/terraform" && terraform output -raw extension_bucket 2>/dev/null || true)}"
[ -n "$BUCKET" ] || {
  echo "ERROR: no extension bucket. Run 'terraform apply' in terraform/ first," >&2
  echo "       or pass EXTENSION_BUCKET=<bucket>." >&2
  exit 1
}

echo "publishing $NAME (version $VERSION) to gs://$BUCKET"
gcloud storage cp "$VSIX" "gs://$BUCKET/$NAME" --quiet

# json.dumps, not a heredoc: notes may contain quotes or newlines.
# THE FIRST PYTHON THAT RUNS, not `python3` by name: on Windows that is the
# Microsoft Store stub, which prints an advert and writes nothing.
PY=""
for cand in python3 python py; do
  command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import json' >/dev/null 2>&1 && { PY="$cand"; break; }
done
[ -n "$PY" ] || { echo "ERROR: no working python found (python3/python/py)" >&2; exit 1; }
"$PY" - "$VERSION" "$NAME" "$NOTES" > "${TMPDIR:-/tmp}/agentos-latest.json" <<'PY'
import json, sys
print(json.dumps({"version": sys.argv[1], "object": sys.argv[2], "notes": sys.argv[3]}, indent=2))
PY
gcloud storage cp "${TMPDIR:-/tmp}/agentos-latest.json" "gs://$BUCKET/latest.json" --quiet
rm -f "${TMPDIR:-/tmp}/agentos-latest.json"

echo "done: the deployment now offers $VERSION at /extension"
