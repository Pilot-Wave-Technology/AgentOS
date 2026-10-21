"""The conductor reaped its own verifier. Measured on execution 208.

A capture Job was seven seconds into `pip install` when reconcile ran:

    reconcile: 1 goal(s), 1 reap, 1 release
    Killing  Stopping container work
    Killing  Stopping container capture

and the verifier then reported

    the Job failed and its pod is gone, so there is no record of what
    the criterion did

-- which was true, and the conductor had done it.

THE CHAIN. `capture_manifest` stamps `managed-by: kernel`, correctly:
the pod IS ours. So `list_containers()` returns it. It stamps
`execution-id` only when the spec carries one, and NEITHER
`prepare._prepare_mount` NOR `environment.build_deps` sets it. So
`orphan_sweep` saw a kernel-owned pod with no execution, concluded "no
matching execution", and destroyed it with `stop_timeout=0`.

WHY IT READ AS FLAKE. A capture runs about 28 seconds and reconcile
lands every 15 ticks, so it only bites on overlap. Three of four
captures survived; the fourth looked like an infrastructure hiccup, and
`error` retries papered over it until the retries ran out.

THE FIX IS THE LABEL, NOT THE ID. Stamping the execution id would
improve provenance and is worth doing, but it is not the invariant. The
invariant is that a pod with its OWN CONTROLLER has its own lifecycle:
the Job decides when it dies, `run_job` deletes it in a `finally`, and
`ttlSecondsAfterFinished` is the backstop. A second reaper racing that
controller can only ever be wrong. `jobs_sweep.py` already handles Jobs
whose execution has finished, which is the real gap this sweep was
reaching for.

AND THE RULE OUTLIVED THE INSTANCE. The guard now comes from
`kernel/ownership.py`, where every reaper reads it -- see
test_every_sweep_knows_what_it_owns.py for the other four. The cases
below are the ones specific to THIS sweep: what it owns (a bare pod no
execution claims), what it must never touch (anything with a
controller), and what it does with a pod it cannot identify.
"""
from __future__ import annotations

import asyncio

from kernel.conductor.orphans import EPHEMERAL_LABEL, orphan_sweep
from kernel.ownership import EPHEMERAL_POD_LABEL
from kernel.verifier import pod as verify_pod


class _Driver:
    # `CLAIM_JOB_MODEL`, WHICH THE REAL DRIVERS DECLARE. This fake used
    # to carry `name = "kubernetes"` -- an attribute NO driver in the
    # tree defines -- and it was the only thing in the system that made
    # `getattr(driver, "name", "") != "kubernetes"` false. So these
    # tests proved the sweep listed, triaged and deleted correctly while
    # the sweep returned [] on every real tick. A fake that invents the
    # attribute a guard needs turns that guard into a test fixture.
    CLAIM_JOB_MODEL = "job"
    name = "kubernetes"

    def __init__(self, pods):
        self._pods = pods
        self.destroyed = []

    async def list_containers(self):
        return self._pods

    async def destroy(self, cid, stop_timeout=0):
        self.destroyed.append(cid)


class _Conn:
    """No execution matches anything -- the worst case for the sweep."""

    def __init__(self, live=None):
        self._live = live or {}
        self.executed = []

    async def fetchval(self, _sql, eid):
        return self._live.get(eid)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def _pod(cid, labels):
    return {"id": cid, "labels": labels, "status": "Running"}


CAPTURE = _pod("cap-e15f0a9f02b0-2p8mm",
               {"app": "agentos-capture", "managed-by": "kernel",
                EPHEMERAL_LABEL: "1", "agentos.io/egress": "capture"})


# ── the regression ─────────────────────────────────────────────────────

def test_a_live_capture_job_is_not_destroyed():
    """THE ONE THAT WOULD HAVE CAUGHT IT. The capture pod carries no
    execution-id, which is exactly the case the sweep treated as an
    orphan."""
    driver = _Driver([CAPTURE])
    asyncio.run(orphan_sweep(_Conn(), driver, keep=set()))
    assert driver.destroyed == [], (
        "the sweep destroyed a Job-owned pod; that Job was mid-install")


def test_it_is_not_even_reported_as_an_action():
    """A dry run that CLAIMS it would reap these is the same mistake
    waiting to be re-enabled by somebody reading the counts."""
    driver = _Driver([CAPTURE])
    actions = asyncio.run(
        orphan_sweep(_Conn(), driver, keep=set(), dry_run=True))
    assert actions == []


def test_the_local_name_is_the_shared_rule():
    """One spelling. A second copy of this label is how the same bug
    arrives in the reaper Phase D adds."""
    assert EPHEMERAL_LABEL == EPHEMERAL_POD_LABEL


def test_a_pod_kubernetes_itself_labelled_for_a_job_is_left_alone():
    """THE ONE THAT PROTECTS THE POD KIND NOBODY HAS WRITTEN YET. `job-name`
    is stamped by Kubernetes on every Job's pod whatever the manifest
    author remembered -- and forgetting a label is precisely how
    execution 208's capture was destroyed."""
    driver = _Driver([_pod("phase-d-something", {"managed-by": "kernel",
                                                 "job-name": "phase-d"})])
    asyncio.run(orphan_sweep(_Conn(), driver, keep=set()))
    assert driver.destroyed == []


