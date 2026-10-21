"""A story in flight can be cancelled, and everything it held lets go.

WHAT USED TO HAPPEN: nothing could. `_cancel_story` refused once a
story reached `in_progress`, on the grounds that its branch was cut
and work was committed, offering two alternatives -- finish it, or
abort the lane by hand in the Git Engine view.

The hazard was real. The remedy was not: a story somebody simply
stopped working on sat `in_progress` for ever, holding its thread, its
checkout, its merge-queue slot and its place in the dependency chain
of everything downstream. The only honest way out was to finish work
nobody wanted.

WHAT ANSWERS THE HAZARD IS THE TEARDOWN. `tear_down_nodes` already
existed for errands and draws the two lines that matter:

  A NODE SHARED with another story loses only this story's
  membership. The branch belongs to the other story too, and
  abandoning it would take somebody else's work down.

  A MERGED NODE IS LEFT ENTIRELY ALONE. This is "merged work stays
  merged", and it is enforced rather than intended -- cancelling says
  "no more work here", never "undo what shipped".
"""
from __future__ import annotations

import inspect

import pytest

from app.services.tests.local_http import local_http


async def _node(h, story, state="BRANCH_CREATED"):
    """A git node for this story, in the given state."""
    repo = await h.conn.fetchval(
        "INSERT INTO git_repos (id, owner, name, clone_url, trunk_branch) "
        "VALUES (gen_random_uuid(),'o','n','https://x/o/n.git','main') "
        "RETURNING id")
    nid = await h.conn.fetchval(
        "INSERT INTO git_nodes (id, goal_id, git_repo_id, branch_name, "
        "  trunk_branch, state) VALUES (gen_random_uuid(), "
        "  (SELECT goal_id FROM stories WHERE id=$1), $2, 'feat/x', "
        "  'main', $3) RETURNING id", story, repo, state)
    await h.conn.execute(
        "INSERT INTO git_node_stories (git_node_id, story_id) VALUES ($1,$2)",
        nid, story)
    return nid


# ── it is allowed at all ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["in_progress", "blocked"])
async def test_a_story_in_flight_can_be_cancelled(state):
    from app.routers.stories import _cancel_story
    async with local_http() as h:
        goal, story = await h.seed_story(state=state, held_by=None)
        r = await _cancel_story(goal, story, "stopping it", actor_uid="p")
        assert r.status_code != 409, r.body
        assert (await h.story(story))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_a_finished_story_is_untouched():
    """Cancelling is for work that is still to come."""
    from app.routers.stories import _cancel_story
    async with local_http() as h:
        goal, story = await h.seed_story(state="done", held_by=None)
        await _cancel_story(goal, story, "x", actor_uid="p")
        # The board does not offer it; the server is not the guard here,
        # but the state must not silently go backwards either.
        assert (await h.story(story))["state"] in ("done", "cancelled")


# ── and merged work survives it ──────────────────────────────────────

@pytest.mark.asyncio
async def test_a_merged_lane_is_left_alone():
    """THE PROPERTY PEOPLE MOST NEED TO TRUST. A story can have landed
    one repo and be abandoned in another; cancelling it must not touch
    what already shipped."""
    from app.routers.stories import _cancel_story
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", held_by=None)
        merged = await _node(h, story, "MERGED")

        await _cancel_story(goal, story, "x", actor_uid="p")

        assert await h.conn.fetchval(
            "SELECT state FROM git_nodes WHERE id = $1", merged) == "MERGED", (
            "a merged lane was torn down by cancelling the story that "
            "produced it")


@pytest.mark.asyncio
async def test_a_live_lane_is_retired():
    """The other half: a branch nobody will work on must not stay
    mergeable, or the hazard the old refusal named is real."""
    from app.routers.stories import _cancel_story
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", held_by=None)
        live = await _node(h, story, "BRANCH_CREATED")

        await _cancel_story(goal, story, "x", actor_uid="p")

        row = await h.conn.fetchrow(
            "SELECT state, branch_cleanup_tries FROM git_nodes WHERE id = $1",
            live)
        assert row["state"] == "ABANDONED"
        assert (row["branch_cleanup_tries"] or 0) >= 1, (
            "the branch was not queued for deletion, so the ref stays "
            "on GitHub with nothing saying why")


@pytest.mark.asyncio
async def test_a_lane_shared_with_another_story_survives():
    """Memory of a real teardown: a branch named after a DEPENDENCY is
    a shared workstream, and abandoning it takes the dependents with
    it. Only this story's membership is dropped."""
    from app.routers.stories import _cancel_story
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", held_by=None)
        _, other = await h.seed_story(state="in_progress", held_by=None)
        node = await _node(h, story)
        await h.conn.execute(
            "INSERT INTO git_node_stories (git_node_id, story_id) "
            "VALUES ($1,$2)", node, other)

        await _cancel_story(goal, story, "x", actor_uid="p")

        assert await h.conn.fetchval(
            "SELECT state FROM git_nodes WHERE id = $1", node) != "ABANDONED"
        assert await h.conn.fetchval(
            "SELECT count(*) FROM git_node_stories WHERE git_node_id = $1",
            node) == 1


# ── and everything it was holding lets go ────────────────────────────

@pytest.mark.asyncio
async def test_the_checkout_is_freed():
    """The checkout row is what `user_busy` counts and what the
    heartbeat refreshes, so a stale claim costs the person their NEXT
    story too — refused in the name of one they already cancelled."""
    from app.routers.stories import _cancel_story
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress",
                                         held_by="person")
        await _cancel_story(goal, story, "x", actor_uid="person")
        row = await h.story(story)
        assert row["checked_out_to_uid"] is None
        assert row["checkout_session_id"] is None


@pytest.mark.asyncio
async def test_the_thread_binding_is_cleared():
    from app.routers.stories import _cancel_story
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", held_by=None)
        await _cancel_story(goal, story, "x", actor_uid="p")
        assert (await h.story(story))["active_thread_id"] in (None, "")


def test_the_slack_channels_are_retired_on_the_same_connection():
    """`_cancel_story` writes `state` with a bare UPDATE rather than
    `record_state` — it branches merge/code and bridges dependents in
    the same statement — so the terminal-state Slack teardown that
    hangs off `record_state` never fired for a cancellation. The
    15-minute sweep caught it, which is why cancelling "worked" and the
    channel lingered.

    ON `conn`, because this runs inside the transaction and a pooled
    read cannot see the uncommitted row. That mistake has been made
    three times in this codebase."""
    from app.routers import stories
    src = inspect.getsource(stories._cancel_story)
    assert "retire_story(conn, sid)" in src, (
        "the Slack channels are retired on a pooled connection, or not "
        "at all")
    assert src.index("retire_story") < src.index("append_event"), (
        "the retire happens after the ledger write; keep it inside the "
        "same transaction as the state change")


def test_the_run_is_stopped_before_the_lane_comes_down():
    """A live run writing to a branch being abandoned underneath it."""
    from app.routers import stories
    src = inspect.getsource(stories._cancel_story)
    assert src.index("stop_the_run") < src.index("tear_down_nodes")
