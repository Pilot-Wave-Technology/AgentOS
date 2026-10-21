"""Run 345: GO cancelled a healthy run 80 seconds into DISPATCHING.

The record, from `execution_transitions` and `proposals`:

    09:40:50  ADMITTED -> DISPATCHING
    09:41:18  first check-in fires (window configured: 600s)
    09:41:24  GO asks "running, or waiting for pod scheduling?"
    09:41:49  GO cancels -- 25s later, before any answer could exist
    09:42:10  DISPATCHING -> CANCELLED

Two independent defects, one per half of this file.
"""
from app.services import errand_checkin


# ── 1. the window was ignored on the first check-in ──────────────────

def test_a_brand_new_run_is_not_due_immediately():
    """`quiet_s` returns None for a run never checked in, and the gate
    read `if quiet is not None and quiet < window` -- so None fell
    through and the first check-in fired on the next sweep.

    Run 345 was 28 seconds old against a 600 second window."""
    assert not errand_checkin.is_due(None, age_s=28, window=600)


def test_the_first_checkin_waits_out_the_window():
    """And then it does fire -- the point is the delay, not silence."""
    assert errand_checkin.is_due(None, age_s=600, window=600)
    assert errand_checkin.is_due(None, age_s=1200, window=600)


def test_the_window_would_have_saved_run_345():
    """The counterfactual, because it is the whole argument for the fix:
    honouring 600s puts GO's first look at 09:50:50, by which time a pod
    Unschedulable at 09:40:50 has long been placed -- `promote_row`
    documents ninety seconds as the normal ceiling."""
    assert not errand_checkin.is_due(None, age_s=80, window=600)


def test_a_run_already_checked_in_uses_its_quiet_clock():
    """Age must not leak in once there IS a check-in history, or a long
    errand would be permanently overdue on its age alone."""
    assert not errand_checkin.is_due(30, age_s=99999, window=600)
    assert errand_checkin.is_due(601, age_s=99999, window=600)


def test_a_missing_age_does_not_crash_the_sweep():
    """One errand must not stop the others."""
    assert not errand_checkin.is_due(None, age_s=None, window=600)


# ── 2. a queued run was described as a dead one ──────────────────────

def test_pre_start_states_are_the_ones_with_nothing_to_stall():
    assert errand_checkin.PRE_START == ("PENDING", "ADMITTED", "DISPATCHING")
    assert "RUNNING" not in errand_checkin.PRE_START


# ── 3. what GO is taught, so it does not re-derive any of this ───────

import io as _io          # noqa: E402
import pathlib as _pl     # noqa: E402

_ROOT = _pl.Path(__file__).resolve().parents[3]


def _go():
    return _io.open(_ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()


def _assistant():
    return _io.open(_ROOT / "kernel/assistant/AGENTS.md",
                    encoding="utf-8").read()


def test_go_is_taught_that_waiting_to_start_is_not_failing():
    """The refusal in `run_control` stops the cancel. It does not stop GO
    spending a turn concluding the run is dead and telling somebody so."""
    go = _go()
    assert "### Waiting to start is not failing" in go
    assert "does not begin the moment it is approved" in go
    assert "THERE IS NOTHING TO ASK AND NOTHING TO STOP" in go
    assert "E_NOT_STARTED" in go


def test_go_is_told_why_asking_the_executor_cannot_help_yet():
    """GO's instinct on run 345 was right -- ask before acting. The trap
    is that the silence of a question nobody can answer reads as more
    evidence of a dead run."""
    go = _go()
    assert "the executor does not exist until the pod does" in go


def test_go_is_taught_an_errand_is_not_spent_by_one_run():
    go = _go()
    assert "the grant is not spent by one run" in go
    assert "ONE AT A TIME, and that is enforced, not requested" in go
    assert "A run count is history, not a budget" in go


def test_go_is_told_a_uid_is_not_a_field_it_fills_in():
    """The provenance fix records GO as the actor, which makes this
    visible. The rule is what stops it happening."""
    go = _go()
    assert "IS A PERSON'S DECISION, NOT A FIELD YOU FILL IN" in go
    assert "Pass a uid only when that person has, in this" in go


def test_the_assistant_learns_the_same_two_things():
    """Both prompts describe errands to people, so a correction in one
    and not the other just moves which agent says the wrong thing."""
    a = _assistant()
    assert "it is not used up by running" in a
    assert "One run at a time" in a
    assert "IS YOURS, AND IT IS NOT THE CANCEL BUTTON" in a
    assert "An errand runs ONCE" not in a
