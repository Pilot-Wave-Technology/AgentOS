-- db_bootstrap.sql — the database setup a MIGRATION CANNOT DO.
--
-- Run by scripts/infra/db_bootstrap.sh, connected as the database's
-- OWNER. Every statement here is idempotent.
--
-- ── WHY THIS IS NOT A MIGRATION ──────────────────────────────────────
--
-- Everything in migrations/ runs as the migrate login. These four
-- things cannot be done by that login, by definition:
--
--   1. GRANT CREATE ON DATABASE. `CREATE SCHEMA kernel` is the first
--      thing the kernel migrations do and it needs CREATE on the
--      DATABASE, which only the owner can grant. A role cannot grant
--      itself a privilege it does not hold.
--
--   2. GRANT <role> TO <login> WITH ADMIN OPTION. Postgres roles are
--      CLUSTER-wide. On the FIRST environment the migrate login
--      creates agentos_worker and is therefore its owner, so the
--      grants inside the migrations work. On the SECOND environment
--      the role ALREADY EXISTS and this database's migrate login does
--      not own it -- so the very same migration fails, on a database
--      that is otherwise identical.
--
--   3. ALTER DEFAULT PRIVILEGES attaches to a GRANTOR. Without a rule
--      naming the migrate login as grantor, every table a FUTURE
--      migration creates is invisible to the app login. This is the
--      one that hides longest: the environment works, and then one
--      deploy adds a table and one endpoint starts returning 500.
--
--   4. REVOKE CONNECT FROM PUBLIC. Postgres grants CONNECT to PUBLIC
--      by default, so on an instance holding more than one database
--      every login can open every other database. Only the owner can
--      revoke it.
--
-- ── NO psql VARIABLES ────────────────────────────────────────────────
-- Every name is read from a SESSION SETTING and quoted with
-- format(%I), never interpolated. That is two things at once:
--
--   * a name containing unusual characters cannot become SQL, and
--   * this file runs under ANY Postgres client, not only psql.
--
-- The second matters more than it looks. The runner has to reach a
-- Cloud SQL instance whose DSN is a UNIX SOCKET, which from a
-- workstation means the proxy -- and the one place that already has a
-- proxy is the conductor pod, which has asyncpg and no psql. A file
-- that only psql can execute is a file that cannot be run where the
-- database actually is.

-- 0 ──────────────────────────────────────────────────────────────────
-- THE RUNNER SETS THESE FIRST, as three parameterised set_config()
-- calls, before executing this file:
--
--   bootstrap.db, bootstrap.app_user, bootstrap.migrate_user
--
-- Refuse rather than proceed if it did not: every statement below
-- would otherwise silently target a name of '' and grant nothing to
-- nobody, reporting success.
DO $REQUIRE$
DECLARE k text;
BEGIN
    FOREACH k IN ARRAY ARRAY['bootstrap.db','bootstrap.app_user','bootstrap.migrate_user',
                           'bootstrap.leg_role']
    LOOP
        IF coalesce(current_setting(k, true), '') = '' THEN
            RAISE EXCEPTION
              'db_bootstrap: % is not set. Run this through db_bootstrap.sh.', k;
        END IF;
    END LOOP;
END
$REQUIRE$;

-- 1 ──────────────────────────────────────────────────────────────────
DO $DBPRIV$
BEGIN
    EXECUTE format('GRANT CREATE, CONNECT, TEMPORARY ON DATABASE %I TO %I',
                   current_setting('bootstrap.db'),
                   current_setting('bootstrap.migrate_user'));
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I',
                   current_setting('bootstrap.db'),
                   current_setting('bootstrap.app_user'));

    -- THE EXECUTION POD'S LOGIN, and it is easy to forget precisely
    -- because it is not one of the two DSNs this script reads. It was
    -- forgotten on the first run against dev: revoking PUBLIC's
    -- CONNECT took the only grant it had, and every execution pod
    -- lost the database. Nothing failed here -- the next pod to start
    -- failed, somewhere else, later.
    -- ON A FIRST RUN THE ROLE DOES NOT EXIST YET: the migration
    -- kernel_leg_role_is_per_environment creates it, and the first
    -- migrate needs THIS script's grants to run at all. So skip it
    -- loudly here, and the run after the first deploy grants it.
    IF coalesce(current_setting('bootstrap.leg_role', true), '') <> '' THEN
        IF EXISTS (SELECT 1 FROM pg_roles
                    WHERE rolname = current_setting('bootstrap.leg_role')) THEN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I',
                           current_setting('bootstrap.db'),
                           current_setting('bootstrap.leg_role'));
        ELSE
            RAISE WARNING 'leg role % does not exist yet (the migrations create it); re-run db_bootstrap after the first deploy',
                          current_setting('bootstrap.leg_role');
        END IF;
    END IF;
END
$DBPRIV$;

-- 2 ──────────────────────────────────────────────────────────────────
-- The roles may not exist yet on a first-ever deployment: the
-- migrations create them. Create them here too, NOLOGIN and empty, so
-- that the membership grants below have something to name. A role that
-- exists with no privileges is inert; the migrations then grant onto
-- it. Doing it in the other order is what fails.
DO $ROLES$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['agentos_worker','agentos_verifier','agentos_human']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END
$ROLES$;

DO $MEMBER$
BEGIN
    EXECUTE format(
      'GRANT agentos_worker, agentos_verifier, agentos_human TO %I WITH ADMIN OPTION',
      current_setting('bootstrap.migrate_user'));
    EXECUTE format(
      'GRANT agentos_worker, agentos_verifier, agentos_human TO %I',
      current_setting('bootstrap.app_user'));
END
$MEMBER$;

-- 3 ── NOT HERE. See db_bootstrap_grants.sql ────────────────────────
-- ALTER DEFAULT PRIVILEGES FOR ROLE <migrate> requires MEMBERSHIP in
-- that role, and the database owner is not a member of it -- Postgres
-- refuses with `permission denied to change default privileges`,
-- which is what this file did on its first run against dev.
--
-- The migrate login CAN set its own, because `ALTER DEFAULT
-- PRIVILEGES` with no FOR ROLE means FOR ROLE current_user. So that
-- half runs on a second connection, as migrate, from the same script.
-- Each connection does only what its identity is allowed to do, which
-- is the same reason this file exists apart from the migrations.

-- 4 ──────────────────────────────────────────────────────────────────
-- LAST, and after the explicit CONNECT grants above, so that revoking
-- PUBLIC cannot lock this database's own logins out of it.
DO $ISOLATE$
BEGIN
    EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC',
                   current_setting('bootstrap.db'));
END
$ISOLATE$;
