"""E4, E13 and E11 — what the adjudicator must decide BEFORE it judges.

Three questions that are not about the work, and every one of them was
capable of producing a wrong answer about the work:

    E4   is the verification environment the one the COMMITTED manifest
         asks for? Execution 207's second generation wrote
         `.agentos/setup.sh` and called `env_setup` again, so "prepare
         when env_setup first succeeds" verifies round two against round
         one's `.venv`
    E13  is a rebuild running right now? A model can commit and claim in
         the same breath, and a claim judged mid-rebuild is judged
         against a half-replaced environment -- which in the
         remove-a-dependency direction is a check PASSING THAT SHOULD
         FAIL (plan 5.9, the dangerous direction)
    E11  did this verification produce no answer because the CLUSTER
         ended it? Plan 5.8 row 3: reported as a refusal, a preempted
         node tells a model its correct work is wrong
"""

from __future__ import annotations

import io
import pathlib
from types import SimpleNamespace as NS

import pytest

from kernel.verifier import pod as verify_pod
from kernel.verifier import preempted, rebuild
from kernel.verifier.outcome import from_exit, unrunnable

KEY = "a" * 64
OTHER = "b" * 64
CONTRACT = {"id": 1, "base_image": "contract:sha"}
CRITERIA = [{"id": 7, "spec": {"run": "pytest"}}]


def _src(module) -> str:
    return io.open(pathlib.Path(module.__file__), encoding="utf-8").read()


class _Driver:
    """A pod-model substrate. `is_pod` reads exactly this attribute."""

    WORKSPACE_MODEL = "pod"
    namespace = "agentos-staging"


class _Conn:
    """Enough of a connection for the gate: the RUNNING sweep and the
    stall_reason write."""

    def __init__(self):
        self.stalls: dict = {}

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return 41

    async def execute(self, sql, *args):
        if "stall_reason" in sql:
            self.stalls[args[0]] = args[1]
        return None


class _Store:
    """`verification_state`, in memory. The SQL itself needs a database;
    what these tests are about is which decision the gate takes from the
    row it reads."""

    def __init__(self, **row):
        # `built_at_generation` is part of the row `current()` returns;
        # a double that omits it makes `cold_due` read None for every
        # test, which is a different question from the one each test
        # asks.
        self.row = {"env_key": None, "env_commit": None, "building_key": None,
                    "build_error": None, "disruptions": 0,
                    "building_for": None, "checked_ago": None,
                    "built_at_generation": None, **row}
        self.built_with: list = []
        self.built_generations: list = []
        self.mounts_env_written: list = []
        self.failed_with: list = []
        self.claims: list = []

    def install(self, monkeypatch, module=rebuild):
        state = self

        async def current(conn, eid):
            return dict(state.row)

        # `mounts_env` is E19's fourth argument: the builder
        # records HOW to use the environment beside WHICH one it
        # is, because `verify_claim` runs in a later pass and
        # often another process. Defaulted here so this double
        # keeps covering callers that do not pass it.
        # `generation` defaulted for the same reason `mounts_env` is:
        # this double covers callers that do not pass it. It stamps the
        # cold-rebuild clock, and only a real build passes one -- a
        # cache hit must not reset it or the cold rebuild never comes
        # due.
        async def built(conn, eid, key, commit, mounts_env=None,
                        generation=None):
            state.built_with.append((key, commit))
            state.mounts_env_written.append(mounts_env)
            state.built_generations.append(generation)
            state.row.update(env_key=key, env_commit=commit,
                             mounts_env=mounts_env,
                             built_at_generation=(
                                 generation
                                 if generation is not None
                                 else state.row.get("built_at_generation")),
                             building_key=None, build_error=None)

        async def build_failed(conn, eid, commit, error):
            state.failed_with.append((commit, error))
            state.row.update(env_commit=commit, build_error=error,
                             building_key=None)

        async def claim_build(conn, eid, key):
            state.claims.append(key)
            state.row["building_key"] = key
            return True

        async def checked(conn, eid):
            state.row["checked_ago"] = 0.0

        async def now_building(conn, eid, key):
            state.row["building_key"] = key

        async def released(conn, eid):
            state.row["building_key"] = None

        fake = NS(current=current, built=built, build_failed=build_failed,
                  claim_build=claim_build, now_building=now_building,
                  checked=checked,
                  released=released,
                  building=lambda row: row.get("building_key"))
        monkeypatch.setattr(module, "verify_state", fake)
        return state


