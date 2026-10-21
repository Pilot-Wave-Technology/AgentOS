"""Three faults found on one goal, GOAL1 on `next`, 2026-10-07/08.

GOAL1 has a workstream whose trunk lane is `develop` — an
actively-developed branch. PR #82 opened at 17:18 and nobody merged it.
Over the next thirteen hours the engine produced an integrate story per
upstream commit:

    ca4fac31 pushed 17:26 → run 416 ≈17:51
    79af2018 pushed 18:31 → run 418 ≈18:51
    b8d44f3e pushed 20:11 → run 419 ≈20:51   PARKED_COLD, 1 open question
                          → run 421 ≈21:51
    a56f56f8 pushed 05:44 → run 424  05:49   ds_b517af460ed40c

Each completed. Each was obsoleted by the next push. The cost is linear
in how long the human takes to review, and nothing anywhere said so.

Separately, on the same goal: `feat/GOAL1-8afcc7c9`'s PR #85 was closed
unmerged by a person, and the node was carded as an engine FAILURE — "the
git engine cannot continue", with a Retry button.
"""
from __future__ import annotations

import inspect

import pytest


# ── 1. an open PR is integrated only when staleness blocks it ────────

def test_only_dirty_and_behind_earn_an_integrate_story():
    from app.services.git_graph import grind

    assert grind._STALENESS_BLOCKS_MERGE == ("DIRTY", "BEHIND"), (
        "these are GitHub's two mergeStateStatus values meaning THIS "
        "branch's staleness is the obstacle; everything else is waiting "
        "on review, on checks, or on nothing")


@pytest.mark.parametrize("state", ["CLEAN", "UNSTABLE", "BLOCKED", "DRAFT",
                                   "HAS_HOOKS", "UNKNOWN"])
def test_a_mergeable_open_pr_is_left_alone(state):
    """The GOAL1 case. PR #82 was CLEAN for most of its life — nothing
    about it needed integrating, and it got five integrate runs."""
    from app.services.git_graph.grind import _STALENESS_BLOCKS_MERGE
    assert state not in _STALENESS_BLOCKS_MERGE


def test_the_gate_sits_before_the_spawn_and_after_the_approval_check():
    src = inspect.getsource(
        __import__("app.services.git_graph.grind", fromlist=["x"])._grind_one)
    gate = src.index("_STALENESS_BLOCKS_MERGE")
    spawn = src.index("_ensure_diff_story(h, trunk_sha)")
    assert gate < spawn, "the gate must run before the story is created"
    assert 'review_decision") == "APPROVED"' in src[:gate], (
        "the existing approved/in-merge-queue skip must still come first")


def test_an_unreadable_merge_state_falls_through_to_the_old_rule():
    """`get_merge_state_status` is wrapped in try/except and yields {} on
    failure. A GitHub outage must not silently stop integration for every
    story in the system — an empty reading keeps the behind/ahead rule
    that was the whole gate until now."""
    src = inspect.getsource(
        __import__("app.services.git_graph.grind", fromlist=["x"])._grind_one)
    assert "if state and state not in _STALENESS_BLOCKS_MERGE" in src, (
        "an empty merge_state_status must NOT skip the integrate")


# ── 2. superseding a story stops its run ─────────────────────────────

def test_a_superseded_story_has_its_run_stopped():
    """Run 419 sat PARKED_COLD with one unanswered question for nine
    hours while 421 and 424 superseded it. Cancelling the story never
    touched the execution, and a parked run holds its question, its
    workspace and its admission slot."""
    from app.services.git_graph import grind

    src = inspect.getsource(grind._spawn_engine_code_story)
    assert "stop_the_run" in src, (
        "superseding cancels the story and leaves the run parked")
    assert src.index("_superseded = [") < src.index("record_states_where("), (
        "the ids must be read BEFORE the cancel — the predicate stops "
        "matching once they move to 'cancelled'")


def test_it_uses_the_sanctioned_cancel_path():
    """`stop_the_run` goes through the mailbox on its own kernel
    connection and never raises, so an unreachable run cannot undo the
    supersede. Writing CANCELLED into the table would skip the machine
    edge, the lease release and the admission slot."""
    from app.services import story_cancellation
    assert inspect.iscoroutinefunction(story_cancellation.stop_the_run)
    doc = inspect.getdoc(story_cancellation.stop_the_run) or ""
    assert "NEVER RAISES" in doc


# ── 3. a human closing a PR is not an engine failure ─────────────────

def test_the_marker_is_spelled_once():
    from app.services.git_graph import detect, node_errors

    assert node_errors.PR_CLOSED_UNMERGED == "PR closed unmerged"
    src = inspect.getsource(detect)
    assert src.count('"PR closed unmerged') == 0, (
        "detect.py has its own copy of the marker again — two copies is "
        "how the card silently reverts to the failure wording")
    assert src.count("PR_CLOSED_UNMERGED") >= 2, (
        "both cancel routes (webhook and poll backstop) must use it")


@pytest.mark.parametrize("err,expected", [
    ("PR closed unmerged (human-cancel)", True),
    ("PR closed unmerged (poll backstop, webhook missed)", True),
    ("403 Forbidden: resource not accessible by integration", False),
    ("", False),
    (None, False),
])
def test_is_human_cancel(err, expected):
    from app.services.git_graph.node_errors import is_human_cancel
    assert is_human_cancel(err) is expected


def test_a_human_cancel_gets_its_own_card():
    from app.services import kernel_node_cards as k

    row = {"branch_name": "feat/GOAL1-8afcc7c9", "trunk_branch": "main",
           "goal_id": "GOAL1", "story_id": "8afcc7c9", "node_id": "gn_x",
           "last_error": "PR closed unmerged (poll backstop, webhook missed)",
           "run_id": 407}
    card = k._human_cancel_card(row)

    assert card["card_type"] == "git_pr_closed_unmerged"
    assert "retry_run" not in card, (
        "Retry is the one action that must not be offered — it re-runs "
        "work a person just rejected")
    body = card["body"]
    assert "closed without merging" in body
    assert "Reopen the PR" in body and "Replan" in body, (
        "the card must name both ways forward")
    assert "cannot continue" not in body, (
        "that is the failure wording; nothing failed here")


def test_the_failure_card_is_still_used_for_real_failures():
    from app.services import kernel_node_cards as k

    row = {"branch_name": "feat/X", "trunk_branch": "main",
           "goal_id": "G", "story_id": "s1", "node_id": "gn_y",
           "last_error": "403 Forbidden", "run_id": 12}
    card = k._card(row)
    assert card["card_type"] == "git_node_error"
    assert card["retry_run"] == 12, (
        "a permission failure IS fixable and retryable — this path must "
        "not lose its button")


def test_the_sweep_chooses_between_them():
    from app.services import kernel_node_cards as k
    src = inspect.getsource(k.sweep)
    assert "is_human_cancel(row[\"last_error\"])" in src
    assert "_human_cancel_card(row)" in src
