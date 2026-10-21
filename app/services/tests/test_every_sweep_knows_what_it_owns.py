"""Every reaper states what it owns, and the statement is one statement.

The conductor destroyed its own verifier on execution 208 -- a capture
Job seven seconds into a `pip install` -- because `orphan_sweep` read
`managed-by: kernel` with no `execution-id` and concluded "no matching
execution". A one-line guard fixed that instance. THIS FILE IS ABOUT THE
CLASS, because Phases D and E each add a pod kind and the fifth reaper
would have been written by copying the fourth.

`kernel/ownership.py` states the rule once: a pod with its OWN
CONTROLLER has its own lifecycle and is never a reaper's business, and
anything a reaper cannot identify is KEPT. Each reaper below is checked
twice -- that it leaves a Job-owned thing alone, and that it still
collects what it exists for, because the sweeps are not decoration:
twelve containers once held bridge ports until an unrelated gate failed
with "no free port in 9200-9399", and the claim quota is a hard ceiling
on how many stories can ever run.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace as NS

import pytest

from kernel import ownership
from kernel.conductor import ephemeral, jobs_sweep, reclaim
from kernel.ownership import (EPHEMERAL_CLAIM_SELECTOR, EPHEMERAL_POD_LABEL,
                              KERNEL_SELECTOR, Owner)
from kernel.verifier import pod as verify_pod

NOW = datetime.datetime.now(datetime.timezone.utc)


# ── the predicate itself ───────────────────────────────────────────────

def test_a_job_made_pod_is_protected_by_kubernetes_own_label():
    """THE PROTECTION FOR THE POD KIND NOBODY HAS WRITTEN YET. Execution
    208 happened because a manifest did not carry a label a sweep wanted.
    Kubernetes stamps `job-name` itself, whatever the author remembered,
    so Phase D's Job is safe before anybody reads the rule."""
    for label in ("job-name", "batch.kubernetes.io/job-name",
                  "controller-uid"):
        labels = {"managed-by": "kernel", label: "whatever"}
        assert ownership.pod_owner(labels) is Owner.CONTROLLER, label
        assert not ownership.may_reap_pod(labels)


def test_our_own_ephemeral_label_says_the_same_thing():
    assert ownership.pod_owner(
        {"managed-by": "kernel", EPHEMERAL_POD_LABEL: "1"}) is Owner.CONTROLLER


def test_a_pod_that_names_another_owner_is_foreign():
    assert ownership.pod_owner({"managed-by": "somebody-else"}) is Owner.FOREIGN


def test_a_missing_managed_by_is_not_foreignness():
    """ABSENCE IS NOT EVIDENCE. Both listers already filter on
    `managed-by=kernel`, and the stray containers the orphan sweep exists
    to collect arrive with their labels gone. Reading "unlabelled" as
    "somebody else's" would disable the sweep while looking careful."""
    assert ownership.pod_owner({}) is Owner.KERNEL
    assert ownership.may_reap_pod({})


def test_an_unreadable_owner_parses_to_none_and_never_raises():
    """`int()` raises, and in `orphan_sweep` that exception escaped the
    loop: one mislabelled pod would have stopped every later reap in the
    same tick."""
    assert ownership.parse_execution_id("not-a-number") is None
    assert ownership.parse_execution_id(None) is None
    assert ownership.execution_of({"execution-id": "207"}) == 207


def test_the_job_rule_is_deliberately_not_the_pod_rule():
    """A Job wears `agentos.ephemeral` too, and there it means
    "throwaway", not "somebody else owns me". Copying the pod guard onto
    Jobs would skip every Job there is -- silently, because a sweep that
    collects nothing looks exactly like a clean cluster."""
    job = {"managed-by": "kernel", EPHEMERAL_POD_LABEL: "1"}
    assert ownership.may_reap_job(job)
    assert not ownership.may_reap_pod(job)


def test_a_claim_name_that_does_not_parse_belongs_to_nobody():
    """`podcheck` writes `ws-podcheck-<ts>` through the same manifest
    builder and wears the same label; an operator's claim wears whatever
    they typed. Reaping on the label alone would take both."""
    assert ownership.workspace_execution("ws-exec-42") == 42
    assert ownership.workspace_execution("ws-podcheck-1726") is None
    assert ownership.workspace_execution("my-debugging-claim") is None