@pytest.fixture()
def gate(monkeypatch):
    """The gate with its two expensive reads stubbed: the head, and the
    tar. Both are real network in production and neither is what these
    tests are about."""
    calls = NS(builds=[], sha="c" * 40)
    calls.commit = "@" + calls.sha          # the fingerprint, one mount

    async def _committed(conn, driver, eid, repo):
        return calls.commit, calls.sha

    async def _tree(driver, conn, eid, repo, commit, mounts):
        return b"tar-of-" + commit.encode()

    async def _load(conn, eid):
        return dict(CONTRACT), list(CRITERIA)

    async def _mounts(conn, eid):
        return ()

    async def _build(driver, image, tar, ats, **kw):
        calls.builds.append({"image": image, "ats": list(ats), **kw})
        return {"mode": "volume", "ok": True, "key": KEY, "key12": KEY[:12],
                "cache": "miss"}

    async def _on_volume(driver, eid, state):
        # The claim probe is `handover`'s and has its own tests below.
        return True

    monkeypatch.setattr(rebuild, "on_volume", _on_volume)
    monkeypatch.setattr(rebuild, "detach", lambda driver, eid: _noop())
    monkeypatch.setattr(rebuild, "attach",
                        lambda driver, eid, contract: _noop())
    monkeypatch.setattr(rebuild, "_committed", _committed)
    monkeypatch.setattr(rebuild, "_tree", _tree)
    monkeypatch.setattr(rebuild, "load_contract", _load)
    monkeypatch.setattr(rebuild, "mounts_for", _mounts)
    monkeypatch.setattr(rebuild.env_payload, "collect",
                        lambda tar, ats, prefix="verify/": {("", "req"): tar})
    monkeypatch.setattr(rebuild.env_payload, "key",
                        lambda image, files: KEY)
    monkeypatch.setattr(rebuild.prepare_volume, "build_on_volume", _build)
    return calls


def _row(eid=203):
    return {"id": eid, "generation": 2, "workspace_path": "/workspace",
            "contract_id": 1}


# ── E4 · the trigger is a manifest CHANGE ──────────────────────────────

@pytest.mark.asyncio
async def test_an_unchanged_manifest_costs_no_job(monkeypatch, gate):
    """5.10's second rule. A model calling `env_setup` defensively five
    times must not churn five pods, and a source-only commit moves the
    head without moving the key."""
    store = _Store(env_key=KEY, env_commit=gate.commit).install(monkeypatch)
    conn = _Conn()

    rows, actions = await rebuild.environments(conn, _Driver(), None,
                                               [_row()])

    assert gate.builds == [], "an unchanged manifest started a prepare Job"
    assert [r["id"] for r in rows] == [203], "the claim was held back"
    assert actions == []
    assert store.claims == []


@pytest.mark.asyncio
async def test_a_moved_head_with_the_same_key_starts_no_job(monkeypatch,
                                                            gate):
    """The head is the CHEAP GATE; the manifest hash is the authority.
    Committing `src/app.py` moves one and not the other, and paying a
    prepare for it would make every commit cost a pod."""
    store = _Store(env_key=KEY, env_commit="root@old").install(monkeypatch)

    await rebuild.environments(_Conn(), _Driver(), None, [_row()])

    assert gate.builds == []
    assert store.built_with == [(KEY, gate.commit)], (
        "the new head was not recorded, so the next tick re-reads the tree")