def test_a_pod_that_says_it_is_somebody_elses_is_left_alone():
    driver = _Driver([_pod("not-ours", {"managed-by": "some-operator"})])
    asyncio.run(orphan_sweep(_Conn(), driver, keep=set()))
    assert driver.destroyed == []


def test_an_unreadable_execution_id_is_kept_and_does_not_raise():
    """`int(eid_label)` used to run here. A pod whose label is not digits
    would have raised out of the loop and stopped every later reap in the
    same tick -- and the sweep's whole reason for existing is the reaps
    it gets through."""
    driver = _Driver([_pod("odd-label", {"managed-by": "kernel",
                                         "execution-id": "e208"}),
                      _pod("exec-311", {"managed-by": "kernel"})])
    asyncio.run(orphan_sweep(_Conn(), driver, keep=set()))
    assert driver.destroyed == ["exec-311"], (
        "an unidentifiable pod must be KEPT, and must not take the pods "
        "behind it out of the pass")


def test_the_guard_is_the_label_and_not_the_name():
    """`cap-` is a naming convention; the label is a contract. A sweep
    keyed on the prefix would miss the next Job kind somebody adds."""
    odd = _pod("something-else-xyz", {"managed-by": "kernel",
                                      EPHEMERAL_LABEL: "1"})
    driver = _Driver([odd])
    asyncio.run(orphan_sweep(_Conn(), driver, keep=set()))
    assert driver.destroyed == []


# ── and it must still do its actual job ────────────────────────────────

def test_an_execution_pod_with_no_execution_is_still_reaped():
    """The sweep exists for a real fault -- twelve containers once
    accumulated holding bridge ports until an unrelated gate failed with
    'no free port in 9200-9399'. Fixing today's bug must not disable
    it."""
    driver = _Driver([_pod("exec-999", {"managed-by": "kernel"})])
    asyncio.run(orphan_sweep(_Conn(), driver, keep=set()))
    assert driver.destroyed == ["exec-999"]


def test_a_terminal_executions_pod_is_still_reaped():
    driver = _Driver([_pod("exec-207", {"managed-by": "kernel",
                                        "execution-id": "207"})])
    asyncio.run(orphan_sweep(_Conn({207: False}), driver, keep=set()))
    assert driver.destroyed == ["exec-207"]


def test_a_live_executions_pod_is_left_alone():
    driver = _Driver([_pod("exec-208", {"managed-by": "kernel",
                                        "execution-id": "208"})])
    asyncio.run(orphan_sweep(_Conn({208: True}), driver, keep=set()))
    assert driver.destroyed == []


def test_the_keep_set_still_wins():
    driver = _Driver([_pod("exec-210", {"managed-by": "kernel"})])
    asyncio.run(orphan_sweep(_Conn(), driver, keep={"exec-210"}))
    assert driver.destroyed == []


# ── Phase E's verification pod: owned deliberately, not by luck ────────
#
# It is a BARE POD on purpose -- a Job runs to completion and a warm pod
# by definition does not -- so `pod_owner` classifies it KERNEL and this
# sweep was already collecting it on terminal before anybody decided it
# should. Plan 5.7 gives it to `reclaim.py`, whose authority is the
# execution ROW. This stays the BACKSTOP, which is the half that must
# not be weakened: `reclaim` reaches it through one more API listing
# than this does, and a listing that fails must not mean a tier-sized
# pod lives for ever.

def _verify_pod(eid):
    return _pod(verify_pod.pod_name(eid), verify_pod.labels_for(eid))


def test_a_live_verifications_pod_is_left_alone():
    """Warm across generations is the design. A refusal re-seeds only
    the tree, and the `.venv` is the expensive half."""
    driver = _Driver([_verify_pod(208)])
    asyncio.run(orphan_sweep(_Conn({208: True}), driver, keep=set()))
    assert driver.destroyed == []


def test_a_terminal_executions_verification_pod_is_still_reaped():
    """THE BACKSTOP, AND IT MUST KEEP WORKING. Nothing else collects a
    controllerless pod if the reclaim pass cannot list its claim."""
    driver = _Driver([_verify_pod(207)])
    asyncio.run(orphan_sweep(_Conn({207: False}), driver, keep=set()))
    assert driver.destroyed == ["verify-207"]


def test_the_reap_says_which_kind_of_pod_it_took():
    """Plan 13: a reap logs what it took and why. "container verify-207
    destroyed" reads as the executor's sandbox, and the whole cost of
    2026-09-17 was evidence nobody could reach."""
    driver = _Driver([_verify_pod(207)])
    actions = asyncio.run(
        orphan_sweep(_Conn({207: False}), driver, keep=set()))
    assert "verification pod" in actions[0]["detail"]


def test_reaping_a_verification_pod_does_not_blank_the_sandbox_reference():
    """`container_id` IS THE WORKER'S ALONE. An execution now owns two
    kernel pods, and nulling that column on the verification pod's
    account would tell every other loop the sandbox had gone while it
    was still running -- the front-door version of execution 208's bug,
    arriving through a reaper that writes a row about a different
    object."""
    conn = _Conn({207: False})
    asyncio.run(orphan_sweep(conn, _Driver([_verify_pod(207)]), keep=set()))
    assert conn.executed, "the row should still be guarded, not skipped"
    sql, args = conn.executed[0]
    assert "container_id = $2" in sql and args == (207, "verify-207")
