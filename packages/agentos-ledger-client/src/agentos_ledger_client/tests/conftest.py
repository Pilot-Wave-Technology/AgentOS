"""Fixtures for the conformance suite.

NO `sys.path` MANIPULATION HERE ANY MORE, and its absence is the point.
These tests used to sit beside the package and had to put `src` on the
path to import it. They now live INSIDE `agentos_ledger_client`, so
whatever imported this module already resolved the package -- an
installed wheel, an editable checkout, or the repo. If the import were
going to fail it would have failed before this file was read.

What remains is what a consumer's run genuinely needs: a bucket name,
and a database.
"""
from __future__ import annotations

import os
from pathlib import Path



def pytest_configure(config) -> None:
    """A bucket NAME, because a consumer's run needs one.

    `resource_bucket_name()` raises when nothing is configured — on
    purpose, since the old behaviour was a hardcoded fallback inside a
    bare `except`, which silently redirected a misconfigured
    deployment's blobs. The over-1MB tests all patch the uploader and
    never reach GCS, but they still need a name to pass through.

    The root conftest sets this too. Standalone runs do not load it, so
    `test_store_and_read_gcs_blob` failed with `RuntimeError: no GCS
    bucket configured` — a correct error about an incorrect setup.

    `setdefault`, so a run that pins a real bucket keeps it.
    """
    os.environ.setdefault("GCS_RESOURCE_BUCKET", "test-ledger-blobs")

    # AND A DATABASE, because without one the eleven tests in
    # `test_a_bare_database_becomes_a_ledger.py` SKIP — and that file is
    # the proof that `pip install agentos-ledger-client`, `init_schema`,
    # append, verify works on an empty database. It is the single most
    # important thing a consumer runs, and standalone was the one
    # invocation where it silently did not.
    #
    # A BARE CLUSTER IS ALL THIS NEEDS, which is why this is twenty lines
    # and the root conftest is ninety. The root one has to migrate the
    # control plane's ~900-migration SEQUENCE into a template because the
    # app's tests expect that schema; these tests CREATE THE SCHEMA
    # THEMSELVES — that is what they are testing. An empty database is
    # not a limitation here, it is the fixture.
    if os.environ.get("DATABASE_URL") or os.environ.get("INTERLOCK_TEST_DSN"):
        return                                   # CI, or the person's own
    dsn = _bare_postgres()
    if dsn:
        os.environ["DATABASE_URL"] = dsn
        print(f"\n[ledger-client conftest] embedded postgres: {dsn}")
    else:
        print("\n[ledger-client conftest] no pgserver and no DATABASE_URL — "
              "the bootstrap tests will SKIP. `pip install pgserver`.")


def _bare_postgres() -> str | None:
    """An empty database on a cached local cluster, or None.

    Never raises: a machine without a working `pgserver` must fall back
    to skipping, not fail collection.

    The data directory is shared with the repo-root conftest on purpose —
    `initdb` is the expensive part and there is no reason to pay it
    twice on one machine. The DATABASE is this package's own, dropped and
    recreated per session so nothing carries over.
    """
    try:
        import pgserver
    except ImportError:
        return None
    try:
        import tempfile
        data = Path(tempfile.gettempdir()) / "agentos-suite-pgdata"
        data.mkdir(parents=True, exist_ok=True)
        server = pgserver.get_server(str(data))
        name = "agentos_ledger_client_test"
        server.psql(f'DROP DATABASE IF EXISTS "{name}"')
        server.psql(f'CREATE DATABASE "{name}"')
        # `dsn_for`, NOT rsplit -- on Linux the URI is a unix-socket one
        # and its last `/` is inside `?host=...`. Same bug as the root
        # conftest had; fixed in one place so it cannot diverge.
        from agentos_ledger_client.testing import dsn_for
        return dsn_for(server.get_uri(), name)
    except Exception as exc:                                  # noqa: BLE001
        print(f"\n[ledger-client conftest] embedded postgres unavailable: {exc}")
        return None
