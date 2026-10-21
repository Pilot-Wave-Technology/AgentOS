"""Every clock that bounds another one, checked against it.

THE CLASS OF BUG. None of these numbers is wrong on its own. They go
wrong in PAIRS, where one bounds another and nobody wrote the relation
down: a slice longer than the bridge read, a job ceiling under the
foreground cap, a leg shorter than the job it waits for. Each pair fails
as something else entirely -- "the bridge is unreachable", "the tool
refuses and sends me somewhere worse", "the leg ended for no reason" --
so the measurement never names the clock that caused it.

Two of these were live. `wait_slice_s` was declared at 120 under a 45
second bridge read, and `lease_clock_seconds` (600) sat under
`job_max_s` (7200), so any wait longer than ten minutes returned its
answer and lost the leg in the same instant.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from kernel import timings
from kernel.bridge import limits

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "kernel" / "bridge"))


# -- the sandbox's own chain -------------------------------------------

def test_a_wait_slice_fits_inside_the_bridge_read():
    """Otherwise every slice times out as a dead bridge, and the
    worker's reachability check destroys a pod that was only waiting."""
    tools_jobs = pytest.importorskip("tools_jobs")
    assert tools_jobs.wait_slice_s() < limits.WAIT_BRIDGE_S
    for profile in timings.PROFILES:
        assert timings.values(profile)["wait_slice_s"] < limits.WAIT_BRIDGE_S


def test_a_foreground_bash_fits_inside_its_own_bridge_read():
    """`bridge_timeout` adds a margin for the same reason."""
    assert limits.bridge_timeout("bash", {"timeout": limits.BASH_MAX_S}) > \
        limits.BASH_MAX_S


def test_the_job_ceiling_stays_above_the_foreground_cap():
    """Below it, `bash_timeout` refuses a long call with "use
    bash_background" while bash_background has the SHORTER ceiling."""
    assert limits.JOB_MAX_S > limits.BASH_MAX_S
    for profile in timings.PROFILES:
        assert timings.values(profile)["job_max_s"] > limits.BASH_MAX_S


def test_a_poll_answers_faster_than_its_own_bridge_read():
    assert limits.bridge_timeout(limits.JOB_POLL, {}) >= limits.POLL_MAX_S


def test_the_busy_refusal_arrives_before_any_caller_gives_up():
    """A 409 that took longer than the caller's timeout is a timeout."""
    import shim

    assert shim._BUSY_AFTER_S < limits.BRIDGE_DEFAULT_S


# -- the fingerprint, which holds the tool lock ------------------------

def test_a_fingerprint_finishes_before_its_caller_stops_waiting():
    """It holds the shim's `_TOOL_LOCK` for its whole run. A budget
    longer than the caller's HTTP timeout leaves the sandbox answering
    409 BUSY to everything with nobody waiting for the result."""
    import inspect

    import treefp

    from kernel.harness import claim

    caller = inspect.getsource(claim.take)
    assert "timeout=180" in caller, "the caller's timeout moved; re-check"
    assert treefp.TOTAL_BUDGET_S < 180


def test_the_fingerprint_budget_is_total_and_not_per_command():
    """Three git commands per repository across N repositories is how a
    per-command timeout becomes twelve minutes."""
    import inspect

    import treefp

    assert not hasattr(treefp, "GIT_TIMEOUT_S"), (
        "a per-command timeout is back; it multiplies by repositories")
    assert "budget" in inspect.signature(treefp.tree_sha).parameters


def test_the_fingerprint_gives_up_rather_than_answering_half(tmp_path):
    """Partial is worse than absent: a fingerprint over some of the
    repositories would compare equal to a different tree."""
    import treefp

    exhausted = treefp._Budget(total=-1.0)
    assert exhausted.spent()
    assert exhausted.slice_s() >= treefp.MIN_COMMAND_S, (
        "a zero-second timeout is a guaranteed failure dressed as a "
        "measurement")


# -- the leg, and the jobs it waits for --------------------------------

def test_time_blocked_in_a_wait_is_not_charged_to_the_leg():
    """`lease_clock_seconds` is 600 and `job_max_s` is 7200. Charged,
    any wait past ten minutes returns its answer and loses the leg in
    the same instant -- the model never acts on what it waited for."""
    import inspect

    from kernel.harness import loop_endings

    src = inspect.getsource(loop_endings.leg_ending)
    assert "_blocked_s" in src
    assert "start_time + blocked" in src


def test_every_exit_from_the_wait_loop_credits_the_time():
    """A credit nothing feeds is a credit of zero -- and a credit on
    only the happy path is worse: a wait that ends on a bridge error
    would leave the whole blocked span charged to the leg, so the
    failure case is the one that loses its leg."""
    import ast
    import inspect
    import textwrap

    from kernel.harness.executor import BridgeToolExecutor

    src = textwrap.dedent(inspect.getsource(
        BridgeToolExecutor._wait_for_job))
    assert "blocked_from = time.monotonic()" in src

    tree = ast.parse(src)
    loop = next(n for n in ast.walk(tree) if isinstance(n, ast.While))
    returns = [n for n in ast.walk(loop) if isinstance(n, ast.Return)]
    assert returns, "the wait loop never returns"

    # Every `return` inside the loop must be immediately preceded by the
    # credit, in the same block.
    for block in [b for n in ast.walk(loop)
                  for b in (getattr(n, "body", []), getattr(n, "orelse", []))]:
        for i, node in enumerate(block):
            if not isinstance(node, ast.Return):
                continue
            assert i > 0 and "_blocked_s" in ast.dump(block[i - 1]), (
                f"a return at line {node.lineno} of the wait loop does "
                f"not credit the blocked time first")


