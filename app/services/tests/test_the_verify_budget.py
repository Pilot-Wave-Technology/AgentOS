"""E3's budget, and a liveness deadline that comes back down.

THERE WERE TWO NUMBERS HERE. A dry run was LATENCY-sensitive -- a person
sat in front of an approval card waiting to be told whether the checks
they were about to approve could tell the difference -- and an
adjudication is THROUGHPUT-sensitive: nobody is watching a verdict
arrive, but it must not take the box. One semaphore would have let a
queue of the second starve the first for as long as the queue lasted, so
E3 gave each its own.

E9 RETIRED THE DRY RUN (plan 5.6) AND `DRYRUN_CONCURRENCY` WITH IT. That
budget existed for exactly one reason -- a dry run spawned Jobs, a
capture (~28s, measured) plus a criterion (~30s), and nothing bounded
how many at once -- so it died the moment they did not. What survives is
`VERIFY_CONCURRENCY`, now the whole of the verifier's budget, and the
argument that brought the 300s liveness deadline back down to the
conductor's 120: a pass must RETURN while its work runs, or the deadline
has to be wider than a Kubernetes Job, and a deadline that lax does not
catch a wedge.

THE SPLIT IS HERE TOO. `adjudicator.py` was 439 code lines, twice the
house ceiling, and it holds four things that were each written for a
measured incident: `verdict_lock`, the `already` guard, the stranded
report, and the parity warnings. The last section asserts that the split
moved code and not behaviour.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest

from kernel.verifier import adjudicator, service
from kernel.worker.supervisor import LegSupervisor

REPO = pathlib.Path(__file__).resolve().parents[3]


def _run(coro):
    """Run, then settle whatever is still in flight.

    Half the point of a bounded pass is that it RETURNS while its work is
    still running, so these tests deliberately leave tasks alive. Closing
    the loop on them prints "Task was destroyed but it is pending!" --
    noise that trains people to ignore test output.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        loop.close()


