"""A knob that configures nothing is worse than a knob that is absent.

THREE OF THE EIGHT DID NOTHING. `stall_ceiling`, `startup_budget_s` and
`reclaim_stale_hours` sat in `_KNOBS`, appeared on the settings page, and
were in neither `SANDBOX_ENV` nor `WORKER_ENV` -- so nothing delivered
them anywhere and their consumers went on reading their own module
constants. A person could set a number, watch it saved, and change
nothing. That is the repo's own canonical defect (`_OPERAND`,
`probe_wedged`, `reconcile_tick`, `scrub`) arriving in a settings table.

A FOURTH WAS DELIVERED AND IGNORED. `wait_slice_s` reached the pod as
`AGENTOS_WAIT_SLICE_S`, `jobs_client` read it for the HARNESS job path,
and `tools_jobs.WAIT_SLICE_S` -- the model's own `bash_wait` -- was a
frozen module constant that never looked.

AND ITS DECLARED PRODUCTION VALUE WOULD HAVE BROKEN EVERY WAIT. 120
seconds under a 45-second bridge read is not a longer wait, it is every
slice timing out as a dead bridge, followed by the worker's reachability
check destroying a pod that was only waiting. Exactly the shape of
`JOB_MAX_S > BASH_MAX_S`, which is asserted; this pair was not.

So: every knob is delivered, every knob is read, and the relations that
are not matters of taste are pinned.
"""

from __future__ import annotations

import pathlib

import pytest

from kernel import timings
from kernel.bridge import limits

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _delivered() -> dict:
    return {**timings.SANDBOX_ENV, **timings.WORKER_ENV}


# -- delivery ----------------------------------------------------------

def test_every_knob_is_delivered_somewhere():
    """In neither map is a number nothing can act on."""
    undelivered = [n for n in timings.knob_names() if n not in _delivered()]
    assert not undelivered, (
        f"{undelivered} are offered by the settings page and reach no "
        f"process; either wire them or take them out of _KNOBS")


def test_every_delivered_name_is_a_knob():
    """The other direction: an env name with no knob behind it is a
    variable nothing ever sets."""
    for name in _delivered():
        pass
    stray = [n for n in _delivered() if n not in timings.knob_names()]
    assert not stray, stray


def test_every_knob_has_a_reader_in_the_tree():
    """Delivered is not read. Each variable must appear somewhere that
    is not the table that declares it."""
    sources = [p for p in (ROOT / "kernel").rglob("*.py")
               if p.name != "timings.py"]
    haystack = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in sources)
    for knob, env in _delivered().items():
        assert env in haystack, (
            f"{knob} is delivered as {env} and nothing in kernel/ reads it")


# -- the two that were removed, and must not drift back ----------------

@pytest.mark.parametrize("gone", ("startup_budget_s", "reclaim_stale_hours"))
def test_the_conductor_knobs_stay_out(gone):
    """Both are facts about a POD -- how long it may take to be ready,
    how long its remains are kept -- and `story_timings_profile.sql`
    argues they are "destructive when they reach work that did not ask
    for them": 0.02 hours takes a failed run's workspace 72 seconds
    after it fails, and that is precisely the run whose workspace
    answers the question."""
    assert gone not in timings.knob_names()


# -- relations that are not matters of taste ---------------------------

def test_the_wait_slice_fits_inside_the_bridge_read():
    """`bridge_timeout("bash_wait")` has to out-wait the slice "or every
    wait comes back as a dead bridge"."""
    for profile in timings.PROFILES:
        slice_s = timings.values(profile)["wait_slice_s"]
        assert slice_s < limits.WAIT_BRIDGE_S, (
            f"{profile} declares a {slice_s}s slice under a "
            f"{limits.WAIT_BRIDGE_S}s bridge read: every wait times out "
            f"and the worker destroys a pod that was only waiting")


def test_the_sandbox_clamps_a_slice_it_is_given_anyway(monkeypatch):
    """The setting is the first guard and this is the second. A value
    that slips past the form must still not break the bridge."""
    import sys

    sys.path.insert(0, str(ROOT / "kernel" / "bridge"))
    tools_jobs = pytest.importorskip("tools_jobs")

    monkeypatch.setenv("AGENTOS_WAIT_SLICE_S", "3000")
    assert tools_jobs.wait_slice_s() < limits.WAIT_BRIDGE_S

    monkeypatch.setenv("AGENTOS_WAIT_SLICE_S", "not-a-number")
    assert tools_jobs.wait_slice_s() == tools_jobs.WAIT_SLICE_S

    monkeypatch.setenv("AGENTOS_WAIT_SLICE_S", "5")
    assert tools_jobs.wait_slice_s() == 5.0


def test_the_job_ceiling_stays_above_the_foreground_cap():
    """Below it, `bash` refuses a long call with "use bash_background"
    while bash_background has the shorter ceiling of the two."""
    for profile in timings.PROFILES:
        assert timings.values(profile)["job_max_s"] > limits.BASH_MAX_S


def test_the_form_refuses_a_slice_the_runtime_would_ignore():
    """A form that silently accepts a number the runtime clamps teaches
    people the setting does not work."""
    from app.services.timings_admin import CEILINGS, clean_overrides

    assert "wait_slice_s" in CEILINGS
    got, complaints = clean_overrides('{"wait_slice_s": 3000}')
    assert got["wait_slice_s"] < limits.WAIT_BRIDGE_S
    assert any("dead bridge" in c for c in complaints)


# -- the model's own wait, which was the one reading nothing -----------

def test_the_models_wait_is_resolved_at_call_time_not_at_import():
    """A module constant is bound when the module is imported, so
    rebinding it after reading a setting changes nothing -- which is
    exactly how this knob came to configure only half the system."""
    import inspect
    import sys

    sys.path.insert(0, str(ROOT / "kernel" / "bridge"))
    tools_jobs = pytest.importorskip("tools_jobs")

    src = inspect.getsource(tools_jobs.tool_bash_wait)
    assert "wait_slice_s()" in src, (
        "the model's bash_wait is back on the frozen constant")


def test_the_stall_ceiling_is_resolved_at_call_time(monkeypatch):
    from kernel.harness.stall import (DEFAULT_STALL_CEILING, check_stall,
                                      stall_ceiling)

    monkeypatch.delenv("AGENTOS_STALL_CEILING", raising=False)
    assert stall_ceiling() == DEFAULT_STALL_CEILING

    monkeypatch.setenv("AGENTOS_STALL_CEILING", "3")
    assert stall_ceiling() == 3

    steps = [{"tool": "bash_jobs", "args": {}} for _ in range(5)]
    assert check_stall(steps).fired, "the compressed ceiling never fires"

    monkeypatch.delenv("AGENTOS_STALL_CEILING")
    assert not check_stall(steps).fired


def test_the_stall_clock_cannot_be_switched_off(monkeypatch):
    """`stall.py`: the clock "may be raised and may not be removed"."""
    from kernel.harness.stall import stall_ceiling

    for hostile in ("0", "-1", "", "nonsense"):
        monkeypatch.setenv("AGENTOS_STALL_CEILING", hostile)
        assert stall_ceiling() >= 2, hostile
