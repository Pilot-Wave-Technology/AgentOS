"""A landing is recorded on the lane it landed on, and says so.

TWO FAULTS IN ONE FUNCTION, and the first one hid the second.

NO DIGEST WAS EVER WRITTEN. `record_landing` found the card to append to
with

    SELECT gres.id FROM goal_resources gres
     WHERE gres.goal_id = gn.goal_id
       AND gres.git_repo_id = gn.git_repo_id
       AND gres.kind = 'repo' LIMIT 1

and `goal_resources.git_repo_id` is written in exactly one place —
`branches.ensure_branch_resource`, for the engine-cut FEATURE branch it
just created. No path sets it on a `kind='repo'` row. So the subquery
returned NULL for every GO-registered repository, the next line returned
False, and the "one row per landing, appended by the MERGED wake" half
of the scanner-v2 design has been writing nothing. Silently: False is
also what an already-digested node returns.

AND THE TARGET WAS THE WRONG CARD ANYWAY. A digest records what landed
WHERE. On the repo row it says "something landed in this repository",
which on a two-lane goal is true of both lanes and informative about
neither — while the lane cards, the ones a planner reads before
sequencing, go on saying nothing has landed. The lane row is the target;
the repo row is the fallback for a goal that declared no lanes.

THE WAKE SAID IT WRONG TOO. "[LANDED] `feat/a` merged to trunk (gr_9f…).
… other branches on this repo are now behind trunk." The parenthesis is
a hash, "trunk" names nothing, and the closing claim is FALSE for every
branch on a lane that did not move — which the GO acts on by
resequencing work that was never behind.
"""
from __future__ import annotations

import inspect

import pytest

from app.services import go_wake_events, resource_digest


class _Pool:
    """Records every statement; answers by shape."""

    def __init__(self, *, lane_id=None, repo_id=None):
        self.lane_id, self.repo_id = lane_id, repo_id
        self.asked: list = []

    async def fetchval(self, sql, *args):
        self.asked.append((sql, args))
        if "role = 'trunk'" in sql:
            return self.lane_id
        if "kind = 'repo'" in sql:
            return self.repo_id
        return None


@pytest.mark.asyncio
async def test_the_digest_goes_to_the_lane_that_moved():
    """THE FIX. `resource_brief` for `cal_backend` now reports the
    landing that happened on `cal_backend`."""
    p = _Pool(lane_id="gb_backend", repo_id="gr_row")
    assert await resource_digest._digest_target(
        p, "G", "acme", "cal", "cal_backend") == "gb_backend"


@pytest.mark.asyncio
async def test_a_goal_with_no_lane_still_gets_its_digest_on_the_repo():
    p = _Pool(lane_id=None, repo_id="gr_row")
    assert await resource_digest._digest_target(
        p, "G", "acme", "cal", "main") == "gr_row"


@pytest.mark.asyncio
async def test_a_node_with_no_lane_does_not_even_look_for_one():
    p = _Pool(lane_id="gb_backend", repo_id="gr_row")
    assert await resource_digest._digest_target(p, "G", "acme", "cal", "") \
        == "gr_row"
    assert not any("role = 'trunk'" in s for s, _ in p.asked)


@pytest.mark.asyncio
async def test_the_lane_is_matched_by_owner_and_name_not_by_git_repo_id():
    """The column that was used is NULL on every GO-registered row, so
    the lookup found nothing at all. Owner as well as name: two repos of
    the same name from different owners in one goal would otherwise take
    each other's landings."""
    p = _Pool(lane_id="gb_backend")
    await resource_digest._digest_target(p, "G", "ACME", "cal",
                                        "cal_backend")
    sql, args = p.asked[0]
    assert "git_repo_id" not in sql
    assert "lower(" in sql and "b.parent_id" in sql
    assert args == ("G", "ACME", "cal", "cal_backend")


def test_the_node_query_no_longer_resolves_the_card_itself():
    src = inspect.getsource(resource_digest.record_landing)
    assert "gres.git_repo_id = gn.git_repo_id" not in src
    assert "_digest_target" in src


def test_a_repository_with_no_card_at_all_says_so():
    """It returned a bare False, which is also what an already-recorded
    node returns — so the failure that ran for months looked identical
    to the success case."""
    src = inspect.getsource(resource_digest.record_landing)
    assert "no resource card for" in src


def test_the_digest_line_names_the_lane():
    src = inspect.getsource(resource_digest.record_landing)
    assert 'lane = n["trunk_branch"]' in src
    assert '"lane": lane' in src, (
        "the lane belongs in the metadata too, so a reader can filter "
        "the digests of one lane out of a repo's history")


# ── the wake ─────────────────────────────────────────────────────────

def test_the_landed_wake_names_the_lane_and_the_repository():
    src = inspect.getsource(go_wake_events.on_node_event)
    assert "merged into " in src
    assert "n['git_repo_id']}). Stories delivered" not in src, (
        "the message identified the repository by its `gr_` hash")
    assert "r.owner, r.name" in src


def test_it_no_longer_claims_the_other_lanes_moved():
    src = inspect.getsource(go_wake_events.on_node_event)
    assert "OTHER lanes are untouched" in src
    landed = src[src.index("[LANDED]"):src.index("elif state ==")]
    assert "other branches on this repo are now behind trunk" not in \
        landed.split("if lane else")[0], (
        "the unconditional claim is false for every branch on a lane "
        "that did not move, and the planner resequences on it")


def test_the_trunk_advance_fanout_was_already_lane_scoped():
    """Asserted so a later tidy-up does not 'simplify' it to match what
    the LANDED message used to say."""
    src = inspect.getsource(go_wake_events.on_trunk_advance)
    assert "trunk_branch = $2" in src
