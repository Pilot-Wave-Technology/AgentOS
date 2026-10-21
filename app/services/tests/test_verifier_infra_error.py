"""A run the substrate ended must never refuse a story.

Two seams, asserted end to end: `from_exit`, which owns the four-valued
vocabulary, and `run_job`, which is what actually talks to a cluster.

R1's rule with teeth is that `fail` is the ONLY refusing verdict, so
anything misfiled as `fail` permanently blocks a completion claim. A
preempted Spot node, an image that would not pull and a broken watch
loop were all filed there.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from kernel.drivers.k8s_run import run_job
from kernel.verifier.outcome import ERROR, FAIL, PASS, from_exit


# ── the vocabulary ───────────────────────────────────────────────────────

def test_an_infra_error_is_error_and_does_not_refuse():
    out = from_exit(None, "", "pytest -q", infra_error="the node was reclaimed")
    assert out.passes == ERROR
    assert out.refuses is False
    assert out.detail["reason"] == "the node was reclaimed"


def test_infra_outranks_a_declared_timeout():
    """THE REGRESSION THAT MATTERS. A pod stuck pulling an image also
    hits its deadline, and a declared budget exceeded is a `fail` — so
    asking the timeout question first refuses a story for a registry
    outage and tells the executor to make its suite faster."""
    out = from_exit(None, "", "pytest -q", timed_out=True,
                    timeout_s=7200, timeout_declared=True,
                    infra_error="the image could not be pulled")
    assert out.passes == ERROR, "a declared-timeout FAIL beat the infra error"


def test_a_declared_timeout_is_still_a_fail_without_an_infra_error():
    """The guard must not fire on the case budget.py exists for."""
    out = from_exit(1, "", "pytest -q", timed_out=True, timeout_s=7200,
                    timeout_declared=True)
    assert out.passes == FAIL


def test_a_missing_exit_code_alone_is_error_not_fail():
    """A driver that returns None has told us nothing. Inventing a
    failing code to fill the hole is exactly how the Kubernetes bug
    happened, so the vocabulary refuses to do it."""
    assert from_exit(None, "", "pytest -q").passes == ERROR


def test_the_ordinary_verdicts_are_untouched():
    assert from_exit(0, "", "pytest -q").passes == PASS
    assert from_exit(1, "", "pytest -q").passes == FAIL
    assert from_exit(127, "", "pytest -q").passes == ERROR


# ── the loop ─────────────────────────────────────────────────────────────

class _Batch:
    def __init__(self, status):
        self._status = status
        self.deleted = False

    def create_namespaced_job(self, **_):
        return None

    def read_namespaced_job_status(self, **_):
        return NS(status=self._status)

    def delete_namespaced_job(self, **_):
        self.deleted = True


class _Core:
    def __init__(self, pod):
        self._pod = pod

    def list_namespaced_pod(self, **_):
        return NS(items=[self._pod] if self._pod else [])

    def read_namespaced_pod_log(self, **_):
        return "collected 12 items"


def _pod(*, reason=None, conditions=None, terminated=None, waiting=None):
    state = NS(terminated=terminated, waiting=waiting, running=None)
    return NS(metadata=NS(name="crit-abc-x1"),
              status=NS(phase="Failed", reason=reason,
                        conditions=conditions or [],
                        container_statuses=[NS(name="criterion", state=state)]))


_MANIFEST = {"metadata": {"name": "crit-abc"}}


async def _run(batch, core):
    return await run_job(batch, core, _MANIFEST, namespace="n", timeout_s=60)


@pytest.mark.asyncio
async def test_a_preempted_job_reports_infra_and_no_exit_code():
    """The whole point. `exit_code` is None so a caller that forgets to
    read `infra_error` gets nothing usable, rather than a fabricated 1
    that reads as a failing check."""
    batch = _Batch(NS(succeeded=None, failed=1,
                      conditions=[NS(type="Failed",
                                     reason="BackoffLimitExceeded")]))
    core = _Core(_pod(conditions=[NS(type="DisruptionTarget",
                                     reason="PreemptionByScheduler")],
                      terminated=NS(exit_code=137, reason="Error")))
    result = await _run(batch, core)
    assert result["exit_code"] is None
    assert result["timed_out"] is False
    assert "PreemptionByScheduler" in result["infra_error"]
    assert batch.deleted, "the Job must still be removed in the finally"


@pytest.mark.asyncio
async def test_a_real_failing_criterion_still_fails():
    batch = _Batch(NS(succeeded=None, failed=1,
                      conditions=[NS(type="Failed",
                                     reason="BackoffLimitExceeded")]))
    core = _Core(_pod(terminated=NS(exit_code=1, reason="Error")))
    result = await _run(batch, core)
    assert result == {"exit_code": 1, "timed_out": False,
                      "infra_error": None, "output": "collected 12 items"}
    assert from_exit(result["exit_code"], result["output"], "pytest",
                     timed_out=result["timed_out"],
                     infra_error=result["infra_error"]).passes == FAIL


@pytest.mark.asyncio
async def test_a_deadline_kill_is_still_a_timeout():
    """The cluster enforcing activeDeadlineSeconds is a real statement
    about a declared budget, and must survive this change."""
    batch = _Batch(NS(succeeded=None, failed=1,
                      conditions=[NS(type="Failed",
                                     reason="DeadlineExceeded")]))
    core = _Core(_pod(terminated=NS(exit_code=143, reason="Error")))
    result = await _run(batch, core)
    assert result["timed_out"] is True
    assert result["infra_error"] is None


@pytest.mark.asyncio
async def test_a_pod_that_never_started_is_not_a_timeout():
    """It hits the deadline while it waits, so the cluster reports
    DeadlineExceeded — but nothing ran, and against a declared budget
    that reading is a `fail`."""
    batch = _Batch(NS(succeeded=None, failed=1,
                      conditions=[NS(type="Failed",
                                     reason="DeadlineExceeded")]))
    core = _Core(_pod(waiting=NS(reason="ImagePullBackOff", message="")))
    result = await _run(batch, core)
    assert result["timed_out"] is False
    assert result["infra_error"]
    assert from_exit(result["exit_code"], "", "pytest",
                     timed_out=result["timed_out"], timeout_s=7200,
                     timeout_declared=True,
                     infra_error=result["infra_error"]).passes == ERROR


@pytest.mark.asyncio
async def test_a_succeeding_job_is_unchanged():
    batch = _Batch(NS(succeeded=1, failed=None, conditions=[]))
    result = await _run(batch, _Core(_pod(terminated=NS(exit_code=0,
                                                        reason="Completed"))))
    assert result["exit_code"] == 0
    assert result["infra_error"] is None


# ── a driver that cannot work must say so at startup ─────────────────────

def test_a_driver_declares_whether_it_can_run_at_all():
    """THE HOUR OF LOGS THIS COST. The Kubernetes client is imported
    lazily — correctly, so E5's module-load check passes on an image with
    no cloud SDK — so nothing touches the import until the first API
    call. The conductor deployed to GKE, rolled out 2/2, logged
    `driver=KubernetesDriver`, and then failed every reconcile pass with
    ModuleNotFoundError once a minute while reporting Ready.

    `preflight` moves that to startup, where `kubectl rollout status`
    already fails the deploy.
    """
    from kernel.driver import Driver
    from kernel.drivers.docker import DockerDriver
    from kernel.drivers.kubernetes import KubernetesDriver

    # Every driver answers the question; the default answer is "fine".
    assert DockerDriver.preflight is Driver.preflight
    # The substrate whose client is optional overrides it.
    assert KubernetesDriver.preflight is not Driver.preflight


def test_the_worker_asks_before_it_loops():
    """A guard nothing calls is the failure mode this repository keeps
    finding, so assert the CALL and not just the method."""
    import pathlib

    # `run_forever` left `service.py` for `service_tick.py` (D2a).
    # The path has to move with it: a guard reading the file its
    # subject has left passes on no evidence at all.
    source = (pathlib.Path(__file__).resolve().parents[3] / "kernel"
              / "worker" / "service_tick.py").read_text(encoding="utf-8")
    assert "driver.preflight()" in source, (
        "run_forever must preflight the driver before entering the loop")