def test_the_selector_has_one_spelling():
    """Four literals of `managed-by=kernel` decided what four reapers
    could see at all. A fifth lister with a typo would list nothing and
    report success."""
    assert jobs_sweep.SELECTOR == KERNEL_SELECTOR == "managed-by=kernel"


# ── jobs_sweep: it owns JOBS, which is a different question ────────────

class _Jobs:
    def __init__(self, jobs):
        self._jobs = jobs
        self.deleted = []
        self.selector = None

    def list_namespaced_job(self, namespace=None, label_selector=None, **_k):
        self.selector = label_selector
        return NS(items=self._jobs)

    def delete_namespaced_job(self, name=None, **_k):
        self.deleted.append(name)


class _JobConn:
    def __init__(self, live):
        self._live = live

    async def fetchval(self, _sql, eid):
        return self._live.get(eid)


def _job(name, labels):
    return NS(metadata=NS(name=name, labels=labels))


@pytest.fixture()
def cluster(monkeypatch):
    """The two seams `sweep_jobs` and `sweep_ephemeral` reach through."""
    async def _quiet(fn, *, what=None, budget=0.0, **kwargs):
        return fn(**kwargs)

    monkeypatch.setattr("kernel.drivers.k8s_call.quiet", _quiet)

    def _install(batch=None, core=None):
        monkeypatch.setattr("kernel.drivers.kubernetes._api",
                            lambda: (NS(BatchV1Api=lambda: batch,
                                        CoreV1Api=lambda: core), None))
        # `CLAIM_JOB_MODEL` is what the real drivers declare;
        # `name` is not an attribute any of them has, and a fake
        # that supplies it is the only reason these sweeps ever
        # looked alive.
        return NS(name="kubernetes", namespace="n",
                  CLAIM_JOB_MODEL="job")
    return _install


@pytest.mark.asyncio
async def test_a_job_whose_execution_is_still_running_is_left_alone(cluster):
    batch = _Jobs([_job("verify-208", {"managed-by": "kernel",
                                       EPHEMERAL_POD_LABEL: "1",
                                       "execution-id": "208"})])
    await jobs_sweep.sweep_jobs(_JobConn({208: True}), cluster(batch=batch))
    assert batch.deleted == []


@pytest.mark.asyncio
async def test_a_job_naming_no_execution_is_left_alone(cluster):
    """Somebody's hand-made debugging Job, until proven otherwise."""
    batch = _Jobs([_job("by-hand", {"managed-by": "kernel"}),
                   _job("gibberish", {"managed-by": "kernel",
                                      "execution-id": "not-a-number"})])
    await jobs_sweep.sweep_jobs(_JobConn({}), cluster(batch=batch))
    assert batch.deleted == []


@pytest.mark.asyncio
async def test_a_finished_executions_job_is_still_deleted(cluster):
    """AND IT MUST BE. `run_job`'s `finally` covers the ordinary path
    only: a cancelled execution leaves its criteria running against a
    tree whose verdict nobody will read, billing for it, and holding a
    slot against `count/jobs.batch: 150`."""
    batch = _Jobs([_job("verify-207", {"managed-by": "kernel",
                                       EPHEMERAL_POD_LABEL: "1",
                                       "execution-id": "207"})])
    await jobs_sweep.sweep_jobs(_JobConn({207: False}), cluster(batch=batch))
    assert batch.deleted == ["verify-207"]
    assert batch.selector == KERNEL_SELECTOR


# ── ephemeral: it owns LABELLED THROWAWAY CLAIMS, by age ───────────────

class _Claims:
    def __init__(self, claims):
        self._claims = claims
        self.deleted = []
        self.selector = None

    def list_namespaced_persistent_volume_claim(self, namespace=None,
                                                label_selector=None, **_k):
        self.selector = label_selector
        return NS(items=self._claims)

    def delete_namespaced_persistent_volume_claim(self, name=None, **_k):
        self.deleted.append(name)


def _claim(name, hours):
    made = None if hours is None else NOW - datetime.timedelta(hours=hours)
    return NS(metadata=NS(name=name, creation_timestamp=made))


