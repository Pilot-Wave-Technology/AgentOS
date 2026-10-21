"""Apply db_bootstrap.sql over asyncpg, for where psql cannot reach.

NOT A SECOND IMPLEMENTATION. It executes the same file, verbatim; the
only thing it does that psql would not is set the three session
settings the file requires, as PARAMETERS rather than as text, so a
name can never be read as SQL.

EVERYTHING COMES FROM THE ENVIRONMENT, including the two SQL files
themselves. The first version took them as module-level globals the
caller prepended to the pipe, which works and is a bad idea: the file
cannot be read, run or linted on its own, and `check_unbound_names`
rightly failed the build over it -- a name read inside a function and
bound nowhere reachable is a NameError waiting for the one path that
does not define it.

    DATABASE_URL            the OWNER connection
    MIGRATE_DATABASE_URL    the migrate connection
    BOOTSTRAP_DB, BOOTSTRAP_APP_USER, BOOTSTRAP_MIGRATE_USER,
    BOOTSTRAP_LEG_ROLE      the four names
    BOOTSTRAP_SQL           the text of db_bootstrap.sql
    BOOTSTRAP_SQL_GRANTS    the text of db_bootstrap_grants.sql

WHY IT EXISTS. The bootstrap must connect as the database OWNER, and
on Cloud SQL the owner's DSN is a UNIX-SOCKET DSN -- a path that
exists inside Cloud Run and nowhere else. The one place in reach that
already has a proxy is the conductor pod, which carries asyncpg and no
psql. Requiring psql would mean the setup step cannot run where the
database is.
"""

import asyncio
import os
import sys

SETTINGS = (
    ("bootstrap.db", "BOOTSTRAP_DB"),
    ("bootstrap.app_user", "BOOTSTRAP_APP_USER"),
    ("bootstrap.migrate_user", "BOOTSTRAP_MIGRATE_USER"),
    ("bootstrap.leg_role", "BOOTSTRAP_LEG_ROLE"),
)


async def _settings(cn) -> None:
    for key, envvar in SETTINGS:
        await cn.execute("SELECT set_config($1, $2, false)",
                         key, os.environ[envvar])


async def _apply() -> int:
    import asyncpg

    required = [e for _, e in SETTINGS] + ["BOOTSTRAP_SQL", "BOOTSTRAP_SQL_GRANTS"]
    missing = [e for e in required if not os.environ.get(e, "").strip()]
    if missing:
        print("db_bootstrap: missing " + ", ".join(missing), file=sys.stderr)
        return 1

    cn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        who = await cn.fetchval("SELECT current_user")
        db = await cn.fetchval("SELECT current_database()")
        owner = await cn.fetchval(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1", db)
        print(f"  connected to {db} as {who} (owner: {owner})")

        # REFUSED RATHER THAN HALF-APPLIED. Several statements in the
        # file need the owner; a non-owner gets through the role
        # grants and fails at GRANT CREATE ON DATABASE, leaving an
        # environment that is neither as it was nor as it should be.
        if who != owner and not await cn.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname = $1", who):
            print(f"\nREFUSING: {who} neither owns {db} nor is a superuser.\n"
                  f"  Only the owner can GRANT CREATE ON DATABASE or REVOKE\n"
                  f"  CONNECT FROM PUBLIC. Connect as {owner}.", file=sys.stderr)
            return 1

        await _settings(cn)
        await cn.execute(os.environ["BOOTSTRAP_SQL"])
        print("  applied (owner)")
    finally:
        await cn.close()

    # ── the second connection, as the MIGRATE login ─────────────────
    #
    # ALTER DEFAULT PRIVILEGES FOR ROLE <r> needs membership in <r>,
    # and the owner has none -- Postgres refuses with `permission
    # denied to change default privileges`. The migrate login needs no
    # membership in itself, so that half runs as migrate.
    #
    # EACH CONNECTION DOES ONLY WHAT ITS IDENTITY MAY. The alternative
    # was giving the owner membership in the migrate role, which would
    # mean the owner could act as the migrate login for ever, to save
    # opening a second connection once per environment.
    mig = os.environ.get("MIGRATE_DATABASE_URL", "").strip()
    if not mig:
        print("  (no MIGRATE_DATABASE_URL -- default privileges NOT set)")
        return 1
    cn = await asyncpg.connect(mig)
    try:
        who = await cn.fetchval("SELECT current_user")
        print(f"  reconnected as {who} for the default privileges")
        await _settings(cn)
        await cn.execute(os.environ["BOOTSTRAP_SQL_GRANTS"])
        print("  applied (migrate)")
        return 0
    finally:
        await cn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_apply()))
