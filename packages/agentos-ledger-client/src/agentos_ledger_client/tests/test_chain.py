from __future__ import annotations

import datetime as dt
import uuid


from agentos_ledger_client.chain import (
    GENESIS,
    GLOBAL_CHAIN_LOCK_ID,
    chain_hash,
    envelope_digest,
    new_event_id,
    payload_json,
)


def test_global_chain_lock_id_is_canonical():
    assert GLOBAL_CHAIN_LOCK_ID == 0x4C454447
    assert GENESIS == "GENESIS"


def test_payload_json_stability_and_types():
    assert payload_json({"b": 2, "a": 1}) == '{"a": 1, "b": 2}'
    assert payload_json({}) == "{}"
    assert payload_json(None) == "{}"

    # Datetime handling
    serialized = payload_json({"timestamp": dt.datetime(2026, 9, 10, 12, 0)})
    assert "2026-09-10" in serialized


def test_chain_hash_computation():
    h1 = chain_hash("GENESIS", {"a": 1, "b": 2})
    h2 = chain_hash("GENESIS", {"b": 2, "a": 1})
    assert h1 == h2
    assert len(h1) == 64

    # Different previous hash gives different result
    h3 = chain_hash("OTHER", {"a": 1, "b": 2})
    assert h3 != h1


def test_envelope_digest_coverage():
    base = {
        "event_type": "task.created",
        "actor_id": uuid.UUID(int=1),
        "actor_type": "human",
        "on_behalf_of": None,
        "payload": {"title": "Test"},
    }
    digest = envelope_digest(**base)
    assert len(digest) == 64

    # Changing any field must change digest
    assert envelope_digest(**{**base, "event_type": "task.updated"}) != digest
    assert envelope_digest(**{**base, "actor_id": uuid.UUID(int=2)}) != digest
    assert envelope_digest(**{**base, "actor_type": "agent"}) != digest
    assert envelope_digest(**{**base, "on_behalf_of": uuid.UUID(int=3)}) != digest
    assert envelope_digest(**{**base, "payload": {"title": "Changed"}}) != digest


def test_new_event_id():
    eid = new_event_id()
    assert isinstance(eid, uuid.UUID)
    eid2 = new_event_id()
    assert eid != eid2


def test_payload_json_non_finite_floats_canonical_strings():
    import json
    import math

    payload = {
        "loss": float("nan"),
        "q_val": float("inf"),
        "min_q": float("-inf"),
        "nested": {
            "arr": [1.5, float("nan"), float("inf"), float("-inf")],
            "tuple_arr": (float("nan"),),
        },
    }

    serialized = payload_json(payload)
    parsed = json.loads(serialized)

    # Exact string literals: must be "NaN", "Infinity", "-Infinity"
    assert parsed["loss"] == "NaN"
    assert parsed["q_val"] == "Infinity"
    assert parsed["min_q"] == "-Infinity"
    assert parsed["nested"]["arr"] == [1.5, "NaN", "Infinity", "-Infinity"]
    assert parsed["nested"]["tuple_arr"] == ["NaN"]

    # Reconstruct via float()
    assert math.isnan(float(parsed["loss"]))
    assert math.isinf(float(parsed["q_val"])) and float(parsed["q_val"]) > 0
    assert math.isinf(float(parsed["min_q"])) and float(parsed["min_q"]) < 0


def test_payload_json_byte_identity_preservation():
    import json
    from decimal import Decimal

    # Payloads containing datetime, UUID, Decimal, bool, int, float, str
    test_dt = dt.datetime(2026, 10, 9, 12, 30, 45, tzinfo=dt.timezone.utc)
    test_uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    test_dec = Decimal("42.500")

    payload = {
        "timestamp": test_dt,
        "uuid": test_uid,
        "amount": test_dec,
        "active": True,
        "count": 100,
        "rate": 3.14159,
        "name": "byte_identity_check",
        "nested": {
            "dt_list": [test_dt, test_uid],
            "dec_val": test_dec,
        },
    }

    # Baseline direct serialization
    baseline = json.dumps(payload, sort_keys=True, default=str)
    # Our payload_json output
    produced = payload_json(payload)

    # MUST be strictly byte-identical
    assert produced == baseline
    assert produced.encode("utf-8") == baseline.encode("utf-8")
