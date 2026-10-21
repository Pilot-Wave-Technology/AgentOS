"""Two write lanes of one repo survive the real resolvers, in the real schema.

The unit tests next door prove the rule. These prove the WIRING: that the SQL
selects the column the rule reads, that the collapse in
`resolve_repos_for_story` keeps both lanes, that `_clone_descriptors` gives
them different directories, and that `story_workspace_nodes` can hold both
with exactly one marked primary.

WHY AGAINST POSTGRES. Every one of these defects was a shape mismatch, not a
logic error: a column that was not selected, a dictionary key that collapsed
two rows, a primary-key tuple that happened to be unique. A fake store agrees
with whatever the code does, which is how the repo-keyed collapse survived
having tests at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import asyncpg
import pytest

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
OWNER, REPO = "pilotwave", "two_lane_repo"


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


class _PoolShim:
    """`resolve_repos_for_story` takes a pool; a transaction-scoped
    connection is what keeps these tests from leaving rows behind."""

    def __init__(self, conn):
        self._conn = conn

    async def fetch(self, *a, **kw):
        return await self._conn.fetch(*a, **kw)

    async def fetchrow(self, *a, **kw):
        return await self._conn.fetchrow(*a, **kw)

    async def fetchval(self, *a, **kw):
        return await self._conn.fetchval(*a, **kw)

    async def execute(self, *a, **kw):
        return await self._conn.execute(*a, **kw)

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


@contextlib.contextmanager
def _multi_repo_on():
    """`multi_repo_enabled` forced on for the duration.

    The flag defaults to False, and `is_write_repo_set_incomplete`
    returns False immediately when it is off — so without this the
    assertions below would pass on a code path that never ran.
    """
    from unittest.mock import patch

    from app import config as config_module
    from app.services import operator_provisioning

    real = config_module.get_settings()

    class _On:
        def __getattr__(self, name):
            return True if name == "multi_repo_enabled" else getattr(real, name)

    with patch.object(operator_provisioning, "get_settings", lambda: _On()):
        yield


async def _two_lane_story(conn) -> tuple[str, str]:
    """A story granted two engine-cut branches of ONE repository."""
    tag = uuid.uuid4().hex[:8]
    goal, story = f"L{tag}", f"s{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')",
        goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'two lanes','accepted','alice')", story, goal)
    for lane in ("backend", "frontend"):
        rid = f"gb_{tag}_{lane}"
        await conn.execute(
            "INSERT INTO goal_resources (id, goal_id, kind, role, name, uri, "
            "  created_by, repo_owner, repo_name, branch_name, base_branch, "
            "  clone_url, status, default_writable) "
            "VALUES ($1,$2,'branch','feature',$3,$4,'engine',$5,$6,$7,$8,$9, "
            "        'active', true)",
            rid, goal, f"{REPO}:{lane}",
            f"https://github.com/{OWNER}/{REPO}/tree/feat-{lane}",
            OWNER, REPO, f"feat/{goal}-{story}-{lane}", lane,
            f"https://github.com/{OWNER}/{REPO}.git")
        await conn.execute(
            "INSERT INTO story_resources (story_id, resource_id, relation) "
            "VALUES ($1,$2,'produces')", story, rid)
    return goal, story


@pytest.mark.asyncio
async def test_both_lanes_survive_the_resolver():
    """THE `continue`. Keyed by repository, the second lane was dropped
    here -- with no log line, so the story simply had one fewer
    checkout than it was granted."""
    from app.services.operator_provisioning import resolve_repos_for_story

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, story = await _two_lane_story(conn)
        resolved = await resolve_repos_for_story(_PoolShim(conn), goal, story)
        assert len(resolved) == 2, (
            f"two lanes were granted and {len(resolved)} resolved")
        assert {r["base_branch"] for r in resolved} == {"backend", "frontend"}
        assert all(r["is_write"] for r in resolved)
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_the_two_lanes_get_two_directories():
    """Same repository, so the same `repos/<owner>__<name>` -- which is
    a collision, not a layout. Cloning both there would leave whichever
    ran second holding the other's checkout."""
    from app.services.operator_provisioning import (_clone_descriptors,
                                                    resolve_repos_for_story)

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, story = await _two_lane_story(conn)
        resolved = await resolve_repos_for_story(_PoolShim(conn), goal, story)
        clones = _clone_descriptors(resolved[0], resolved[1:])
        subdirs = sorted(c["subdir"] for c in clones)
        assert subdirs == [f"repos/{OWNER}__{REPO}@backend",
                           f"repos/{OWNER}__{REPO}@frontend"], subdirs
        assert len(set(subdirs)) == 2
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_one_lane_is_left_exactly_where_it_was():
    """THE REGRESSION TEST FOR EVERY EXISTING STORY. A single-lane story
    must keep `repos/<owner>__<name>`: it is written into every recorded
    node, every plan root and every CLAUDE.md already on disk, and a
    suffix here would move all of them at once."""
    from app.services.operator_provisioning import (_clone_descriptors,
                                                    resolve_repos_for_story)

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, story = await _two_lane_story(conn)
        await conn.execute(
            "DELETE FROM story_resources WHERE story_id = $1 "
            "  AND resource_id LIKE '%frontend'", story)
        resolved = await resolve_repos_for_story(_PoolShim(conn), goal, story)
        clones = _clone_descriptors(resolved[0], resolved[1:])
        assert [c["subdir"] for c in clones] == [f"repos/{OWNER}__{REPO}"]
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_both_lanes_are_recorded_and_exactly_one_is_primary():
    """`story_workspace_nodes` is keyed (story, repo, BRANCH), so it can
    hold both. The sticky-primary decision was keyed on the repo alone,
    which marked BOTH lanes primary -- and `primary_write_subdir` then
    picks whichever sorts first and may pick the other one next time,
    moving the story's plan root between its own two lanes."""
    from app.services.operator_provisioning import (
        _clone_descriptors, _record_story_workspace_nodes,
        resolve_repos_for_story)

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, story = await _two_lane_story(conn)
        resolved = await resolve_repos_for_story(_PoolShim(conn), goal, story)
        clones = _clone_descriptors(resolved[0], resolved[1:])
        pool = _PoolShim(conn)

        await _record_story_workspace_nodes(pool, story, clones)
        rows = await conn.fetch(
            "SELECT branch_name, subdir, is_primary FROM story_workspace_nodes"
            " WHERE story_id = $1 ORDER BY subdir", story)
        assert len(rows) == 2
        assert sum(1 for r in rows if r["is_primary"]) == 1
        first = [r["branch_name"] for r in rows if r["is_primary"]][0]

        # STICKY ACROSS RE-PROVISIONS, in the reverse order. This is the
        # instability the decision exists to remove, and keying it on
        # the repo alone reintroduced it for a two-lane story.
        await _record_story_workspace_nodes(pool, story, list(reversed(clones)))
        again = await conn.fetch(
            "SELECT branch_name, is_primary FROM story_workspace_nodes "
            " WHERE story_id = $1 AND is_primary", story)
        assert len(again) == 1
        assert again[0]["branch_name"] == first, (
            "the primary lane moved between provisions")
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_half_provisioned_two_lane_story_is_reported_incomplete():
    """`is_write_repo_set_incomplete` is what notices a workspace that
    finished cloning without everything the story was granted — it is
    the freshness check the kickoff dispatcher relies on. Comparing by
    repository id alone, two lanes of one repo share that id, so
    recording EITHER lane read as "complete" and the missing one was
    never re-provisioned. The story then runs with one of its two
    checkouts and nothing anywhere says so."""
    from app.services.operator_provisioning import (
        _clone_descriptors, _record_story_workspace_nodes,
        is_write_repo_set_incomplete, resolve_repos_for_story)

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal, story = await _two_lane_story(conn)
        pool = _PoolShim(conn)
        resolved = await resolve_repos_for_story(pool, goal, story)
        clones = _clone_descriptors(resolved[0], resolved[1:])

        # FORCED ON rather than skipped. The predicate short-circuits to
        # False when `multi_repo_enabled` is off, so a skip here would
        # report green on the one configuration the bug needs.
        with _multi_repo_on():
            # Record ONLY the first lane — the half-provisioned state.
            await _record_story_workspace_nodes(pool, story, clones[:1])
            assert await is_write_repo_set_incomplete(
                pool, goal, story) is True, (
                "one of two granted lanes is recorded and this says complete")

            await _record_story_workspace_nodes(pool, story, clones)
            assert await is_write_repo_set_incomplete(
                pool, goal, story) is False
    finally:
        await tx.rollback()
        await conn.close()
