"""A local run can keep what it produced.

`kernel.artifact_manifest.execution_id` was NOT NULL and a local run creates no
execution -- the conductor is never involved -- so an errand run in an editor
could produce its whole deliverable and have nowhere to put it. A coding story
had the same hole for the outputs that are not the tree.

THE BYTES WERE NEVER THE PROBLEM. `aput(data: bytes)` is the entire storage
signature, the key is a sha256 of the content, and the bucket comes from the
control plane's own environment -- so both lanes already wrote one store and
deduped against each other. Only the CATALOGUE was execution-shaped.

What these pin is the part that is easy to half-do: the row is written, the
goal can see it, the chain does not fork across lanes, and the pod path is
untouched.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


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


async def _story(conn, sid: str, work_kind: str = "errand"):
    await conn.execute("INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
                       f"{sid}-goal", f"fixture {sid}", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, created_by, execution_mode, work_kind) "
        "VALUES ($1,$2,$3,$4,'local',$5)",
        sid, f"{sid}-goal", "artifact fixture", f"{PREFIX}-user", work_kind)
    return f"{sid}-goal"


@contextlib.contextmanager
def _pool_is(conn):
    from app.services import local_artifacts
    orig = local_artifacts.get_pool
    local_artifacts.get_pool = lambda: conn
    try:
        yield
    finally:
        local_artifacts.get_pool = orig


@pytest.mark.asyncio
async def test_a_local_errand_can_keep_its_answer():
    from app.services.local_artifacts import emit_local_artifact

    async with _conn() as conn:
        sid = f"{PREFIX}-art-emit"
        goal = await _story(conn, sid)
        with _pool_is(conn):
            r = await emit_local_artifact(
                sid, "report", "findings.md", b"# what I found\n", "the answer")
        assert r["ok"] is True
        row = await conn.fetchrow(
            "SELECT story_id, goal_id, execution_id, role, name, bytes "
            "  FROM kernel.artifact_manifest WHERE artifact_id = $1", r["artifact_id"])
        assert row["story_id"] == sid
        assert row["execution_id"] is None
        assert row["goal_id"] == goal, (
            "goal_id must be ON THE ROW -- the goal artifact list filters on it, "
            "and reaching through the execution join is exactly what hid local rows"
        )
        assert row["role"] == "report" and row["name"] == "findings.md"


@pytest.mark.asyncio
async def test_the_goal_can_see_it():
    # The half-landing this guards: stored, catalogued, invisible. The list
    # query INNER JOINed executions, so a story-keyed row matched nothing.
    from app.services.local_artifacts import emit_local_artifact

    async with _conn() as conn:
        sid = f"{PREFIX}-art-visible"
        goal = await _story(conn, sid)
        with _pool_is(conn):
            await emit_local_artifact(sid, "report", "r.md", b"body", "s")
        seen = await conn.fetch(
            """
            SELECT m.artifact_id
              FROM kernel.artifact_manifest m
              LEFT JOIN kernel.executions e ON e.id = m.execution_id
             WHERE COALESCE(e.goal_id, m.goal_id) = $1
            """, goal)
        assert len(seen) == 1, "a local artifact is invisible to its own goal"


@pytest.mark.asyncio
async def test_re_emitting_supersedes_rather_than_forking():
    from app.services.local_artifacts import emit_local_artifact

    async with _conn() as conn:
        sid = f"{PREFIX}-art-chain"
        await _story(conn, sid)
        with _pool_is(conn):
            first = await emit_local_artifact(sid, "report", "r.md", b"v1", "one")
            second = await emit_local_artifact(sid, "report", "r.md", b"v2", "two")
        assert second["supersedes"] == first["artifact_id"], (
            "two runs of one errand leave two answers and no way to tell which "
            "is current"
        )

        from app.services.errand_result import artifacts_for_story
        heads = await artifacts_for_story(conn, sid)
        assert [h["artifact_id"] for h in heads] == [second["artifact_id"]], (
            "the GO report would list every version as a separate deliverable"
        )


@pytest.mark.asyncio
async def test_a_story_can_emit_too():
    # Not errand-only: a benchmark, a migration note, an analysis is a real
    # output that does not belong in the diff.
    from app.services.local_artifacts import emit_local_artifact

    async with _conn() as conn:
        sid = f"{PREFIX}-art-story"
        await _story(conn, sid, work_kind="story")
        with _pool_is(conn):
            r = await emit_local_artifact(sid, "dataset", "bench.csv", b"a,b\n1,2\n", "")
        assert r["ok"] is True


@pytest.mark.asyncio
async def test_the_refusals_say_which_rule():
    from app.services.local_artifacts import ArtifactRefused, emit_local_artifact

    async with _conn() as conn:
        sid = f"{PREFIX}-art-refuse"
        await _story(conn, sid)
        with _pool_is(conn):
            for role, name, data, expect in (
                ("wrong", "r.md", b"x", "role must be one of"),
                ("report", "   ", b"x", "no name"),
                ("report", "r.md", b"", "no bytes"),
            ):
                with pytest.raises(ArtifactRefused) as caught:
                    await emit_local_artifact(sid, role, name, data)
                assert expect in str(caught.value), (
                    f"the refusal for {role}/{name!r} does not say which rule"
                )


@pytest.mark.asyncio
async def test_every_version_of_a_report_is_catalogued():
    """The grantable resource must follow the answer, not stick on version 1.

    PRE-EXISTING AND SHARED, found by the local tests and reproduced on the pod
    path before anything was changed. `goal_resources` has carried
    `UNIQUE (goal_id, kind, name)` since migration 014. The catalogue label was
    `"{name} - from {produced_by}"`, identical for every version of one report,
    so the second insert hit that constraint; `ON CONFLICT (id)` cannot absorb a
    conflict on a different index, and the caller's savepoint swallowed the
    raise. The manifest chained version 2 correctly while the resource later
    work can be GRANTED stayed pointed at version 1 -- for an errand, whose
    whole deliverable is the report, that is the stale answer.
    """
    from app.services.local_artifacts import emit_local_artifact

    async with _conn() as conn:
        sid = f"{PREFIX}-art-versions"
        goal = await _story(conn, sid)
        with _pool_is(conn):
            one = await emit_local_artifact(sid, "report", "r.md", b"v1", "first")
            two = await emit_local_artifact(sid, "report", "r.md", b"v2", "second")

        assert one["resource_id"] and two["resource_id"], (
            "a version that is not catalogued cannot be granted to later work"
        )
        assert one["resource_id"] != two["resource_id"]
        rows = await conn.fetch(
            "SELECT id, name FROM goal_resources WHERE goal_id = $1 ORDER BY id", goal)
        assert len(rows) == 2, "the second version was dropped from the catalogue"
        names = [r["name"] for r in rows]
        assert len(set(names)) == 2, (
            "both versions carry the same label, which is what collided with "
            "uq_goal_resource_kind_name in the first place"
        )
        for r in rows:
            assert r["name"].startswith("r.md"), (
                "the file name must still lead the label -- it is what a person "
                "scans a picker for"
            )
