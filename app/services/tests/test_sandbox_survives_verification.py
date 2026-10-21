"""A live sandbox survives verification and the acceptance park.

WHAT IT USED TO DO. The reconciler destroyed a VERIFYING execution's
container unconditionally, on the reasoning that "the Verifier runs its
own ephemeral container against the COMMITTED tree, so the executor's
sandbox is dead weight from the moment the claim is made". True of
adjudication and false of everything after it:

  a refused claim returns VERIFYING -> RUNNING, so every rejection paid
  for a full pod rebuild

  a PASS parks the run on an acceptance -- "accept as done, or describe
  what more this story should do" -- and the person was asked that with
  no workspace to look at, because the file explorer reads the pod and
  the pod had been destroyed

`kernel/conductor/reclaim.py` argues this in its own docstring and the
reconciler tore it down anyway. Errands never reach the branch at all:
their self-check runs in-leg and they park straight onto acceptance,
keeping their sandbox. So a story and an errand behaved differently for
no reason either could name.

THE POD NOW GOES WHEN THE RUN DOES: marked done, reviewed, or the run
cancelled -- terminal states, handled below in the same pass, and by
the reclaim sweep for the claim underneath it.

AND THE LEAK THE BRANCH WAS WRITTEN FOR STAYS CLOSED. That was a
STOPPED container in VERIFYING that neither loop touched -- eight of
them on a laptop. `has_compute` is false for exactly those, and
clearing `container_id` hands them to the orphan sweep, which is what
removed them. Destroying LIVE sandboxes was never the part that fixed
it.
"""
from __future__ import annotations

import inspect

import pytest

from kernel.conductor import reconcile


def _branch() -> str:
    """The VERIFYING branch, comments stripped -- read what RUNS."""
    src = inspect.getsource(reconcile)
    start = src.index('if state == "VERIFYING":')
    end = src.index("# ── Nothing to do, said explicitly", start)
    return "\n".join(line.split("#")[0] for line in src[start:end].splitlines())


def test_verification_does_not_destroy_the_sandbox():
    """THE FIX. The panel is blank without it and a rejection rebuilds."""
    assert "destroy" not in _branch(), (
        "a VERIFYING execution's pod is destroyed, so the acceptance "
        "card asks a person to judge work whose tree they cannot open, "
        "and a refused claim pays for a full rebuild")


def test_a_live_pod_keeps_its_reference():
    """Nulling container_id on a LIVE pod hands it to the orphan sweep,
    which would reap the very thing this branch decided to keep."""
    branch = _branch()
    assert "if not has_compute:" in branch, (
        "the reference must be cleared only when the compute is already "
        "gone -- otherwise the pod is kept and then reaped as an orphan")


def test_a_dead_one_is_still_cleaned_up():
    # The original leak: a stopped container in VERIFYING, touched by
    # neither loop. Clearing the reference is what hands it to the sweep.
    assert "container_id = NULL" in _branch()


def test_the_slot_is_still_held_and_the_lease_released():
    branch = _branch()
    assert "execution_leases" in branch, "the lease must still be released"
    # Holding the slot is the absence of a release; asserted by the
    # detail text the pass reports, which a reader of the summary sees.
    assert "slot HELD" in inspect.getsource(reconcile)


def test_the_summary_no_longer_claims_a_teardown():
    """It said "container destroyed" because it did. A line that keeps
    claiming an action nobody performs is one somebody reads instead of
    looking."""
    lines = [line.split("#")[0]
             for line in inspect.getsource(reconcile).splitlines()]
    assert "container destroyed" not in chr(10).join(lines)


@pytest.mark.asyncio
async def test_terminal_states_still_take_the_pod():
    """The other half of the promise: kept until the run ends, not for
    ever. `DONE`, `FAILED` and `CANCELLED` are handled in the same pass
    and destroy whatever is still live."""
    src = inspect.getsource(reconcile)
    assert "terminal_states" in src
    assert "await driver.destroy(cid, stop_timeout=0)" in src, (
        "nothing destroys a terminal execution's pod any more, so the "
        "sandbox kept through verification is now kept for ever")
