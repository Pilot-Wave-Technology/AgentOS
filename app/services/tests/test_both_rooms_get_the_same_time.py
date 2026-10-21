"""How long a setup may take, and the three numbers that must agree.

`setups.py` promises that the executor and the Verifier run the same
committed text: "because both rooms build from the same committed text,
the executor cannot end up with an environment the Verifier cannot
rebuild. That is the whole fix for 'the checks fail for a reason that is
not the work'."

THE PROMISE WAS HALF KEPT. Same text, different clocks:

    executor init container   SETUP_BUDGET_S       1860s
    Verifier prepare Job      DEFAULT_TIMEOUT_S     900s

Less than half, for the same installs. A repository whose dependencies
take twenty minutes worked for the executor and timed out in
verification -- the exact failure the contract exists to prevent, caused
by the contract's own two implementations disagreeing about a number.

AND IT SHRANK AS A STORY GREW. `prepare_script` emits one `sh -c` that
runs every mount's setup in sequence under `set -e`, so a five-repo
story did five installs inside one 900s budget. It was a per-story
number pretending to be a per-install one.

THE THIRD NUMBER IS THE DANGEROUS ONE. `verify_state.STALE_BUILD_S`
releases a build claim on the reasoning that its Job "has long since hit
its deadline". True at 900s against 1800. The moment a prepare may
legitimately run longer, that reaper releases the claim of a Job still
writing to the disk, `claim_build` lets a second Job take it, and two
prepares attach one ReadWriteOncePod volume -- which `pvc_manifest` chose
RWOP to make the API server refuse, so the second sits Pending while the
execution waits on an environment nobody is building.

A prepare Job is a KUBERNETES Job: it outlives the verifier that
submitted it. So the reaper cannot be tuned to "how long a restart
takes"; it has to exceed the longest build any story may ask for.
"""
from __future__ import annotations

import pytest

from kernel.bridge import limits
from kernel.verifier import setups, verify_state


def test_the_two_rooms_get_the_same_budget_for_one_mount():
    """The contract is about the same text run twice. Two different
    clocks is the same defect as two different texts."""
    from kernel.conductor.unscheduled import SETUP_BUDGET_S

    assert SETUP_BUDGET_S == setups.budget_for(1), (
        f"the executor gets {SETUP_BUDGET_S}s and the Verifier "
        f"{setups.budget_for(1)}s for the one committed command they "
        f"both run")


def test_the_budget_scales_with_the_number_of_installs():
    """One Job, N setups, `set -e`, sequential. A flat number gets
    tighter the more repositories a story has -- exactly backwards."""
    one, three = setups.budget_for(1), setups.budget_for(3)

    assert three > one, (
        "a three-repo story gets no more time than a one-repo story, so "
        "the budget shrinks per install as the story grows")
    assert three == pytest.approx(setups.BUDGET_S * 3)


def test_there_is_a_ceiling_and_a_floor():
    """A prepare holds an RWOP claim for its whole run and the warm pod
    cannot attach until it exits, so this cannot be unbounded -- the
    contract is authored by the model whose work is being checked."""
    assert setups.budget_for(0) == setups.BUDGET_S, "no floor"
    assert setups.budget_for(1000) == setups.CEILING_S, "no ceiling"


def test_the_budget_has_one_origin():
    """`limits.JOB_DEFAULT_S` is already "how long an install takes when
    nobody declared a number". Restating it would have been a third copy
    of one fact, and the third copy is the one that drifts."""
    assert setups.BUDGET_S == limits.JOB_DEFAULT_S + setups.OVERHEAD_S


# ── the coupling nothing else would catch ──────────────────────────────

def test_the_stale_reaper_cannot_outrun_a_legitimate_build():
    """THE INVARIANT, and it is a cross-module one: raising the prepare
    budget without raising this releases the claim of a Job that is
    still writing, and two prepares meet on one RWOP disk.

    Pinned as a RELATION, not a number, so it keeps holding when either
    side moves.
    """
    assert verify_state.STALE_BUILD_S > setups.CEILING_S, (
        f"a build may run for {setups.CEILING_S:.0f}s and its claim is "
        f"released as stale after {verify_state.STALE_BUILD_S:.0f}s -- "
        f"so a live prepare Job loses its claim and a second one starts "
        f"against the same ReadWriteOncePod volume")


def test_the_margin_is_deliberate_and_not_incidental():
    """The gap covers the Job's teardown and the cluster's scheduling
    latency, neither of which is instant when the deadline lands."""
    assert verify_state.STALE_BUILD_S == (setups.CEILING_S
                                          + verify_state.STALE_MARGIN_S)
    assert verify_state.STALE_MARGIN_S > 0


def test_a_declared_check_budget_still_outranks_the_prepare_ceiling():
    """Different things: `budget.MAX_S` is how long the CHECKS may run,
    this is how long building their environment may take. They are
    allowed to be equal; what must not happen is the environment budget
    silently exceeding what a criterion is ever given."""
    from kernel.verifier import budget

    assert setups.CEILING_S <= budget.MAX_S