@pytest.mark.asyncio
async def test_the_workspace_claims_are_not_even_listed(cluster):
    """WHAT IT MUST NEVER TOUCH, enforced at the query. An execution's
    `ws-exec-<id>` claim wears `managed-by` and NOT the throwaway label,
    so it is outside this reaper's selector -- releasing it is
    reclaim.py's, and only once the story is finished with it."""
    core = _Claims([])
    await ephemeral.sweep_ephemeral(None, cluster(core=core))
    assert core.selector == EPHEMERAL_CLAIM_SELECTOR != KERNEL_SELECTOR


@pytest.mark.asyncio
async def test_a_claim_with_no_age_is_left_alone(cluster):
    core = _Claims([_claim("ws-podcheck-1726", None)])
    await ephemeral.sweep_ephemeral(None, cluster(core=core))
    assert core.deleted == []


@pytest.mark.asyncio
async def test_a_young_claim_is_left_alone_and_an_old_one_is_collected(
        cluster):
    """Measured 2026-09-17: a `ws-podcheck` claim Pending for nineteen
    hours against a 120-claim quota that a claim never gives back."""
    core = _Claims([_claim("ws-podcheck-fresh", 0.5),
                    _claim("ws-podcheck-1726", 19)])
    await ephemeral.sweep_ephemeral(None, cluster(core=core))
    assert core.deleted == ["ws-podcheck-1726"]


# ── reclaim: its authority is the ROW, not a label ─────────────────────

class _ReclaimDriver:
    def __init__(self, held):
        self.held = held
        self.calls = []

    async def list_workspaces(self):
        return list(self.held)

    async def destroy(self, cid, stop_timeout=0):
        self.calls.append(("destroy", cid))

    async def release_workspace(self, eid):
        self.calls.append(("release", eid))
        return True


class _ReclaimConn:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    async def fetch(self, sql, *args):
        if "FROM executions e" in sql:
            return self._rows
        return [{"id": r["id"]} for r in self._rows]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytest.mark.asyncio
async def test_reclaim_can_only_reach_what_an_execution_named():
    """A capture Job's pod is never written into `container_id`, so this
    reaper cannot reach one even by accident -- and `list_workspaces`
    offers it only claims whose NAME parses. Both halves are ownership,
    not filtering: the row and the name each say whose it is."""
    driver = _ReclaimDriver(held=[])
    assert await reclaim.releasable(_ReclaimConn([]), driver) == []
    assert driver.calls == []


@pytest.mark.asyncio
async def test_a_finished_storys_pod_and_disk_are_still_reclaimed():
    """The ceiling is hard: forty claims EVER, after which nothing runs
    a story at all. The pod goes first -- a claim deleted underneath a
    live writer is I/O errors inside a container nobody is watching."""
    row = {"id": 7, "container_id": "exec-7", "why": "story done"}
    driver = _ReclaimDriver(held=[7])
    await reclaim.release(_ReclaimConn([row]), driver, row, dry_run=False)
    assert driver.calls == [("destroy", "exec-7"), ("release", 7)]


# ── reclaim, second pair: the verification pod and its claim ───────────
#
# Plan 5.7. `verify-ws-<id>` is deliberately outside every other sweep's
# reach -- `list_workspaces` will not parse the name and `ephemeral`'s
# selector does not match the labels -- which is right, and meant that
# until E10 NOTHING collected it. The quota is 120 claims and every
# execution now holds two, so a reaper that is merely careful here is a
# reaper that closes the cluster.

class _VerifyDriver:
    # `CLAIM_JOB_MODEL`, WHICH THE REAL DRIVERS DECLARE. This fake used
    # to carry `name = "kubernetes"` -- an attribute NO driver in the
    # tree defines -- and it was the only thing in the system that made
    # `getattr(driver, "name", "") != "kubernetes"` false. So these
    # tests proved the sweep listed, triaged and deleted correctly while
    # the sweep returned [] on every real tick. A fake that invents the
    # attribute a guard needs turns that guard into a test fixture.
    CLAIM_JOB_MODEL = "job"
    name = "kubernetes"
    namespace = "n"

    def __init__(self, pods=(), claims=()):
        self._pods = list(pods)
        self._claims = list(claims)

    async def list_containers(self):
        return self._pods

    def _core_api(self):
        outer = self

        class _Core:
            @staticmethod
            def list_namespaced_persistent_volume_claim(**_k):
                return NS(items=[NS(metadata=NS(name=n))
                                 for n in outer._claims])
        return _Core


