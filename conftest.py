"""Repo-root pytest configuration.

TWO JOBS, both about a suite that runs from a plain checkout:
make `agentos_ledger_client` importable, and make sure there is a
database.

── 1. THE PACKAGE ──

`app/ledger/*` and `kernel/ledger/chain.py` re-export it, and it lives
under `packages/agentos-ledger-client/src` -- a `src` layout, so having
the repo root on `sys.path` does not reach it. The Docker image installs
it editable (`Dockerfile`, builder stage) and CI therefore passes, which
is exactly what makes the gap hard to see: every test that imports
`app.ledger` fails at COLLECTION on a developer's own machine and
nowhere else.

AT THE ROOT, not in `app/services/tests/conftest.py`, and that
distinction cost a suite run to find. A conftest applies to its own
directory downwards, so putting this there left
`app/services/model_registry/tests/test_router_view_model.py` --
which imports the app, which imports the ledger -- still failing with
`ModuleNotFoundError: No module named 'agentos_ledger_client'`. Any test
anywhere in the repository can reach the ledger through an import chain,
so the fix has to sit above all of them.

A PATH ENTRY RATHER THAN REQUIRING `pip install -e`: a test run must not
depend on each person having remembered a setup step. The editable
install is still the right thing for a working venv -- this makes the
suite honest when somebody has not done it yet.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PKG_SRC = _ROOT / "packages" / "agentos-ledger-client" / "src"

if _PKG_SRC.is_dir() and str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

# ── A DATABASE WITHOUT DOCKER ────────────────────────────────────────
#
# THE FAILURE THIS REMOVES. The suite reads `DATABASE_URL` and assumes
# something is listening. When nothing is -- a stopped container, a
# Docker Desktop restart, a fresh clone -- asyncpg spends its full
# connect timeout per test, so a missing database presents as a SLOW
# SUITE rather than an error. One run cost fifteen minutes before anyone
# thought to check `docker ps`, and the container had exited twenty-eight
# minutes earlier when Docker restarted.
#
# `pgserver` ships an actual PostgreSQL binary as a wheel and runs it
# from Python: no Docker, no system install, no port to remember. MARL
# already uses it for the same reason, which is the other half of why it
# is the right choice here -- two repositories that will share a ledger
# client should not need two different ways to get a database.
#
# ONLY WHEN `DATABASE_URL` IS UNSET. CI sets it (cloudbuild's `test-db`
# step), and so does anyone running their own server; this is the
# fallback for the case that used to be a quiet timeout. Setting it is
# also the escape hatch if pgserver ever misbehaves.
#
# COSTS, MEASURED ON THIS MACHINE: 11.5s the first time (that is
# `initdb`), 0.33s on every run after, because the data directory is
# cached under the system temp dir and the server is reused. A fresh
# DATABASE is created per session inside it, so state never carries
# between runs even though the cluster does.
#
# dev-only: `pgserver` belongs in requirements-dev.txt and must never
# reach the runtime image.
_PG_DATA = pathlib.Path(tempfile.gettempdir()) / "agentos-suite-pgdata"
_PG_DB = "agentos_test"
# Migrated once per machine; every session's database is copied from it.
# Bump the suffix to force a rebuild after a schema change that the
# migration runner cannot apply forward.
_PG_TEMPLATE = "agentos_test_tmpl"

# ── running in parallel (pytest-xdist) ────────────────────────────────
#
# Serial runs are completely unaffected by everything below: with no
# xdist, `PYTEST_XDIST_WORKER` is unset, the database keeps its old
# name and the lock is taken once, uncontended. That is the whole
# compatibility story -- Cloud Build passes no `-n`, so it takes
# exactly the path it took before.
#
# Under `-n`, each worker is a SEPARATE PROCESS that runs this
# conftest, and three things then race: starting the cluster,
# migrating the template, and stamping a session database from it.
# `CREATE DATABASE ... TEMPLATE` also refuses to run while anything
# else is reading the template. So the whole setup is serialised on a
# file lock, and each worker gets its own database.
_PG_SETUP_LOCK = pathlib.Path(tempfile.gettempdir()) / "agentos-suite-pg.lock"


def _worker_id() -> str:
    """`gw0`, `gw1`, ... under xdist; empty string when serial."""
    return os.environ.get("PYTEST_XDIST_WORKER", "")


def _session_db() -> str:
    """This process's database. Unchanged when not running in parallel."""
    worker = _worker_id()
    return f"{_PG_DB}_{worker}" if worker else _PG_DB


