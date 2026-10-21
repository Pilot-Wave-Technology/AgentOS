"""A leg already running is not thrown away by a shutdown.

WHAT WAS TRUE BEFORE, AND IT COST A LEG PER DEPLOY.

`LegSupervisor.drain` was written for this, says so in its own
docstring -- "For shutdown and for tests. `timeout` of 0 means wait as
long as it takes, because a leg interrupted halfway is a sandbox left
mid-tool" -- and had NO CALLER anywhere outside tests. The same shape as
`reconcile_tick`, which this codebase found written, tested and reachable
from nowhere.

So SIGTERM did only half the job. `stopping` stopped NEW passes; the
loop fell out, `main` closed the pool, the process exited, and the event
loop cancelled every in-flight leg on its way down:

    worker service: SIGTERM received, draining
    worker service: closing pool
    worker: leg for 196 cancelled

Measured on execution 196 while iterating, and again on 197. Both times
the leg restarted from its checkpoint, so nothing was LOST -- but a
model turn that had already been paid for was thrown away, and the
sandbox was left mid-tool.

IT IS NOT A DEV-LOOP PROBLEM. The same path runs on every rolling
update: `maxUnavailable: 0, maxSurge: 1` brings a new pod up and sends
the old one SIGTERM, so shipping a fix discarded whatever legs happened
to be running. With twenty stories in flight that is twenty turns. It is
one of the reasons a conductor deploy was listed as an argument FOR
moving the leg into the pod -- and it turns out to be a missing call
rather than a property of the architecture.

BOUNDED, BECAUSE SIGKILL IS NOT NEGOTIABLE. `terminationGracePeriodSeconds`
ends the argument, so the drain has a ceiling and the manifest's grace
period is set above it. A leg past the bound is cancelled exactly as
before; what changes is that the ORDINARY leg is not.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from kernel.worker import service
from kernel.worker.service_drain import drain_legs
from kernel.worker.supervisor import LegSupervisor


# ── the missing call ─────────────────────────────────────────────────

def test_the_loop_drains_before_it_returns():
    """The defect was a function with no caller, so this asserts the
    call exists at all -- and the behavioural tests below assert it
    does something."""
    # BOTH HALVES, because the call moved (D2a): `run_forever` now
    # calls `drain_legs`, and `drain_legs` is what calls `.drain(`.
    # Asserting only the first would pass while the supervisor went
    # undrained again, which is the exact defect this test exists for.
    assert "drain_legs(supervisor)" in inspect.getsource(
        service.run_forever), "run_forever no longer drains at all"
    assert ".drain(" in inspect.getsource(drain_legs), (
        "LegSupervisor.drain is written, documented for shutdown, and "
        "called from nowhere -- which is how a deploy came to discard "
        "every leg in flight")


def test_every_pool_this_loop_fills_is_drained():
    """WHATEVER IT SPAWNS, IT DRAINS. There were two supervisors here --
    an execution is either taking a leg or being adjudicated -- and
    draining one would have left the other's work to be cancelled, the
    same bug with half the blast radius.

    E12 moved the verdict to `kernel/verifier/service.py`, so this loop
    spawns legs and nothing else; its drain is now the whole of what it
    starts. Asserted as a RELATION rather than a list of names, so
    another pool added here without a drain fails this rather than
    passing because the two names it used to check are still present."""
    src = inspect.getsource(service.run_forever)
    spawned = {line.split("=")[0].strip() for line in src.splitlines()
               if "LegSupervisor()" in line and "=" in line}
    assert spawned, "the loop no longer builds a supervisor at all"
    body = src[src.index("LET THE LEGS IN FLIGHT FINISH"):]
    for name in spawned:
        assert name in body, (
            f"{name} is filled by this loop and not drained on the way "
            f"out; every leg in it is cancelled by the exiting loop")


# ── and it waits, for real ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_running_leg_finishes():
    """The behaviour. A slow leg must COMPLETE, not be cancelled."""
    done = []

    async def _slow():
        await asyncio.sleep(0.2)
        done.append("finished")
        return [{"action": "done"}]

    sup = LegSupervisor()
    assert sup.spawn(1, _slow())
    await sup.drain(timeout=5)

    assert done == ["finished"], (
        "the leg was cut off; a cancelled leg is a paid-for model turn "
        "thrown away and a sandbox left mid-tool")
    assert not sup.in_flight()


@pytest.mark.asyncio
async def test_the_wait_is_bounded():
    """A leg that outlasts the bound must not hold the process past the
    grace period -- SIGKILL would end it anyway, less tidily."""
    async def _forever():
        await asyncio.sleep(30)

    sup = LegSupervisor()
    sup.spawn(2, _forever())

    started = asyncio.get_running_loop().time()
    left = await sup.drain(timeout=0.2)
    waited = asyncio.get_running_loop().time() - started

    assert waited < 5, f"drain ignored its timeout ({waited:.1f}s)"
    assert left, "the still-running leg should be reported"
    assert sup.in_flight() == {2}
    for task in list(sup._tasks.values()):
        task.cancel()


@pytest.mark.asyncio
async def test_nothing_in_flight_is_free():
    """The ordinary shutdown, which must not pay a timeout for nothing."""
    sup = LegSupervisor()
    started = asyncio.get_running_loop().time()
    assert await sup.drain(timeout=10) == 0
    assert asyncio.get_running_loop().time() - started < 1


# ── the bound, and the manifest that has to allow it ─────────────────

def test_the_bound_is_readable_and_has_no_hidden_default(monkeypatch):
    monkeypatch.setenv(service.DRAIN_ENV, "42")
    assert service.drain_max_s() == 42.0
    monkeypatch.delenv(service.DRAIN_ENV)
    assert service.drain_max_s() == service.DRAIN_DEFAULT_S


@pytest.mark.parametrize("bad", ["", "soon", "-1", "  "])
def test_nonsense_falls_back_rather_than_to_zero(monkeypatch, bad):
    """Zero would mean "cancel everything immediately", which is the old
    behaviour arriving through a typo."""
    monkeypatch.setenv(service.DRAIN_ENV, bad)
    assert service.drain_max_s() == service.DRAIN_DEFAULT_S


def test_the_grace_period_outlasts_the_drain():
    """THE ONE THAT KEEPS THEM IN STEP. A drain longer than the grace
    period is SIGKILL arriving mid-drain -- the drain would then be a
    delay rather than a guarantee, and the legs would be cancelled after
    waiting for them."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    manifest = (root / "deploy" / "k8s" / "conductor.yaml").read_text(
        encoding="utf-8")
    found = [int(m) for m in re.findall(
        r"^\s*terminationGracePeriodSeconds:\s*(\d+)", manifest, re.M)]
    assert found, "no terminationGracePeriodSeconds in the manifest"
    for grace in found:
        assert grace > service.DRAIN_DEFAULT_S, (
            f"grace period {grace}s does not outlast the "
            f"{service.DRAIN_DEFAULT_S:.0f}s drain; raise the manifest "
            f"BEFORE raising the drain")
