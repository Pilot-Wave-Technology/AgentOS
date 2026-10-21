"""E19 — the cutover nobody was assigned.

THE PLAN SAID IT FIRST, on execution 211, and nothing changed by 217:

    "the code that REAPS verification pods is wired, and the code that
    CREATES them is not."

`k8s_verify.ensure` had exactly two callers and BOTH required the thing
it creates:

    handover.attach     reached only from rebuild._refresh, i.e. AFTER a
                        build -- and a build needs the claim
    preempted           `if not await has_claim(driver, eid): return ""`

So `on_volume` was False for every execution that has ever run,
`rebuild` never fired a prepare Job, `verification_state` stayed empty,
`batch.warm_for` found no `env_key`, and `verify_claim` took the tar
path every single time: archive the tree, push 44MB through the object
store, and spawn one ephemeral Job per criterion.

Measured on 217, identical to 211: `exec-217`, `cap-058a1e916137`, three
`crit-*` Jobs. No `verify-217`. No claim.

AND EVERY GATE BELOW IT WAS CORRECT. `warm_for` returns None "on ANY
doubt" because judging against a volume nobody has identified is a check
PASSING THAT SHOULD FAIL. `on_volume` judged rather than blocked. Not
one of them was wrong; there was simply nothing at the top. That is why
it degraded silently instead of erroring, and it is the third time in
one day this shape has cost a run -- `for_execution` with no caller,
`locate` called after the thing that reads it, and now a claim nothing
creates.
"""
from __future__ import annotations

import asyncio
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class _Pod:
    WORKSPACE_MODEL = "pod"
    CLAIM_JOB_MODEL = "job"


class _Docker:
    WORKSPACE_MODEL = "local"


class _Conn:
    async def fetchval(self, sql, *a):
        return None

    async def fetchrow(self, sql, *a):
        return None

    async def execute(self, sql, *a):
        return None

    async def fetch(self, sql, *a):
        return []


# ── the helper itself ──────────────────────────────────────────────────

def test_provision_creates_the_claim_and_the_pod(monkeypatch):
    """THE MISSING CALLER. `ensure` makes the claim then the pod."""
    from kernel.drivers import k8s_verify
    from kernel.verifier import handover, writer

    seen = {}

    async def fake_ensure(driver, eid, *, image, size, **kw):
        seen["eid"] = eid
        return f"verify-{eid}"

    async def fake_contract(conn, eid):
        return ({"base_image": "contract:1"}, [])

    monkeypatch.setattr(k8s_verify, "ensure", fake_ensure)
    monkeypatch.setattr(writer, "load_contract", fake_contract)

    got = asyncio.run(handover.provision(_Conn(), _Pod(), 217))

    assert got is True
    assert seen["eid"] == 217, "nothing created the verification volume"


def test_provision_declines_where_there_are_no_claims():
    """Docker and E0. `warm_for` says the same at the other end: the tar
    path there is not a fallback, it is the only thing possible."""
    from kernel.verifier import handover

    assert asyncio.run(handover.provision(_Conn(), _Docker(), 217)) is False


def test_a_failure_leaves_the_tar_path_exactly_as_it_was(monkeypatch):
    """THE SAFETY OF THE WHOLE CHANGE. Returning False puts the caller
    back on the path it took before this existed, so an RBAC gap or a
    quota degrades to today's behaviour rather than stopping
    verification."""
    from kernel.drivers import k8s_verify
    from kernel.verifier import handover, writer

    async def boom(*a, **kw):
        raise RuntimeError("persistentvolumeclaims is forbidden")

    async def fake_contract(conn, eid):
        return ({}, [])

    monkeypatch.setattr(k8s_verify, "ensure", boom)
    monkeypatch.setattr(writer, "load_contract", fake_contract)

    assert asyncio.run(handover.provision(_Conn(), _Pod(), 217)) is False


# ── the two places it is called from ───────────────────────────────────