class _VerifyConn:
    """One row per execution, or none at all -- which is its own case."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, _sql, held, _hours=None):
        return [r for r in self._rows if r["id"] in held]


def _vpod(eid, labels=None):
    return {"id": verify_pod.pod_name(eid), "status": "Running",
            "labels": verify_pod.labels_for(eid) if labels is None
            else labels}


def _row(eid, *, live=True, idle=False, state="RUNNING"):
    return {"id": eid, "state": state, "live": live, "idle": idle}


@pytest.fixture()
def reaped(monkeypatch):
    """What `reap_verification` actually handed the substrate."""
    calls = []

    async def _destroy(_driver, name, grace=15):
        calls.append(("pod", name))

    async def _release(_driver, eid):
        calls.append(("claim", eid))
        return True

    monkeypatch.setattr("kernel.drivers.k8s_verify.destroy", _destroy)
    monkeypatch.setattr("kernel.drivers.k8s_verify.release", _release)
    return calls


def _quiet(monkeypatch):
    async def _q(fn, *, what=None, budget=0.0, **kwargs):
        return fn(**kwargs)
    monkeypatch.setattr("kernel.drivers.k8s_call.quiet", _q)


# ── the name rule, which is what made it invisible in the first place ──

def test_the_two_claim_prefixes_cannot_read_each_others_names():
    """THE PROTECTION AND THE LEAK ARE THE SAME FACT. `reclaim`'s
    workspace pass must not parse a verification claim -- it would
    delete an environment mid-verification -- and because it cannot,
    nothing collected one until this trigger existed."""
    assert ownership.workspace_execution("verify-ws-42") is None
    assert ownership.execution_in_name(
        "verify-ws-42", verify_pod.CLAIM_PREFIX) == 42
    assert ownership.execution_in_name(
        "ws-exec-42", verify_pod.CLAIM_PREFIX) is None
    assert ownership.execution_in_name(
        "verify-ws-podcheck", verify_pod.CLAIM_PREFIX) is None


# ── what it must never touch ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_live_verifications_pod_and_claim_are_left_alone(monkeypatch):
    """A warm pod between generations is the whole design. Reaping one
    is a `.venv` rebuilt because a reaper was counting."""
    _quiet(monkeypatch)
    driver = _VerifyDriver(pods=[_vpod(208)],
                           claims=[verify_pod.claim_name(208)])
    assert await reclaim.verification_reapable(
        _VerifyConn([_row(208)]), driver) == []


@pytest.mark.asyncio
async def test_a_pod_whose_execution_id_does_not_parse_is_not_offered(
        monkeypatch):
    """The rule every reaper follows: unidentified is KEPT."""
    _quiet(monkeypatch)
    labels = dict(verify_pod.labels_for(208))
    labels["execution-id"] = "e208"
    driver = _VerifyDriver(pods=[_vpod(208, labels)])
    assert await reclaim.verification_reapable(_VerifyConn([]), driver) == []


@pytest.mark.asyncio
async def test_the_executors_own_workspace_claim_is_not_in_this_pass(
        monkeypatch):
    """`ws-exec-<id>` belongs to the workspace half, which releases it
    only once the STORY is finished with it -- hours after the run."""
    _quiet(monkeypatch)
    driver = _VerifyDriver(claims=["ws-exec-208", "ws-podcheck-1726"])
    assert await reclaim.verification_reapable(_VerifyConn([]), driver) == []


@pytest.mark.asyncio
async def test_docker_has_no_verification_pod_to_reap():
    driver = _VerifyDriver()
    driver.name = "docker"
    assert await reclaim.verification_reapable(_VerifyConn([]), driver) == []


# ── and it must still collect what it exists for ───────────────────────

@pytest.mark.asyncio
async def test_a_terminal_executions_pod_and_claim_both_go(
        monkeypatch, reaped):
    """TRIGGER 1, the normal case. Pod before claim: a claim deleted
    under a live writer is I/O errors inside a container nobody
    watches."""
    _quiet(monkeypatch)
    driver = _VerifyDriver(pods=[_vpod(207)],
                           claims=[verify_pod.claim_name(207)])
    rows = await reclaim.verification_reapable(
        _VerifyConn([_row(207, live=False, state="DONE")]), driver)
    assert [r["why"] for r in rows] == ["execution DONE"]
    await reclaim.release(None, driver, rows[0], dry_run=False)
    assert reaped == [("pod", "verify-207"), ("claim", 207)]


@pytest.mark.asyncio
async def test_an_idle_execution_loses_the_pod_and_keeps_the_disk(
        monkeypatch, reaped):
    """TRIGGER 3, the backstop for a run that wedges without ever
    terminating. The `.venv` is still the one the next round wants, so
    being early costs a container start and not an hour of pip."""
    _quiet(monkeypatch)
    driver = _VerifyDriver(pods=[_vpod(209)],
                           claims=[verify_pod.claim_name(209)])
    rows = await reclaim.verification_reapable(
        _VerifyConn([_row(209, live=True, idle=True)]), driver)
    assert rows and rows[0]["keep_claim"] is True
    await reclaim.release(None, driver, rows[0], dry_run=False)
    assert reaped == [("pod", "verify-209")]


@pytest.mark.asyncio
async def test_the_idle_trigger_goes_quiet_once_the_pod_is_gone(monkeypatch):
    """A SWEEP THAT FINDS NOTHING STAYS QUIET. Driven from the objects
    the substrate holds, so a reap already done is not re-reported every
    tick until somebody stops reading the summary."""
    _quiet(monkeypatch)
    driver = _VerifyDriver(claims=[verify_pod.claim_name(209)])
    assert await reclaim.verification_reapable(
        _VerifyConn([_row(209, live=True, idle=True)]), driver) == []


@pytest.mark.asyncio
async def test_a_claim_whose_execution_row_is_gone_is_still_collected(
        monkeypatch, reaped):
    """THE LEAK WITH NO OTHER BACKSTOP AT ALL: no state left to reach a
    terminal value, no pod to find it by, and a name no other sweep
    parses. 120 claims is a hard ceiling on how many stories ever run."""
    _quiet(monkeypatch)
    driver = _VerifyDriver(claims=[verify_pod.claim_name(66)])
    rows = await reclaim.verification_reapable(_VerifyConn([]), driver)
    assert [r["why"] for r in rows] == ["no execution row"]
    await reclaim.release(None, driver, rows[0], dry_run=False)
    assert reaped == [("pod", "verify-66"), ("claim", 66)]


@pytest.mark.asyncio
async def test_a_dead_verifier_does_not_keep_the_pod_alive(
        monkeypatch, reaped):
    """If the verifier Deployment dies mid-verification the warm pod
    outlives it -- there is no controller to take it. Bounded, not
    leaked: the authority is the execution ROW, so this collects it with
    no verifier alive to ask. Do not "fix" that by giving the pod an
    owner; an owner is a controller, and a controller would tear the
    `.venv` down between rounds."""
    _quiet(monkeypatch)
    driver = _VerifyDriver(pods=[_vpod(210)])          # claim already gone
    rows = await reclaim.verification_reapable(
        _VerifyConn([_row(210, live=False, state="CANCELLED")]), driver)
    await reclaim.release(None, driver, rows[0], dry_run=False)
    assert ("pod", "verify-210") in reaped


@pytest.mark.asyncio
async def test_a_dry_run_says_what_it_would_take_and_takes_nothing(
        monkeypatch, reaped):
    _quiet(monkeypatch)
    driver = _VerifyDriver(pods=[_vpod(207)],
                           claims=[verify_pod.claim_name(207)])
    rows = await reclaim.verification_reapable(
        _VerifyConn([_row(207, live=False, state="FAILED")]), driver)
    act = await reclaim.release(None, driver, rows[0], dry_run=True)
    assert act["action"] == "would_reap_verification"
    assert "pod and claim" in act["detail"]
    assert reaped == []


@pytest.mark.asyncio
async def test_the_manifest_trigger_is_a_call_and_not_a_sweep(reaped):
    """TRIGGER 2. The hash is computed from the tree's own manifest
    bytes and stamped on the volume, so the conductor cannot know what
    is wanted -- whoever is about to verify does. It must run BEFORE
    `k8s_verify.ensure`, which is idempotent and would otherwise adopt
    the stale pod and verify against a `.venv` missing a dependency the
    model just added."""
    act = await reclaim.reap_verification(_VerifyDriver(), 203,
                                          why="manifest hash changed")
    assert reaped == [("pod", "verify-203"), ("claim", 203)]
    assert act["action"] == "verification_reaped"
