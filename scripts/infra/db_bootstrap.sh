#!/usr/bin/env bash
#
# db_bootstrap.sh <env> — create the database access a MIGRATION CANNOT.
#
#   scripts/infra/db_bootstrap.sh dev
#
# Idempotent. Run it before the first deploy of a new environment, and
# again any time preflight reports a missing grant.
#
# ── WHY A SEPARATE STEP AND NOT PART OF THE BUILD ─────────────────────
#
# It connects as the database's OWNER. The build connects as the
# migrate login, and the whole point of this file is the set of things
# that login cannot do for itself -- so running it from the build would
# need the build to hold owner rights permanently, to do something that
# happens once per environment. The privilege would outlive the need.
#
# ── WHAT IT NEEDS ─────────────────────────────────────────────────────
#
#   OWNER_DSN      a DSN for a role that OWNS the database (or is a
#                  superuser). Not stored anywhere by this script.
#   The three logins are DERIVED from the DSNs the environment already
#   has in Secret Manager, never typed -- a name typed here that does
#   not match the one the services connect as produces an environment
#   that bootstraps cleanly and then cannot read its own tables.
set -euo pipefail

ENV_NAME="${1:?environment name required}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
HERE="$(cd "$(dirname "$0")" && pwd)"

case "$ENV_NAME" in
  "") echo "ERROR: environment name must not be empty" >&2; exit 1 ;;
  *[!a-z0-9-]*)
    echo "ERROR: environment name '${ENV_NAME}' must be lowercase letters, digits and hyphens" >&2
    exit 1 ;;
esac

command -v psql >/dev/null || { echo "ERROR: psql is required" >&2; exit 1; }

# ── the logins, read out of the DSNs the services actually use ────────
#
# DERIVED, NOT CONFIGURED, for the reason names.sh gives at length: two
# values that must agree are two values that eventually do not. The
# failure here is quiet -- grants land on a role nobody connects as.
user_of() {
  local secret="$1" dsn
  dsn=$(gcloud secrets versions access latest --secret="$secret" \
          --project="$PROJECT" 2>/dev/null) || {
    echo "ERROR: cannot read secret ${secret}." >&2
    echo "       Create it before bootstrapping: it is the DSN the" >&2
    echo "       services connect with, and its USERNAME is what this" >&2
    echo "       script grants to." >&2
    return 1; }
  # postgresql://USER:pass@host/db  ->  USER
  printf '%s' "$dsn" | sed -n 's|^[a-z+]*://\([^:/@]*\).*|\1|p'
}

APP_USER="$(user_of "$(agentos_secret database-url)")"
MIGRATE_USER="$(user_of "$(agentos_secret database-url-migrate)")"
# DATABASE comes from names.sh, which this file already sources. It
# used to be re-derived here as `control_panel_${ENV_NAME}` -- a second
# copy of the rule, in the script that CREATES the grants, so a
# single-environment install bootstrapped `control_panel_agentos`
# while every other part of the system connected to `control_panel`.
# Two scripts agreeing by coincidence is the exact failure names.sh
# exists to prevent.

[ -n "$APP_USER" ]     || { echo "ERROR: no username in $(agentos_secret database-url)" >&2; exit 1; }
[ -n "$MIGRATE_USER" ] || { echo "ERROR: no username in $(agentos_secret database-url-migrate)" >&2; exit 1; }

OWNER_DSN="${OWNER_DSN:-}"
if [ -z "$OWNER_DSN" ]; then
  echo "ERROR: set OWNER_DSN to a DSN for a role that OWNS ${DATABASE}." >&2
  echo >&2
  echo "  NOT the migrate login, and on Cloud SQL usually NOT \`postgres\`" >&2
  echo "  either: \`postgres\` is a member of cloudsqlsuperuser, which does" >&2
  echo "  not make it the owner. A database created by an app role is" >&2
  echo "  owned by that role, and only the owner can GRANT CREATE on it." >&2
  echo "  Find it with:" >&2
  echo "      SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='${DATABASE}';" >&2
  exit 1
fi

# THE EXECUTION POD'S LOGIN, derived before anything is applied
# because db_bootstrap.sql has to GRANT CONNECT to it in the same
# breath as it revokes PUBLIC's. Deriving it afterwards is how it was
# missed the first time.
LEG_ROLE="${AGENTOS_LEG_ROLE:-}"
if [ -z "$LEG_ROLE" ]; then
  if [ -z "${AGENTOS_POOL_SCOPE-}" ]; then
    LEG_ROLE="agentos_leg"
  else
    LEG_ROLE="agentos_leg_${ENV_NAME}"
  fi
fi

