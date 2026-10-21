"""`mounts_for` must not invent a git mount it cannot clone.

THE BUG. With no `execution_mounts` rows, `mounts_for` fell back to a
single git mount sourced from `executions.workspace_source` — and returned
that mount even when the column was NULL. Nothing can clone from None, so
`prepare_workspace` found no `.git`, raised `workspace incomplete`, and the
worker's dispatch sweep caught it, wrote `stall_reason` and NEVER
DISPATCHED. The run sat at PENDING for ever.

WHY IT STAYED HIDDEN. The E0 console calls `prepare_workspace` inside the
contract try/except, which swallows the error — so the one path anybody
drove by hand was the one path that worked. No test named it: grepping
`workspace incomplete` finds only the raise site.

The fallback itself is still right for what it was written for. An
execution that HAS a `workspace_source` predates `execution_mounts` and
must still get its repo. It is just not a reason to invent a mount when
there is nothing to mount.

Skips cleanly with no database. `asyncio.run` in a sync wrapper because
pytest-asyncio is not installed here and an async test without it ERRORS
rather than skips.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip


def _run(fn):
    async def go():
        conn = await _connect_or_skip()
        try:
            if not await conn.fetchval(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'kernel' "
                    "AND table_name = 'execution_mounts'"):
                pytest.skip("no kernel schema here")
            await conn.execute("SET search_path TO kernel, public")
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


def test_no_mounts_and_no_source_yields_no_mounts():
    """THE REGRESSION. An execution with neither must get an empty tuple,
    not a git mount with `source=None`."""
    from kernel.workspace.mounts import mounts_for

    async def check(conn):
        eid = await conn.fetchval("""
            SELECT e.id FROM executions e
             WHERE e.workspace_source IS NULL
               AND NOT EXISTS (SELECT 1 FROM execution_mounts m
                                WHERE m.execution_id = e.id)
             LIMIT 1""")
        if eid is None:
            pytest.skip("no mountless executions in this database")
        assert await mounts_for(conn, eid) == ()
    _run(check)


def test_no_execution_anywhere_gets_a_git_mount_it_cannot_clone():
    """The property, across every row rather than one sample.

    A git mount with no source is the exact shape that makes
    `prepare_workspace` raise, so none may exist for any execution.
    """
    from kernel.workspace.mounts import mounts_for

    async def check(conn):
        ids = [r["id"] for r in await conn.fetch(
            "SELECT id FROM executions ORDER BY id DESC LIMIT 200")]
        if not ids:
            pytest.skip("no executions")
        bad = []
        for eid in ids:
            for m in await mounts_for(conn, eid):
                if m.binding == "git" and not m.source:
                    bad.append(eid)
        assert not bad, (
            f"{len(bad)} executions get a sourceless git mount — each one "
            f"raises `workspace incomplete` and never dispatches: {bad[:5]}")
    _run(check)


def test_a_real_workspace_source_still_gets_its_repo():
    """The fallback is not being deleted, only narrowed. An execution that
    has a source must still receive exactly one git mount at the root —
    that bridge is what keeps pre-`execution_mounts` runs working."""
    from kernel.workspace.mounts import mounts_for

    async def check(conn):
        eid = await conn.fetchval("""
            SELECT e.id FROM executions e
             WHERE e.workspace_source IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM execution_mounts m
                                WHERE m.execution_id = e.id)
             LIMIT 1""")
        if eid is None:
            # None in this database — assert the branch directly instead,
            # so the test still protects the behaviour rather than
            # skipping into a false green.
            import inspect

            src = inspect.getsource(mounts_for)
            assert "if not legacy:" in src and "return ()" in src
            assert 'binding="git", source=legacy' in src, (
                "the legacy bridge was removed, not narrowed")
            return
        mounts = await mounts_for(conn, eid)
        assert len(mounts) == 1
        assert mounts[0].binding == "git" and mounts[0].source
        assert mounts[0].at == ""
    _run(check)


def test_mounted_executions_are_untouched():
    """Story executors are the ones in daily use. An execution WITH rows
    returns early, before the fallback is reached at all."""
    from kernel.workspace.mounts import mounts_for

    async def check(conn):
        rows = await conn.fetch("""
            SELECT e.id, (SELECT count(*) FROM execution_mounts m
                           WHERE m.execution_id = e.id) AS n
              FROM executions e
             WHERE EXISTS (SELECT 1 FROM execution_mounts m
                            WHERE m.execution_id = e.id)
             ORDER BY e.id DESC LIMIT 20""")
        if not rows:
            pytest.skip("no mounted executions")
        for r in rows:
            got = await mounts_for(conn, r["id"])
            assert got, f"execution {r['id']} lost its mounts"
            assert all(m.source for m in got if m.binding == "git")
    _run(check)
