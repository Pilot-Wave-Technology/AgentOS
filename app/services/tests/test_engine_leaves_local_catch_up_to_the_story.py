"""The engine opens the PR and stops. The story catches itself up.

PHASE 5.1 IS A VERIFICATION, and this file is its result. The question
was whether the git engine does anything to a self-integrating LOCAL
node beyond opening its pull request -- specifically whether it merges
trunk into the branch, or parks the branch `CONFLICTED` when that merge
fails. Either would collide head-on with the local catch-up, which is
the story's own cycle: two integrations of one branch, one of them
invisible to the person running it.

THE ANSWER IS NO, AND NOT BECAUSE OF ANYTHING LOCAL. The merge pass
(`run_merge_pass` -> `_drain_repo` -> `_merge_one`) is the only code
that merges trunk into a branch or parks it `CONFLICTED`, and it is
DEAD: the engine retired self-merging, a human merges on GitHub, and
`engine._tick` no longer calls it. So the property holds for every
story, and the local lane inherits it.

WHICH IS WHY THIS IS PINNED RATHER THAN LEFT ALONE. "Correct because a
whole subsystem is unreachable" is only true while it stays unreachable,
and re-arming the merge pass would be one call in a tick -- a change
whose connection to the local lane nobody would see.
"""
from __future__ import annotations

import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


# ── the engine does not merge, for anybody ───────────────────────────

def test_the_tick_does_not_run_the_merge_pass():
    """The one thing that would make this untrue is one line long."""
    tick = _read("services/git_graph/engine.py")
    assert "run_merge_pass" not in tick.replace("merge_lane.run_merge_pass", ""), (
        "the engine tick calls the merge pass again; a local story's PR "
        "would be merged by the platform instead of by its person, and "
        "trunk would be merged into the branch under the catch-up")


def test_only_the_dead_merge_pass_merges_trunk_into_a_branch():
    """`merge_ref_into` is the call that would race the catch-up. It must
    stay inside the pass nothing invokes."""
    live = [
        name for name in ("grind.py", "detect.py", "engine.py", "reconcile.py")
        if "merge_ref_into" in _read(f"services/git_graph/{name}")
    ]
    assert live == [], (
        f"{live} merge trunk into a branch; for a local story that is the "
        "catch-up's job and doing both loses the person's own merge")


def test_only_the_dead_merge_pass_parks_a_branch_conflicted():
    live = [
        name for name in ("grind.py", "detect.py", "engine.py", "reconcile.py")
        if "'CONFLICTED'" in _read(f"services/git_graph/{name}")
        and "SET state = 'CONFLICTED'" in _read(f"services/git_graph/{name}")
    ]
    assert live == [], (
        f"{live} park a node CONFLICTED; a local story would be told its "
        "branch is stuck by the one component that is not integrating it")


def test_the_pr_the_engine_opens_says_the_merge_is_the_humans():
    """Not decoration -- it is the contract the whole lane rests on."""
    body = _read("services/git_graph/grind.py")
    fn = body[body.index("async def _ensure_pr_open("):]
    nxt = fn.find("\nasync def ", 10)
    fn = fn[:nxt] if nxt != -1 else fn   # it may be the last one
    assert "the merge is yours" in fn
    assert "Do not merge from the engine" in fn


# ── and the behind-branch path defers to the story ───────────────────

def test_grind_asks_whether_the_story_integrates_itself_before_cutting():
    """A `ds_*` diff story for a local lane would be a second assistant
    integrating the same branch, in a thread nobody asked for."""
    body = _read("services/git_graph/grind.py")
    assert body.index("node_self_integrates") < body.index("_ensure_diff_story("), (
        "the diff story is cut before anybody asks whose job the catch-up is")


@pytest.mark.asyncio
async def test_a_local_story_waiting_on_its_merge_integrates_itself(monkeypatch):
    """The predicate the engine asks. `awaiting_merge` is the state a
    local story sits in with its PR open, and it is still alive -- the
    person is in the editor the branch was written in."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services.tests.local_http import local_http
    from app.services.trunk_integration import node_self_integrates

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await h.seed_node(story, goal, state="BRANCH_CREATED")
        node = await h.conn.fetchval(
            "SELECT git_node_id FROM git_node_stories WHERE story_id = $1", story)
        assert await node_self_integrates(h.conn, node) is True


@pytest.mark.asyncio
async def test_an_errand_is_integrated_by_nobody(monkeypatch):
    """Not the story and not the engine. An errand's branch never lands,
    so catching it up is work for a merge that will not happen."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services.tests.local_http import local_http
    from app.services.trunk_integration import node_is_errand, node_self_integrates

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", work_kind="errand")
        await h.seed_node(story, goal, state="BRANCH_CREATED")
        node = await h.conn.fetchval(
            "SELECT git_node_id FROM git_node_stories WHERE story_id = $1", story)
        assert await node_self_integrates(h.conn, node) is False
        assert await node_is_errand(h.conn, node) is True
