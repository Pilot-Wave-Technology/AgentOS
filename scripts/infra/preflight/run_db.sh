#!/usr/bin/env bash
#
# run_db.sh — run check_db.py somewhere the database can actually be
# reached, and say which route was taken.
#
# ── THE PROBLEM ───────────────────────────────────────────────────────
#
# The migrate DSN is a Cloud SQL UNIX-SOCKET DSN:
#
#   postgresql://user:pw@/control_panel_dev?host=/cloudsql/<instance>
#
# That path exists inside Cloud Run and nowhere else. Run preflight
# from a workstation and the connection fails -- with an EMPTY error,
# because asyncpg's str() for a missing socket is blank. "Could not
# reach the database:" and nothing after it.
#
# The failure is honest (undetermined, therefore fatal) and useless.
#
# ── THE ROUTE ─────────────────────────────────────────────────────────
#
# Every environment already runs a pod with a cloud-sql-proxy sidecar
# on 127.0.0.1:5432 -- the conductor. So when the DSN is socket-form
# and there is a conductor to borrow, the same script runs THERE, with
# the same credentials rewritten to the sidecar's address.
#
# STILL AS THE MIGRATE LOGIN. The conductor's own DSN belongs to a
# different role, and the privileges in question are the migrate
# login's: it is the one that has to CREATE SCHEMA and re-grant the
# three cluster-wide roles. Borrowing the pod's network is not the
# same as borrowing its identity, and only the first is wanted.
#
# Sourced by env_preflight.sh.

_dsn_is_socket() { case "$1" in *"host=/cloudsql/"*) return 0 ;; *) return 1 ;; esac; }

# Rewrite a Cloud SQL socket DSN to the sidecar's TCP address, keeping
# user, password and database. Done in Python because a password may
# contain anything, and sed on a credential is how credentials break.
_dsn_to_sidecar() {
  "$PY" - "$1" <<'PY'
import sys
from urllib.parse import urlsplit, unquote, quote
u = urlsplit(sys.argv[1])
user = unquote(u.username or "")
pw   = unquote(u.password or "")
db   = (u.path or "/").lstrip("/")
print("postgresql://%s:%s@127.0.0.1:5432/%s"
      % (quote(user, safe=""), quote(pw, safe=""), db))
PY
}

# run_db_check DSN — prints the check's output, returns its status.
run_db_check() {
  local dsn="$1" sidecar

  if ! _dsn_is_socket "$dsn"; then
    if ! "$PY" -c 'import asyncpg' >/dev/null 2>&1; then
      unknown "database" "${PY} has no asyncpg (pip install asyncpg)"
      return 1
    fi
    echo "  (connecting directly)"
    DATABASE_URL="$dsn" ENV_NAME="$ENV_NAME" APP_DB_USER="$APP_DB_USER" LEG_ROLE="${LEG_ROLE:-}" \
      "$PY" "${HERE}/preflight/check_db.py"
    return $?
  fi

  # Socket-form. Only a pod can open it.
  if ! kubectl get deploy conductor -n "$NAMESPACE" >/dev/null 2>&1; then
    unknown "database" \
      "the DSN is a Cloud SQL socket DSN, reachable only from inside the cluster, and ${NAMESPACE} has no conductor to run the check in. Start the execution plane first, or run preflight with a TCP DSN."
    return 1
  fi

  sidecar="$(_dsn_to_sidecar "$dsn")" || {
    unknown "database" "could not rewrite the socket DSN to the sidecar address"
    return 1; }

  echo "  (socket DSN -- running inside ${NAMESPACE}/conductor via its proxy sidecar)"

  # THE VALUES GO IN ON STDIN, NOT AS ARGUMENTS. `kubectl exec --env`
  # does not exist in every kubectl, and `-- env VAR=... python -`
  # would put the DSN -- password and all -- in the pod's process
  # table for anything with /proc access to read. Prepending them to
  # the script keeps them in the pipe.
  #
  # Written by Python's repr, so a password containing quotes or
  # backslashes cannot terminate the literal.
  local prelude
  prelude=$("$PY" - "$sidecar" "$ENV_NAME" "${APP_DB_USER:-}" "${LEG_ROLE:-}" <<'PY'
import sys
print('import os')
for k, v in zip(('DATABASE_URL', 'ENV_NAME', 'APP_DB_USER', 'LEG_ROLE'),
                sys.argv[1:]):
    print('os.environ[%s] = %s' % (ascii(k), ascii(v)))
PY
)

  # MSYS_NO_PATHCONV ON THIS CALL ONLY. Git Bash rewrites arguments
  # that look like absolute paths, which mangles `deploy/conductor`
  # and the `--` separator. Exporting it for the whole run instead
  # breaks gcloud, whose launcher depends on the conversion -- every
  # gcloud check then fails with a doubled drive root, which is how
  # this was found.
  {
    printf '%s\n' "$prelude"
    cat "${HERE}/preflight/check_db.py"
  } | MSYS_NO_PATHCONV=1 kubectl exec -i -n "$NAMESPACE" \
        deploy/conductor -c conductor -- python -
}
