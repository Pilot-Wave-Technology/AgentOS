"""The memory home lock, against a REAL PostgreSQL.

The thing it has to do is exclude, and exclusion is the one property a
mocked pool cannot show. So this drives two concurrent holders against a
live `pg_try_advisory_lock` and asserts the second one is refused while the
first holds it, and admitted after.

Why the lock exists: since Stage K an assistant's threads share one
workspace and one `MEMORY.md`, which the model writes by reading the file
and writing the whole thing back. Two story-completion checkpoints
overlapping lose one of them, silently, in the turn whose entire job is to
record what happened.

**How to run.** Start the local stack (`local-development/scripts/stack.ps1`);
these SKIP rather than fail when no database is reachable. Nothing is
written — advisory locks touch no rows.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.memory_v2.home_lock import key_for

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


def test_key_is_namespaced_and_per_home():
    """Two threads of ONE assistant share a home; two assistants do not."""
    assert key_for("abhi-shca-KER14") == "memhome:abhi-shca-KER14"
    assert key_for("abhi-shca-KER14") != key_for("sam-shca-KER14")
    # The namespace matters: the dispatcher hashes thread ids into the same
    # advisory-lock space, and a bare gateway id could collide with one.
    assert key_for("x").startswith("memhome:")


async def _dsn_or_skip() -> str:
    for dsn in (os.environ.get("STAGEK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception:                             # noqa: BLE001
            continue
        await conn.close()
        return dsn
    pytest.skip("no local database — start the local stack")


def test_a_second_holder_is_refused_then_admitted():
    """Exactly the shape of two checkpoints in one home."""
    async def run():
        dsn = await _dsn_or_skip()
        key = key_for("zz-stagek-home-fixture")
        a = await asyncpg.connect(dsn)
        b = await asyncpg.connect(dsn)
        try:
            first = await a.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", key)
            assert first is True

            # A different connection — the second story's checkpoint — must
            # NOT get in. This is the assertion the whole change rests on.
            second = await b.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", key)
            assert second is False, (
                "two memory turns held the same home at once — MEMORY.md "
                "would be read-modify-written by both")

            await a.execute("SELECT pg_advisory_unlock(hashtext($1)::bigint)", key)

            # …and must get in once the first one is done.
            again = await b.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", key)
            assert again is True
            await b.execute("SELECT pg_advisory_unlock(hashtext($1)::bigint)", key)
        finally:
            await a.close()
            await b.close()
    asyncio.run(run())


def test_different_homes_do_not_block_each_other():
    """One person's assistant must never wait on another person's."""
    async def run():
        dsn = await _dsn_or_skip()
        a = await asyncpg.connect(dsn)
        b = await asyncpg.connect(dsn)
        try:
            ka, kb = key_for("zz-stagek-home-a"), key_for("zz-stagek-home-b")
            assert await a.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", ka) is True
            assert await b.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", kb) is True
            await a.execute("SELECT pg_advisory_unlock(hashtext($1)::bigint)", ka)
            await b.execute("SELECT pg_advisory_unlock(hashtext($1)::bigint)", kb)
        finally:
            await a.close()
            await b.close()
    asyncio.run(run())