echo "bootstrapping ${DATABASE}"
echo "  app login      ${APP_USER}"
echo "  migrate login  ${MIGRATE_USER}"
echo "  leg login      ${LEG_ROLE}"

# ── WHERE TO RUN IT ───────────────────────────────────────────────────
#
# A Cloud SQL DSN is a UNIX-SOCKET DSN: the path exists inside Cloud
# Run and nowhere else, so psql on a workstation cannot open it. The
# one place already holding a proxy is the conductor pod -- which has
# asyncpg and no psql, which is why db_bootstrap.sql was written
# without psql variables.
#
# So: psql when the DSN is reachable, the pod when it is not, and a
# refusal naming both when neither is available. The SQL is the same
# file either way.
HERE_SQL="${HERE}/db_bootstrap.sql"
# psql's \i takes the path as SQL text, so Git Bash never translates it:
# a Windows psql handed /c/... reports "No such file". cygpath -m gives
# C:/... which both shells and psql read; elsewhere the path is unchanged.
PSQL_DIR="$HERE"
command -v cygpath >/dev/null 2>&1 && PSQL_DIR="$(cygpath -m "$HERE")"

_py() {
  local c
  for c in python3 python py; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys' >/dev/null 2>&1 || continue
    printf "%s" "$c"; return 0
  done
  return 1
}
PY="$(_py || true)"

case "$OWNER_DSN" in
  *"host=/cloudsql/"*) SOCKET_DSN="yes" ;;
  *)                   SOCKET_DSN="no"  ;;
esac

if [ "$SOCKET_DSN" = "no" ] && command -v psql >/dev/null 2>&1; then
  psql "$OWNER_DSN" --set=ON_ERROR_STOP=1 -q <<SQLPRELUDE
SELECT set_config('bootstrap.db',            '${DATABASE}',     false);
SELECT set_config('bootstrap.app_user',      '${APP_USER}',     false);
SELECT set_config('bootstrap.migrate_user',  '${MIGRATE_USER}', false);
SELECT set_config('bootstrap.leg_role',      '${LEG_ROLE}',     false);
\i '${PSQL_DIR}/db_bootstrap.sql'
SQLPRELUDE
  # MIGRATE_DSN overrides the secret on THIS path only. The secret is the
  # Cloud Run socket form, which psql cannot open from a laptop; a new
  # environment has no conductor pod yet, and its first migrate needs the
  # owner grants above -- so the first run has to come through a local
  # cloud-sql-proxy, with both DSNs pointed at it.
  MIGRATE_DSN_RAW="${MIGRATE_DSN:-$(gcloud secrets versions access latest       --secret="$(agentos_secret database-url-migrate)" --project="$PROJECT")}"
  psql "$MIGRATE_DSN_RAW" --set=ON_ERROR_STOP=1 -q <<SQLGRANTS
SELECT set_config('bootstrap.app_user', '${APP_USER}', false);
\i '${PSQL_DIR}/db_bootstrap_grants.sql'
SQLGRANTS
  echo "  applied (psql)"
