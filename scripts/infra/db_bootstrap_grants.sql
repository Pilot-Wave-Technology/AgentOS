-- db_bootstrap_grants.sql — run as the MIGRATE login, not the owner.
--
-- WHY IT IS SPLIT OFF. `ALTER DEFAULT PRIVILEGES FOR ROLE <r>`
-- requires membership in <r>. The database owner is not a member of
-- the migrate login, so running this beside the owner-only statements
-- fails with `permission denied to change default privileges` -- and
-- fails PART WAY THROUGH, leaving the database neither as it was nor
-- as it should be.
--
-- The migrate login needs no membership in itself: `ALTER DEFAULT
-- PRIVILEGES` with no FOR ROLE clause means FOR ROLE current_user.
--
-- BOTH DIRECTIONS, and the first is easy to forget. The default-
-- privileges rule only covers tables created AFTER it exists; without
-- the one-off sweep, every table created before this file was first
-- run stays unreachable to the app login. That is the failure that
-- hides longest -- the environment works, and then one endpoint
-- returns 500 because it touches an older table.
--
-- Reads bootstrap.app_user, set by the runner.

DO $GRANTS$
DECLARE
    s   text;
    app text := current_setting('bootstrap.app_user');
BEGIN
    IF coalesce(app, '') = '' THEN
        RAISE EXCEPTION 'db_bootstrap: bootstrap.app_user is not set';
    END IF;

    FOREACH s IN ARRAY ARRAY['public','kernel']
    LOOP
        -- kernel does not exist until its own migrations have run, and
        -- a first bootstrap legitimately happens before that.
        CONTINUE WHEN NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = s);

        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', s, app);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I',
            s, app);
        EXECUTE format(
            'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', s, app);

        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', s, app);
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
            'GRANT USAGE, SELECT ON SEQUENCES TO %I', s, app);
    END LOOP;
END
$GRANTS$;