def test_the_credit_only_covers_waiting():
    """Everything the lease clock guards against -- a model looping, a
    provider hanging, a leg that will not converge -- stays charged."""
    import inspect

    from kernel.harness.executor import BridgeToolExecutor

    whole = inspect.getsource(BridgeToolExecutor)
    loop = inspect.getsource(BridgeToolExecutor._wait_for_job)
    # COUNTED AGAINST THE LOOP, not against a fixed number. The invariant
    # is "credited only where the leg is genuinely blocked", and the wait
    # loop grew a third return when the mailbox peek landed -- a literal
    # 2 here would have made adding a legitimate return look like a
    # violation, which is the opposite of what this guards.
    assert whole.count("self._blocked_s +=") == loop.count(
        "self._blocked_s +="), (
        "blocked time is credited somewhere other than the wait loop")
    assert loop.count("self._blocked_s +=") >= 2


def test_the_lease_clock_still_fires_on_a_leg_that_is_not_waiting():
    import time

    from kernel.harness.predicates import check_lease_clock

    assert check_lease_clock(time.monotonic() - 700, 600.0).fired
    assert not check_lease_clock(time.monotonic() - 100, 600.0).fired


def test_a_credited_leg_survives_a_wait_longer_than_its_lease():
    """The whole point, as arithmetic."""
    import time

    from kernel.harness.predicates import check_lease_clock

    started = time.monotonic() - 2400          # a forty-minute leg
    blocked = 2350.0                           # almost all of it waiting
    assert check_lease_clock(started, 600.0).fired
    assert not check_lease_clock(started + blocked, 600.0).fired


# -- the conductor does not reap a run that is merely slow -------------

def test_the_pending_detector_never_touches_a_running_execution():
    """A forty-minute `bash_wait` is one step and journals nothing.
    Anything sweeping on silence would take the pod out from under it."""
    import inspect

    from kernel.conductor import admit

    src = inspect.getsource(admit.report_stuck)
    assert "'PENDING', 'ADMITTED'" in src or '"PENDING", "ADMITTED"' in src
    assert "RUNNING" not in src


def test_the_stuck_detector_is_a_detector():
    import inspect

    from kernel.conductor import admit

    assert "A DETECTOR AND NOT A REAPER" in inspect.getsource(admit)


# -- the token budgets, which collide the same way the clocks do -------

def test_a_leg_budget_leaves_room_for_the_reply():
    """`peak_context` counts `input + cacheRead + cacheWrite` and NOT
    output -- "the reply is generated, not held". So the window a step
    actually needs is the budget PLUS `max_output`, and a budget set to
    the window exactly overflows by the size of the answer.

    A budget above what the model can hold is not a bigger leg: the
    predicate never fires, and the provider errors instead of the
    harness ending the leg cleanly.
    """
    from kernel.harness.models.registry import SUPPORTED

    for spec in SUPPORTED.values():
        assert spec.leg_token_budget + spec.max_output <= spec.max_context, (
            f"{spec.id}: a {spec.leg_token_budget:,} leg plus a "
            f"{spec.max_output:,} reply exceeds its {spec.max_context:,} "
            f"window")


def test_the_two_hundred_k_models_keep_the_smaller_leg():
    """Haiku "ACCEPTS the header and does not get the window -- measured,
    refused at 242,032 tokens". Raising every model together is how that
    note stops being true."""
    from kernel.harness.models.registry import SUPPORTED

    for spec in SUPPORTED.values():
        if spec.max_context <= 200_000:
            assert spec.leg_token_budget <= 100_000, spec.id


def test_the_spend_backstop_stays_above_an_honest_leg():
    """It catches a leg that is cheap per step and grinding, so it only
    means anything while it sits well clear of what a full leg spends.
    Each step re-sends the conversation, so cumulative spend scales with
    the context budget, not with it divided by anything."""
    from kernel.harness.models.config import LegBudget
    from kernel.harness.models.registry import SUPPORTED

    biggest = max(s.leg_token_budget for s in SUPPORTED.values())
    assert LegBudget().spend_tokens >= 5 * biggest, (
        "the cost backstop would end healthy legs and read as a cost "
        "problem")


def test_an_undeclared_check_gets_more_than_a_foreground_command():
    """`run_check` exists so a check can outlive `bash`'s 120s ceiling.
    A default only 2.5x that ceiling meant the tool for running a LONG
    check barely ran a longer one."""
    from kernel.verifier.budget import DEFAULT_S

    assert DEFAULT_S >= 5 * limits.BASH_MAX_S


def test_the_default_check_budget_sits_inside_its_own_ceiling():
    from kernel.verifier.budget import DEFAULT_S, MAX_S, MIN_S

    assert MIN_S < DEFAULT_S < MAX_S


def test_a_declared_budget_still_beats_the_default_both_ways():
    """Raising the default must not make it a floor: a check that asks
    for less is a check somebody thought about."""
    from kernel.verifier.budget import DEFAULT_S, timeout_for

    assert timeout_for({}, {"spec": {"timeout_s": 60}}) == 60
    assert timeout_for({}, {"spec": {"timeout_s": 3600}}) == 3600
    assert timeout_for({}, {"spec": {}}) == DEFAULT_S


def test_the_schema_tells_the_model_the_real_default():
    """A model told the default is 300 declares a budget it does not need,
    or skips one it does. The number in the prose has to be the number in
    the code."""
    from kernel.tools.catalog import CATALOG
    from kernel.verifier.budget import DEFAULT_S

    text = str(CATALOG["register_plan"])
    assert f"{int(DEFAULT_S)}s default" in text, (
        "the register_plan schema names a default the code does not use")
