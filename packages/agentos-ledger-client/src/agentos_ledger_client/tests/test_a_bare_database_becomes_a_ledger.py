"""The thing a second repository actually does on day one.

`pip install agentos-ledger-client`, point it at an empty Postgres, call
`init_schema`, append an event. Until this PR that sequence was
impossible: the client read and wrote three tables and created none of
them, so "callable from another codebase" stopped at the first call.

RUNS AGAINST A REAL DATABASE, and skips without one. A fake connection
would assert that the SQL was SENT, which is the half that was never in
doubt — the question is whether Postgres accepts it on a database that
has nothing, which is the only database a consumer starts with.

EACH TEST GETS ITS OWN SCHEMA, created and dropped, so this never sees
or touches the control panel's tables even when DATABASE_URL points at
a database that has them.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")

from agentos_ledger_client import init_schema, schema_sql   # noqa: E402

from agentos_ledger_client.testing import is_reachable   # noqa: E402

# REACHABLE, NOT MERELY SET. This used to skip on `not DSN`, and a
# non-empty DATABASE_URL is not a promise that a server exists:
# `app/services/model_registry/tests/conftest.py` sets a placeholder
# (`postgresql://x:x@localhost:5432/x`) at import time so
# `app.config.get_settings()` can construct. Alone, these tests skipped
# correctly; in a whole-suite run that placeholder arrived first and the
# nine tests below ERRORED against a server that was never there.
# See `is_reachable` for why it probes rather than connects.
DSN = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not is_reachable(DSN),
    reason="no reachable Postgres at DATABASE_URL — this needs a real one")


@pytest_asyncio.fixture
async def bare():
    """An empty schema on the live server, dropped afterwards."""
    name = "lc_" + uuid.uuid4().hex[:12]
    conn = await asyncpg.connect(DSN)
    await conn.execute(f'CREATE SCHEMA "{name}"')
    # THE TEMP SCHEMA ALONE, WITH `public` OFF THE PATH.
    #
    # This used to be `"{name}", public` so the blob tests could reach
    # `ledger_blobs`. They still can: it is written `public.ledger_blobs`
    # everywhere, so it resolves by qualification and never needed the
    # search path at all.
    #
    # Leaving `public` on broke the point of the fixture once the
    # embedded Postgres started running the full migrations. `public`
    # then holds `actors`, `templates`, `stories` and the rest — so a
    # "bare database" test was no longer testing a bare database, and
    # `test_an_append_survives_a_database_that_has_only_the_ledger`
    # failed on its own premise check. Off the path, a consumer's
    # situation is reproduced honestly whether the cluster behind it is
    # empty or fully migrated.
    await conn.execute(f'SET search_path TO "{name}"')
    try:
        yield conn, name
    finally:
        try:
            await conn.execute(f'DROP SCHEMA "{name}" CASCADE')
        finally:
            await conn.close()


async def _tables(conn, schema: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        " WHERE table_schema = $1", schema)
    return {r["table_name"] for r in rows}


@pytest.mark.asyncio
async def test_init_schema_builds_the_three_tables(bare):
    conn, name = bare
    assert await _tables(conn, name) == set(), "the fixture is not bare"

    await init_schema(conn)

    got = await _tables(conn, name)
    for t in ("ledger_events", "chain_head"):
        assert t in got, f"{t} was not created (got {sorted(got)})"
    # ledger_blobs is schema-qualified to `public` by the DDL itself.
    assert "ledger_blobs" in await _tables(conn, "public")


@pytest.mark.asyncio
async def test_calling_it_twice_is_safe(bare):
    """A test fixture calls it every run; a service may call it every
    boot."""
    conn, _ = bare
    await init_schema(conn)
    await init_schema(conn)        # must not raise


@pytest.mark.asyncio
async def test_an_event_appends_and_chains(bare):
    """END TO END on a database that had nothing a moment ago: the
    append path, the hash chain and the head row."""
    from agentos_ledger_client import append_event, chain_hash

    conn, _ = bare
    await init_schema(conn)
    actor = uuid.uuid4()

    async with conn.transaction():     # append_event takes the advisory lock
        first = await append_event(
            conn, event_type="task.created", payload={"n": 1},
            actor_id=actor, actor_type="system")
    assert first and first["event_id"]
    assert first["integrity_hash"] == chain_hash("GENESIS", {"n": 1})

    async with conn.transaction():
        second = await append_event(
            conn, event_type="task.completed", payload={"n": 2},
            actor_id=actor, actor_type="system")
    assert second["integrity_hash"] == chain_hash(first["integrity_hash"],
                                                  {"n": 2})

    head = await conn.fetchrow("SELECT last_hash, event_count FROM chain_head")
    assert head["last_hash"] == second["integrity_hash"]
    assert head["event_count"] == 2, "the head row did not advance"


@pytest.mark.asyncio
async def test_the_actor_type_the_readme_shows_is_rejected(bare):
    """THE README'S OWN QUICK START FAILS. It passes
    `actor_type="agent"`, and the column admits only
    'human', 'shca', 'independent_agent', 'system'. The first thing a new
    consumer copies does not work, and the error arrives from Postgres
    rather than from the client.

    Pinned as a test rather than only fixed in the README so the four
    legal values stay discoverable from the code.
    """
    from agentos_ledger_client import append_event

    conn, _ = bare
    await init_schema(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await append_event(
                conn, event_type="task.created", payload={},
                actor_id=uuid.uuid4(), actor_type="agent")


@pytest.mark.asyncio
async def test_a_small_blob_needs_no_cloud_storage(bare):
    """Payloads at or under 1MB are inline BYTEA. That is what makes a
    consumer's fully-local setup possible — no GCS, no credentials."""
    from agentos_ledger_client import read_blob_by_sha256, store_blob

    conn, _ = bare
    await init_schema(conn)
    body = b"a local checkpoint" * 100

    ref = await store_blob(conn, body, content_type="application/octet-stream")
    assert ref.storage_backend == "inline"
    assert ref.sha256 == hashlib.sha256(body).hexdigest()
    assert await read_blob_by_sha256(conn, ref.sha256) == body


