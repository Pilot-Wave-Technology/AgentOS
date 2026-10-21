"""Compressed timings are one story's, and production is the fallback.

WHY THIS IS NOT A PLATFORM SWITCH. Two of the knobs are destructive when
they reach work that did not ask for them:

  reclaim_stale_hours 0.02   takes a FAILED execution's pod, volume and
                             workspace 72 s after it fails -- and
                             `reclaim` says exactly one FAILED execution
                             ever holds a pod, the leg-ceiling
                             non-convergence, "precisely the run whose
                             workspace answers the question"
  startup_budget_s 60        sits below the ~100 s a node takes to scale
                             up, so a concurrent story's pod is destroyed
                             while it is still scheduling

So the profile is scoped to one story, set by the platform, and STAMPED
on the execution at dispatch. The stamp is what makes an expiry safe: a
run carries the profile it started under, so a TTL lapsing mid-flight
cannot leave a job with a compressed budget and production reclaim
patience at the same time -- a state neither profile describes.

AND THE STAMP IS WHY THE SANDBOX WORKS. Three knobs are enforced inside
a container with no database and a closed env list. Resolved at dispatch
they are literals in the pod spec; resolved live they are unreachable.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

from app.services.tests.shipped_tree import (
    IS_DISTRIBUTION, create_table, shipped_grants)
from kernel import timings

_BRIDGE = str(pathlib.Path(__file__).resolve().parents[3] / "kernel" / "bridge")
if _BRIDGE not in sys.path:
    sys.path.insert(0, _BRIDGE)


# ── production is the default and the fallback ──────────────────────

def test_no_profile_is_production():
    assert timings.values(None) == timings.values(timings.PRODUCTION)


def test_an_unknown_profile_is_production():
    """A settings fault must not change behaviour at the moment it can
    least explain itself -- the direction the verification switch chose."""
    assert timings.values("turbo") == timings.values(timings.PRODUCTION)


def test_production_matches_what_is_true_today():
    live = timings.values(timings.PRODUCTION)
    assert live["job_default_s"] == 1800
    assert live["job_max_s"] == 7200
    assert live["job_output_max"] == 32 * 1024
    assert live["stall_ceiling"] == 8
    # `wait_slice_s` PRODUCTION IS 20, which is what the sandbox has
    # always used. 120 was declared once and would have broken every
    # wait: `bridge_timeout("bash_wait")` is 45 and "has to out-wait
    # that slice or every wait comes back as a dead bridge".
    assert live["wait_slice_s"] == 20
    assert live["poll_interval_max_s"] == 15


# ── the relations that compression could break ──────────────────────

def test_compressed_job_max_stays_above_the_foreground_ceiling():
    """`JOB_MAX_S > BASH_MAX_S` is asserted elsewhere, and below it the
    refusal becomes a lie: `bash_timeout` sends a long foreground call to
    `bash_background`, which would then have the SHORTER ceiling of the
    two."""
    import limits

    assert timings.values(timings.COMPRESSED)["job_max_s"] > limits.BASH_MAX_S


def test_the_conductor_knobs_are_not_offered():
    """They were, and they reached nothing: `promote.STARTUP_BUDGET_S`
    and `reclaim.STALE_HOURS` went on reading their own constants while
    a page invited a person to set them.

    Removed rather than wired. Both are facts about a POD, and this
    table's own migration argues they are "destructive when they reach
    work that did not ask for them" -- 0.02 hours takes a failed run's
    workspace 72 seconds after it fails, and that is precisely the run
    whose workspace answers the question.
    """
    assert "startup_budget_s" not in timings.knob_names()
    assert "reclaim_stale_hours" not in timings.knob_names()

def test_setup_budget_is_not_a_knob():
    """It is `job_default_s + 60` by construction and a test pins the
    equality. Two knobs for one number is how "the checks fail for a
    reason that is not the work" comes back."""
    assert "setup_budget_s" not in timings.knob_names()


def test_the_discredited_knobs_are_absent():
    """`leg_budget` was measured and written down -- a leg below the
    orientation floor does not make a run shorter, it makes it never
    finish. `wedged_probe_s` would configure a function with no caller."""
    for gone in ("leg_budget", "wedged_probe_s"):
        assert gone not in timings.knob_names()


# ── overrides are bounded, and never fatal ──────────────────────────

def test_an_override_applies_and_keeps_its_type():
    got = timings.values(timings.PRODUCTION, {"stall_ceiling": "4"})
    assert got["stall_ceiling"] == 4


def test_an_unknown_override_is_ignored_not_raised():
    """A typo must not take down the dispatch that reads it."""
    got = timings.values(timings.PRODUCTION, {"nonsense": 1})
    assert got == timings.values(timings.PRODUCTION)


def test_a_malformed_override_is_ignored_not_raised():
    got = timings.values(timings.PRODUCTION, {"stall_ceiling": "eight"})
    assert got["stall_ceiling"] == 8


# ── the sandbox's three, and only those ─────────────────────────────

