"""A local story's branch is not deleted when its node merges.

THE CLOUD LANE'S RUN ENDS AT THE MERGE. Its workspace is collected, so
deleting the branch then is tidy and right.

A LOCAL STORY KEEPS GOING. Between the merge and `done` the person may
ask for more work -- which pushes to THIS branch -- a re-import clones
it, and the closeout review reads the tree it produced. Deleting at the
merge took the branch out from under all three.

Abhishek, 2026-10-14, on seeing it happen: "it should be deleted only
after the story is marked done."
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _merged_node(h, *, mode="local", state="awaiting_merge"):
    goal, story = await h.seed_story(state=state)
    await h.conn.execute(
        "UPDATE stories SET execution_mode = $2 WHERE id = $1", story, mode)
    node = await h.seed_node(story, goal, state="MERGED")
    return story, node


@pytest.mark.asyncio
async def test_a_local_story_still_running_keeps_its_branch():
    from app.services.git_graph.branch_cleanup import _story_unfinished

    async with local_http() as h:
        _, node = await _merged_node(h)
        assert await _story_unfinished(h.conn, node) is True


@pytest.mark.parametrize("state", ["done", "reviewed", "cancelled"])
@pytest.mark.asyncio
async def test_a_finished_local_story_releases_it(state):
    """Otherwise every local branch ever cut survives for ever, which is
    the clutter the cleanup exists to prevent."""
    from app.services.git_graph.branch_cleanup import _story_unfinished

    async with local_http() as h:
        story, node = await _merged_node(h)
        await h.conn.execute(
            "UPDATE stories SET state = $2 WHERE id = $1", story, state)
        assert await _story_unfinished(h.conn, node) is False


@pytest.mark.asyncio
async def test_a_CLOUD_story_keeps_its_branch_too():
    """A DELIBERATE WIDENING, Abhishek 2026-10-14: "the branches have to
    stay alive until the story is marked done." The cloud lane has the
    same window for the same reason -- `kernel_more_work` pushes to the
    same branch, and its run ending is not its story ending."""
    from app.services.git_graph.branch_cleanup import _story_unfinished

    async with local_http() as h:
        _, node = await _merged_node(h, mode="operator")
        assert await _story_unfinished(h.conn, node) is True


@pytest.mark.asyncio
async def test_a_node_with_no_story_is_unaffected():
    """An engine-cut node nobody owns. Treating "no story" as "unfinished"
    would strand every orphan branch."""
    from app.services.git_graph.branch_cleanup import _story_unfinished

    async with local_http() as h:
        goal, story = await h.seed_story()
        node = await h.seed_node(story, goal, state="MERGED")
        await h.conn.execute(
            "DELETE FROM git_node_stories WHERE git_node_id = $1", node)
        assert await _story_unfinished(h.conn, node) is False


def test_the_guard_runs_before_the_delete():
    """A check after the `delete_ref` call would be an audit, not a
    guard."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "git_graph" / "branch_cleanup.py").read_text(encoding="utf-8")
    # The cleanup function's body, which ends where the helper begins
    # in file order -- the helper is defined after it.
    start = src.index("        row = await pool.fetchrow(")
    fn = src[start:src.index("ok = await gh.delete_ref", start) + 30]
    assert fn.index("_story_unfinished") < fn.index("delete_ref("), (
        "the branch is deleted before anybody asks whether the story "
        "still needs it")


# ── and the deferral has to come back ────────────────────────────────

@pytest.mark.asyncio
async def test_a_deferral_does_not_spend_the_retry_budget():
    """SECOND-ORDER EFFECT OF KEEPING BRANCHES ALIVE. `record` had two
    answers -- settled (`NOT_OURS`) or failed (which spends one of five
    tries). Treated as a failure, a story taking more than five sweep
    ticks to finish exhausts the budget and its branch survives for
    ever: exactly the clutter this module exists to prevent, arrived at
    by way of the fix that keeps branches alive until `done`."""
    from app.services.git_graph.branch_cleanup_state import record

    async with local_http() as h:
        _, node = await _merged_node(h)
        for _ in range(8):
            await record(h.conn, node, "story unfinished")
        row = await h.conn.fetchrow(
            "SELECT branch_cleanup_tries, branch_deleted_at, branch_cleanup_error "
            "  FROM git_nodes WHERE id = $1", node)
        assert row["branch_cleanup_tries"] == 0, "a deferral is being counted a failure"
        assert row["branch_deleted_at"] is None, (
            "a deferral marked the branch settled — it will never be deleted")
        assert row["branch_cleanup_error"] == "story unfinished"


@pytest.mark.asyncio
async def test_a_deferred_branch_is_still_swept():
    """`tries > 0` was the whole definition of "somebody has looked at
    this", and a branch held back on its FIRST look has tries = 0 — so
    the sweep would never see it again and the branch would outlive the
    story it was waiting for."""
    from app.services.git_graph.branch_cleanup_state import pending, record

    async with local_http() as h:
        _, node = await _merged_node(h)
        await record(h.conn, node, "story unfinished")
        assert node in [r["id"] for r in await pending(h.conn)]


@pytest.mark.asyncio
async def test_a_real_failure_still_spends_one():
    """The budget exists because a branch GitHub will not delete should
    stop being asked about. Only deferrals are exempt."""
    from app.services.git_graph.branch_cleanup_state import record

    async with local_http() as h:
        _, node = await _merged_node(h)
        await record(h.conn, node, "github refused")
        assert await h.conn.fetchval(
            "SELECT branch_cleanup_tries FROM git_nodes WHERE id = $1", node) == 1


@pytest.mark.asyncio
async def test_a_settled_outcome_is_still_settled():
    """`not an engine branch` means nobody intends to delete it. That
    must keep stopping the sweep, or it asks for ever."""
    from app.services.git_graph.branch_cleanup_state import record

    async with local_http() as h:
        _, node = await _merged_node(h)
        await record(h.conn, node, "not an engine branch")
        assert await h.conn.fetchval(
            "SELECT branch_deleted_at FROM git_nodes WHERE id = $1", node) is not None