@pytest.mark.asyncio
async def test_a_changed_manifest_builds_and_persists_the_key(monkeypatch,
                                                              gate):
    """5.16 item 2: somebody must persist `report["key"]`, because
    `volume.stale` takes the stamped value as a parameter."""
    store = _Store(env_key=OTHER, env_commit="root@old").install(monkeypatch)

    await rebuild.environments(_Conn(), _Driver(), None, [_row()])

    assert len(gate.builds) == 1
    assert store.built_with == [(KEY, gate.commit)]


@pytest.mark.asyncio
async def test_the_persisted_key_is_handed_back_as_stamped(monkeypatch,
                                                           gate):
    """The other half of 5.16 item 2. Passing None every time would
    rebuild a correct environment on every round."""
    _Store(env_key=OTHER, env_commit="root@old").install(monkeypatch)

    await rebuild.environments(_Conn(), _Driver(), None, [_row()])

    assert gate.builds[0]["stamped"] == OTHER
    assert gate.builds[0]["claim"] == "verify-ws-203"
    assert gate.builds[0]["image"] == "contract:sha", (
        "the prepare must run the CONTRACT's image, never the executor's")


@pytest.mark.asyncio
async def test_a_substrate_without_volumes_is_left_alone(monkeypatch, gate):
    """On Docker the environment is rebuilt inside every verification, so
    there is no cache to invalidate -- and a gate that skipped claims
    there would wedge every story on the one substrate that cannot be
    unwedged by waiting."""
    _Store().install(monkeypatch)

    rows, actions = await rebuild.environments(
        _Conn(), NS(WORKSPACE_MODEL="local"), None, [_row()])

    assert [r["id"] for r in rows] == [203]
    assert (actions, gate.builds) == ([], [])


def test_the_manifest_is_read_from_the_committed_tree():
    """The executor's `env_setup` reads the WORKING tree. A model that
    edits requirements.txt, runs env_setup and does not commit has an
    environment the verifier can never reproduce."""
    body = _src(rebuild)
    # The head comes from `tree_state` (committed) and the bytes from
    # `stage._archive`, whose three branches all archive a COMMIT.
    assert "tree_state(" in body
    assert "from kernel.verifier.stage import _archive" in body
    for working in ("setups.from_dir", "os.walk", "podfs_client.read"):
        assert working not in body, (
            f"{working} reads a working tree; the key must not depend on "
            f"anything the executor has not committed")


@pytest.mark.asyncio
async def test_git_is_given_a_commit_and_never_the_fingerprint(monkeypatch,
                                                               gate):
    """Two values, two jobs. The FINGERPRINT is every mount's head joined
    together and exists to answer "has anything been committed since we
    last looked"; `git archive` and the prepare Job's own fetch need a
    commit-ish. Handing the joined form to git archives nothing."""
    store = _Store(env_key=OTHER, env_commit="@old").install(monkeypatch)
    seen: dict = {}

    async def _tree(driver, conn, eid, repo, commit, mounts):
        seen["commit"] = commit
        return b"tar"

    monkeypatch.setattr(rebuild, "_tree", _tree)

    await rebuild.environments(_Conn(), _Driver(), None, [_row()])

    assert seen["commit"] == gate.sha, "git was handed the fingerprint"
    assert store.built_with == [(KEY, gate.commit)], (
        "the fingerprint is what gets stored, because it is what the next "
        "tick compares")


@pytest.mark.asyncio
async def test_the_fingerprint_covers_every_repository(monkeypatch):
    """E7f. One sha names a fraction of a multi-repo workspace, so a
    manifest change in the second repository would hide behind an
    unchanged first."""
    async def _mounts(conn, eid):
        return ()

    async def _state(conn, eid, root, mounts):
        return (("", "aaa"), ("repos/b", "bbb")), "aaa"

    monkeypatch.setattr(rebuild, "mounts_for", _mounts)
    monkeypatch.setattr(rebuild, "tree_state", _state)

    fingerprint, commit = await rebuild._committed(_Conn(), _Driver(), 203,
                                                   "/workspace")

    assert fingerprint == "@aaa,repos/b@bbb"
    assert commit == "aaa"


