"""The database half of preflight: roles, memberships, privileges.

WHY THIS IS NOT A TEST. A test asserts the code is right. This asserts
the ENVIRONMENT is right, against the live database, before anything
deploys into it -- and the four defects it looks for all shipped green
test suites. They were never in the code.

WHY IT IS NOT A MIGRATION EITHER. Everything here is a fact a migration
cannot establish about itself:

  * A migrate role cannot GRANT CREATE ON DATABASE to itself; that
    needs the owner.
  * Roles are CLUSTER-wide. On the SECOND environment `agentos_worker`
    already exists and this database's migrate role does not own it,
    so `GRANT agentos_worker TO <app>` fails without ADMIN OPTION --
    which the first environment never needed and so never granted.
  * Default privileges are attached to a GRANTOR. `next` works only
    because a blanket rule was set by hand years ago; nothing in the
    repo creates it, and `dev` therefore did not have it.

So the migrations run, report success, and leave an environment that
cannot serve a request. This file is what notices.

Exit 0 = every fact holds. Exit 1 = something is missing or could not
be determined. `could not be determined` is a failure: see report.sh.
"""
# NO `from __future__ import annotations`. This file is piped into
# `python -` with a few os.environ assignments prepended, and a future
# import must be the first statement in a file -- prepending anything
# makes it a SyntaxError. The annotations it would enable (`str |
# None`) are native from 3.10, and both the pod and the workstation
# run 3.12.

import asyncio
import os
import sys

MEMBER_ROLES = ("agentos_worker", "agentos_verifier", "agentos_human")

_miss: list[str] = []
_unknown: list[str] = []


def ok(what: str, detail: str) -> None:
    print(f"  [ ok ] {what:<10} {detail}")


def miss(what: str, detail: str, fix: str = "") -> None:
    _miss.append(detail)
    print(f"  [MISS] {what:<10} {detail}")
    if fix:
        print(f"         fix: {fix}")


def unknown(what: str, detail: str) -> None:
    _unknown.append(detail)
    print(f"  [ ?? ] {what:<10} {detail}")
    print("         could not determine -- treating as failure")