def test_only_the_sandbox_knobs_travel_as_env():
    """The rest are read by processes that have a database. Sending them
    anyway would put numbers in the pod that nothing there reads."""
    env = timings.sandbox_env(timings.COMPRESSED)
    # `AGENTOS_WAIT_SLICE_S` IS HERE TOO, and that is the fix rather
    # than a leak: the model's `bash_wait` blocks IN THE SANDBOX, and
    # until it read this variable the knob configured only the harness
    # job path while the model's wait stayed on a frozen constant.
    assert set(env) == {"AGENTOS_WAIT_SLICE_S",
                        "AGENTOS_JOB_DEFAULT_S", "AGENTOS_JOB_MAX_S",
                        "AGENTOS_JOB_OUTPUT_MAX"}
    assert all(isinstance(v, str) for v in env.values())


def test_the_sandbox_reads_what_dispatch_writes(monkeypatch):
    """The round trip that matters: the names the spec sets are the names
    the sandbox looks up."""
    import limits
    import tools_jobs

    env = timings.sandbox_env(timings.COMPRESSED)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert limits.job_default_s() == 20
    assert limits.job_max_s() == 180
    assert tools_jobs._max_output() == 2048


def test_a_production_run_carries_no_timings_env():
    """The reader falls back to the same numbers when a variable is
    absent, so the common case ships nothing."""
    import limits

    assert limits.job_default_s() == limits.JOB_DEFAULT_S


# ── the stamp ───────────────────────────────────────────────────────

def test_the_stamp_is_written_once_at_dispatch():
    import inspect

    from kernel.conductor import sandbox_spec

    src = inspect.getsource(sandbox_spec.spec_for_dispatch)
    assert "timings_store.for_story" in src
    assert "timings_store.stamp" in src


def test_production_writes_no_stamp():
    """NULL already means production, so every execution that predates
    this and every dispatch by code that has not learned about it reads
    correctly without a backfill."""
    import inspect

    from kernel import timings_store

    src = inspect.getsource(timings_store.stamp)
    assert "if profile == timings.PRODUCTION:" in src
    assert "return" in src


def test_the_pod_is_not_granted_the_table():
    """`agentos_leg` reads its own `executions` row, which carries the
    stamp. A second source it could consult would be a second answer."""
    if IS_DISTRIBUTION:
        grants = shipped_grants()
        assert re.search(r"GRANT [^;]*\bON kernel\.story_timings TO agentos_worker\b",
                         grants)
        assert not re.search(r"GRANT [^;]*\bON kernel\.story_timings TO agentos_leg\b",
                             grants)
        return
    sql = pathlib.Path("migrations/story_timings_profile.sql").read_text(
        encoding="utf-8")
    assert "TO agentos_worker" in sql
    assert "GRANT SELECT ON kernel.story_timings TO agentos_leg" not in sql


def test_expiry_is_required():
    if IS_DISTRIBUTION:
        assert re.search(r"^\s+expires_at timestamp with time zone NOT NULL",
                         create_table("kernel.story_timings"), re.M)
        return
    sql = pathlib.Path("migrations/story_timings_profile.sql").read_text(
        encoding="utf-8")
    assert "expires_at  TIMESTAMPTZ NOT NULL" in sql


def test_a_compressed_run_says_so():
    """The lesson inverted: a feature's absence once looked identical to
    it having nothing to do, so compressed timings must never look like
    production."""
    assert timings.label(timings.PRODUCTION) == ""
    assert "COMPRESSED" in timings.label(timings.COMPRESSED)


# ── the worker's two ────────────────────────────────────────────────

def test_the_worker_knobs_travel_as_env():
    env = timings.worker_env(timings.COMPRESSED)
    assert set(env) == {"AGENTOS_WAIT_SLICE_S", "AGENTOS_POLL_INTERVAL_MAX_S",
                        "AGENTOS_STALL_CEILING"}


def test_the_poll_ceiling_is_resolved_at_call_time_not_at_import(monkeypatch):
    """`run_job`'s `wait_s` was a DEFAULT ARGUMENT, bound when the
    function was defined. Rebinding the module constant after reading a
    setting would have changed nothing -- the knob would have looked
    wired and done nothing, which is the defect this repository already
    names after `probe_wedged`."""
    from kernel.bridge import jobs_client

    assert jobs_client.poll_interval_max_s() == jobs_client.MAX_INTERVAL_S
    monkeypatch.setenv("AGENTOS_POLL_INTERVAL_MAX_S", "2")
    monkeypatch.setenv("AGENTOS_WAIT_SLICE_S", "10")
    assert jobs_client.poll_interval_max_s() == 2.0
    assert jobs_client.wait_slice_s() == 10.0


def test_run_job_takes_none_rather_than_a_frozen_default():
    import inspect

    from kernel.bridge import jobs_client

    sig = inspect.signature(jobs_client.run_job)
    assert sig.parameters["wait_s"].default is None


def test_a_two_hour_job_costs_a_few_hundred_internal_polls():
    """The number that matters for "the model does not burn tokens": the
    internal re-arm is not a model step, but it is still a request, and
    1,440 of them was the five-second ceiling's cost."""
    from kernel.bridge import jobs_client

    two_hours = 2 * 60 * 60
    assert two_hours / jobs_client.MAX_INTERVAL_S < 600


def test_a_short_job_still_answers_quickly():
    """The eager start is what keeps a three-second `npm ci` from paying
    the ceiling."""
    from kernel.bridge import jobs_client

    assert jobs_client.FIRST_INTERVAL_S <= 1.0