# ── E13 · adjudication WAITS for a rebuild ─────────────────────────────

@pytest.mark.asyncio
async def test_a_claim_mid_rebuild_is_skipped_and_says_why(monkeypatch,
                                                           gate):
    """The dangerous direction (5.9): verifying against a half-replaced
    `.venv` can let a check pass that should fail."""
    _Store(env_key=OTHER, env_commit="root@old",
           building_key=KEY).install(monkeypatch)
    conn = _Conn()

    rows, actions = await rebuild.environments(conn, _Driver(), None,
                                               [_row()])

    assert rows == [], "the claim was judged while its environment moved"
    assert [a["action"] for a in actions] == ["awaiting_environment"]
    assert KEY[:12] in actions[0]["detail"]
    assert KEY[:12] in conn.stalls[203], (
        "the stall_reason must name the hash being built")
    assert gate.builds == [], "a second Job was started on the same claim"


@pytest.mark.asyncio
async def test_a_stale_build_claim_does_not_wedge_the_run(monkeypatch,
                                                          gate):
    """A verifier killed mid-prepare must not hold an execution for ever.
    `verify_state.building` is where that bound lives; this is the
    behaviour it buys."""
    from kernel.verifier import verify_state

    old = {"building_key": KEY,
           "building_for": verify_state.STALE_BUILD_S + 1}
    assert verify_state.building(old) is None
    assert verify_state.building({"building_key": KEY,
                                  "building_for": 5.0}) == KEY


@pytest.mark.asyncio
async def test_a_failed_prepare_errors_every_criterion(monkeypatch, gate):
    """5.8 row 5. A genuinely broken manifest IS about the work, and the
    executor is the only party who can fix it -- but nothing was checked,
    so it is `error` and never `fail`."""
    _Store(env_key=OTHER, env_commit=gate.commit,
           build_error="pip: no matching distribution").install(monkeypatch)
    seen: dict = {}

    async def _errored(conn, eid, generation, claim_seq, results):
        seen["results"] = results
        return {"execution_id": eid, "action": "errored"}

    async def _load(conn, eid):
        return dict(CONTRACT), list(CRITERIA)

    monkeypatch.setattr("kernel.verifier.errored.errored_claim", _errored)
    monkeypatch.setattr("kernel.verifier.writer.load_contract", _load)

    rows, actions = await rebuild.environments(_Conn(), _Driver(), None,
                                               [_row()])

    assert rows == [] and actions[0]["action"] == "errored"
    assert [o.passes for _, o in seen["results"]] == ["error"], (
        "a prepare failure must never be reported as a failing check")
    assert "pip: no matching distribution" in seen["results"][0][1].detail[
        "reason"]


# ── E11 · five failure modes, and row 3 is new ─────────────────────────

def _cluster_ended(reason: str):
    """A criterion outcome for a run the cluster ended.

    TAGGED, and the tag is the whole of row 3 now. Writing the sentence
    by hand here and expecting `preempted` to recognise it would be
    re-asserting the prefix match this change removed.
    """
    from kernel.drivers.infra_error import DISRUPTED, InfraError

    return from_exit(None, "", "pytest", infra_error=InfraError(
        f"the cluster ended this run ({reason}); it says nothing about "
        f"the work", DISRUPTED))


def _classify(**state):
    """A real pod through the real classifier. No cluster: a pod is data."""
    from kernel.drivers.k8s_outcome import classify

    fields = {"terminated": None, "waiting": None, "running": None, **state}
    statuses = [NS(name="criterion", state=NS(**fields))] if state else []
    return classify(NS(status=NS(conditions=[], phase="Failed", reason=None,
                                 container_statuses=statuses)))


