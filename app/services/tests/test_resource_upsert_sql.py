"""Re-uploading a document REPLACES it, against a real PostgreSQL.

WHAT THIS EXISTS TO STOP COMING BACK. `goal_resources` carries
`uq_goal_resource_kind_name UNIQUE (goal_id, kind, name)`, and the chat
upload route did a plain INSERT. Re-uploading a revised `PRICE_LIST.xlsx`
raised a unique violation nobody caught: the person saw an internal
error with no message, and no way to tell a duplicate name from a broken
server.

Three claims, and the constraint is real here rather than mocked — a
mock cannot violate a unique index, which is precisely why this had to
be a SQL test:

  * an existing document is FOUND by (goal, kind, name)
  * replacing keeps the id — grants and story links are keyed on it, so
    a new id would leave every holder pointing at the previous revision
  * replacing marks the cached summary stale — a brief is a summary OF a
    file, and when the file changes the brief is wrong

**How to run.** Start the local stack; these run against its Postgres
and SKIP if none is reachable. **Safety:** one transaction, always
rolled back, fixture ids namespaced `zz-upsert-test%`.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.services.resource_upsert import existing_document, replace_document

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-upsert-test"
GOAL = f"{PREFIX}-goal"


def _candidate_dsns() -> list[str]:
    seen, out = set(), []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"),
                LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


async def _connect_or_skip():
    """Resolved at TEST time, not import time — a sibling conftest sets a
    dummy DATABASE_URL at import, and a guard that hides itself is worse
    than no guard."""
    attempts = []
    for dsn in _candidate_dsns():
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            attempts.append(f"{type(exc).__name__}")
            continue
        ok = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name IN ('goal_resources','resource_events')")
        if ok == 2:
            return conn
        await conn.close()
        attempts.append(f"only {ok}/2 tables")
    pytest.skip("no local database with the expected schema; tried: "
                + "; ".join(attempts))


async def _seed_goal(conn):
    """`goal_resources.goal_id` has a real foreign key, so the fixture
    needs a real goal. Rolled back with everything else."""
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1, $2, $3) "
        "ON CONFLICT (id) DO NOTHING",
        GOAL, "upsert test goal", "zz-upsert-test")


async def _seed(conn, rid: str, name: str, size: int, status: str = "active"):
    await conn.execute(
        """
        INSERT INTO goal_resources
            (id, goal_id, kind, name, uri, gcs_uri, file_type,
             file_size_bytes, summary_stale, status, created_by)
        VALUES ($1, $2, 'document', $3, $4, $4, 'xlsx', $5, FALSE, $6,
                'zz-upsert-test')
        """,
        rid, GOAL, name, f"gs://bucket/{rid}/{name}", size, status)


def _run(body):
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await _seed_goal(conn)
            await body(conn)
        finally:
            # ALWAYS rolled back, and asserted rather than assumed.
            await tx.rollback()
            left = await conn.fetchval(
                "SELECT count(*) FROM goal_resources WHERE goal_id = $1", GOAL)
            await conn.close()
            assert left == 0, "fixture rows survived the rollback"
    asyncio.run(go())


def test_an_existing_document_is_found_by_name():
    async def body(conn):
        await _seed(conn, f"{PREFIX}-1", "PRICE_LIST.xlsx", 5621)
        found = await existing_document(conn, GOAL, "PRICE_LIST.xlsx")
        assert found and found["id"] == f"{PREFIX}-1"
        assert found["file_size_bytes"] == 5621
        assert await existing_document(conn, GOAL, "OTHER.xlsx") is None
    _run(body)


def test_an_archived_document_is_not_revived():
    """Archiving is a person saying "finished with this"; matching on the
    name and quietly writing over it would undo their decision."""
    async def body(conn):
        await _seed(conn, f"{PREFIX}-2", "OLD.xlsx", 100, status="archived")
        assert await existing_document(conn, GOAL, "OLD.xlsx") is None
    _run(body)


def test_replacing_keeps_the_id_and_marks_the_summary_stale():
    async def body(conn):
        rid = f"{PREFIX}-3"
        await _seed(conn, rid, "PRICE_LIST.xlsx", 5621)
        await replace_document(conn, rid, GOAL, "PRICE_LIST.xlsx",
                               f"gs://bucket/{rid}/PRICE_LIST.xlsx",
                               "xlsx", 5641)
        row = await conn.fetchrow(
            "SELECT id, file_size_bytes, summary_stale FROM goal_resources "
            "WHERE goal_id = $1 AND name = 'PRICE_LIST.xlsx'", GOAL)
        # THE ID IS THE THING THAT MUST NOT CHANGE: grants and story
        # links are keyed on it.
        assert row["id"] == rid
        assert row["file_size_bytes"] == 5641
        assert row["summary_stale"] is True
        # And still exactly one row -- the whole point is that the
        # unique constraint is satisfied by not inserting.
        n = await conn.fetchval(
            "SELECT count(*) FROM goal_resources WHERE goal_id = $1 "
            "AND name = 'PRICE_LIST.xlsx'", GOAL)
        assert n == 1
    _run(body)


def test_the_unique_constraint_would_have_rejected_a_second_insert():
    """The bug, reproduced — so the fix is measured against the real
    failure rather than against a description of it."""
    async def body(conn):
        await _seed(conn, f"{PREFIX}-4", "PRICE_LIST.xlsx", 5621)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _seed(conn, f"{PREFIX}-5", "PRICE_LIST.xlsx", 5641)
    _run(body)
