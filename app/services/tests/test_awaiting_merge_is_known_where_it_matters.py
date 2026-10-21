"""Every place that asks "is this story still live?" has to say yes.

A new story state is only half-added when the constraint permits it. The other
half is the dozens of predicates that decide what a story IS, and they come in
two shapes that behave oppositely when a state is added:

  * `state NOT IN ('done', 'reviewed', 'cancelled', 'rejected')` -- an
    EXCLUSION list. A new state is absent from it, so it reads as live. Safe by
    construction, and nothing below needs to guard those.

  * `state IN ('accepted', 'in_progress', 'blocked')` -- an INCLUSION list. A
    new state is absent from it, so it reads as NOT live, silently. Every one
    of these is a place `awaiting_merge` disappears from.

So these are the inclusion lists, pinned. Each assertion says what breaks if the
state falls out of that particular one, because "add it back" is useless advice
without knowing what it was holding up.
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]


def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_it_is_a_non_terminal_state():
    """The canonical named set, and the one that resolves which story a
    thread is about. Out of here, the assistant stops being able to find the
    story the person is talking to it about."""
    from app.services.story_state import _NON_TERMINAL_STATES
    assert "awaiting_merge" in _NON_TERMINAL_STATES, (
        "a story holding its pod on an open PR is not terminal; dropping it "
        "here makes resolve_active_focus_story return None and the assistant "
        "lose the story mid-conversation")


def test_the_github_token_keeps_being_refreshed():
    """Installation tokens die at one hour. The person can still ask for one
    more change, and that change still has to push."""
    src = _src("app/services/token_refresh.py")
    assert "'awaiting_merge'" in src, (
        "the refresh loop no longer covers stories awaiting a merge — their "
        "credential helper goes stale after an hour and the next push fails "
        "with an auth error that looks like a permissions problem")


def test_a_pushed_branch_with_no_commits_escalates_and_never_finalizes():
    """THE SHARP EDGE, and the reason this file exists.

    `grind` has an arm for a branch still sitting on its cut point. It asks
    whether a rider is still live (not pushed yet -- wait) or finished (claims
    work that isn't there -- escalate). A state in NEITHER list falls past both
    into `_finalize_merged`, which marks the node MERGED and tells the GO the
    work landed. That is how the first kernel story "landed" with its commits
    still in the sandbox, and `awaiting_merge` means "I pushed", so an empty
    branch under it is exactly that failure again.
    """
    src = _src("app/services/git_graph/grind.py")
    assert "'done', 'reviewed', 'awaiting_merge'" in src, (
        "awaiting_merge is no longer classified in grind's never-moved arm; a "
        "story whose push silently failed would fall through to "
        "_finalize_merged and be reported as landed with an empty branch")


def test_the_branch_counts_as_started():
    src = _src("app/services/git_graph/branches.py")
    assert "'awaiting_merge'" in src, (
        "the workstream no longer counts as started once its story is "
        "awaiting a merge")


def test_it_shows_up_as_work_in_flight():
    """Three views of "what is this person / resource / goal busy with". A
    story holding a pod on an open PR is busy by any honest reading."""
    for rel, what in [
        ("app/services/goal_membership.py", "the assignee's current load"),
        ("app/services/go_events.py", "stories still using a resource"),
        ("app/routers/chat.py", "the person's own work list"),
    ]:
        assert "'awaiting_merge'" in _src(rel), (
            f"{rel} no longer counts a story awaiting a merge as in flight "
            f"({what}), so it reads as idle while it holds a pod")


def test_the_board_sorts_it_where_a_person_will_see_it():
    """It is the one state on the board that is waiting on a HUMAN to act.
    Sorting it into the ELSE bucket puts it below `reviewed`, which is where
    finished work lives."""
    src = _src("app/routers/stories.py")
    assert "WHEN 'awaiting_merge' THEN 2" in src, (
        "awaiting_merge fell out of the board's sort order; it lands in the "
        "ELSE bucket below reviewed, where nobody is looking for something "
        "that needs them")


def test_the_migration_permits_it_and_indexes_the_sweep():
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, baseline_mentions

    if IS_DISTRIBUTION:
        # The squashed schema: the constraint and the index themselves.
        assert baseline_mentions(
            r"CONSTRAINT stories_state_check CHECK \(\(state = ANY \(ARRAY\[[^\]]*"
            r"'awaiting_merge'::text"), "the constraint refuses awaiting_merge"
        assert baseline_mentions(
            r"CREATE INDEX stories_awaiting_merge ON public\.stories .*?"
            r"WHERE \(state = 'awaiting_merge'::text\);"), (
            "the partial index the deadline sweep runs on is gone")
        return
    sql = _src("migrations/story_awaiting_merge_state.sql")
    assert "'awaiting_merge'::text" in sql
    assert "stories_awaiting_merge" in sql, (
        "the partial index the deadline sweep runs on is gone")
    seq = _src("migrations/SEQUENCE")
    assert "story_awaiting_merge_state" in seq.split(), (
        "the migration is not in SEQUENCE, so it never runs — and it must be "
        "the bare name, not the filename with .sql")