def test_a_disrupted_run_is_recognised_from_what_the_driver_says():
    """THE ANTI-DRIFT TEST, AND IT NOW GUARDS A FIELD RATHER THAN PROSE.

    It used to assert that `preempted.DISRUPTED_PREFIX` still matched the
    sentence `k8s_outcome.disrupted` writes -- two spellings of one fact,
    held together by this test. That is fault 10's shape: a channel
    written for a person and parsed by a machine. The kind is the channel
    now, and what must not drift is that `k8s_outcome` is its only
    producer and `preempted` its only consumer, END TO END through the
    outcome adjudication actually reads.
    """
    from kernel.drivers.infra_error import DISRUPTED, kind_of
    from kernel.drivers.k8s_outcome import classify

    pod = NS(status=NS(conditions=[NS(type="DisruptionTarget",
                                      reason="PreemptionByScheduler")],
                       phase="Failed", container_statuses=[]))
    verdict = classify(pod)
    assert verdict["exit_code"] is None
    assert kind_of(verdict["infra_error"]) == DISRUPTED
    outcome = from_exit(None, "", "pytest",
                        infra_error=verdict["infra_error"])
    assert kind_of(outcome.detail["reason"]) == DISRUPTED, (
        "the kind must survive `from_exit` into the criterion's detail; "
        "it is the only thing telling 5.8 row 3 from row 2")

    assert preempted.disrupted([({"id": 7}, outcome)]) == \
        verdict["infra_error"]


def test_the_sentence_explains_and_the_kind_decides():
    """BOTH DIRECTIONS OF THE SAME POINT. The sentence is written for a
    person beside a verdict and may be reworded without a deploy's worth
    of archaeology; and an infra error that never went through the
    classifier must land in row 2, because an untagged value defaulting
    to row 3 would buy free re-runs for every ending nobody recognised."""
    from kernel.drivers.infra_error import DISRUPTED, InfraError

    reworded = unrunnable(InfraError("a node went away, in words nobody "
                                     "has written yet", DISRUPTED),
                          infra=True)
    assert preempted.disrupted([({}, reworded)])

    untagged = unrunnable("the cluster ended this run (Evicted); it says "
                          "nothing about the work", infra=True)
    assert preempted.disrupted([({}, untagged)]) is None


def test_the_kind_survives_being_re_wrapped_as_a_string():
    """A caller that hands the Job's infra error on as `str(...)` would
    ordinarily lose a str SUBCLASS's attributes there -- dropping that
    preemption from row 3 into row 2, silently, which is the whole
    failure this change exists to end. `InfraError.__str__` returns
    `self` for that reason. (The measured case was E2's batch runner,
    retired at E9; the property is about the type, not that caller.)"""
    from kernel.drivers.infra_error import DISRUPTED, InfraError, kind_of

    tagged = InfraError("the cluster ended this run (Evicted)", DISRUPTED)
    assert kind_of(str(tagged)) == DISRUPTED
    assert kind_of(f"{tagged}") == DISRUPTED


def test_an_ordinary_error_is_not_a_disruption():
    """Row 2 and row 3 are different rows, and STRUCTURALLY different
    now. An OOM or a missing tool is the checks failing to run -- three
    strikes and back to the executor -- and treating it as preemption
    would re-run it for ever."""
    from kernel.drivers.infra_error import OOM, kind_of

    killed = _classify(terminated=NS(exit_code=137, reason="OOMKilled"))
    assert kind_of(killed["infra_error"]) == OOM
    oom = from_exit(None, "", "pytest", infra_error=killed["infra_error"])
    missing = unrunnable("command not found", infra=False)

    assert preempted.disrupted([({}, oom), ({}, missing)]) is None


