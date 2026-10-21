"""Legs run at the same time, and the number is configuration.

WHAT WAS WRONG. `worker_tick` awaited each claimable execution's leg in turn on
one connection, so `WORKER_CONCURRENCY = 4` meant "advance four runs per sweep,
one after another". A leg is a model turn and its tool calls — seconds to
minutes, nearly all of it waiting on a provider — so N stories cost N times the
wall clock for work that was never CPU-bound. Measured on KER16: two stories,
one 20-step block from run 1531 with run 1532 idle throughout, then the reverse.

WHY IT NEEDED A CONNECTION EACH, and why that is the whole of the change:
`leg_lock` is a Postgres advisory lock held for the length of the leg, and its
docstring requires acquire and release on the same connection. asyncpg also
refuses concurrent use of one connection outright. So sharing `conn` was not a
slow version of this — it was the reason this could not exist.

These tests use fakes for the driver, the leg and the executor. That is the
point: what is under test is the LOOP's decision to overlap, not what a leg
contains — the same reason `run_leg_fn`, `make_executor` and `adapter` are
injected rather than imported.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time

from kernel.worker.concurrency import (  # noqa: I001
    DEFAULT_LEG_CONCURRENCY,
    LEG_CONCURRENCY_ENV,
    leg_concurrency,
    pool_safe_fanout,
    run_each_on_its_own_connection,
)


# ── the number is configuration ──────────────────────────────────────────

@contextlib.contextmanager
def _env(value):
    was = os.environ.pop(LEG_CONCURRENCY_ENV, None)
    if value is not None:
        os.environ[LEG_CONCURRENCY_ENV] = value
    try:
        yield
    finally:
        os.environ.pop(LEG_CONCURRENCY_ENV, None)
        if was is not None:
            os.environ[LEG_CONCURRENCY_ENV] = was


def test_the_default_is_four_and_is_asserted_not_derived():
    """Four, matching the admission bucket, so a goal at its ceiling is not
    also queueing behind this. A change to it should be a change somebody
    made on purpose."""
    with _env(None):
        assert leg_concurrency() == 4
        assert DEFAULT_LEG_CONCURRENCY == 4


def test_it_can_be_raised_and_lowered():
    with _env("1"):
        assert leg_concurrency() == 1          # back to the old behaviour
    with _env("8"):
        assert leg_concurrency() == 8


def test_it_is_capped_at_what_the_pool_can_survive():
    """Exhausting the pool does not fail loudly — it makes every other query
    in the process wait for a connection, which reads as "the database got
    slow" and never as "somebody raised a setting"."""
    with _env("999"):
        assert leg_concurrency() == pool_safe_fanout()


def test_the_clamp_is_derived_from_the_pool_and_is_not_a_ceiling():
    """D5. This number used to be a flat `MAX_LEG_CONCURRENCY = 10` that
    two other modules read as THE concurrency ceiling. It is a
    connection-pool clamp and nothing else: derived from `POOL_MAX` so it
    cannot drift from its reason, and the ceiling now lives in one place,
    `admit.SYSTEM_CEILING`."""
    from kernel.loopbridge import POOL_MAX

    assert pool_safe_fanout() == POOL_MAX - 6 == 10
    assert pool_safe_fanout() >= 1


def test_nonsense_falls_back_rather_than_stopping_or_flooding():
    """A typo must not silently halt the fleet, and must not silently take
    every connection either."""
    for bad in ("banana", "0", "-3", "  "):
        with _env(bad):
            assert leg_concurrency() == DEFAULT_LEG_CONCURRENCY, bad


# ── the legs actually overlap ────────────────────────────────────────────

class _FakeConn:
    """Refuses concurrent use, exactly as asyncpg does.

    This is the assertion that matters most: if the loop ever hands two legs
    one connection again, this raises instead of quietly working in a test and
    failing in production under load.
    """

    def __init__(self, name):
        self.name = name
        self._busy = False

    @contextlib.contextmanager
    def _one_at_a_time(self):
        if self._busy:
            raise RuntimeError(
                f"another operation is in progress on {self.name}")
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    async def fetch(self, *a, **k):
        with self._one_at_a_time():
            await asyncio.sleep(0)
            return []

    async def fetchval(self, *a, **k):
        with self._one_at_a_time():
            await asyncio.sleep(0)
            return 0

    async def execute(self, *a, **k):
        with self._one_at_a_time():
            await asyncio.sleep(0)
            return "OK"


def _pool_of(conns):
    """`acquire` as `pool.acquire` presents it: a fresh connection each time."""
    handed = []

    @contextlib.asynccontextmanager
    async def acquire():
        c = conns[len(handed) % len(conns)]
        handed.append(c)
        yield c

    return acquire, handed


