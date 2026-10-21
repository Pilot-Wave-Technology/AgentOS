"""A criterion's verdict IS its exit code. No exit code, no verdict.

These assert the rule that k8s_outcome.py exists to enforce, against the
pod shapes Kubernetes actually produces. A pod is data, so none of this
needs a cluster — the same reason k8s_spec.py is a separate module from
the driver.

THE BUG THESE COVER. run_job used to report every failed Job as
`exit_code=1`, and `from_exit` reads a non-zero exit as `fail`: the work
is wrong. So a Spot preemption refused a story, and so did an image that
would not pull. Both are `error` now, which R1 makes non-refusing.

AND `error` IS NOT ONE THING. Plan 5.8 row 3 -- the CLUSTER ended the
run -- must cost a re-run and nothing else, while row 2 spends strikes
and reaches a person. The difference used to live in the wording of the
sentence; it is a `kind` now, and this file is where each branch's kind
is pinned, because this is the only module that can know it.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from kernel.drivers.infra_error import (DISRUPTED, NEVER_STARTED, OOM,
                                        UNKNOWN, UNOBSERVED, kind_of)
from kernel.drivers.k8s_outcome import classify


def _pod(*, phase="Failed", reason=None, conditions=None, state=None,
         name="criterion"):
    statuses = None if state is None else [NS(name=name, state=state)]
    return NS(status=NS(phase=phase, reason=reason,
                        conditions=conditions or [],
                        container_statuses=statuses))


def _terminated(exit_code, reason="Error"):
    return NS(terminated=NS(exit_code=exit_code, reason=reason),
              waiting=None, running=None)


def _waiting(reason, message=""):
    return NS(terminated=None, running=None,
              waiting=NS(reason=reason, message=message))


# ── the substrate ended it, and that is not a verdict ────────────────────

def test_a_preempted_pod_is_not_a_failing_check():
    """THE BUG. A Spot reclaim comes back as a failed Job with a non-zero
    container, and `fail` is the only refusing verdict there is. A story
    would be refused because Google took a node back."""
    pod = _pod(conditions=[NS(type="DisruptionTarget",
                              reason="TerminationByKubelet")],
               state=_terminated(137))
    out = classify(pod)
    assert out["exit_code"] is None
    assert "TerminationByKubelet" in out["infra_error"]
    assert kind_of(out["infra_error"]) == DISRUPTED


def test_the_disruption_condition_is_checked_by_type_not_only_reason():
    """The reason strings have moved between releases; the condition
    TYPE has not. An unrecognised reason must still be a disruption."""
    pod = _pod(conditions=[NS(type="DisruptionTarget", reason="SomethingNew")],
               state=_terminated(1))
    assert kind_of(classify(pod)["infra_error"]) == DISRUPTED


def test_the_older_spot_shape_is_caught_too():
    """GKE versions predating the condition mark the POD `Terminated`."""
    older = classify(_pod(reason="Terminated", state=_terminated(143)))
    assert kind_of(older["infra_error"]) == DISRUPTED
    evicted = classify(_pod(reason="Evicted", state=_terminated(1)))
    assert kind_of(evicted["infra_error"]) == DISRUPTED


# ── the container really did run and really did answer ───────────────────

def test_an_ordinary_failing_criterion_keeps_its_exit_code():
    """The guard must not fire on the case it exists to protect. A check
    that ran and said no is a `fail`, and turning that into an `error`
    would make the verifier unable to refuse anything."""
    out = classify(_pod(state=_terminated(1)))
    assert out == {"exit_code": 1, "infra_error": None}


def test_the_real_exit_code_survives_so_127_can_still_mean_error():
    """THE SECOND BUG IN THE SAME SEVEN LINES. run_job hardcoded
    `exit_code = 1`, so `from_exit`'s 127 branch could never fire here:
    a criterion in an image with no pytest was recorded as `fail`. That
    is the exact inversion G0 found on the Docker path, reintroduced on
    this one."""
    assert classify(_pod(state=_terminated(127)))["exit_code"] == 127
    assert classify(_pod(state=_terminated(0)))["exit_code"] == 0


# ── it never got to the starting line ────────────────────────────────────

def test_an_image_that_will_not_pull_checked_nothing():
    """And it hits the deadline while it waits, so without this it would
    be reported as a timeout — telling the executor its suite is slow
    when the registry was down."""
    out = classify(_pod(state=_waiting("ImagePullBackOff", "back-off pulling")))
    assert out["exit_code"] is None
    assert "never started" in out["infra_error"]
    assert kind_of(out["infra_error"]) == NEVER_STARTED


def test_a_pod_with_no_container_status_ran_nothing():
    """Rejected by quota, unschedulable, or killed before the kubelet
    reported. There is no exit code to read and none may be invented."""
    out = classify(_pod(phase="Pending"))
    assert out["exit_code"] is None
    assert "Pending" in out["infra_error"]
    # NEVER_STARTED and not DISRUPTED: unschedulable looks like a
    # cluster that took the pod away, but nothing here saw it take
    # anything, and row 3 buys re-runs for ever if guessed at.
    assert kind_of(out["infra_error"]) == NEVER_STARTED


def test_an_exit_code_the_node_did_not_observe_is_not_a_result():
    """`ContainerStatusUnknown` carries an exitCode field that is a
    placeholder. Reading it would be reading a made-up number."""
    out = classify(_pod(state=_terminated(137, "ContainerStatusUnknown")))
    assert out["exit_code"] is None
    assert kind_of(out["infra_error"]) == UNOBSERVED


def test_oom_against_a_ceiling_nobody_declared_is_not_a_verdict():
    """kernel/verifier/kinds.py hardcodes 1024MiB — no contract chose it.
    Blowing an undeclared budget is the same shape as blowing the default
    timeout, and gets the same answer."""
    out = classify(_pod(state=_terminated(137, "OOMKilled")))
    assert out["exit_code"] is None
    assert "nobody declared" in out["infra_error"]
    # ROW 2, AND KEPT APART FROM THE REST OF IT. Re-running an OOM
    # unchanged buys the same death again, and 9.6's sizing is the only
    # thing that answers it -- so it must be askable on its own.
    assert kind_of(out["infra_error"]) == OOM


# ── the default, which is the part that matters longest ──────────────────

def test_an_unclassifiable_ending_is_infra_and_not_a_failing_check():
    """There will be Kubernetes failure modes nobody here anticipated.
    The default must cost a re-run, not condemn a story: wrongly
    refusing is worse than wrongly not refusing."""
    out = classify(_pod(state=NS(terminated=None, waiting=None,
                                 running=NS(started_at="now"))))
    assert out["exit_code"] is None
    assert out["infra_error"]
    # UNKNOWN IS ROW 2, which is the safe default in the same direction
    # everything else here points: three strikes and a person, rather
    # than a re-run loop nobody is watching.
    assert kind_of(out["infra_error"]) == UNKNOWN


def test_a_criterion_that_answered_carries_no_kind_at_all():
    """The field exists to tell two NOT-verdicts apart. A run that
    produced an exit code produced a verdict, and there is nothing for
    an infra kind to be about."""
    assert classify(_pod(state=_terminated(1)))["infra_error"] is None


def test_the_module_needs_no_kubernetes_sdk():
    """E0's image has none and E5's module-load imports everything under
    kernel/. Every field is read through getattr for that reason."""
    import kernel.drivers.k8s_outcome as m

    assert "client" not in vars(m)