@pytest.mark.asyncio
async def test_a_run_with_no_verification_volume_is_left_alone(monkeypatch):
    """The guard that keeps all of this inert until E7 has provisioned a
    claim for the run. A prepare Job aimed at a claim that does not exist
    fails, and 5.8 row 5 would report that as a broken manifest."""
    from kernel.verifier import handover

    async def _no(driver, eid):
        return False

    monkeypatch.setattr(handover, "has_claim", _no)
    assert await handover.on_volume(_Driver(), 203, {"env_key": None}) is False


@pytest.mark.asyncio
async def test_the_claim_question_goes_through_the_seam():
    """5.17 item 11. `has_claim` used to reach `driver._core_api()` and
    import `kubernetes._not_found` from inside the verifier -- a
    substrate's private module, in a function every substrate calls. It
    asks the driver now, and the verifier keeps only the thing that is
    its own: which execution owns which claim."""
    from kernel.verifier import handover

    asked: list = []

    class _Substrate:
        async def claim_exists(self, claim):
            asked.append(claim)
            return True

    assert await handover.has_claim(_Substrate(), 203) is True
    assert asked == [verify_pod.claim_name(203)]


@pytest.mark.asyncio
async def test_a_substrate_with_no_claims_refuses_with_a_sentence():
    """THE BEHAVIOUR THAT MUST SURVIVE THE HOIST, and the reason the
    neighbouring seam has it: 'this substrate has no volumes' and 'that
    claim is gone' are different facts, so the base driver says the first
    in words rather than answering the second with `False`."""
    from kernel.driver import Driver
    from kernel.drivers.kubernetes import KubernetesDriver

    assert Driver.CLAIM_JOB_MODEL == "none"
    assert KubernetesDriver.claim_exists is not Driver.claim_exists

    with pytest.raises(NotImplementedError) as caught:
        await Driver.claim_exists(NS(CLAIM_JOB_MODEL="none"), "c")
    assert "CLAIM_JOB_MODEL" in str(caught.value)


@pytest.mark.asyncio
async def test_a_substrate_that_will_not_answer_keeps_deciding(monkeypatch):
    """...but only for a run we have already built an environment for.
    Judging that one against an environment we cannot identify is the
    false-pass direction; judging one we never prepared is free."""
    from kernel.verifier import handover

    async def _boom(driver, eid):
        raise RuntimeError("the API server said no")

    monkeypatch.setattr(handover, "has_claim", _boom)
    assert await handover.on_volume(_Driver(), 203, {"env_key": KEY}) is True
    assert await handover.on_volume(_Driver(), 203,
                                    {"env_key": None}) is False


@pytest.mark.asyncio
async def test_the_pod_is_detached_before_the_job_and_back_after(
        monkeypatch, gate):
    """RWOP. The claim takes one attacher, so a prepare Job submitted
    while the warm pod holds it never starts -- which is not an error to
    catch, it is a Pending pod and a deadline."""
    _Store(env_key=OTHER, env_commit="root@old").install(monkeypatch)
    order: list = []

    async def _detach(driver, eid):
        order.append("detach")

    async def _attach(driver, eid, contract):
        order.append("attach")

    async def _build(driver, image, tar, ats, **kw):
        order.append("build")
        return {"ok": True, "key": KEY, "key12": KEY[:12], "cache": "miss"}

    monkeypatch.setattr(rebuild, "detach", _detach)
    monkeypatch.setattr(rebuild, "attach", _attach)
    monkeypatch.setattr(rebuild.prepare_volume, "build_on_volume", _build)

    await rebuild.environments(_Conn(), _Driver(), None, [_row()])

    assert order == ["detach", "build", "attach"]


