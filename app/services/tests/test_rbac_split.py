"""One Role for three deployments was one Role too few. Plan 11.5a.

WHAT E1 FOUND. `deploy/k8s/conductor.yaml` bound a single Role,
`agentos-conductor`, to the conductor Deployment AND the verifier
Deployment. A Role shared is a Role that grants the UNION of everything
its holders do, and the union here was the conductor's reaper surface:

    pods, persistentvolumeclaims   get list watch create delete
    jobs                           get list watch create delete

So the verifier -- which creates Jobs, reads their logs and keeps one
warm pod -- also held `delete` on `persistentvolumeclaims`. `ws-exec-<id>`
is the disk an execution's workspace lives on, so a compromised verifier
could delete the very work it was being asked to judge, and 5.18 then
gave it podfs write on top. The item grew rather than shrank.

WHY THE CONDUCTOR'S HALF STAYS BROAD. Four reapers run in it and all
four destroy things -- `orphans.py` (execution pods), `reclaim.py`
(verification pods and claims), `jobs_sweep.py` (Jobs), `ephemeral.py`
(PVCs). Ownership is decided in `kernel/ownership.py`, by label and by
database row, and never by RBAC. Narrowing a verb there disables a
reaper, and a sweep that collects nothing looks exactly like a clean
cluster.

AND WHAT THIS FILE CANNOT ASSERT, SAID PLAINLY SO NOBODY READS MORE INTO
IT THAN IS THERE. Kubernetes RBAC has no label selector, and
`resourceNames` is an exact-match allowlist with no wildcards, so "may
delete `verify-<id>` but not `exec-<id>`" is not expressible at all. The
verifier keeps `delete` on `pods` for `handover.detach`, which is on the
hot path of `rebuild._refresh`; the last test here PINS that residual to
its single caller so the verb cannot outlive the reason for it.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

REPO = pathlib.Path(__file__).resolve().parents[3]

# The KSA each Deployment wears. Spelled here rather than read from the
# manifest, because half of what these tests check is that the names did
# not quietly collapse back into one.
CONDUCTOR = "agentos-conductor"
VERIFIER = "agentos-verifier-control"
EXECUTION = "agentos-execution"


def _docs():
    from app.services.tests.test_k8s_spec import _rendered

    return [d for d in yaml.safe_load_all(_rendered()) if d]


def _of_kind(kind: str) -> dict:
    return {d["metadata"]["name"]: d for d in _docs() if d["kind"] == kind}


def _service_accounts() -> dict:
    return {name: d["spec"]["template"]["spec"].get("serviceAccountName")
            for name, d in _of_kind("Deployment").items()}


def _roles_bound_to(account: str) -> set:
    """Every Role name this ServiceAccount can exercise."""
    return {b["roleRef"]["name"] for b in _of_kind("RoleBinding").values()
            if any(s["kind"] == "ServiceAccount" and s["name"] == account
                   for s in b["subjects"])}


def _verbs(role: str, resource: str) -> set:
    out = set()
    for rule in _of_kind("Role")[role]["rules"]:
        if resource in rule["resources"]:
            out |= set(rule["verbs"])
    return out


# ── the split itself ───────────────────────────────────────────────────

def test_the_two_deployments_no_longer_share_a_role():
    """THE FINDING, as one assertion. Two processes on one Role is one
    process holding the other's verbs, whatever the comments say."""
    accounts = _service_accounts()
    assert accounts["conductor"] == CONDUCTOR
    assert accounts["verifier"] == VERIFIER
    conductor, verifier = _roles_bound_to(CONDUCTOR), _roles_bound_to(VERIFIER)
    assert conductor == {"conductor"}, conductor
    assert verifier == {"verifier"}, verifier
    assert not conductor & verifier, (
        "the conductor and the verifier are back on a shared Role, so "
        "each holds the union of what both processes do")


def test_the_verifier_may_not_delete_the_disk_the_work_lives_on():
    """The verb the split exists to remove.

    A deleted POD is survivable by design -- `destroy()` keeps the claim,
    which is the preemption guarantee `reclaim.py` argues for at length.
    A deleted CLAIM is a story's work with nothing left to re-attach, and
    `handover.detach` passes `keep_claim=True` every time it is called,
    so the verifier has never needed this verb."""
    claims = _verbs("verifier", "persistentvolumeclaims")
    assert "delete" not in claims, (
        "a compromised verifier can destroy the workspace of the very "
        "execution it is judging")
    assert claims == {"get", "create"}, claims


def test_the_verifier_cannot_enumerate_what_it_is_not_judging():
    """`list` is discovery. It reads ONE claim by a name it computes and
    one Job by a name it chose; enumerating claims and Jobs belongs to
    the conductor's sweeps, which is where the database row that says
    whether a thing is wanted also is."""
    assert "list" not in _verbs("verifier", "persistentvolumeclaims")
    assert "list" not in _verbs("verifier", "jobs")