def test_legs_on_separate_connections_overlap_in_time():
    """Four legs that each wait 100ms finish in well under 400ms.

    Timing is the only honest assertion for concurrency, so the margin is
    generous: serial is >=400ms and nothing under 250ms can be serial. The
    connections REFUSE concurrent use, so if the fan-out ever shares one this
    fails loudly here instead of quietly under production load.
    """
    async def run():
        rows = [{"id": i} for i in range(4)]
        acquire, handed = _pool_of([_FakeConn(f"c{i}") for i in range(4)])
        started = []

        async def advance(conn, row):
            started.append(row["id"])
            await conn.fetchval("SELECT 1")      # would raise if shared
            await asyncio.sleep(0.1)
            await conn.execute("SELECT 1")
            return [{"execution_id": row["id"], "action": "leg"}]

        t0 = time.monotonic()
        out = await run_each_on_its_own_connection(acquire, rows, advance)
        elapsed = time.monotonic() - t0

        assert len(out) == 4
        assert all(not isinstance(o, BaseException) for o in out), out
        assert [o[0]["execution_id"] for o in out] == [0, 1, 2, 3], (
            "order must match the input, or `actions` stops being readable")
        assert elapsed < 0.25, f"took {elapsed:.2f}s — the legs did not overlap"
        assert len(started) == 4
    asyncio.run(run())


def test_one_leg_blowing_up_does_not_cancel_the_others():
    """The rows are unrelated runs. A bare gather would cancel three
    mid-model-call because a fourth raised."""
    async def run():
        rows = [{"id": i} for i in range(4)]
        acquire, _ = _pool_of([_FakeConn(f"c{i}") for i in range(4)])
        finished = []

        async def advance(conn, row):
            if row["id"] == 2:
                raise RuntimeError("boom")
            await asyncio.sleep(0.05)
            finished.append(row["id"])
            return [{"execution_id": row["id"], "action": "leg"}]

        out = await run_each_on_its_own_connection(acquire, rows, advance)
        assert isinstance(out[2], RuntimeError), out[2]
        assert sorted(finished) == [0, 1, 3], finished
    asyncio.run(run())


def test_a_shared_connection_would_be_caught():
    """Proves the guard above has teeth: hand every leg ONE connection and the
    fake raises exactly as asyncpg does."""
    async def run():
        shared = _FakeConn("shared")

        @contextlib.asynccontextmanager
        async def acquire():
            yield shared

        async def advance(conn, row):
            await conn.fetchval("SELECT 1")
            await asyncio.sleep(0.05)
            await conn.execute("SELECT 1")
            return []

        out = await run_each_on_its_own_connection(
            acquire, [{"id": i} for i in range(3)], advance)
        assert any(isinstance(o, RuntimeError) for o in out), (
            "a shared connection must be detected, or this whole file "
            "proves nothing")
    asyncio.run(run())


# ── the loop wires it up, read off the file ──────────────────────────────
#
# `kernel.worker.loop` imports the conductor, which imports the model
# adapters, which import an SDK that is not installed everywhere this suite
# runs. A gate that skips on the machines that need it is not a gate, so these
# read the source instead.

def _loop_src() -> str:
    import pathlib
    return pathlib.Path(__file__).resolve().parents[3].joinpath(
        "kernel", "worker", "loop.py").read_text(encoding="utf-8")


def test_a_caller_with_no_pool_stays_serial():
    """Every test and the CLI call `worker_tick` without `acquire`. They keep
    exactly the behaviour they had — one connection, one leg at a time."""
    src = _loop_src()
    assert "acquire=None" in src, "the parameter must default to off"
    assert "if acquire is None or len(rows) < 2:" in src, (
        "the serial fallback must remain, and must also cover the "
        "single-row case where fanning out buys nothing")


def test_the_leg_connection_reaches_the_executor():
    """`make_executor` used to close over the sweep's single connection for its
    sub-agent context. Invisible while legs were serial; 'another operation is
    in progress' the moment two sub-agents touch the database together."""
    assert 'row["conn"] = conn' in _loop_src(), (
        "the leg's own connection must go on the row, the same way the bridge "
        "port does — make_executor is synchronous and the row is what it is "
        "given")


def test_the_fanout_has_one_implementation():
    """The loop must CALL the named fan-out, not keep a second copy. This
    file's own history is the argument: `worker_pass` was a second
    `worker_tick` and drifted until three features were unreachable."""
    src = _loop_src()
    assert "run_each_on_its_own_connection" in src
    assert "asyncio.gather" not in src, (
        "the gather belongs in concurrency.py, where it is tested")