@pytest.mark.asyncio
async def test_the_same_bytes_are_stored_once(bare):
    """Content-addressed: UNIQUE(sha256). A checkpoint written twice is
    one row."""
    from agentos_ledger_client import store_blob

    conn, _ = bare
    await init_schema(conn)
    body = b"same-" + uuid.uuid4().hex.encode()      # unique to this run
    a = await store_blob(conn, body, content_type="text/plain")
    b = await store_blob(conn, body, content_type="text/plain")
    assert a.sha256 == b.sha256
    # Counted by sha256, not by table total: ledger_blobs is shared
    # `public`, so a total would depend on what else ran.
    assert await conn.fetchval(
        "SELECT count(*) FROM public.ledger_blobs WHERE sha256 = $1",
        a.sha256) == 1


def test_the_ddl_is_available_without_executing_it():
    """A consumer with its own migration runner wants the statements, to
    paste into a numbered migration of its own."""
    sql = schema_sql()
    assert "CREATE TABLE IF NOT EXISTS ledger_events" in sql
    assert "CREATE TABLE IF NOT EXISTS chain_head" in sql
    assert "ledger_blobs" in sql


@pytest.mark.asyncio
async def test_ledger_blobs_cannot_live_in_a_consumers_schema(bare):
    """A WART WORTH KNOWING, pinned rather than fixed here.

    `ledger_events` and `chain_head` are unqualified, so they follow the
    connection's `search_path`. `ledger_blobs` is written
    `public.ledger_blobs` in the DDL AND in every query in blobs.py. A
    consumer that puts the ledger in its own schema therefore gets two
    tables where it asked and one in `public`.

    Not changed in this PR: the qualification comes from
    `migrations/ledger_blobs.sql`, and dropping it would alter where the
    control panel's own blobs resolve. Worth a decision of its own —
    either qualify all three or none.
    """
    conn, name = bare
    await init_schema(conn)
    assert "ledger_blobs" not in await _tables(conn, name)
    assert "ledger_blobs" in await _tables(conn, "public")


@pytest.mark.asyncio
async def test_an_append_survives_a_database_that_has_only_the_ledger(bare):
    """THE BUG THE HOST IMPORTS CAUSED, pinned.

    `append_event` used to call `app.ledger.actors.actor_is_registered`
    -- `SELECT 1 FROM actors WHERE id = $1` -- inside the caller's
    transaction and inside `except Exception: pass`. A consumer's
    database has no `actors` table, so the query failed, the except
    swallowed it, and Postgres had already poisoned the transaction: the
    INSERT then died with `InFailedSQLTransactionError`, naming neither
    the cause nor the missing table.

    This test is the consumer's exact situation -- `init_schema` and
    nothing else -- and it was red before the integration seam.
    """
    from agentos_ledger_client import append_event

    conn, _ = bare
    await init_schema(conn)
    assert not await conn.fetchval(
        "SELECT to_regclass('actors') IS NOT NULL"), (
        "this test is pointless unless `actors` is genuinely absent")

    async with conn.transaction():
        got = await append_event(
            conn, event_type="rlagt.sim.tick", payload={"step": 1},
            actor_id=uuid.uuid4(), actor_type="independent_agent",
            context_goal_id="GOAL1")
    assert got and got["event_id"]