@pytest.mark.asyncio
async def test_the_build_is_claimed_before_the_tree_is_read(monkeypatch,
                                                            gate):
    """The claim IS E13's exclusion. Taken after the tar -- tens of
    seconds -- the tick four seconds later would find no rebuild
    recorded and judge the claim against the environment being replaced.
    """
    store = _Store(env_key=OTHER, env_commit="root@old").install(monkeypatch)
    order: list = []

    async def _claim(conn, eid, key):
        order.append("claim:" + key[:4])
        store.row["building_key"] = key
        return True

    async def _tree(driver, conn, eid, repo, commit, mounts):
        order.append("tree")
        return b"tar"

    monkeypatch.setattr(rebuild.verify_state, "claim_build", _claim)
    monkeypatch.setattr(rebuild, "_tree", _tree)

    await rebuild.environments(_Conn(), _Driver(), None, [_row()])

    assert order[0].startswith("claim:") and order[1] == "tree"


@pytest.mark.asyncio
async def test_a_build_another_process_holds_is_not_started_twice(
        monkeypatch, gate):
    """Two verifier replicas and the conductor all run this pass, and a
    prepare Job outlives the connection that submitted it -- so the
    exclusion is a row, not an advisory lock."""
    _Store(env_key=OTHER, env_commit="root@old").install(monkeypatch)

    async def _taken(conn, eid, key):
        return False

    monkeypatch.setattr(rebuild.verify_state, "claim_build", _taken)

    rows, actions = await rebuild.environments(_Conn(), _Driver(), None,
                                               [_row()])

    assert [a["action"] for a in actions] == ["preparing_elsewhere"]
    assert rows == [] and gate.builds == []


def test_the_detach_keeps_the_claim():
    """`keep_claim=True` is the whole difference between rebuilding an
    environment in place and destroying the disk it lives on."""
    from kernel.verifier import handover

    body = _src(handover)
    assert "reap_verification" in body and "keep_claim=True" in body


@pytest.mark.asyncio
async def test_a_preempted_run_writes_no_verdict(monkeypatch):
    """Row 3, and the whole point of it: no verdict, no strike, and the
    pod is put back on the claim it already has."""
    counts: dict = {"n": 0}

    async def _disrupted(conn, eid):
        counts["n"] += 1
        return counts["n"]

    async def _decided(conn, eid):
        counts["reset"] = True

    monkeypatch.setattr(preempted.verify_state, "disrupted", _disrupted)
    monkeypatch.setattr(preempted.verify_state, "decided", _decided)
    monkeypatch.setattr(preempted, "_recreate",
                        lambda driver, eid, contract: _noop())
    conn = _Conn()
    ended = _cluster_ended("Evicted")

    got = await preempted.not_the_work(conn, _Driver(), _row(),
                                       [({"id": 7}, ended)], CONTRACT)

    assert got["action"] == "verification_disrupted"
    assert "no verdict" in got["detail"]
    assert "says nothing about the work" in conn.stalls[203]


async def _noop():
    return ""


@pytest.mark.asyncio
async def test_a_cluster_that_keeps_ending_it_stops_being_free(monkeypatch):
    """Past the bound it is no longer bad luck. The ordinary undecided
    path takes over -- strikes and all -- so the run reaches a person
    instead of being re-run for ever."""
    async def _disrupted(conn, eid):
        return preempted.DISRUPTION_LIMIT + 1

    monkeypatch.setattr(preempted.verify_state, "disrupted", _disrupted)
    ended = _cluster_ended("Shutdown")

    got = await preempted.not_the_work(_Conn(), _Driver(), _row(),
                                       [({"id": 7}, ended)], CONTRACT)

    assert got is None, "the run would be re-verified for ever"


@pytest.mark.asyncio
async def test_a_refusal_is_never_suppressed_by_a_preemption():
    """A criterion that ran and found the work wrong is still right about
    it whatever happened to another criterion's node. `_apply` asks this
    question only in the not-refused branch; this asserts the branch is
    still where it was."""
    from kernel.verifier import verdict

    body = _src(verdict)
    before = body.index("preempted.not_the_work")
    guard = body.rindex("if not refused:", 0, before)
    assert "refused = refuses(results)" in body[:guard]
