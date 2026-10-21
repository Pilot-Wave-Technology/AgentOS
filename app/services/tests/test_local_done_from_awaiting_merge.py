"""Mark Done works from `awaiting_merge`, and only for the local lane.

THE DEFECT, AND IT WAS MINE. Phase 4 dropped `awaiting_merge -> done`
from `STORY_VALID_TRANSITIONS` on the stated reasoning that "the local
Mark Done path touches neither that table nor `apply_story_transition`".
`local_execution_run.story_done` calls `apply_story_transition`
directly. So every local Mark Done from `awaiting_merge` came back 409
-- and the extension, which reads any 409 from that route as the plan
gate, reported "its plan state changed", sending people to look at a
plan that was fine. Seen on 66317ffc, 2026-10-14.

DROPPING IT FROM THE TABLE WAS STILL RIGHT. `awaiting_merge -> done`
there would let Slack's `/done` force a CLOUD story to done with its
pull request still open, which is the one thing that state exists to
prevent. What was missing is the other half of rule zero's own
instruction: a local-only path with its own answer.

So the caller opts in. The table is untouched -- pinned separately --
and only the route that has just confirmed every lane merged may use
the exemption.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import PoolOnConn, local_http


async def _with_plan(h, story_id: str) -> None:
    """`apply_story_transition("done")` asks `story_done_blocker` too, so a
    story with no approved plan is refused before the table is reached."""
    await h.conn.execute(
        "INSERT INTO plan_versions (id, story_id, version, cycle, kind, "
        "   plan_doc_path, plan_json, proposed_by, status, approved_at, "
        "   approved_by) "
        "VALUES (gen_random_uuid(), $1, 1, 1, 'work', 'p.md', '{}'::jsonb, "
        "        'local', 'approved', now(), 'person')", story_id)


@pytest.mark.asyncio
async def test_the_shared_table_still_refuses_it():
    """The guard that makes the opt-in meaningful. If the table ever
    grows this transition, Slack `/done` gets it too."""
    from app.models.story import STORY_VALID_TRANSITIONS

    assert "done" not in STORY_VALID_TRANSITIONS.get("awaiting_merge", []), (
        "`awaiting_merge -> done` is in the shared table; a cloud story "
        "can now be forced done with its pull request open")


@pytest.mark.asyncio
async def test_without_the_opt_in_the_transition_is_refused():
    from app.services.story_transitions import apply_story_transition

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story)
        ok, reason = await apply_story_transition(
            goal, story, "done", "person", PoolOnConn(h.conn))
        assert ok is False
        assert "not allowed" in reason, repr(reason)


@pytest.mark.asyncio
async def test_with_the_opt_in_it_goes_through():
    from app.services.story_transitions import apply_story_transition

    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge")
        await _with_plan(h, story)
        await h.conn.execute(
            "UPDATE stories SET assigned_to = 'person' WHERE id = $1", story)
        ok, reason = await apply_story_transition(
            goal, story, "done", "person", PoolOnConn(h.conn),
            extra_allowed_from=("awaiting_merge",))
        assert ok is True, repr(reason)
        assert await h.conn.fetchval(
            "SELECT state FROM stories WHERE id = $1", story) == "done"


@pytest.mark.asyncio
async def test_the_opt_in_does_not_permit_anything_else():
    """It widens exactly one FROM state, not the action. A cancelled
    story must not become done because the caller opted in."""
    from app.services.story_transitions import apply_story_transition

    async with local_http() as h:
        goal, story = await h.seed_story(state="cancelled")
        ok, _ = await apply_story_transition(
            goal, story, "done", "person", PoolOnConn(h.conn),
            extra_allowed_from=("awaiting_merge",))
        assert ok is False


def test_the_local_route_is_the_one_that_opts_in():
    """Pinned at source: the exemption belongs to the route that has
    just checked every lane merged, and nowhere else."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    route = (root / "routers" / "local_execution_run.py").read_text(encoding="utf-8")
    fn = route[route.index("async def story_done("):]
    fn = fn[:fn.index("\n@router.post", 10)]
    assert 'extra_allowed_from=("awaiting_merge",)' in fn
    # The blocker runs FIRST. Opting past the table while the branch is
    # still open would be the very thing the table was protecting.
    assert fn.index("story_done_blocker") < fn.index("extra_allowed_from")


def test_nothing_else_in_the_codebase_opts_in():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    users = [p.name for p in root.rglob("*.py")
             if "extra_allowed_from=" in p.read_text(encoding="utf-8")
             and p.name not in ("story_transitions.py",
                                "test_local_done_from_awaiting_merge.py")]
    assert users == ["local_execution_run.py"], (
        f"{users} also bypass the transition table")
