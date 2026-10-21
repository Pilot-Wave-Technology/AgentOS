"""The deadline on `awaiting_merge` is load-bearing, so it is tested.

A coding story now holds its pod between push and merge. That is the
feature. The hazard it creates is a state with no exit: the execution is
parked (a healthy state), the story is `awaiting_merge` (a correct
state), nothing is in error, and the pod, the workspace claim and the
admission slot are held for ever. Exactly the shape `reclaim.py` warns
about for the review phase -- "a phase that never ends now holds the
workspace, the slot and the node indefinitely… neither is optional once
this line exists".

THE FIRST VERSION OF THIS FILE TESTED THE WRONG THING, and the test
passing is what hid it. It asserted the janitor moved the story to
`blocked` and never touched compute -- both true of the code, and
together useless, because `reclaim` releases on the story reaching
`done`, `reviewed`, `cancelled` or `rejected`, or on the EXECUTION
reaching `CANCELLED` or a stale `FAILED`. `blocked` is in neither list.
The janitor ran, logged, moved the row, and freed nothing. So these now
test the property that actually matters: that what the janitor writes is
a signal `reclaim` acts on.
"""
from __future__ import annotations

import inspect

from app.services import awaiting_merge_janitor as j


def _reclaim_src() -> str:
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    return (root / "kernel/conductor/reclaim.py").read_text(encoding="utf-8")


def test_what_the_janitor_writes_is_something_reclaim_releases_on():
    """THE TEST THE FIRST VERSION OF THIS FILE DID NOT HAVE.

    The janitor frees nothing by itself -- it writes a state and
    `reclaim` collects on it. So the only assertion worth making is that
    the state it writes appears in `reclaim`'s release predicate. Pin
    them to each other and the two cannot drift apart silently, which is
    exactly how the first version came to be wrong.
    """
    written = inspect.getsource(j._stand_down)
    assert "'CANCELLED'" in written, (
        "the janitor no longer cancels the execution; check what it writes "
        "instead is something reclaim releases on, or it frees nothing")
    assert "e.state = 'CANCELLED'" in _reclaim_src(), (
        "reclaim no longer releases on a CANCELLED execution — the janitor's "
        "whole mechanism is gone and a stale landing park holds its pod "
        "indefinitely again")


def test_it_does_not_claim_the_story_finished_or_was_stopped():
    """`done` would assert the work landed, which is false and would
    satisfy dependents. `cancelled` would assert somebody stopped the
    story, which is also false -- the PR is open and still mergeable.
    The story stays where it is, which is the only true option."""
    src = inspect.getsource(j)
    assert "record_state" not in src, (
        "the janitor moves the STORY again; it should move only the run. "
        "The branch is pushed and the PR is open, so every story state it "
        "could write would be a false claim")


def test_the_deadline_is_measured_from_the_park_and_not_the_story_row():
    """`stories.updated_at` is touched by anything that edits the story.
    A person retitling it on the board would silently reset a deadline
    measured in days; the question's `created_at` is the moment the wait
    began and nothing else writes it."""
    src = inspect.getsource(j.waiting)
    assert "q.created_at <" in src, (
        "the deadline reads something other than the park's own age — a "
        "story edit would postpone it")
    assert "s.updated_at" not in src


def test_the_deadline_is_days_and_not_minutes():
    """A merge is a human act. A deadline tuned like a machine timeout
    would take workspaces from people who are simply not at their desk."""
    assert j.STALE_AFTER_HOURS >= 48, (
        "the merge deadline is under two days; somebody taking a weekend "
        "would lose their story's workspace")
    assert j.WARN_AFTER_HOURS < j.STALE_AFTER_HOURS, (
        "the warning must come before the deadline — the first a person "
        "hears of this should not be the workspace being taken away")
    assert j.SWEEP_INTERVAL_SECONDS >= 300, (
        "sweeping faster than every few minutes buys nothing against a "
        "deadline measured in days")


def test_it_never_touches_a_pod_directly():
    """`reclaim` is the one authority over compute. A janitor that
    deleted a pod would be a second one, free to disagree with the first
    at the worst possible moment."""
    src = inspect.getsource(j)
    for forbidden in ("delete_pod", "destroy(", "kubectl", "container_id",
                      "list_workspaces"):
        assert forbidden not in src, (
            f"the janitor references {forbidden!r}; it must write a state "
            f"and let reclaim collect on the signal it already trusts")


def test_an_errand_is_out_of_reach_by_construction():
    """An errand's work never lands, so it never owes a landing and never
    has a landing park. The query needs no errand carve-out -- the safest
    shape, because there is nothing to forget. This notices if it ever
    widens to something an errand CAN have."""
    src = inspect.getsource(j.waiting)
    assert "q.kind IN ('landing', 'closeout')" in src
    assert "s.state = 'awaiting_merge'" in src
    for reachable in ("'acceptance'", "'clarification'", "'authorization'"):
        assert reachable not in src, (
            f"the sweep now matches {reachable}, which an errand reaches — "
            f"it would stand down errands waiting on a person")


def test_a_merge_landing_mid_sweep_wins():
    """The story is selected, then the person merges, then the janitor
    writes. Without a CAS the janitor would cancel a run that had just
    been released by the merge."""
    src = inspect.getsource(j._stand_down)
    assert "AND status = 'OPEN'" in src, (
        "the park is retired without a CAS on its status; a merge that "
        "answered it microseconds earlier would be overwritten")
    assert "state IN ('PARKED_HOT', 'PARKED_COLD')" in src, (
        "the run is cancelled without a guard on it still being parked")


def test_both_halves_commit_together():
    """A retired park on a live run is a wait nothing will end. A
    cancelled run with an open park trips `questions_one_open` against
    whatever asks next."""
    src = inspect.getsource(j._stand_down)
    assert "conn.transaction()" in src, (
        "the park retirement and the run cancellation are no longer one "
        "transaction; either half alone leaves a worse state than neither")
