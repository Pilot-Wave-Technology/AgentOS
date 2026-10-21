"""D2: the leg runs in the pod, and its result survives the trip back.

The seam this pins is not "a dict became JSON". It is that the two ends
of that dict are now in different CONTAINERS, built from the same tree
but not restarted together: a pod dispatched at 10:00 outlives a
conductor rollout at 10:01, which is the whole reason `leg_lock` had to
move into the pod (plan section 10). So most of what follows is about
DISAGREEMENT -- a reader on an old build, a stream with something else
in it, a runner that died before it could say anything -- because those
are the cases a hand-rolled `json.loads(stdout)` answered wrongly and
silently.

AND THE SEAM ITSELF, WHICH IS AN ORDERING RATHER THAN A FUNCTION. The
lock is taken before anything is spent, and only then is a model called.
Both facts are asserted from the source rather than by running a leg,
because the alternative is a test that needs a database, a provider and
a pod -- and a check nobody can run is a check that stops being run. D2's
four inherited items are in `test_the_pod_fans_out_over_its_own_pool.py`;
what D4 adds inside this same lock is in
`test_the_kill_switch_reaches_the_pod.py`.

WHAT THIS CANNOT PROVE, said here rather than implied by its absence: a
wire format is a string, so none of it shows that a pod actually ran a
leg. Nothing starts `leg_runner` in a pod yet and `legs_run_in_the_pod()`
is False in every environment that exists.
"""

from __future__ import annotations

import ast
import io
import pathlib

import pytest

from kernel.harness.loop_result import refusal
from kernel.harness.loop_wire import (MARKER, WireError, decode, encode,
                                      encode_error)

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# -- the wire ------------------------------------------------------------

def test_a_result_survives_the_round_trip_unprojected():
    """No key list. A caller that reads `stop_reason` by `in` cannot tell a
    key that was never sent from one the format dropped on the way past."""
    result = {"steps": 3, "predicate": None, "proposals": [],
              "invented_later": {"nested": [1, 2]}}
    assert decode(encode(result)) == result


def test_the_line_is_found_among_log_output():
    """stdout is not private: every module under kernel/ logs, and one
    banner line from a provider SDK used to be enough to fail a leg that
    had completed perfectly."""
    noise = ("INFO loading adapters\n"
             + encode({"steps": 1}) + "\n"
             "WARNING deprecation notice\n")
    assert decode(noise) == {"steps": 1}


def test_the_last_marked_line_wins():
    """A retried runner, or a wrapper echoing its child, legitimately
    produces two. The later one describes the leg that finished."""
    both = encode({"steps": 1}) + "\n" + encode({"steps": 9})
    assert decode(both)["steps"] == 9


def test_a_log_line_that_merely_mentions_the_marker_is_not_fatal():
    """The marker is a string. A warning quoting it must not be able to
    fail a leg that succeeded."""
    text = (f"WARNING could not parse the {MARKER} line\n"
            + encode({"steps": 2}))
    assert decode(text)["steps"] == 2


def test_an_old_reader_says_so_instead_of_misreading():
    """The pod and the conductor being on different deploys is EXPECTED,
    which is why the version is checked rather than assumed."""
    ahead = encode({"steps": 1}).replace(f'"{MARKER}": 1', f'"{MARKER}": 2')
    with pytest.raises(WireError) as e:
        decode(ahead)
    assert "different deploys" in str(e.value)


def test_a_runner_that_died_is_not_a_leg_that_did_nothing():
    with pytest.raises(WireError) as empty:
        decode("Traceback (most recent call last):\n  ...\n")
    assert "died before it could report" in str(empty.value)

    with pytest.raises(WireError) as failed:
        decode(encode_error("RuntimeError: no bridge"))
    assert "no bridge" in str(failed.value)


def test_the_error_type_separates_a_deploy_fault_from_a_failed_leg():
    """Collapsing them is how a version skew comes to be reported as
    twelve failed legs."""
    assert issubclass(WireError, RuntimeError)


def test_the_bytes_are_stable_so_a_test_may_assert_on_the_line():
    assert encode({"b": 1, "a": 2}) == encode({"a": 2, "b": 1})


# -- the refusal, which is the shape a pod sends when it runs no leg -----

def test_a_refusal_always_carries_the_key_that_identifies_it():
    """`stop_reason` is the ONE key distinguishing the two shapes, and a
    caller reads its ABSENCE as 'this leg ran'. Three call sites spelling
    the dict by hand is how the third one omits it."""
    for reason in ("busy", "frozen", "cancelled", "preflight_failed"):
        got = decode(encode(refusal(reason)))
        assert got["stop_reason"] == reason
        assert got["steps"] == 0 and got["stopped"] is True
        assert got["proposals"] == []


# -- the lock, and that nothing is spent outside it ----------------------

def _run_body():
    tree = ast.parse(_src("kernel/harness/leg_runner.py"))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            return node
    raise AssertionError("leg_runner.run has moved")


def _calls_under(node) -> list[str]:
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            out.append(func.id if isinstance(func, ast.Name)
                       else getattr(func, "attr", ""))
    return out


def _leg_lock_block(body):
    for node in ast.walk(body):
        if not isinstance(node, ast.AsyncWith):
            continue
        heads = ast.Module(body=[ast.Expr(value=i.context_expr)
                                 for i in node.items], type_ignores=[])
        if "leg_lock" in _calls_under(heads):
            return node
    raise AssertionError("leg_runner.run no longer takes leg_lock")


def test_the_leg_is_inside_the_lock_the_pod_holds():
    """D1 moved the connection into the pod and nothing used it. The leg
    running OUTSIDE that lock is the double-run `lease.py` exists to
    prevent, with the two callers a deploy apart instead of a tick."""
    assert "run_leg" in _calls_under(_leg_lock_block(_run_body()))


def test_the_pool_is_asked_for_one_connection_and_the_lock_is_on_it():
    """Two acquires would be two different claims about who owns this
    execution -- and in a pool of two, one spare fewer than exists."""
    assert _calls_under(_run_body()).count("acquire") == 1
