"""The quiesce check is the memory guard, so it fails closed.

WHY THIS IS NOT POLITENESS. It reads like "don't cut somebody's reply
off", and that is the smaller half. Agents write `MEMORY.md`
continuously and the flush runs INSIDE a turn, during async compaction --
so a gateway stopped mid-turn can be stopped mid-flush, and a truncated
memory file is the one way this whole procedure can destroy memory with a
perfect allowlist, because it has nothing to do with what we write.

No turn in flight means no flush in flight. That is the entire argument.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from app.services.agent_refresh.quiesce import (
    BUSY_MESSAGE_STATES, BUSY_RUN_STATES, is_quiet)

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


def _uid(p: str) -> str:
    return f"{p}-{uuid.uuid4().hex[:8]}"


async def _thread_with_message(conn, *, goal_status: str, msg_status: str):
    gid = _uid("G")[:12]
    await conn.execute(
        "INSERT INTO goals (id, name, status, created_by) "
        "VALUES ($1, $1, $2, 'test')", gid, goal_status)
    tid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO threads (id, goal_id, agent_id, created_by) "
        "VALUES ($1, $2, '', 'test')", tid, gid)
    await conn.execute(
        "INSERT INTO messages (id, thread_id, sender, role, content, "
        "                      status) "
        "VALUES ($1, $2, 'test', 'human', 'hello', $3)",
        str(uuid.uuid4()), tid, msg_status)
    return gid


@pytest.mark.asyncio
@pytest.mark.parametrize("state", BUSY_MESSAGE_STATES)
async def test_a_queued_message_does_NOT_block(state):
    """CORRECTED AFTER THE FIRST REAL RUN.

    This used to block, and that made the tool unusable: the fleet had
    30 queued messages across four goals, the oldest 26 hours old, with
    zero agents actually thinking. A backlog that never drains meant the
    apply could never run.

    It is also the wrong risk. An apply writes prompts, tools and skills
    over RPC -- it touches no memory file and stops no process, so it
    cannot interrupt a flush. That danger belongs to the RESTART, which
    is a separate manual step.
    """
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "DELETE FROM chat_run_state WHERE status = ANY($1::text[])",
            list(BUSY_RUN_STATES))
        await _thread_with_message(conn, goal_status="active",
                                   msg_status=state)
        assert await is_quiet(conn), (
            f"a {state} message is pending work, not a turn in progress")
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_the_backlog_is_reported_even_though_it_does_not_block():
    """Not blocking is not the same as not mentioning. A queue this old
    is worth a person seeing."""
    from app.services.agent_refresh.quiesce import backlog

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        await _thread_with_message(conn, goal_status="active",
                                   msg_status="queued")
        assert "queued" in await backlog(conn)
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_turn_already_running_blocks_it():
    """THE ONE AN EARLIER DRAFT MISSED. `thinking` lives in
    `chat_run_state`, not in `messages` -- `messages_status_chk` permits
    only queued/processing/done/failed. Looking for it among the message
    statuses reported the fleet quiet while a turn, and therefore
    possibly a memory flush, was in progress."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "UPDATE messages SET status = 'done' WHERE status = ANY($1::text[])",
            list(BUSY_MESSAGE_STATES))
        await conn.execute("UPDATE kernel.executions SET state = 'DONE' "
                           "WHERE state = 'RUNNING'")
        gid = _uid("G")[:12]
        await conn.execute(
            "INSERT INTO goals (id, name, status, created_by) "
            "VALUES ($1, $1, 'active', 'test')", gid)
        tid = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO threads (id, goal_id, agent_id, created_by) "
            "VALUES ($1, $2, '', 'test')", tid, gid)
        await conn.execute(
            "INSERT INTO chat_run_state (thread_id, run_id, status) "
            "VALUES ($1, 'r1', $2)", tid, BUSY_RUN_STATES[0])
        quiet = await is_quiet(conn)
        assert not quiet, "a turn in flight must block the refresh"
        assert "mid-turn" in quiet.reason
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_running_execution_does_not_block_either():
    """A leg runs in its own pod and never reads the gateway's agent
    config, so refreshing an assistant underneath it changes nothing
    about the leg."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "UPDATE messages SET status = 'done' WHERE status = ANY($1::text[])",
            list(BUSY_MESSAGE_STATES))
        await conn.execute(
            "INSERT INTO kernel.executions (story_id, state, generation, "
            "                               kind, domain) "
            "VALUES ($1, 'RUNNING', 0, 'story', 'coding')",
            _uid("s")[:12])
        assert await is_quiet(conn)
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_it_fails_closed_when_it_cannot_tell():
    """Refusing a refresh costs a retry. Permitting one during a flush
    costs an agent's memory, which nothing else holds a copy of."""
    class Broken:
        async def fetchval(self, *a, **k):
            raise RuntimeError("database unreachable")

    quiet = await is_quiet(Broken())
    assert not quiet, "an unreadable fleet must read as busy, not idle"
    assert "busy" in quiet.reason.lower()
