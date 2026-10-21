from __future__ import annotations

import uuid

import pytest

from agentos_ledger_client.client import LedgerClient
from agentos_ledger_client.types import AppendResult, BlobRef, ChainHead


class DummyConn:
    def __init__(self) -> None:
        self.head = {"id": 1, "last_event_id": uuid.uuid4(), "last_hash": "GENESIS", "event_count": 5}

    async def execute(self, query: str, *args):
        pass

    async def fetchrow(self, query: str, *args):
        if "FROM chain_head WHERE id = 1" in query:
            return self.head
        return None


@pytest.mark.asyncio
async def test_ledger_client_get_chain_head():
    conn = DummyConn()
    client = LedgerClient(target=conn)
    head = await client.get_chain_head()
    assert head is not None
    assert head.id == 1
    assert head.event_count == 5
    assert head.last_hash == "GENESIS"


def test_ledger_client_static_methods():
    h = LedgerClient.compute_chain_hash("GENESIS", {"a": 1})
    assert len(h) == 64

    d = LedgerClient.compute_envelope_digest(
        event_type="test",
        actor_id=uuid.uuid4(),
        actor_type="human",
    )
    assert len(d) == 64
