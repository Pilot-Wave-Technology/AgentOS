"""The instrument must not be able to take down what it measures.

Kept out of `test_loop_heartbeats_sql.py` on purpose: that file skips at
module level when no database is reachable, and these two properties are
exactly the ones you want asserted on a laptop with nothing running.
"""

from __future__ import annotations

import asyncio

import pytest

from kernel.obs import heartbeat as hb


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _DeadDB:
    async def execute(self, *a, **k):
        raise RuntimeError("database gone")


def test_a_failing_heartbeat_write_does_not_fail_the_pass():
    async def go():
        async with hb.beat(_DeadDB(), "worker.pass", every=4.0) as b:
            b.did(1)
        return "finished"

    assert _run(go()) == "finished", (
        "a heartbeat that can take down the loop it measures is a "
        "liability, not an instrument -- and a missing row already reads "
        "as 'late', which is the correct alarm anyway")


def test_the_body_s_exception_still_propagates():
    """Recording a failure must not swallow it."""
    class _OK:
        async def execute(self, *a, **k):
            return None

    async def go():
        async with hb.beat(_OK(), "worker.pass", every=4.0):
            raise ValueError("drain exploded")

    with pytest.raises(ValueError, match="drain exploded"):
        _run(go())


def test_work_defaults_to_zero_not_one():
    """`tick_count` says the pass ran; `work_count` says it found
    something. Conflating them hides a loop that ticks and does nothing."""
    seen = {}

    class _Capture:
        async def execute(self, _sql, *args):
            seen["work"] = args[5]

    async def go():
        async with hb.beat(_Capture(), "worker.pass", every=4.0):
            pass

    _run(go())
    assert seen["work"] == 0


def test_instance_id_is_never_empty():
    """The primary key is (loop_name, instance_id); an empty one would
    silently collapse two instances into a single row and hide exactly
    the two-revisions-at-once case the table exists to show."""
    assert hb.instance_id()
