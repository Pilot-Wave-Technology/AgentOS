"""Liveness is a row. These pin what the row has to mean.

The alerts in the plan rest on three properties of this table and none
of them are obvious from the DDL: a heartbeat is written per PASS rather
than per loop, the last error survives a recovery, and two builds
ticking the same loop are visible as two rows rather than one
overwriting the other.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from kernel.obs import heartbeat as hb  # noqa: E402

DSN = (os.environ.get("TEST_DATABASE_URL")
       or os.environ.get("DATABASE_URL")
       or "postgresql://agentos:agentos@localhost:5433/agentos")


_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


# ONE reachability check for the whole module. Per-test it cost a TCP
# connect timeout SEVEN times -- twenty-eight seconds to report that a
# database is absent, on every run, which is how a suite gets skipped
# rather than fixed.
try:
    _probe = _run(asyncpg.connect(DSN, timeout=3))
    _run(_probe.close())
except Exception as _exc:                                   # noqa: BLE001
    pytest.skip("no database at %s (%s)"
                % (DSN.rsplit("@", 1)[-1], type(_exc).__name__),
                allow_module_level=True)


@pytest.fixture()
def db():
    conn = _run(asyncpg.connect(DSN))
    yield conn
    _run(conn.close())


@pytest.fixture()
def loop_name(db):
    name = "test.pass.%s" % uuid.uuid4().hex[:10]
    yield name
    _run(db.execute("DELETE FROM public.loop_heartbeats WHERE loop_name = $1",
                    name))


def _row(db, name):
    return _run(db.fetchrow(
        "SELECT * FROM public.loop_heartbeats WHERE loop_name = $1", name))


def test_a_pass_writes_a_row(db, loop_name):
    async def once():
        async with hb.beat(db, loop_name, every=4.0) as b:
            b.did(3)

    _run(once())
    row = _row(db, loop_name)
    assert row is not None, "the pass ran and left no evidence"
    assert row["tick_count"] == 1
    assert row["work_count"] == 3
    assert row["expected_interval_s"] == 4
    assert row["last_error"] is None


def test_ticks_and_work_accumulate_separately(db, loop_name):
    """A loop that ticks happily while doing nothing is a distinct
    failure from one that stopped, and the two are indistinguishable
    without both counters."""
    async def go():
        for did in (2, 0, 0):
            async with hb.beat(db, loop_name, every=1.0) as b:
                b.did(did)

    _run(go())
    row = _row(db, loop_name)
    assert row["tick_count"] == 3, "ticks did not accumulate"
    assert row["work_count"] == 2, "idle passes counted as work"


def test_a_failure_is_recorded_and_still_raised(db, loop_name):
    """A heartbeat that swallows what it is measuring is worse than
    none."""
    async def boom():
        async with hb.beat(db, loop_name, every=1.0):
            raise ValueError("drain exploded")

    with pytest.raises(ValueError):
        _run(boom())

    row = _row(db, loop_name)
    assert row is not None, "a failing pass left no row at all"
    assert "ValueError: drain exploded" in row["last_error"]
    assert row["error_count"] == 1
    assert row["last_error_at"] is not None


def test_the_last_error_survives_a_recovery(db, loop_name):
    """A pass that fails every third tick is invisible in a gauge and
    obvious in a kept error."""
    async def boom():
        async with hb.beat(db, loop_name, every=1.0):
            raise RuntimeError("transient")

    async def fine():
        async with hb.beat(db, loop_name, every=1.0) as b:
            b.did(1)

    with pytest.raises(RuntimeError):
        _run(boom())
    _run(fine())

    row = _row(db, loop_name)
    assert row["tick_count"] == 2
    assert "RuntimeError: transient" in (row["last_error"] or ""), (
        "the error was cleared by the next success, so an intermittent "
        "failure leaves no trace")
    assert row["error_count"] == 1


def test_two_builds_of_one_loop_are_two_rows(db, loop_name, monkeypatch):
    """The stale-revision alert IS count(distinct build_sha), and it only
    works if the two do not overwrite each other."""
    async def tick():
        async with hb.beat(db, loop_name, every=1.0) as b:
            b.did(1)

    monkeypatch.setenv("HOSTNAME", "revision-00074")
    monkeypatch.setenv("BUILD_SHA", "aaaaaaa")
    _run(tick())
    monkeypatch.setenv("HOSTNAME", "revision-00075")
    monkeypatch.setenv("BUILD_SHA", "bbbbbbb")
    _run(tick())

    rows = _run(db.fetch(
        "SELECT build_sha FROM public.loop_heartbeats WHERE loop_name = $1",
        loop_name))
    assert len(rows) == 2, "one revision overwrote the other"
    assert {r["build_sha"] for r in rows} == {"aaaaaaa", "bbbbbbb"}

    split = _run(hb.split_brain(db, within_seconds=600))
    assert any(r["loop_name"] == loop_name and r["builds"] == 2
               for r in split), "split_brain did not see two builds"


def test_stale_skips_loops_with_no_declared_interval(db, loop_name):
    """An alert on an invented threshold is an alert that gets muted."""
    async def tick():
        async with hb.beat(db, loop_name, every=None):
            pass

    _run(tick())
    _run(db.execute(
        "UPDATE public.loop_heartbeats SET last_tick_at = now() - "
        "INTERVAL '1 day' WHERE loop_name = $1", loop_name))
    late = _run(hb.stale(db, factor=3.0))
    assert not any(r["loop_name"] == loop_name for r in late)
