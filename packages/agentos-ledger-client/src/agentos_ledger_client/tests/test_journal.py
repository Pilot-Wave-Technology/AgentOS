from __future__ import annotations

from typing import Any
import uuid

import asyncpg
import pytest

from agentos_ledger_client.chain import GLOBAL_CHAIN_LOCK_ID, chain_hash
from agentos_ledger_client.journal import (
    append_event,
    generate_event_id,
    register_post_append_hook,
    register_pre_append_hook,
)


class MockJournalConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.chain_head: dict[str, Any] | None = None
        self.events: dict[uuid.UUID, dict[str, Any]] = {}
        self.fail_idempotency: bool = False

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))
        if "INSERT INTO ledger_events" in query and self.fail_idempotency:
            raise asyncpg.UniqueViolationError("unique constraint: idempotency_key")
        if "INSERT INTO chain_head" in query:
            self.chain_head = {
                "id": 1,
                "last_event_id": args[0],
                "last_hash": args[1],
            }

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "SELECT last_hash FROM chain_head WHERE id = 1" in query:
            return self.chain_head
        return None


@pytest.mark.asyncio
async def test_append_event_happy_path():
    conn = MockJournalConnection()
    actor_id = uuid.uuid4()
    payload = {"task": "test", "priority": 1}

    res = await append_event(
        conn,
        event_type="task.created",
        payload=payload,
        actor_id=actor_id,
        actor_type="agent",
    )

    assert res is not None
    assert "event_id" in res
    assert "integrity_hash" in res

    # Verify lock acquisition with canonical lock ID
    lock_executed = [args for q, args in conn.executed if "pg_advisory_xact_lock" in q]
    assert len(lock_executed) == 1
    assert lock_executed[0][0] == GLOBAL_CHAIN_LOCK_ID

    # Verify chain hash matches GENESIS + payload
    expected_hash = chain_hash("GENESIS", payload)
    assert res["integrity_hash"] == expected_hash

    # Verify chain head was upserted
    assert conn.chain_head is not None
    assert conn.chain_head["last_hash"] == expected_hash


@pytest.mark.asyncio
async def test_append_event_subsequent_hash_chain():
    conn = MockJournalConnection()
    conn.chain_head = {"id": 1, "last_event_id": uuid.uuid4(), "last_hash": "PREV_HASH"}

    actor_id = uuid.uuid4()
    payload = {"status": "completed"}

    res = await append_event(
        conn,
        event_type="task.completed",
        payload=payload,
        actor_id=actor_id,
    )

    assert res is not None
    expected_hash = chain_hash("PREV_HASH", payload)
    assert res["integrity_hash"] == expected_hash


@pytest.mark.asyncio
async def test_append_event_idempotency_duplicate():
    conn = MockJournalConnection()
    conn.fail_idempotency = True

    res = await append_event(
        conn,
        event_type="task.created",
        payload={"k": "v"},
        actor_id=uuid.uuid4(),
        idempotency_key="dup-key",
    )
    assert res is None


@pytest.mark.asyncio
async def test_journal_hooks():
    conn = MockJournalConnection()
    pre_called = []
    post_called = []

    def pre_hook(conn, actor_id, actor_type, event_type):
        pre_called.append((actor_id, event_type))

    def post_hook(event_id, event_type, payload, context_goal_id, context_task_id):
        post_called.append((event_id, event_type))

    register_pre_append_hook(pre_hook)
    register_post_append_hook(post_hook)

    aid = uuid.uuid4()
    await append_event(
        conn,
        event_type="task.started",
        payload={"foo": "bar"},
        actor_id=aid,
    )
    assert len(pre_called) > 0
    assert len(post_called) > 0


def test_generate_event_id():
    eid = generate_event_id()
    assert isinstance(eid, uuid.UUID)


class MockReadCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class MockReadConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self._in_tx = False

    def is_in_transaction(self) -> bool:
        return self._in_tx

    def cursor(self, query: str, *args: Any):
        self.queries.append((query, args))
        return MockReadCursor(self.rows)

    async def fetch(self, query: str, *args: Any):
        self.queries.append((query, args))
        return self.rows

    async def fetchrow(self, query: str, *args: Any):
        self.queries.append((query, args))
        if "WHERE sequence_number < $1" in query:
            limit_val = args[0]
            matching = [r for r in self.rows if r.get("sequence_number", 0) < limit_val]
            return matching[-1] if matching else None
        return None


@pytest.mark.asyncio
async def test_read_events_streams_and_populates_fields():
    import datetime as dt
    from agentos_ledger_client.journal import read_events
    from agentos_ledger_client.types import LedgerEvent

    eid = uuid.uuid4()
    aid = uuid.uuid4()
    now = dt.datetime.now(dt.timezone.utc)
    mock_row = {
        "event_id": eid,
        "event_type": "task.created",
        "envelope_version": 1,
        "event_version": 1,
        "timestamp": now,
        "sequence_number": 42,
        "actor_id": aid,
        "actor_type": "system",
        "on_behalf_of": None,
        "correlation_id": None,
        "causation_ids": [],
        "context_task_id": "task-1",
        "context_goal_id": "goal-1",
        "context_team_id": "",
        "context_conversation_id": "",
        "classification": "normal",
        "integrity_hash": "hash-abc",
        "previous_hash": "GENESIS",
        "payload": '{"title": "test", "reward": "NaN"}',
        "extensions": "{}",
        "idempotency_key": "idemp-1",
    }
    conn = MockReadConnection([mock_row])

    events = [ev async for ev in read_events(conn, event_type="task.created")]
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, LedgerEvent)
    assert ev.event_id == eid
    assert ev.sequence_number == 42
    assert ev.timestamp == now
    assert ev.created_at == now
    assert ev.payload["reward"] == "NaN"
    assert "ORDER BY sequence_number ASC" in conn.queries[0][0]


@pytest.mark.asyncio
async def test_verify_chain_success_and_divergence():
    from agentos_ledger_client.chain import GENESIS, chain_hash
    from agentos_ledger_client.journal import verify_chain

    e1_id = uuid.uuid4()
    p1 = {"step": 1}
    h1 = chain_hash(GENESIS, p1)

    e2_id = uuid.uuid4()
    p2 = {"step": 2}
    h2 = chain_hash(h1, p2)

    rows = [
        {
            "event_id": e1_id,
            "sequence_number": 1,
            "previous_hash": GENESIS,
            "integrity_hash": h1,
            "payload": p1,
        },
        {
            "event_id": e2_id,
            "sequence_number": 2,
            "previous_hash": h1,
            "integrity_hash": h2,
            "payload": p2,
        },
    ]

    # Valid chain
    conn = MockReadConnection(rows)
    res = await verify_chain(conn)
    assert res.ok is True
    assert res.bad_event_id is None

    # Corrupted payload on row 2
    corrupt_rows = [
        rows[0],
        {
            "event_id": e2_id,
            "sequence_number": 2,
            "previous_hash": h1,
            "integrity_hash": h2,
            "payload": {"step": 2, "tampered": True},
        },
    ]
    conn2 = MockReadConnection(corrupt_rows)
    res2 = await verify_chain(conn2)
    assert res2.ok is False
    assert res2.bad_event_id == e2_id
    assert res2.actual_hash == h2
    assert res2.expected_hash == chain_hash(h1, {"step": 2, "tampered": True})

    # Broken linkage on row 2
    broken_link_rows = [
        rows[0],
        {
            "event_id": e2_id,
            "sequence_number": 2,
            "previous_hash": "WRONG_PREV",
            "integrity_hash": h2,
            "payload": p2,
        },
    ]
    conn3 = MockReadConnection(broken_link_rows)
    res3 = await verify_chain(conn3)
    assert res3.ok is False
    assert res3.bad_event_id == e2_id
    assert res3.actual_hash == "WRONG_PREV"
    assert res3.expected_hash == h1
