"""The loops run in one place, always, and can say how they are doing.

THREE FAULTS THIS PINS, all measured on `next` rather than reasoned:

  CLOUD RUN THROTTLES CPU BETWEEN REQUESTS, so a `while True` loop only
  advances while the instance serves something. git-engine-next managed
  2 ticks in ten minutes against the same code in an environment
  deployed with --no-cpu-throttling doing 30. A story waited 84 seconds
  for a branch that is one API call.

  EVERY INSTANCE RAN EVERY LOOP, up to maxScale 100, against a database
  that hands out 400 connections and sees 24 per instance.

  A DEAD LOOP LOOKED LIKE AN IDLE ONE. The park-card sweep failed every
  five seconds for two and a half days, logged it every time, and was
  found when somebody asked about a missing card.

WHAT MUST STAY TRUE, and these are the assertions below:

  * a loop the scheduler owns does NOT also run in the web tier
  * the scheduler service is deployed so its loops can actually run
  * a loop that raises is restarted rather than lost
  * a loop that never reports is `None`, not `ok`
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
import yaml

from app.services.scheduler.registry import loops
from app.services.scheduler.supervisor import Loop, Supervisor

REPO = pathlib.Path(__file__).resolve().parents[3]
MAIN = REPO / "app" / "main.py"
BUILD = REPO / "cloudbuild-next.yaml"


def _step(name: str) -> dict:
    spec = yaml.safe_load(BUILD.read_text(encoding="utf-8"))
    for step in spec["steps"]:
        if step.get("id") == name:
            return step
    raise AssertionError(f"no {name} step in cloudbuild-next.yaml")


# ── one home ────────────────────────────────────────────────────────
def test_every_scheduler_loop_is_gated_in_the_web_tier():
    """A loop in both places runs twice, and several take no lock.

    The registry is the list; `_scheduler_loops_run_here()` is the gate.
    Any call site in main.py that starts a registry loop outside that
    gate is a reaper running once per instance.
    """
    main = MAIN.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(main)
              if "create_task(_park_cards())" in ln
              or "create_task(_tok_refresh_loop())" in ln
              or "create_task(_cred_watchdog_loop())" in ln
              or "create_task(_dag_janitor_loop())" in ln]
    assert len(starts) == 4, (
        f"expected the four moved loops to still have call sites in "
        f"main.py (gated); found {len(starts)}")
    for i in starts:
        window = "\n".join(main[max(0, i - 3):i])
        assert "_scheduler_loops_run_here()" in window, (
            f"main.py line {i + 1} starts a scheduler-owned loop without "
            f"the gate above it: {main[i].strip()!r}")


def test_the_gate_defaults_to_running_them():
    """A control panel with no scheduler beside it must not go quiet.

    dev, feature and a laptop deploy the web tier alone. If the default
    were off, moving these loops would stop them everywhere that had not
    also gained a scheduler -- silently, since a loop that never starts
    logs nothing.
    """
    from app.main import _scheduler_loops_run_here

    assert _scheduler_loops_run_here() is True


def test_the_registry_names_real_callables():
    entries = loops()
    assert entries, "the registry is empty"
    assert len({x.name for x in entries}) == len(entries), "duplicate names"
    for entry in entries:
        assert callable(entry.factory), f"{entry.name} has no factory"
        assert entry.why, f"{entry.name} does not say what it is for"


# ── deployed so it can run ──────────────────────────────────────────
@pytest.mark.parametrize("step", ["deploy-scheduler", "deploy-git-engine"])
def test_a_loop_only_service_gets_cpu_between_requests(step):
    """Without this flag the service is deployed and does not work.

    Cloud Run gives a container CPU while it serves a request. These two
    serve nothing -- their whole job is a loop -- so throttled they
    advance only when something else wakes the instance.
    """
    args = _step(step)["args"]
    assert "--no-cpu-throttling" in args, (
        f"{step} has no --no-cpu-throttling, so its loops will run at "
        f"whatever rate incidental traffic allows. Measured: 2 ticks in "
        f"ten minutes against 30.")
    assert "--min-instances=1" in args, f"{step} can scale to zero"


def test_the_scheduler_is_a_singleton():
    args = _step("deploy-scheduler")["args"]
    assert "--max-instances=1" in args, (
        "two schedulers means two of every reaper; the leader lock "
        "narrows the window but the cap is what keeps it to one")


def test_the_web_tier_hands_its_loops_over_and_is_capped():
    """Both halves, because either alone is wrong.

    Without the env var the web tier keeps running loops the scheduler
    now also runs. Without the cap, 100 instances x 24 connections asks
    a 400-connection server for 2400 -- and that failure takes the
    conductor with it, not just the UI.
    """
    args = _step("deploy-control-panel")["args"]
    env = next(a for a in args if a.startswith("--set-env-vars="))
    assert "AGENTOS_SCHEDULER_LOOPS=0" in env, (
        "the control panel has not handed its loops to the scheduler")
    cap = [a for a in args if a.startswith("--max-instances=")]
    assert cap, "the control panel has no instance cap"
    assert int(cap[0].split("=")[1]) <= 10, (
        f"{cap[0]} exceeds what 400 server connections allow at ~24 per "
        f"instance")


# ── it can say how the loops are doing ──────────────────────────────
@pytest.mark.asyncio
async def test_a_loop_that_raises_is_restarted_not_lost():
    """Today a loop that raised out of `while True` was simply gone."""
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        raise RuntimeError("boom")

    sup = Supervisor()
    import app.services.scheduler.supervisor as mod
    mod.BACKOFF_START_S, mod.STAGGER_S = 0.01, 0.0
    sup.start((Loop("flaky", flaky, "test"),))
    await asyncio.sleep(0.2)
    await sup.stop()
    assert calls["n"] > 1, "a raising loop was not restarted"
    assert sup.status()["loops"]["flaky"]["error"], "the error was not recorded"


def test_a_loop_that_never_reports_is_unknown_rather_than_healthy():
    """The distinction the outage turned on.

    The sweep ran on time, caught its own exception and did nothing for
    two and a half days. Liveness said healthy. `ok_age_s` is None until
    a loop actually reports a pass that worked, so an unreported loop
    reads as unknown -- which is true -- instead of green.
    """
    from app.services.scheduler.supervisor import _State

    sup = Supervisor()

    sup._state["quiet"] = _State(running=True)
    assert sup.status()["loops"]["quiet"]["ok_age_s"] is None

    sup.beat("quiet")
    assert sup.status()["loops"]["quiet"]["ok_age_s"] is not None
