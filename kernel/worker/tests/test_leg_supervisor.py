"""A sweep dispatches legs; it does not wait for them.

THE GAP THIS CLOSES, measured rather than imagined. Giving each leg its own
connection made legs claimed in the SAME tick run together, and only that.
`_worker_forever` awaits `worker_pass` before sleeping, so while a batch is in
flight no new sweep starts. On KER17:

    13:06:01  run 1551 un-parks -> the next tick claims it ALONE
    13:06:03  run 1550 un-parks -> two seconds late; the sweep is inside
                                   1551's leg and will not return until it ends
    (later)   1550 takes its first step the instant 1551 parks

Two seconds of skew serialised two independent stories completely. The leg loop
had stopped being the bottleneck and the sweep loop had become one.

The test that matters here is `test_a_run_that_arrives_mid_batch_...`: it
reproduces that skew exactly and asserts the late arrival starts while the
first leg is still running.
"""
from __future__ import annotations

import asyncio

from kernel.worker.supervisor import LegSupervisor


def test_spawn_returns_immediately_and_the_leg_runs_after():
    """The whole point: the sweep must not be the thing waiting."""
    async def run():
        sup = LegSupervisor()
        done = []

        async def leg():
            await asyncio.sleep(0.05)
            done.append("finished")
            return [{"action": "leg"}]

        assert sup.spawn(1, leg()) is True
        assert done == [], "spawn awaited the leg instead of dispatching it"
        assert sup.in_flight() == {1}

        await sup.drain()
        assert done == ["finished"]
        assert sup.in_flight() == set(), "the slot was never released"
    asyncio.run(run())


def test_a_run_that_arrives_mid_batch_starts_without_waiting():
    """KER17, reproduced. The second leg must begin while the first is still
    running — not after it ends."""
    async def run():
        sup = LegSupervisor()
        started, order = {}, []

        def leg(name, secs):
            async def _l():
                started[name] = asyncio.get_running_loop().time()
                await asyncio.sleep(secs)
                order.append(name)
                return []
            return _l()

        # Sweep 1: a long leg, as 1551's was.
        sup.spawn(1551, leg("first", 0.30))
        # Sweep 2, one tick later: the run that un-parked two seconds late.
        await asyncio.sleep(0.05)
        assert sup.free_slots(4) == 3, "a busy leg must not consume every slot"
        assert sup.spawn(1550, leg("second", 0.05)) is True

        await sup.drain()
        # The SHORT leg finishes first — impossible if it had waited its turn.
        assert order == ["second", "first"], order
        assert started["second"] < started["first"] + 0.30
    asyncio.run(run())


def test_the_same_execution_is_never_taken_twice():
    """`leg_lock` would refuse it anyway; this avoids spending a connection
    and a claim to be told so."""
    async def run():
        sup = LegSupervisor()
        ran = []

        async def leg():
            ran.append(1)
            await asyncio.sleep(0.05)
            return []

        assert sup.spawn(7, leg()) is True
        assert sup.spawn(7, leg()) is False, "double-took a live execution"
        await sup.drain()
        assert ran == [1]
    asyncio.run(run())


def test_free_slots_bounds_what_a_sweep_may_start():
    async def run():
        sup = LegSupervisor()

        async def leg():
            await asyncio.sleep(0.05)
            return []

        for i in range(4):
            sup.spawn(i, leg())
        assert sup.free_slots(4) == 0
        # A limit lowered between sweeps must not read as a negative count.
        assert sup.free_slots(2) == 0
        await sup.drain()
        assert sup.free_slots(4) == 4
    asyncio.run(run())


def test_a_failing_leg_releases_its_slot_and_spares_the_others():
    """A leg that fails in a way its own handler did not predict must not
    wedge its execution in-flight for ever — that is a run that never moves
    again with nothing anywhere saying why."""
    async def run():
        sup = LegSupervisor()
        survived = []

        async def boom():
            raise RuntimeError("leg exploded")

        async def fine():
            await asyncio.sleep(0.05)
            survived.append("ok")
            return []

        sup.spawn(1, boom())
        sup.spawn(2, fine())
        await sup.drain()

        assert sup.in_flight() == set(), "a failed leg held its slot"
        assert survived == ["ok"], "one leg's failure took another down"
        # And the execution can be started again on the next sweep.
        assert sup.spawn(1, fine()) is True
        await sup.drain()
    asyncio.run(run())


def test_the_tick_dispatches_rather_than_gathers_when_given_a_supervisor():
    """Read off the file: `kernel.worker.loop` imports the model SDKs, which
    are not installed everywhere this suite runs."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[3].joinpath(
        "kernel", "worker", "loop.py").read_text(encoding="utf-8")
    tick = src[src.index("async def worker_tick"):]

    assert "if supervisor is not None and acquire is not None:" in tick
    # It must RETURN after dispatching, or the sweep still waits.
    dispatch = tick.index("supervisor.spawn(eid, _leg())")
    assert "return actions" in tick[dispatch:dispatch + 400], (
        "the dispatch branch must return without awaiting the legs")
    # And it must skip what is already running.
    assert "supervisor.in_flight()" in tick
    assert "supervisor.free_slots(max_concurrent)" in tick