def test_the_verifying_path_provisions_instead_of_giving_up():
    """`_claim_row` used to `return None` the moment `on_volume` was
    False -- correct in isolation ("nothing to be stale about") and, with
    nothing else creating volumes, the reason none ever existed."""
    src = _src("kernel/verifier/rebuild.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_claim_row":
            continue
        calls = {c.func.id if isinstance(c.func, ast.Name)
                 else getattr(c.func, "attr", "")
                 for c in ast.walk(node) if isinstance(c, ast.Call)}
        assert "provision" in calls, (
            "_claim_row does not provision, so a run that reaches "
            "VERIFYING without a volume never gets one")
        return
    raise AssertionError("_claim_row is gone; re-point this check")


def test_the_overlap_asks_for_the_manifest_before_it_makes_a_volume():
    """ORDER, AND IT IS A CAPACITY DECISION. Provisioning before knowing
    whether anything needs installing puts a 10Gi RWOP claim on every
    RUNNING execution, against the 120-PVC ceiling W1 records -- and
    most would never build a thing."""
    src = _src("kernel/verifier/rebuild.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_overlap":
            continue
        at = {}
        for c in ast.walk(node):
            if isinstance(c, ast.Call):
                n = (c.func.id if isinstance(c.func, ast.Name)
                     else getattr(c.func, "attr", ""))
                if n in ("_committed", "provision") and n not in at:
                    at[n] = c.lineno
        assert "provision" in at, "_overlap never provisions"
        assert "_committed" in at, "re-point this check"
        assert at["_committed"] < at["provision"], (
            "the overlap provisions a volume before it knows whether the "
            "run has a manifest to install")
        return
    raise AssertionError("_overlap is gone; re-point this check")


def test_a_run_with_no_manifest_stops_being_asked_every_tick():
    """`_committed` is a podfs round trip and `_overlap` takes fifty rows
    a pass. Without stamping the cooldown, a repo that declares no
    dependencies is re-read every four seconds for the life of the
    run."""
    src = _src("kernel/verifier/rebuild.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_overlap":
            continue
        # THE BRANCH, NOT THE FUNCTION. The first draft of this asserted
        # `"checked(conn, eid)" in ast.unparse(node)` and PASSED AGAINST
        # HEAD -- `_overlap` already stamped the cooldown in its
        # manifest-unchanged branch, so the check proved nothing about
        # the branch it was written for. Caught by running it against
        # the pre-fix commit, which is the only thing that would have.
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            if "fingerprint is None" not in ast.unparse(inner.test):
                continue
            taken = ast.unparse(ast.Module(body=inner.body, type_ignores=[]))
            assert "checked" in taken, (
                "the no-manifest branch does not stamp the cooldown, so "
                "`_committed` asks the pod again every four seconds for "
                "the life of every run that declares no dependencies")
            return
        raise AssertionError(
            "no `fingerprint is None` branch in _overlap; re-point this")
    raise AssertionError("_overlap is gone; re-point this check")


# ── the standing lesson ────────────────────────────────────────────────

def test_everything_that_creates_a_verification_object_is_reachable():
    """THE CHECK THAT WOULD HAVE CAUGHT THIS, and the two before it.

    Three times in one day: `choice.for_execution` written and never
    called, `locate` called after the function that reads its output,
    and `k8s_verify.ensure` reachable only from paths that require what
    it creates. Each half correct; the wire missing.

    So: `ensure` must have at least one caller that does NOT first
    require the claim to exist. `attach` and the `preempted` path both
    do -- `provision` is the one that does not, and if it ever loses its
    callers this fails rather than quietly returning the fleet to the
    tar path.
    """
    callers = []
    for rel in ("kernel/verifier/handover.py", "kernel/verifier/rebuild.py",
                "kernel/verifier/preempted.py"):
        for node in ast.walk(ast.parse(_src(rel))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for c in ast.walk(node):
                if not isinstance(c, ast.Call):
                    continue
                name = (c.func.id if isinstance(c.func, ast.Name)
                        else getattr(c.func, "attr", ""))
                if name == "provision":
                    callers.append(f"{rel}:{node.name}")

    unconditional = [c for c in callers if "provision" not in c.split(":")[1]]
    assert unconditional, (
        "nothing calls `handover.provision`, so no verification claim is "
        "ever created and every story silently verifies through the tar "
        "path -- which is exactly the state executions 211 and 217 were "
        "found in")


@pytest.mark.parametrize("rel,needle", [
    ("kernel/verifier/batch.py", "warm_for"),
    ("kernel/verifier/verify.py", "batch.run"),
])
def test_the_warm_path_downstream_is_still_wired(rel, needle):
    """Provisioning only matters because everything after it exists:
    `warm_for` finds the pod, `verify_claim` runs the checks in it via
    `batch.run` instead of one Job per criterion. If either goes, this
    change buys a PVC and nothing else."""
    assert needle in _src(rel), f"{rel} no longer references {needle}"

# ── on Kubernetes the pod is not optional ──────────────────────────────

def test_one_spelling_of_whether_the_substrate_has_claims():
    """There were three -- `batch.warm_for`, `prepare_volume` and
    `handover` each asked `CLAIM_JOB_MODEL == "job"` in their own words.
    A fourth answer waiting to disagree is how the tar path came to run
    on the one substrate that is not supposed to use it."""
    raw = []
    for path in pathlib.Path("kernel").rglob("*.py"):
        if "test" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if 'CLAIM_JOB_MODEL", "none"' in text:
            raw.append(str(path).replace("\\", "/"))

    assert raw == ["kernel/verifier/handover.py"], (
        f"`CLAIM_JOB_MODEL` is asked directly in {raw}; use "
        f"`handover.claim_capable` so there is one answer")


def test_kubernetes_waits_for_its_pod_instead_of_using_the_tar():
    """THE DIRECTIVE. On GKE verification happens in the verification
    pod -- that is what it is for. A claim-capable substrate that cannot
    verify there HOLDS, with the reason on the row, rather than quietly
    producing a verdict from a second implementation.

    Executions 211, 217 and 218 all went green through the tar path,
    months apart, and nothing said the designed path had not run.
    """
    src_ = _src("kernel/verifier/rebuild.py")
    tree = ast.parse(src_)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_claim_row":
            continue
        body = ast.unparse(node)
        assert "claim_capable" in body, (
            "_claim_row no longer distinguishes a claim-capable "
            "substrate, so Kubernetes can silently verify through the "
            "tar path again")
        assert "_hold" in body, (
            "_claim_row has no hold; a substrate that cannot reach its "
            "verification pod falls through to a verdict instead of "
            "waiting")
        return
    raise AssertionError("_claim_row is gone; re-point this check")


def test_docker_and_e0_still_take_the_tar():
    """The other direction, and it must not break: there is no claim
    there, so the tar is not a fallback but the only thing possible.
    Holding would wedge every local and corpus run."""
    from kernel.verifier.handover import claim_capable

    assert claim_capable(_Pod()) is True
    assert claim_capable(_Docker()) is False


def test_attach_waits_for_the_pod_to_answer():
    """MEASURED ON 218. `attach` returned the moment the pod object was
    created, `_refresh` recorded `env_key` on the next line, the row was
    judgeable four seconds later, and a pod that young has no address --
    so `warm_for` refused it and the tar ran against a volume that was
    built and unused."""
    tree = ast.parse(_src("kernel/verifier/handover.py"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "attach":
            continue
        calls = {c.func.id if isinstance(c.func, ast.Name)
                 else getattr(c.func, "attr", "")
                 for c in ast.walk(node) if isinstance(c, ast.Call)}
        assert "await_ready" in calls, (
            "attach does not wait for the pod, so the first verification "
            "after every prepare races it and loses")
        return
    raise AssertionError("attach is gone; re-point this check")


def test_a_hold_writes_a_reason_a_person_can_read():
    """7b: "nothing waits without a reason". A run waiting on a
    verification pod with an empty `stall_reason` is the silence that
    made eight dead buckets invisible for days."""
    tree = ast.parse(_src("kernel/verifier/rebuild.py"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_hold":
            continue
        body = ast.unparse(node)
        assert "stall_reason" in body
        assert "awaiting_verification_pod" in body
        return
    raise AssertionError("_hold is gone; re-point this check")


def test_the_verifying_path_asks_for_the_manifest_before_it_provisions():
    """THE SAME ORDERING `_overlap` HAS, and `_claim_row` did not.

    Provisioning above `_committed` gave a claim and a pod to every run
    that reaches VERIFYING -- including the ones with nothing to
    install: a docs story, an errand, any repository declaring no
    dependencies. Each is a 10GiB ReadWriteOncePod disk against W1's
    120-claim ceiling, for an environment no prepare Job will ever
    build. The run then took the tar path regardless, because
    `warm_for` needs an `env_key` only a prepare writes.

    Found by tracing my own change rather than by a failure: nothing
    breaks, it just quietly spends a disk per non-coding story.
    """
    src = _src("kernel/verifier/rebuild.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_claim_row":
            continue
        at = {}
        for c in ast.walk(node):
            if isinstance(c, ast.Call):
                n = (c.func.id if isinstance(c.func, ast.Name)
                     else getattr(c.func, "attr", ""))
                if n in ("_committed", "provision") and n not in at:
                    at[n] = c.lineno
        assert "_committed" in at and "provision" in at, at
        assert at["_committed"] < at["provision"], (
            f"_claim_row provisions at line {at['provision']} before it "
            f"knows at line {at['_committed']} whether there is anything "
            f"to install -- a claim per no-manifest story")
        return
    raise AssertionError("_claim_row is gone; re-point this check")