@contextlib.contextmanager
def _setup_lock(timeout: float = 600.0):
    """Serialise cluster setup across workers.

    `O_EXCL` rather than a library, so this adds no dependency for a
    few lines of logic. A crashed run can leave the file behind; it is
    only ever held for the seconds of setup, so one older than ten
    minutes is stale and is cleared rather than deadlocking the suite.
    """
    deadline = time.time() + timeout
    handle = None
    while handle is None:
        try:
            handle = os.open(str(_PG_SETUP_LOCK),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time.time() - _PG_SETUP_LOCK.stat().st_mtime > 600
            except FileNotFoundError:
                continue                    # released between the two calls
            if stale:
                _PG_SETUP_LOCK.unlink(missing_ok=True)
                continue
            if time.time() > deadline:
                raise RuntimeError(
                    f"waited {timeout:.0f}s for {_PG_SETUP_LOCK} and it was "
                    "never released; delete it and re-run")
            time.sleep(0.2)
    try:
        yield
    finally:
        os.close(handle)
        _PG_SETUP_LOCK.unlink(missing_ok=True)


def _migrate(dsn: str) -> None:
    """Run `migrations/apply.py` against `dsn`.

    In a subprocess rather than by import: `apply.py` is a script with
    module-level state and its own asyncio.run, and the suite's own
    event loop has not started yet. A failure here is fatal on purpose —
    a half-migrated template would produce the "relation does not exist"
    confusion this whole block exists to prevent, and it would be cached
    for every run afterwards.
    """
    import subprocess
    env = {**os.environ, "DATABASE_URL": dsn}
    out = subprocess.run(
        [sys.executable, str(_ROOT / "migrations" / "apply.py"), dsn],
        env=env, capture_output=True, text=True, cwd=str(_ROOT))
    if out.returncode != 0:
        raise RuntimeError(
            "migrating the test template failed:\n"
            + (out.stderr or out.stdout)[-2000:])


def _start_local_postgres() -> str | None:
    """Boot an embedded Postgres and return a DSN, or None.

    Never raises: a machine without a working `pgserver` must fall
    through to whatever the caller already does, not fail collection.
    """
    try:
        import pgserver
    except ImportError:
        return None
    try:
        from agentos_ledger_client.testing import dsn_for
    except ImportError:                       # package not on the path yet
        return None
    try:
        with _setup_lock():       # uncontended and free when serial
            _PG_DATA.mkdir(parents=True, exist_ok=True)
            server = pgserver.get_server(str(_PG_DATA))
            # NOT `get_uri().rsplit("/", 1)[0]`, which is what this was and
            # which is wrong on Linux: there the URI is a unix-socket one,
            # `postgresql://postgres:@/postgres?host=/tmp/...`, whose last
            # `/` lives inside the host query parameter. Slicing there left
            # the database as `postgres` and moved the SOCKET DIRECTORY to
            # one that does not exist. See `dsn_for` for the whole story --
            # it passed on Windows, which is the only place it was run.
            base_uri = server.get_uri()

            # AN EMPTY DATABASE IS NOT A SUBSTITUTE FOR THE CONTAINER'S.
            #
            # The Docker database carries the migrated schema, so tests that
            # touch `templates`, `stories`, `goals` and the rest pass against
            # it. A bare cluster has none of that, and swapping one for the
            # other without the migrations would turn "no database" into
            # "relation does not exist" -- a different confusing failure
            # rather than a fix.
            #
            # SO: migrate ONCE into a template database that lives in the
            # cached cluster, then stamp each session's database from it.
            # `CREATE DATABASE ... TEMPLATE` is a file copy, so the 900-line
            # SEQUENCE is paid on the first run on this machine and never
            # again, while every session still starts clean.
            # `psql()` returns FORMATTED OUTPUT, not rows: a miss comes back
            # as " ?column? \n----------\n(0 rows)\n", which is perfectly
            # truthy. Checking it with `.strip()` silently decided the
            # template already existed on every run, skipped the migrations,
            # and then created the session database from a template that did
            # not exist. Count the rows instead.
            have_template = "(0 rows)" not in server.psql(
                f"SELECT 1 FROM pg_database WHERE datname = '{_PG_TEMPLATE}'")
            if not have_template:
                print(f"\n[conftest] first run: migrating {_PG_TEMPLATE} "
                      f"(once per machine, ~900 migrations)")
                server.psql(f'CREATE DATABASE "{_PG_TEMPLATE}"')
                # ROLES ARE CLUSTER-WIDE AND THE MIGRATIONS ASSUME SOME OF
                # THEM. `kernel_roles_grants_seed` creates agentos_worker,
                # agentos_verifier and agentos_human itself, but the LOGIN
                # roles a provisioned database already has -- `agentos` and
                # friends, made by db_bootstrap.sh, not by any migration --
                # are simply expected to exist, and a GRANT to a missing role
                # fails the whole run. NOLOGIN: nothing connects as these
                # here, the suite connects as `postgres`; they exist so the
                # GRANTs have a target and least-privilege gates have
                # something to assert about.
                for role in ("agentos", "agentos_migrate", "agentos_debug",
                             "agentos_leg", "agentos_worker",
                             "agentos_verifier", "agentos_human"):
                    server.psql(
                        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
                        f"WHERE rolname = '{role}') THEN CREATE ROLE {role} "
                        "NOLOGIN; END IF; END $$;")
                # ALL OR NOTHING. A failed migration used to leave the
                # database created but empty, so the next run saw a template
                # that "existed", skipped the migrations and stamped every
                # session from an empty schema -- a cached failure that
                # looked like a different bug. Drop it and let the next run
                # try again.
                try:
                    _migrate(dsn_for(base_uri, _PG_TEMPLATE))
                except Exception:
                    server.psql(f'DROP DATABASE IF EXISTS "{_PG_TEMPLATE}"')
                    raise
            else:
                # AND AN EXISTING TEMPLATE IS BROUGHT FORWARD, which it was
                # not before. The template is built once per machine and was
                # then never migrated again, so every migration added after
                # it was built was INVISIBLE to the local suite -- a test of
                # a new column passed against a template that did not have
                # it only because the rest of the run never looked. The
                # failure mode is the worst shape available: green locally,
                # red in CI, on a schema change that was correct all along.
                #
                # CHEAP, because `apply.py` tracks `_migrations` and runs
                # only what is pending: on a current template this is one
                # connection and "No pending migrations".
                _migrate(dsn_for(base_uri, _PG_TEMPLATE))

            # Postgres refuses a TEMPLATE copy while anything is connected to
            # the source, and refuses the DROP while anything is connected to
            # the target. Nothing else should be, but say so if it is.
            session_db = _session_db()
            server.psql(f'DROP DATABASE IF EXISTS "{session_db}"')
            server.psql(
                f'CREATE DATABASE "{session_db}" TEMPLATE "{_PG_TEMPLATE}"')
            return dsn_for(base_uri, session_db)
    except Exception as exc:                                # noqa: BLE001
        print(f"\n[conftest] embedded postgres unavailable: {exc}")
        return None


def pytest_configure(config) -> None:
    """Give the suite a database before anything imports the app."""
    # AND A BLOB BUCKET NAME, for the same reason the database is here.
    #
    # `resource_bucket_name()` used to fall back to a hardcoded
    # "${RESOURCE_BUCKET}" inside a bare `except Exception`, so a
    # configuration failure silently redirected ledger blobs instead of
    # raising. Removing that fallback is right, and it means the tests
    # that exercise the over-1MB path -- all of which patch the uploader
    # and never reach GCS -- now need a name to pass through.
    #
    # Supplying it HERE rather than in each test: the value is not what
    # any of them are about, and a per-test literal is how the old
    # hardcoded name came to be asserted in the first place. Only when
    # unset, so a run can still pin a real bucket.
    os.environ.setdefault("GCS_RESOURCE_BUCKET", "test-ledger-blobs")

    # EITHER NAME COUNTS AS "A DATABASE IS ALREADY PROVIDED", and
    # getting this wrong broke a build.
    #
    # I wrote this guard against DATABASE_URL alone, asserting that CI
    # set it and was therefore untouched. CI does not: cloudbuild passes
    # `INTERLOCK_TEST_DSN`, which is what the database-backed tests
    # actually read (`os.environ.get("INTERLOCK_TEST_DSN")`, ~40 call
    # sites). So the guard never fired there, the conftest tried to boot
    # an embedded Postgres inside the CI container beside the one
    # already running, and the test step failed.
    #
    # Checking both is not belt-and-braces: they are two names for the
    # same fact, and the fallback exists for the case where NEITHER is
    # set -- a developer's machine.
    provided = os.environ.get("DATABASE_URL") or os.environ.get(
        "INTERLOCK_TEST_DSN")
    # A WORKER INHERITS THE CONTROLLER'S ENVIRONMENT, and that includes
    # the DSN the controller's own embedded server just produced. That
    # is not an externally supplied DSN, and reading it as one made
    # every worker raise the guard below and die at startup
    # ("node down: Not properly terminated", no tests run). The
    # sentinel is how the two are told apart: only this conftest sets
    # it, and only after it started the server itself.
    worker = _worker_id()
    ours = os.environ.get("AGENTOS_EMBEDDED_PG") == "1"

    if provided and not (worker and ours):
        # A GENUINELY EXTERNAL DSN AND `-n` CANNOT BE COMBINED, and
        # this refuses rather than degrading. Down this path nothing
        # creates a database: every worker would share the one it was
        # handed, and tests that truncate a table or rely on a clean
        # row set would fail each other intermittently, in a way that
        # reads as flakiness rather than as a configuration mistake.
        # Cloud Build takes this path and passes no `-n`, so it never
        # fires there.
        if worker:
            raise RuntimeError(
                "a parallel run (-n) was asked for against a DSN supplied "
                "in the environment. Every worker would share that one "
                "database and corrupt each other's fixtures. Either drop "
                "-n, or unset DATABASE_URL/INTERLOCK_TEST_DSN and let the "
                "embedded server give each worker its own database.")
        # Publish it under the name the app reads, so a CI run that only
        # sets INTERLOCK_TEST_DSN still gives `app.database` a DSN --
        # previously that came from the container's own environment.
        os.environ.setdefault("DATABASE_URL", provided)
        return                              # CI, or the person's own server
    dsn = _start_local_postgres()
    if dsn:
        os.environ["DATABASE_URL"] = dsn
        # Marks the DSN as one WE made, so a worker inheriting it knows
        # to stamp its own database instead of refusing to share.
        os.environ["AGENTOS_EMBEDDED_PG"] = "1"
        where = f" [{worker}]" if worker else ""
        print(f"\n[conftest] embedded postgres{where}: {dsn}")
    else:
        # SAY SO, LOUDLY. The whole point is that the previous behaviour
        # — degrade into per-test connect timeouts — was indistinguishable
        # from the suite merely being slow.
        print("\n[conftest] NO DATABASE. Set DATABASE_URL, or "
              "`pip install pgserver` for an embedded one. Database tests "
              "will fail rather than hang.")
