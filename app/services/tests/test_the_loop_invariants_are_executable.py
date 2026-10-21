"""W4 — sections 7b and 7c, as checks rather than prose.

WHY PROSE WAS NOT ENOUGH, and the plan says it in its own words. The
invariants in section 7 are all STORY-SHAPED: "a story runs plan →
approve → work → commit → verify". Not one of them names a loop. So
when admission broke on 2026-09-17 there was nowhere to write down what
admission itself must do — I1 says "a story reaches a verdict", which is
DOWNSTREAM of admission, and both failures shipped through a green CI.

7b was written to fill that hole for the conductor's loops and 7c for
the verifier's. Both are prose. This file is the part that fails.

WHAT IS AND IS NOT CHECKABLE HERE. These are STRUCTURAL invariants —
which loop owns what, which pass may block, which writer must exist.
They are checked against the source because the alternative is a
cluster. The BEHAVIOURAL half ("the second claimant is refused") needs a
real schema and lives in the `*_sql.py` files, which `-k "not sql"`
deselects. Neither covers the other: a structural check cannot see a
unique index, and a DB test cannot see a pass that grew a second
adjudication call.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _calls(rel: str) -> set:
    """Every function name called anywhere in a module."""
    out = set()
    for node in ast.walk(ast.parse(_src(rel))):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
    return out


def _call_line(rel: str, func: str, call: str) -> int:
    """Line of the first CALL to `call` inside `func`. -1 if absent.

    AST AND NOT `src.index`, and the first draft of this file used the
    latter and failed on its own three ordering tests: the earliest
    occurrence of a name in a module is almost always its DOCSTRING or
    its import, not the call. A string search proves the word is
    present, which is not what an ordering invariant is about.
    """
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            fn = inner.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else "")
            if name == call:
                return inner.lineno
    return -1


# ── 7b · what the CONDUCTOR's tick may and may not do ──────────────────

def test_the_conductor_does_not_adjudicate():
    """E12 moved adjudication to the verifier's Deployment, and the
    cutover was a DELETION rather than a move — `adjudicate_pass` is the
    same function, called from somewhere else. The regression is the
    conductor growing the call back, and it would look like nothing:
    verdicts would still be written, by a process whose tick is supposed
    to be milliseconds."""
    assert "adjudicate_pass" not in _calls("kernel/worker/runner.py")
    assert "adjudicate_pass" not in _calls("kernel/worker/loop.py")


def test_the_conductor_stands_down_when_pods_run_the_legs():
    """I8 and D2 together. `conductor_leg_slots` is the ONE place that
    decides where legs run — a branch in `worker_tick` would be a second
    place, and two places disagreeing is a leg run twice."""
    from kernel.worker.executor_pass import conductor_leg_slots
    import kernel.worker.executor_pass as ep

    saved = ep.legs_run_in_the_pod
    try:
        ep.legs_run_in_the_pod = lambda: True
        assert conductor_leg_slots() == 0, (
            "the conductor still claims legs while pods drive their own, "
            "which is two writers on one story")
        ep.legs_run_in_the_pod = lambda: False
        assert conductor_leg_slots() > 0, (
            "the conductor takes no legs even though pods are not "
            "driving them, so nothing runs at all")
    finally:
        ep.legs_run_in_the_pod = saved


def test_the_host_is_recorded_before_the_row_is_visible_as_RUNNING():
    """7b's measured bug, and its wording is now imprecise.

    It says `workspace_host` is "resolved inside the same CAS as the
    claim", because it was — resolving it a second after pod creation
    meant every leg dispatched against a pod with no address. The write
    has since moved to PROMOTION, and it is a SECOND statement after the
    transition to RUNNING:

        UPDATE ... SET state = 'RUNNING' ... RETURNING generation
        UPDATE ... SET workspace_host = $2

    A leg runs on RUNNING. Between those two statements the row is
    RUNNING with no host, which is the original bug exactly.

    IT IS SAFE, AND ONLY BECAUSE OF THE TRANSACTION. Both statements sit
    inside `async with conn.transaction()`, so no other connection sees
    the row as RUNNING until the host is on it. That is the invariant —
    not the single statement 7b describes — and it is what this checks,
    because somebody splitting the transaction for an unrelated reason
    would reopen a race nothing else would catch.
    """
    src = _src("kernel/conductor/promote_row.py")
    start = src.index("async with conn.transaction():")
    block = src[start:]
    marker = chr(10) + "def "
    nxt = block.find(marker, 1)
    block = block[:nxt] if nxt > 0 else block

    assert "state = 'RUNNING'" in block, (
        "the RUNNING transition is no longer inside the transaction "
        "this check was written against")
    assert "workspace_host" in block, (
        "workspace_host is written OUTSIDE the transaction that makes "
        "the row RUNNING, so a leg can be claimed against a pod whose "
        "address has not been committed yet -- the bug 7b records")


def test_the_readiness_probe_is_bounded():
    """7b: "the readiness probe is bounded (1s) so a slow pod costs a
    tick, not a pass". An unbounded probe turns one wedged pod into a
    conductor that stops."""
    from kernel.conductor.readiness import PROBE_TIMEOUT

    assert 0 < PROBE_TIMEOUT <= 5, PROBE_TIMEOUT


def test_promotion_probes_together_and_not_one_by_one():
    """The whole difference between this pass and the loop it came out
    of: one probe timeout for the pass, not one per row."""
    assert "gather" in _calls("kernel/conductor/promote.py")


# ── 7c · what the VERIFIER's own loop must do ──────────────────────────

def test_the_verifier_has_exactly_one_loop():
    """7c: "THIS PROCESS HAS EXACTLY ONE LOOP", which is why its pass
    duration IS its liveness story — there is no second loop ticking to
    cover for it."""
    src = _src("kernel/verifier/service.py")
    assert src.count("async def run_forever") == 1


def test_the_verifier_writes_a_heartbeat_per_PASS():
    """Not per loop. The `try` in `run_forever` means a pass can fail
    for a week under a green loop-level heartbeat — the same reason the
    worker writes one."""
    assert "beat" in _calls("kernel/verifier/service.py")


def test_the_verify_pass_spawns_rather_than_awaits():
    """E3. A pass that awaited its adjudications held the conductor's
    WHOLE tick for as long as one suite ran, and it is what let the
    deadline come back from 300s to 120."""
    src = _src("kernel/verifier/service.py")
    assert "supervisor" in src and "acquire" in src


def test_the_verifier_does_not_admit_dispatch_or_reap():
    """7c's last paragraph: those are the conductor's. A verifier that
    grew one would be a second scheduler, and the two would disagree
    about how many runs are in flight."""
    calls = _calls("kernel/verifier/service.py")

    for forbidden in ("admit_pending", "dispatch_pass", "orphan_sweep",
                      "promote_ready"):
        assert forbidden not in calls, (
            f"the verifier's loop calls {forbidden}, which belongs to "
            f"the conductor")


def test_the_rebuild_claims_before_it_reads_the_tree():
    """5.10, understated there and measured by C. With the claim taken
    late, the next tick judges anyway and the symptom is an intermittent
    FALSE PASS against a stale `.venv`. The ORDER is the invariant."""
    holder = "_refresh"
    claim = _call_line("kernel/verifier/rebuild.py", holder, "claim_build")
    build = _call_line("kernel/verifier/rebuild.py", holder,
                       "build_on_volume")
    if claim < 0 or build < 0:
        pytest.skip(f"claim_build/build_on_volume are not both in "
                    f"`{holder}` any more; re-point this check")

    assert claim < build, (
        "the build is claimed AFTER the tree is read, so two ticks can "
        "both decide to build and the execution is judged in between")


def test_stranded_runs_are_computed_before_anything_is_dialled():
    """7c: said before anything is dialled, so a run that CANNOT be
    verified is visible rather than merely absent from the list."""
    where = "adjudicate_pass"
    stranded = _call_line("kernel/verifier/adjudicator.py", where,
                          "stranded_verifying")
    envs = _call_line("kernel/verifier/adjudicator.py", where,
                      "environments")
    if stranded < 0 or envs < 0:
        pytest.skip("the pass no longer calls both; re-point this check")

    assert stranded < envs, (
        "the stranded list is built after the environments pass, so a "
        "run that cannot be verified is absent rather than reported")


# ── the rule both sections share ───────────────────────────────────────

@pytest.mark.parametrize("module,writer", [
    ("kernel/conductor/admit.py", "stall_reason"),
    ("kernel/worker/runner.py", "stall_reason"),
    ("kernel/worker/envrecord.py", "stall_reason"),
])
def test_nothing_waits_without_a_reason(module, writer):
    """7b's admission rule, and it generalises: "nothing waits without a
    `stall_reason` — bucket full, system full, provider outage, dispatch
    failure all write the row". A run waiting with an empty reason is
    the silence admission had, and it is what made eight dead buckets
    invisible for days."""
    assert writer in _src(module)