def test_the_execution_identity_holds_no_kubernetes_rights_at_all():
    """`agentos-execution` is reachable from the metadata server by a
    process running a model's bash. It has no Role and no RoleBinding --
    ABSENT rather than empty, because an empty Role is a thing somebody
    later adds one verb to."""
    assert _roles_bound_to(EXECUTION) == set()
    assert EXECUTION in _service_accounts().values(), (
        "the execution identity is gone from the manifest entirely; this "
        "test would then be passing vacuously")


# ── and the shape of both Roles ────────────────────────────────────────

def test_the_conductor_keeps_exactly_what_its_four_reapers_need():
    """Broad on purpose, and each verb has a file behind it:
    orphans.py (execution pods), reclaim.py (verification pods and
    claims), jobs_sweep.py (Jobs), ephemeral.py (PVCs)."""
    assert _verbs("conductor", "pods") == {"get", "list", "create", "delete"}
    assert _verbs("conductor", "persistentvolumeclaims") == {
        "list", "create", "delete"}
    assert _verbs("conductor", "jobs") == {
        "get", "list", "create", "delete"}
    for name in ("orphans", "reclaim", "jobs_sweep", "ephemeral"):
        assert (REPO / "kernel" / "conductor" / f"{name}.py").exists(), (
            f"{name}.py is gone; the conductor's broad delete is now "
            f"justified by a reaper that does not exist")


def test_the_conductor_can_run_the_jobs_its_own_tick_still_runs():
    """THE TEST THIS REPLACES ASSERTED THE WRONG FACT, CONFIDENTLY.

    E12 removed `create` on `jobs` and the `jobs/status` subresource on
    the reasoning that `adjudicate_pass` had moved to the verifier and
    `dryrun_pass` had retired, "so nothing in the conductor process runs
    a Job". Two of the three Job-runners in `loop.py` were enumerated.
    The third is `deliver_questions` -- the answerer -- and it reaches
    the same `k8s_run.run_job`.

    The cost was silent and total: from E12 until 2026-09-22 every
    question anyone asked about a story came back as "the model that
    answers questions about a story is not available right now", while
    the model was fine and the account could not create the Job. Run
    262, questions 207 and 208, on `next`.

    So this asserts the RELATION rather than a remembered verb list: a
    Job-runner in the tick means the verbs, and no Job-runner means no
    verbs. Either half changing fails here.
    """
    loop = (REPO / "kernel" / "worker" / "loop.py").read_text(
        encoding="utf-8")
    runners = [n for n in ("deliver_questions", "adjudicate_pass",
                           "dryrun_pass") if f"{n}(" in loop]
    if runners:
        assert "create" in _verbs("conductor", "jobs"), (
            f"{', '.join(runners)} runs a Job in the conductor's tick "
            f"and the Role cannot create one")
        assert "get" in _verbs("conductor", "jobs/status"), (
            "`run_job` polls read_namespaced_job_status, and `get` on "
            "`jobs` does not carry the subresource")
    else:
        assert "create" not in _verbs("conductor", "jobs")
        assert not _verbs("conductor", "jobs/status")


def test_neither_role_is_cluster_wide_or_a_wildcard():
    """A ClusterRole would let one compromise reach production's
    namespace on the same cluster, which is the trade this design
    accepts only while the boundaries are real."""
    assert not [d for d in _docs()
                if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    for name, role in _of_kind("Role").items():
        assert role["metadata"].get("namespace"), name
        for rule in role["rules"]:
            assert "*" not in rule["verbs"], name
            assert "*" not in rule["resources"], name


def test_nothing_anywhere_is_granted_watch():
    """It was granted on both resources before this change and nothing
    in the kernel opens one: every call goes through
    `kernel/drivers/k8s_call.py`, which bounds a request and says in its
    own comment that a watch must not come through it."""
    for name, role in _of_kind("Role").items():
        for rule in role["rules"]:
            assert "watch" not in rule["verbs"], name


def test_the_pod_delete_the_verifier_keeps_is_pinned_to_its_one_caller():
    """THE RESIDUAL, AND THE TRIPWIRE ON IT.

    RBAC cannot scope a delete by label, so the verifier still holds
    `delete` on `pods` -- and it needs it for exactly one thing:
    `handover.detach` takes the warm pod off a ReadWriteOncePod claim so
    the prepare Job can attach it. Take that away and a 403 lands inside
    `rebuild._refresh`'s try, which returns `prepare_crashed` on every
    tick and the execution never becomes judgeable.

    So this asserts the justification still exists. The day `detach`
    moves behind the conductor -- which already owns `reap_verification`
    -- this test fails and the verb comes out with it."""
    assert _verbs("verifier", "pods") == {"get", "list", "create", "delete"}
    handover = (REPO / "kernel" / "verifier" / "handover.py").read_text(
        encoding="utf-8")
    assert "reap_verification(" in handover, (
        "the verifier no longer deletes its own pod, so `delete` on "
        "`pods` should come out of its Role")
