"""A declared trunk lane is found without its repo resource.

THE INCIDENT. Goal GOAL1, 2026-10-05. The operator wanted the trunk moved
from `main` to `develop`, had no operation for it, and so
deleted the repo resource and re-added it. `resolve_repo_trunk` required an
ACTIVE repo row before it would look at any lane:

    repo = SELECT ... kind='repo' AND status='active' ...
    if repo is None: return "main"
    trunk = SELECT ... role='trunk' AND parent_id = repo["id"]

so the delete severed the only path to the lanes and resolution silently
returned the literal `"main"` -- the one outcome the switch existed to
prevent. Four stories stalled for a day with nothing in any log.

WHAT THESE PIN. Branch rows carry their own `repo_owner`/`repo_name`, so a
lane can identify itself; the repo row is a label, not an index.

A repo carries AS MANY trunk lanes as the goal needs, and each story is
granted the one it lands on. What this resolves is only the fallback for
work granted no lane -- which is itself a defect, so the tie-break stays
oldest-declared rather than becoming configurable.

THE SAFETY PROPERTY these exist to defend is in `test_nothing_moves_for_an
_unflagged_goal`: on a goal with no flag set anywhere, this resolves
exactly as it did before. That is what makes it applicable to live agents.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip

OWNER = "Pilot-Wave-Technology"
REPO = "agentos"


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _goal(conn, sid: str) -> str:
    gid = f"{sid}-goal"
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "lane fixture", f"{PREFIX}-user")
    return gid


async def _repo(conn, gid: str, rid: str, *, status="active", default_branch="main"):
    await conn.execute(
        "INSERT INTO goal_resources (id,goal_id,kind,name,role,status,"
        " repo_owner,repo_name,default_branch,created_by) "
        "VALUES ($1,$2,'repo',$3,'feature',$4,$5,$6,$7,$8)",
        rid, gid, "agentOS", status, OWNER, REPO, default_branch, f"{PREFIX}-user")
    return rid


async def _lane(conn, gid: str, rid: str, branch: str, *, parent=None,
                status="active", own_identity=True):
    await conn.execute(
        "INSERT INTO goal_resources (id,goal_id,kind,name,role,status,"
        " repo_owner,repo_name,branch_name,parent_id,created_by) "
        "VALUES ($1,$2,'branch',$3,'trunk',$4,$5,$6,$7,$8,$9)",
        rid, gid, f"{REPO}:{branch}", status,
        OWNER if own_identity else None, REPO if own_identity else None,
        branch, parent, f"{PREFIX}-user")
    return rid


async def _trunk(conn, gid):
    from app.services.git_graph.repo_identity import resolve_repo_trunk
    return await resolve_repo_trunk(conn, gid, OWNER, REPO)


@pytest.mark.asyncio
async def test_the_lane_survives_its_repo_row_being_deleted():
    """The GOAL1 case exactly: delete the repo, keep the lane."""
    async with _conn() as c:
        gid = await _goal(c, f"{PREFIX}-lane-del")
        await _repo(c, gid, f"{PREFIX}-r1", status="deleted")
        await _lane(c, gid, f"{PREFIX}-l1", "develop",
                    parent=f"{PREFIX}-r1")
        assert await _trunk(c, gid) == "develop", (
            "resolution fell back to 'main' because the repo row was deleted"
        )


@pytest.mark.asyncio
async def test_a_goal_with_no_repo_row_at_all_still_resolves():
    """Branch-only goals were a dead end: `if repo is None: return 'main'`."""
    async with _conn() as c:
        gid = await _goal(c, f"{PREFIX}-lane-norepo")
        await _lane(c, gid, f"{PREFIX}-l2", "develop", parent=None)
        assert await _trunk(c, gid) == "develop"


@pytest.mark.asyncio
async def test_a_lane_with_no_identity_of_its_own_is_found_via_its_parent():
    """The old matching rule still works -- this is additive, not a swap.

    A trunk row's own `repo_owner` is often empty (the comment in
    kernel/go/lanes.py says so); those rows must keep resolving.
    """
    async with _conn() as c:
        gid = await _goal(c, f"{PREFIX}-lane-parent")
        await _repo(c, gid, f"{PREFIX}-r3")
        await _lane(c, gid, f"{PREFIX}-l3", "release", parent=f"{PREFIX}-r3",
                    own_identity=False)
        assert await _trunk(c, gid) == "release"


@pytest.mark.asyncio
async def test_the_fallback_tie_break_is_oldest_declared():
    """THE SAFETY PROPERTY. Ungranted work resolves exactly as it did
    before, so applying this to live goals cannot re-point anybody's
    merges. The tie-break stays dumb on purpose -- reaching it at all
    means a grant is missing, and the fix for that is the grant."""
    async with _conn() as c:
        gid = await _goal(c, f"{PREFIX}-lane-order")
        await _repo(c, gid, f"{PREFIX}-r4")
        await _lane(c, gid, f"{PREFIX}-l4a", "main", parent=f"{PREFIX}-r4")
        await _lane(c, gid, f"{PREFIX}-l4b", "refactor", parent=f"{PREFIX}-r4")
        assert await _trunk(c, gid) == "main", (
            "the fallback changed for an existing goal"
        )


@pytest.mark.asyncio
async def test_an_archived_lane_is_not_a_lane():
    """Archiving the old trunk is the no-code way to switch; it must work."""
    async with _conn() as c:
        gid = await _goal(c, f"{PREFIX}-lane-arch")
        await _repo(c, gid, f"{PREFIX}-r6")
        await _lane(c, gid, f"{PREFIX}-l6a", "main", parent=f"{PREFIX}-r6",
                    status="archived")
        await _lane(c, gid, f"{PREFIX}-l6b", "refactor", parent=f"{PREFIX}-r6")
        assert await _trunk(c, gid) == "refactor"


@pytest.mark.asyncio
async def test_with_no_lane_the_repo_default_branch_holds():
    async with _conn() as c:
        gid = await _goal(c, f"{PREFIX}-lane-fallback")
        await _repo(c, gid, f"{PREFIX}-r7", default_branch="dev")
        assert await _trunk(c, gid) == "dev"


@pytest.mark.asyncio
async def test_a_deleted_repo_with_no_lane_does_not_resurrect_its_branch():
    """`default_branch` is read off ACTIVE repo rows only. Reading it off a
    row the operator deleted would be un-deleting it a field at a time."""
    async with _conn() as c:
        gid = await _goal(c, f"{PREFIX}-lane-ghost")
        await _repo(c, gid, f"{PREFIX}-r8", status="deleted", default_branch="dev")
        assert await _trunk(c, gid) == "main"


def test_the_migration_is_in_sequence():
    """A migration not in SEQUENCE never runs."""
    from app.services.tests.shipped_tree import (
        IS_DISTRIBUTION, baseline_mentions, table_has_column)
    if IS_DISTRIBUTION:
        # The pair's net result: the designation column was added and then
        # dropped, so neither it nor its index is in the schema.
        assert not table_has_column("goal_resources", "is_default_lane")
        assert not baseline_mentions(r"goal_resources_default_lane_idx")
        return
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    seq = (root / "migrations" / "SEQUENCE").read_text(encoding="utf-8").split()
    for name in ("trunk_lane_can_be_designated",
                 "trunk_lane_has_no_default_designation"):
        assert name in seq
        assert (root / "migrations" / f"{name}.sql").exists()