else
  NS="${NAMESPACE}"
  [ -n "$PY" ] || { echo "ERROR: no working python on PATH" >&2; exit 1; }
  kubectl get deploy conductor -n "$NS" >/dev/null 2>&1 || {
    echo "ERROR: the owner DSN is a Cloud SQL socket DSN, so psql cannot" >&2
    echo "       open it from here, and ${NS} has no conductor pod to run" >&2
    echo "       it in. Either run this from somewhere with a TCP DSN (or" >&2
    echo "       a local cloud-sql-proxy), or start the execution plane" >&2
    echo "       first." >&2
    exit 1; }

  # user, password and database kept; address replaced by the
  # sidecar's. Done in Python because a password may contain anything,
  # and sed on a credential is how credentials break.
  SIDECAR_DSN=$("$PY" - "$OWNER_DSN" <<'PY'
import sys
from urllib.parse import urlsplit, unquote, quote, parse_qs
u = urlsplit(sys.argv[1])
db = (u.path or "/").lstrip("/")
print("postgresql://%s:%s@127.0.0.1:5432/%s"
      % (quote(unquote(u.username or ""), safe=""),
         quote(unquote(u.password or ""), safe=""), db))
PY
)

  # The SECOND connection the applier needs. Same rewrite, the
  # migrate login instead of the owner.
  MIGRATE_DSN_RAW=$(gcloud secrets versions access latest \
      --secret="$(agentos_secret database-url-migrate)" --project="$PROJECT")
  MIGRATE_SIDECAR=$("$PY" - "$MIGRATE_DSN_RAW" <<'PY'
import sys
from urllib.parse import urlsplit, unquote, quote
u = urlsplit(sys.argv[1])
print("postgresql://%s:%s@127.0.0.1:5432/%s"
      % (quote(unquote(u.username or ""), safe=""),
         quote(unquote(u.password or ""), safe=""),
         (u.path or "/").lstrip("/")))
PY
)

  echo "  (socket DSN -- applying inside ${NS}/conductor via its proxy sidecar)"
  {
    "$PY" - "$SIDECAR_DSN" "$DATABASE" "$APP_USER" "$MIGRATE_USER" "$MIGRATE_SIDECAR" "$LEG_ROLE" <<'PY'
import sys
print('import os')
for k, v in zip(('DATABASE_URL', 'BOOTSTRAP_DB', 'BOOTSTRAP_APP_USER',
                 'BOOTSTRAP_MIGRATE_USER', 'MIGRATE_DATABASE_URL',
                 'BOOTSTRAP_LEG_ROLE'), sys.argv[1:]):
    print('os.environ[%s] = %s' % (ascii(k), ascii(v)))
PY
    "$PY" -c "import io,sys; print('os.environ[%s] = %s' % (ascii(sys.argv[2]), ascii(io.open(sys.argv[1], encoding='utf-8').read())))" "$HERE_SQL" BOOTSTRAP_SQL
    "$PY" -c "import io,sys; print('os.environ[%s] = %s' % (ascii(sys.argv[2]), ascii(io.open(sys.argv[1], encoding='utf-8').read())))" "${HERE}/db_bootstrap_grants.sql" BOOTSTRAP_SQL_GRANTS
    cat "${HERE}/db_bootstrap_apply.py"
  } | MSYS_NO_PATHCONV=1 kubectl exec -i -n "$NS" \
        deploy/conductor -c conductor -- python -
fi

# ── the leg login's password ──────────────────────────────────────────
#
# SET FROM THE SECRET, EVERY RUN, rather than compared with it. There
# is no way to read a Postgres password back, so two values that must
# agree can only be kept agreeing by writing one from the other.

if LEG_PW=$(gcloud secrets versions access latest               --secret="$(agentos_secret execution-db-password)"               --project="$PROJECT" 2>/dev/null); then
  if [ "$SOCKET_DSN" = "no" ] && command -v psql >/dev/null 2>&1 \
     && [ -z "$(psql "$OWNER_DSN" -tAc "SELECT 1 FROM pg_roles WHERE rolname = '${LEG_ROLE}'")" ]; then
    # First run, before the migration that creates the role. See the
    # matching skip in db_bootstrap.sql.
    echo "  WARNING: ${LEG_ROLE} does not exist yet; its password is set by" >&2
    echo "           the run after the first deploy." >&2
  elif [ "$SOCKET_DSN" = "no" ] && command -v psql >/dev/null 2>&1; then
    PGPASSWORD_SQL=$(psql "$OWNER_DSN" -tAc "SELECT quote_literal('${LEG_PW}')")
    psql "$OWNER_DSN" --set=ON_ERROR_STOP=1 -q       -c "ALTER ROLE ${LEG_ROLE} PASSWORD ${PGPASSWORD_SQL}"
    echo "  ${LEG_ROLE} password set from execution-db-password-${ENV_NAME}"
  else
    {
      "$PY" - "$SIDECAR_DSN" "$LEG_ROLE" "$LEG_PW" <<'PY'
import sys
print('import os')
for k, v in zip(('DATABASE_URL', 'LEG_ROLE', 'LEG_PW'), sys.argv[1:]):
    print('os.environ[%s] = %s' % (ascii(k), ascii(v)))
PY
      cat <<'PY'
import asyncio, os
async def m():
    import asyncpg
    cn = await asyncpg.connect(os.environ["DATABASE_URL"])
    role = await cn.fetchval("SELECT quote_ident($1)", os.environ["LEG_ROLE"])
    pw = await cn.fetchval("SELECT quote_literal($1)", os.environ["LEG_PW"])
    await cn.execute(f"ALTER ROLE {role} PASSWORD {pw}")
    await cn.close()
asyncio.run(m())
PY
    } | MSYS_NO_PATHCONV=1 kubectl exec -i -n "${NAMESPACE}"           deploy/conductor -c conductor -- python -
    echo "  ${LEG_ROLE} password set from execution-db-password-${ENV_NAME}"
  fi
else
  echo "  WARNING: execution-db-password-${ENV_NAME} not found." >&2
  echo "           ${LEG_ROLE} has no password, so execution pods cannot" >&2
  echo "           log in -- pod_main calls leg_dsn() unconditionally." >&2
fi

echo
echo "done. Verify with:  scripts/infra/env_preflight.sh ${ENV_NAME}"