class _Conn:
    """Enough of a connection for a sweep: it grants advisory locks.

    `adjudicate_pass` takes `verdict_lock` before spending a container,
    and a fake that refuses it makes every spawned task return
    `adjudicating_elsewhere` in microseconds -- which looks exactly like
    a budget that is not being consumed. Granting it is what makes the
    saturation in these tests real.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            return True
        return None

    async def execute(self, sql, *args):
        return None

    async def fetch(self, sql, *args):
        return []


def _acquire():
    return _Conn()


@pytest.fixture()
def slow_verdict(monkeypatch):
    """One adjudication that takes far longer than a tick, because it is
    a criterion container awaited to completion."""
    started = []

    async def _one(conn, driver, row):
        started.append(row["id"])
        await asyncio.sleep(0.25)
        return {"execution_id": row["id"], "action": "verdict"}

    async def _verifying(conn, goal_id=None):
        return [{"id": i} for i in range(100, 110)]

    monkeypatch.setattr(adjudicator, "adjudicate_one", _one)
    monkeypatch.setattr(adjudicator, "verifying", _verifying)
    return started


# THE PASS RETURNING BEFORE ITS CONTAINERS, a running adjudication not
# being restarted by the next tick, and the serial degrade all live in
# `test_adjudication_off_the_tick.py`. This file is about the BUDGET:
# how many may run at once, and what a pass says when it cannot start
# one.


# ── the budget is real, and a saturated pass says so ──────────────

def test_more_claims_than_the_budget_do_not_all_start(slow_verdict):
    """Each adjudication holds a pooled connection (POOL_MAX 16) for the
    length of a criterion container, so ten claims landing together would
    take ten of them and the next pass would wait on a connection that
    was not coming back."""
    sup = LegSupervisor()

    async def go():
        actions = await adjudicator.adjudicate_pass(
            _Conn(), object(), None, acquire=_acquire, supervisor=sup)
        await asyncio.sleep(0.05)
        return actions

    actions = _run(go())
    assert len(slow_verdict) == adjudicator.verify_concurrency() == 6, (
        "%d adjudications started against a budget of %d"
        % (len(slow_verdict), adjudicator.verify_concurrency()))
    assert len(actions) == 6, "the pass claimed more than it started"


def test_a_saturated_pass_says_so_rather_than_going_silent(slow_verdict):
    """Phase S's rule: every skip reports. A claim waiting for a slot must
    not be indistinguishable from one nobody looked at."""
    sup = LegSupervisor()

    async def go():
        await adjudicator.adjudicate_pass(_Conn(), object(), None,
                                          acquire=_acquire, supervisor=sup)
        saturated = await adjudicator.adjudicate_pass(
            _Conn(), object(), None, acquire=_acquire, supervisor=sup)
        # Read INSIDE the loop: `_run` settles the in-flight tasks on the
        # way out, so a slot count taken afterwards counts nothing.
        return saturated, sup.free_slots(adjudicator.verify_concurrency())

    saturated, free = _run(go())
    assert saturated[0]["action"] == "verify_saturated"
    assert free == 0, "adjudication was not actually saturated"


# ── one budget, and it is still overridable without a restart ─────

def test_the_budget_is_its_own_number():
    assert adjudicator.verify_concurrency() == 6


def test_the_budget_is_overridable_without_a_restart(monkeypatch):
    """Read per call, so a soak can widen it without a rollout."""
    monkeypatch.setenv("AGENTOS_VERIFY_CONCURRENCY", "11")
    assert adjudicator.verify_concurrency() == 11


def test_the_budget_does_not_read_an_invisible_default():
    """`os.environ.get(x, default)` is banned under kernel/: a default
    nobody can see is a policy nobody reviewed."""
    for rel in ("kernel/verifier/adjudicator.py",
                "kernel/verifier/service.py"):
        src = (REPO / rel).read_text(encoding="utf-8")
        assert "environ.get(" not in src, rel


def test_the_verifier_loop_has_one_supervisor_and_one_pass():
    """ONE object, and one pass for it to bound. There were two until E9
    took the dry run out -- a second `LegSupervisor` now would be a budget
    for a pass that does not exist, and an empty pool to drain on every
    deploy."""
    src = inspect.getsource(service.run_forever)
    assert src.count("LegSupervisor()") == 1, src
    sig = inspect.signature(service.verify_pass)
    assert list(sig.parameters) == ["pool", "driver", "supervisor"]


def test_the_shutdown_drains_what_is_in_flight():
    """An adjudication is a criterion container already paid for and a
    pooled connection; cancelling one throws away the container and
    leaves a Job nothing is watching. `run_forever` must actually call
    the drain -- the worker's version of this defect was a documented
    function with no caller."""
    src = inspect.getsource(service.drain)
    assert "adjudication(s)" in src
    assert "dry run(s)" not in src, (
        "the dry-run pool retired with the pass at E9")
    assert "drain(supervisor)" in inspect.getsource(service.run_forever)


# ── the deadline, and the arithmetic behind it ────────────────────────

def _verifier_probe():
    yaml = pytest.importorskip("yaml")
    from app.services.tests.test_k8s_spec import _rendered

    for doc in yaml.safe_load_all(_rendered()):
        if not doc or doc.get("kind") != "Deployment":
            continue
        if doc["metadata"]["name"] != "verifier":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] == "verifier":
                return container["livenessProbe"]
    raise AssertionError("no verifier container in the manifest")


def test_the_deadline_came_back_down_to_the_conductors():
    probe = _verifier_probe()
    assert float(probe["exec"]["command"][3]) == 120.0, (
        "the 300s concession was for a SERIAL dry run. A deadline left "
        "generous after the reason for it is gone is how a wedge goes "
        "unnoticed for thirty minutes")


def test_the_deadline_is_still_above_the_worst_case_pass():
    """THE ARITHMETIC, tied to the constants rather than to a comment.

    A pass is now `pool.acquire()` (bounded at ACQUIRE_MAX_S, and past it
    it raises rather than waits) plus ~24 indexed statements plus the
    spawns, which do not await. Three consecutive worst-case passes is
    the headroom this deadline buys, and this process has exactly ONE
    loop -- there is no second one ticking to cover a bad pass.

    If ACQUIRE_MAX_S or the interval ever grows, this fails and the
    deadline gets rethought rather than silently becoming too tight.
    """
    from kernel.worker.service import ACQUIRE_MAX_S, INTERVAL_S

    deadline = float(_verifier_probe()["exec"]["command"][3])
    assert deadline >= 3 * (ACQUIRE_MAX_S + INTERVAL_S)


# ── the split moved code, not behaviour ───────────────────────────────

@pytest.mark.parametrize("rel", [
    "kernel/verifier/adjudicator.py", "kernel/verifier/verdict.py",
    "kernel/verifier/errored.py", "kernel/verifier/second_key.py",
    "kernel/verifier/crashed.py", "kernel/verifier/steer.py",
    "kernel/verifier/service.py",
])
def test_every_file_the_split_left_is_under_the_ceiling(rel):
    """~200 CODE lines, measured rather than guessed: docstrings and
    comment lines do not count, because in this codebase the comments are
    the incident record and shortening them is the wrong economy."""
    import ast
    import io
    import tokenize

    path = REPO / rel
    src = path.read_text(encoding="utf-8")
    doc: set = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            doc.update(range(body[0].lineno, body[0].end_lineno + 1))
    comments = {t.start[0] for t in
                tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type == tokenize.COMMENT}
    code = [i for i, line in enumerate(src.splitlines(), 1)
            if line.strip() and i not in doc
            and not (i in comments and line.strip().startswith("#"))]
    assert len(code) <= 200, f"{rel} is {len(code)} code lines"


@pytest.mark.parametrize("name", ["_apply", "_tell_executor",
                                  "_adjudicator_failed", "ADJUDICATOR",
                                  "adjudicate_one", "verifying",
                                  "stranded_verifying"])
def test_the_import_path_did_not_move_with_the_code(name):
    """`kernel/worker/loop.py` and `finish_pass.py` import
    `_tell_executor` from here, the CI suite imports `_apply`, and
    `test_adjudication_off_the_tick` monkeypatches `_adjudicator_failed`
    ONTO this module -- which only works if the sweep reads it out of
    this module's globals."""
    assert getattr(adjudicator, name, None) is not None


