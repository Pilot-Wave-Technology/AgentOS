"""Test helpers for anyone running this client against a local Postgres.

Shipped WITH the package, not kept in agentos's conftest, because a
consumer -- multi-agent-RL first -- needs exactly this to run the client's
tests on its own machine, and the alternative is each codebase writing it
again. The bug below is what happens when they do.
"""
from __future__ import annotations

import os
import socket
from urllib.parse import parse_qs, urlparse, urlunparse

__all__ = ["dsn_for", "is_reachable"]


def is_reachable(dsn: str, timeout: float = 1.0) -> bool:
    """True if something is listening where `dsn` points.

    WHY PRESENCE IS NOT REACHABILITY, which cost nine errors on the
    first Linux run of the suite.

    `test_a_bare_database_becomes_a_ledger.py` skipped on
    `not os.environ["DATABASE_URL"]`, reading a non-empty value as "a
    real Postgres is available". It is not. `app/services/model_registry/
    tests/conftest.py` sets

        os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost:5432/x")

    at import time, and legitimately so -- `app.config.get_settings()`
    must construct before any `app.*` module imports, and that field is
    required. It is a SETTINGS PLACEHOLDER, not a promise of a server.

    Run the package's tests alone and the variable is unset, so they
    skip and all is well. Run the whole suite in one process and that
    placeholder arrives first, the guard believes it, and nine tests try
    to dial `localhost:5432` and ERROR where they meant to SKIP. A test
    that cannot run must not look like a test that failed.

    IT OPENS A REAL CONNECTION, and a socket probe was tried first and
    was not enough. The placeholder points at `localhost:5432`, and a
    developer machine running its own Postgres answers there -- so the
    probe said "reachable", asyncpg then failed on credentials, and the
    nine errors came back. "Is a port open" is not the question; "can
    these tests use this database" is.

    EVERY FAILURE MEANS SKIP, including a bad password. That is a
    deliberate trade: the alternative is erroring on a machine whose
    only sin is running an unrelated Postgres. The skip reason carries
    the error, so `-rs` says exactly what was wrong rather than leaving
    a silent gap.
    """
    if not dsn:
        return False
    try:
        import asyncio

        import asyncpg
    except ImportError:
        pass
    else:
        async def _try() -> bool:
            try:
                conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout)
            except Exception:
                return False
            await conn.close()
            return True
        try:
            return asyncio.run(_try())
        except Exception:
            return False
    parsed = urlparse(dsn)
    host_q = parse_qs(parsed.query).get("host")
    if host_q and host_q[0].startswith("/"):
        # unix socket: the directory holds .s.PGSQL.<port>
        d = host_q[0]
        try:
            return any(n.startswith(".s.PGSQL.") for n in os.listdir(d))
        except OSError:
            return False
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def dsn_for(base_uri: str, dbname: str) -> str:
    """`base_uri` pointed at a different database on the same server.

    WHY THIS IS NOT `base_uri.rsplit("/", 1)[0] + "/" + dbname`, which is
    what it was, and which was wrong on every platform that matters.

    `pgserver.get_server(...).get_uri()` returns two different shapes:

        Windows   postgresql://postgres:@127.0.0.1:49778/postgres
        Linux     postgresql://postgres:@/postgres?host=/tmp/agentos-suite-pgdata

    Linux uses a UNIX SOCKET, and the socket directory travels in a query
    parameter. `rsplit` finds the last `/` -- which on Linux is inside
    `?host=/tmp/...`, not the database separator at all. So the old form
    produced

        postgresql://postgres:@/postgres?host=/tmp/agentos_test

    which leaves the database as `postgres` and silently moves the SOCKET
    DIRECTORY to one that does not exist. The connection then fails with
    "No such file or directory", the caller reads that as "no database
    available", and every database-backed test skips or errors.

    IT PASSED EVERYWHERE IT WAS RUN. The TCP shape has its last `/` in
    exactly the right place, so on Windows the broken version is
    indistinguishable from the correct one -- and CI never exercised it
    at all, because CI supplies its own DSN. The first Linux run was an
    executor pod, which is a poor place to discover it.

    Parsing instead of slicing: the path is the database, the query is
    everything else, and only the path is replaced.
    """
    parsed = urlparse(base_uri)
    return urlunparse(parsed._replace(path=f"/{dbname.lstrip('/')}"))