async def check(dsn: str, env: str) -> None:
    import asyncpg

    cn = await asyncpg.connect(dsn)
    me = await cn.fetchval("SELECT current_user")
    db = await cn.fetchval("SELECT current_database()")
    print(f"\n-- database {db} as {me} " + "-" * 30)

    # ── the roles exist ──────────────────────────────────────────────
    # `agentos_leg` holds the pods' grants, so it is always required. The
    # login the pods CONNECT as (LEG_ROLE, derived as the deploy derives
    # it) is required too when it is a different role -- a per-environment
    # login that works only through membership. Asking for
    # `agentos_leg_<env>` regardless failed every install whose pods
    # connect as `agentos_leg` itself, single-environment ones included.
    leg_role = os.environ.get("LEG_ROLE", "").strip()
    want = list(MEMBER_ROLES) + ["agentos_leg"]
    if leg_role and leg_role != "agentos_leg":
        want.append(leg_role)
    have = {r["rolname"] for r in await cn.fetch(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY($1::text[])", want)}
    for r in want:
        if r in have:
            ok("role", r)
        else:
            miss("role", f"{r} does not exist",
                 "the migrations create these; did migrate-execute run?")

    # ── this login can create the kernel schema ──────────────────────
    #
    # `CREATE SCHEMA kernel` is the first thing the kernel migrations
    # do, and it needs CREATE on the DATABASE -- a privilege separate
    # from anything inside it. dev failed here twice, as a bare
    # `permission denied for database control_panel_dev`.
    if await cn.fetchval("SELECT has_database_privilege($1, $2, 'CREATE')", me, db):
        ok("db-create", f"{me} may CREATE in {db}")
    else:
        miss("db-create", f"{me} cannot CREATE in {db} — CREATE SCHEMA kernel will fail",
             f"GRANT CREATE ON DATABASE {db} TO {me};  (run as the database OWNER)")

    # ── membership, and the ADMIN OPTION that lets it be re-granted ──
    #
    # ASKED OF POSTGRES, NOT READ FROM ONE ROW. From PostgreSQL 16 a
    # membership can have several pg_auth_members rows, one per grantor:
    # the login that CREATES a role is granted it WITH ADMIN OPTION by the
    # bootstrap superuser, and a later plain `GRANT role TO login` adds a
    # second row without it. Reading one row picked whichever came first
    # and failed a fresh install whose migrate login can re-grant (proven
    # by doing it). pg_has_role weighs every grant.
    for role in MEMBER_ROLES:
        if not await cn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
            continue   # absence is reported by the role check above
        member = await cn.fetchval(
            "SELECT pg_has_role($1, $2, 'MEMBER')", me, role)
        admin = await cn.fetchval(
            "SELECT pg_has_role($1, $2, 'MEMBER WITH ADMIN OPTION')", me, role)
        if not member:
            miss("member", f"{me} is not a member of {role}",
                 f"GRANT {role} TO {me} WITH ADMIN OPTION;  (run as the database OWNER)")
        elif not admin:
            miss("member", f"{me} holds {role} without ADMIN OPTION — cannot re-grant it",
                 f"GRANT {role} TO {me} WITH ADMIN OPTION;")
        else:
            ok("member", f"{me} holds {role} WITH ADMIN OPTION")

    # ── the app login can actually read and write ────────────────────
    #
    # NOT A SAMPLE. The failure this catches is a table added by a later
    # migration that the default-privileges rule does not cover, and a
    # sample is exactly what would miss it.
    app = os.environ.get("APP_DB_USER", "").strip()
    if not app:
        unknown("app-priv", "APP_DB_USER not set — cannot check the app login's grants")
    elif not await cn.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", app):
        miss("app-priv", f"app login {app} does not exist")
    else:
        bad = await cn.fetch(
            """SELECT c.relname, n.nspname
                 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r','p') AND n.nspname IN ('public','kernel')
                  AND NOT has_table_privilege($1, c.oid, 'SELECT')""", app)
        if bad:
            names = ", ".join(f'{r["nspname"]}.{r["relname"]}' for r in bad[:6])
            miss("app-priv", f"{app} cannot SELECT {len(bad)} table(s): {names}"
                             + (" …" if len(bad) > 6 else ""),
                 f"run scripts/infra/db_bootstrap.sh {env} — it sets ALTER DEFAULT PRIVILEGES "
                 f"so later migrations stay covered")
        else:
            ok("app-priv", f"{app} can SELECT every table in public and kernel")

    # ── PUBLIC must not be able to reach this database ───────────────
    #
    # Postgres defaults to granting CONNECT to PUBLIC. On an instance
    # holding more than one environment that means every login can open
    # every other environment's database.
    # BOTH DIRECTIONS. The first version of this check asked only
    # whether PUBLIC could connect, and passed an environment whose
    # EXECUTION PODS had just lost the database: revoking PUBLIC took
    # the only CONNECT grant agentos_leg_<env> had. A check that tests
    # the lock and not the key reports a locked door as secure.
    leg = os.environ.get("LEG_ROLE", "").strip()
    for role in [r for r in (me, app, leg) if r]:
        if not await cn.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
            continue
        if await cn.fetchval("SELECT has_database_privilege($1, $2, 'CONNECT')",
                             role, db):
            ok("can-connect", f"{role} may CONNECT to {db}")
        else:
            miss("can-connect", f"{role} has NO CONNECT on {db}",
                 f"GRANT CONNECT ON DATABASE {db} TO {role};")

    if await cn.fetchval("SELECT has_database_privilege('public', $1, 'CONNECT')", db):
        miss("isolation", f"PUBLIC holds CONNECT on {db} — any login on this instance can open it",
             f"REVOKE CONNECT ON DATABASE {db} FROM PUBLIC;  then GRANT CONNECT to each real login")
    else:
        ok("isolation", f"PUBLIC cannot CONNECT to {db}")

    await cn.close()


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    env = os.environ.get("ENV_NAME", "").strip()
    if not dsn or not env:
        print("check_db: DATABASE_URL and ENV_NAME are required", file=sys.stderr)
        return 1
    try:
        asyncio.run(check(dsn, env))
    except Exception as exc:                                    # noqa: BLE001
        # A connection that fails is NOT an environment that passed.
        # THE TYPE, NOT JUST THE MESSAGE. asyncpg raises several
        # exceptions whose str() is empty -- a socket that is not
        # there being the common one -- and "could not reach the
        # database: " tells the reader nothing at all.
        detail = str(exc) or exc.__class__.__name__
        unknown("connect", f"could not reach the database: {detail}")
    print(f"\ndatabase: {len(_miss)} missing, {len(_unknown)} undetermined")
    return 1 if (_miss or _unknown) else 0


if __name__ == "__main__":
    raise SystemExit(main())