def test_the_sweep_still_resolves_its_helpers_through_this_module():
    """The monkeypatch above is not decorative: `adjudicate_pass` must
    call `verifying` and `_adjudicator_failed` as bare names, not as
    `crashed._adjudicator_failed`, or patching the module does nothing
    and the test that guards the sweep passes vacuously."""
    src = inspect.getsource(adjudicator.adjudicate_pass)
    assert "await verifying(conn, goal_id)" in src
    assert "_adjudicator_failed(own, row, exc)" in src
    assert "_adjudicator_failed(conn, row, e)" in src


def test_the_four_behaviours_the_split_was_warned_about_are_all_here():
    """Each was written for a measured incident and the comments say
    which. This asserts the CODE, in whichever file now holds it."""
    adj = (REPO / "kernel/verifier/adjudicator.py").read_text(
        encoding="utf-8")
    # verdict_lock -- execution 203, claim 56: both replicas adjudicated.
    assert adj.count("verdict_lock(") == 2
    # the `already` guard, and its tail-finishing repair -- KER13.
    assert "already_adjudicated" in adj
    assert "never left VERIFYING" in adj and "finish_passed_claim" in adj
    # the stranded report -- a skip must never be silent.
    assert "async def stranded_verifying" in adj
    assert adj.count("return stranded + actions") == 2
    # the parity warnings -- the only defence against drift is a number.
    assert "concerns(parity)" in adj


@pytest.mark.asyncio
async def test_the_already_guard_still_finishes_an_abandoned_tail(
        monkeypatch):
    """The behaviour, not the string. A claim that PASSED whose run never
    left VERIFYING must be finished, not reported as
    `already_adjudicated` every four seconds for ever -- watched live on
    KER13."""
    finished = []

    async def _load(conn, eid):
        return {"base_image": "x"}, [{"id": 1}]

    async def _claim(conn, eid):
        return 22, 99

    async def _finish(conn, eid, generation, claim_seq, row, summary=None):
        finished.append((eid, claim_seq))
        return {"execution_id": eid, "action": "passed"}

    class _C:
        async def fetchval(self, sql, *args):
            if "FROM verdicts" in sql:
                return True          # the claim passed
            if "state = " in sql and "questions" in sql:
                return 1             # and the run never left VERIFYING
            return None

    monkeypatch.setattr(adjudicator, "load_contract", _load)
    monkeypatch.setattr(adjudicator, "_claim", _claim)
    monkeypatch.setattr("kernel.verifier.finish_pass.finish_passed_claim",
                        _finish)

    got = await adjudicator.adjudicate_one(
        _C(), object(), {"id": 5, "generation": 3})
    assert finished == [(5, 22)], got
    assert got["action"] == "passed"