@pytest.mark.asyncio
async def test_a_host_can_supply_the_audit_and_it_runs(bare):
    """The other half: agentos still gets its check. Absent means
    skip; present means run, and a False is reported rather than
    swallowed."""
    from agentos_ledger_client import append_event
    from agentos_ledger_client.integration import configure, reset

    conn, _ = bare
    await init_schema(conn)
    seen: list = []

    async def _check(c, actor_id) -> bool:
        seen.append(actor_id)
        return False                      # "not registered"

    configure(actor_check=_check)
    try:
        async with conn.transaction():
            await append_event(
                conn, event_type="task.created", payload={},
                actor_id=uuid.uuid4(), actor_type="system")
        assert len(seen) == 1, "the host's check was not consulted"
    finally:
        reset()


def test_the_package_imports_nothing_from_the_host():
    """The seam itself. Any `from app.` here is a consumer inheriting
    agentos's database layout by accident."""
    import pathlib

    import agentos_ledger_client
    # THE INSTALLED PACKAGE, not a source tree two directories up. The
    # old form walked `parents[1]/src/...`, which only resolved from the
    # old location beside the package -- and, more importantly, checked
    # the REPOSITORY rather than the thing the consumer actually has.
    src = pathlib.Path(agentos_ledger_client.__file__).resolve().parent
    offenders = [p.name for p in src.glob("*.py")
                 if "from app." in p.read_text(encoding="utf-8")]
    assert not offenders, f"these reach back into the host: {offenders}"


@pytest.mark.asyncio
async def test_non_finite_floats_reach_postgres_and_read_back_faithfully(bare):
    """Part 2 conformance: non-finite floats in payload survive jsonb insert,
    are stored as canonical string literals, and reconstruct losslessly via float().
    Part 1 conformance: read_events streams back LedgerEvent with sequence_number
    and timestamp/created_at parity, and verify_chain passes.
    """
    import math
    from agentos_ledger_client import (
        LedgerClient,
        append_event,
        read_events,
        verify_chain,
    )

    conn, _ = bare
    await init_schema(conn)

    actor_id = uuid.uuid4()
    payload = {
        "reward": float("nan"),
        "q_max": float("inf"),
        "loss": float("-inf"),
        "step": 1,
    }

    # Appending an event with non-finite floats does not fail in Postgres jsonb
    async with conn.transaction():
        res1 = await append_event(
            conn,
            event_type="rl.step",
            payload=payload,
            actor_id=actor_id,
            actor_type="system",
        )
    assert res1 is not None

    async with conn.transaction():
        res2 = await append_event(
            conn,
            event_type="rl.step",
            payload={"step": 2, "reward": 10.5},
            actor_id=actor_id,
            actor_type="system",
        )
    assert res2 is not None

    # Read events back
    events = [ev async for ev in read_events(conn, event_type="rl.step")]
    assert len(events) == 2

    # Check ordering by sequence_number
    assert events[0].sequence_number == 1
    assert events[1].sequence_number == 2

    # Parity between timestamp and created_at
    assert events[0].timestamp is not None
    assert events[0].created_at == events[0].timestamp

    # Lossless float reconstruction from canonical strings
    ev1 = events[0]
    assert ev1.payload["reward"] == "NaN"
    assert ev1.payload["q_max"] == "Infinity"
    assert ev1.payload["loss"] == "-Infinity"

    assert math.isnan(float(ev1.payload["reward"]))
    assert math.isinf(float(ev1.payload["q_max"])) and float(ev1.payload["q_max"]) > 0
    assert math.isinf(float(ev1.payload["loss"])) and float(ev1.payload["loss"]) < 0

    # Chain verification succeeds
    verification = await verify_chain(conn)
    assert verification.ok is True
    assert verification.bad_event_id is None

    # High-level client tests
    client = LedgerClient(target=conn)
    client_events = [ev async for ev in client.read_events(event_type="rl.step")]
    assert len(client_events) == 2
    client_verif = await client.verify_chain()
    assert client_verif.ok is True

