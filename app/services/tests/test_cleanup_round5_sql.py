"""F1–F4 — the four fixes that only SQL and a real schema can verify.

These four were all found on live KER25 runs, and three of them are SQL
that reads fine and is wrong:

  * **F1** a branch deletion that failed left the node at READY — the
    same value a successful one leaves — so nothing could list the
    failures and nothing retried them.
  * **F2** thread closure was wired to the merge path, which an errand
    cannot reach: `close_threads_for_landed` requires a MERGED node and
    D1 guarantees an errand never lands. Six live threads were stranded
    on terminal stories when this was written.
  * **F3** a `needs` grant on the lane a story's own branch was cut from
    produced a SECOND full clone of the same commit.
  * **F4** the lease sweep's real period was 5–7 minutes against a
    15-minute TTL.

Run against the local stack; SKIP when no database is reachable. Every
test rolls its transaction back — the dev database is never mutated.

`asyncio.run`, not `@pytest.mark.asyncio`: pytest-asyncio is NOT
installed here, and an async test without it ERRORS rather than skipping
— which is how twelve tests once sat dead behind a green gate.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import re

import pytest

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


def _candidate_dsns() -> list[str]:
    seen, out = set(), []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


async def _connect_or_skip(search_path: str = ""):
    attempts: list[str] = []
    for dsn in _candidate_dsns():
        settings = {"search_path": search_path} if search_path else None
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings=settings), timeout=5)
        except Exception as exc:                                # noqa: BLE001
            attempts.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
            continue
        return conn
    pytest.skip("no local database — start local-development/scripts/"
                f"stack.ps1. Tried: {'; '.join(attempts)}")


@contextlib.asynccontextmanager
async def _rolled_back():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


# ── F1 · deleted and failed-to-delete are different rows ─────────────────

def test_the_schema_can_tell_deleted_from_failed():
    async def go():
        async with _rolled_back() as conn:
            cols = {r["column_name"]: r for r in await conn.fetch(
                "SELECT column_name, is_nullable FROM information_schema."
                "columns WHERE table_name = 'git_nodes'")}
            for name in ("branch_deleted_at", "branch_cleanup_error",
                         "branch_cleanup_tries"):
                assert name in cols, (
                    f"{name} is missing; migration "
                    f"git_nodes_branch_cleanup_state not applied")
            # The counter must never be NULL or the retry comparison
            # silently matches nothing.
            assert cols["branch_cleanup_tries"]["is_nullable"] == "NO"
    asyncio.run(go())


def test_the_retry_only_looks_at_what_was_actually_attempted():
    """`tries > 0` is what makes this a RETRY and not a second cleanup
    path. A node nobody asked about yet is not its business — sweeping
    those would delete branches the merge path deliberately left alone."""
    async def go():
        async with _rolled_back() as conn:
            from app.services.git_graph import branch_cleanup_state as st

            src = open("app/services/git_graph/branch_cleanup_state.py",
                       encoding="utf-8").read()
            assert "branch_cleanup_tries > 0" in src
            assert "branch_cleanup_tries < $1" in src
            # And both queries actually run on this schema.
            assert isinstance(await st.pending(conn), list)
            assert isinstance(await st.exhausted(conn), list)
    asyncio.run(go())


def test_a_failure_increments_and_a_success_settles():
    async def go():
        async with _rolled_back() as conn:
            from app.services.git_graph.branch_cleanup_state import record

            repo = await conn.fetchval("SELECT id FROM git_repos LIMIT 1")
            node = await conn.fetchval("SELECT id FROM git_nodes LIMIT 1")
            if not (repo and node):
                pytest.skip("no git_nodes rows to exercise against")

            await record(conn, node, "github refused")
            row = await conn.fetchrow(
                "SELECT branch_deleted_at, branch_cleanup_error, "
                "branch_cleanup_tries FROM git_nodes WHERE id = $1", node)
            assert row["branch_deleted_at"] is None, (
                "a failure marked the branch gone")
            assert row["branch_cleanup_error"] == "github refused"
            assert row["branch_cleanup_tries"] >= 1

            await record(conn, node, "deleted")
            row = await conn.fetchrow(
                "SELECT branch_deleted_at, branch_cleanup_error "
                "FROM git_nodes WHERE id = $1", node)
            assert row["branch_deleted_at"] is not None
            assert row["branch_cleanup_error"] is None, (
                "the stale failure survived the success that replaced it")
    asyncio.run(go())


def test_a_branch_that_is_not_ours_stops_being_retried():
    """"Not an engine branch" is a settled answer, not a failure. Left
    as a failure it would be re-asked five times and then appear in the
    list of things needing a person."""
    async def go():
        async with _rolled_back() as conn:
            from app.services.git_graph.branch_cleanup_state import (
                NOT_OURS, record)

            node = await conn.fetchval("SELECT id FROM git_nodes LIMIT 1")
            if not node:
                pytest.skip("no git_nodes rows to exercise against")
            await record(conn, node, NOT_OURS[0])
            row = await conn.fetchrow(
                "SELECT branch_deleted_at, branch_cleanup_tries "
                "FROM git_nodes WHERE id = $1", node)
            assert row["branch_deleted_at"] is not None
            assert row["branch_cleanup_tries"] == 0, (
                "a settled answer counted as a failed attempt")
    asyncio.run(go())


# ── F2 · an errand's thread closes on acceptance ─────────────────────────

def test_an_errand_thread_closes_without_a_merge():
    async def go():
        async with _rolled_back() as conn:
            from app.services.git_graph.story_threads import (
                close_thread_for_errand)

            row = await conn.fetchrow(
                "SELECT t.id AS thread, t.story_id, t.goal_id FROM threads t "
                "WHERE t.left_goal_at IS NULL AND t.story_id IS NOT NULL "
                "LIMIT 1")
            if row is None:
                pytest.skip("no open story thread to exercise against")

            closed = await close_thread_for_errand(conn, row["story_id"])
            assert row["thread"] in closed

            after = await conn.fetchrow(
                "SELECT left_goal_at, status FROM threads WHERE id = $1",
                row["thread"])
            assert after["left_goal_at"] is not None
            assert after["status"] == "archived"

            # The story's pointer goes too — a pointer at an archived
            # thread is the stale-binding bug that resumed an
            # un-imported story.
            ptr = await conn.fetchval(
                "SELECT active_thread_id FROM stories WHERE id = $1",
                row["story_id"])
            assert ptr is None
    asyncio.run(go())


def test_closing_twice_is_a_no_op():
    """`left_goal_at IS NULL` in the WHERE is what makes this idempotent.
    Acceptance can be re-delivered, and a second close must not re-stamp
    a thread with a later time than the one it actually closed at."""
    async def go():
        async with _rolled_back() as conn:
            from app.services.git_graph.story_threads import (
                close_thread_for_errand)

            row = await conn.fetchrow(
                "SELECT story_id FROM threads WHERE left_goal_at IS NULL "
                "AND story_id IS NOT NULL LIMIT 1")
            if row is None:
                pytest.skip("no open story thread to exercise against")
            first = await close_thread_for_errand(conn, row["story_id"])
            second = await close_thread_for_errand(conn, row["story_id"])
            assert first and second == []
    asyncio.run(go())


# ── F3 · the duplicate trunk clone ───────────────────────────────────────

def test_the_mount_query_is_valid_sql_with_the_collapse_clause():
    """The collapse lives in a WHERE clause of a 100-line query. Nothing
    short of the real planner can say it parses, and a query that
    throws would take out every mount, not just the duplicate."""
    async def go():
        conn = await _connect_or_skip()
        try:
            src = open("kernel/workspace/mounts.py", encoding="utf-8").read()
            i = src.index("FROM story_resources sr")
            sql = src[src.rindex('"""', 0, i) + 3:src.index('"""', i)]
            await conn.execute(f"PREPARE f3_check AS {sql}")
            await conn.execute("DEALLOCATE f3_check")
        finally:
            await conn.close()
    asyncio.run(go())


def test_the_collapse_is_narrow_enough_to_spare_a_cross_lane_read():
    """THE ARM THAT MUST SURVIVE. A `cal_frontend` story granted
    `needs cal_backend` has a different `trunk_branch`, so it does not
    match — that mount exists because such a story "had no way to see
    what the backend lane already contains"."""
    src = open("kernel/workspace/mounts.py", encoding="utf-8").read()
    i = src.index("F3/R3-P2")
    clause = src[i:i + 2200]
    assert "gn3.trunk_branch = gr.branch_name" in clause, (
        "the collapse does not require the branch to have been cut from "
        "this very lane, so it would eat cross-lane reads too")
    assert "sr.relation = 'needs'" in clause, (
        "the collapse is not restricted to read grants")
    assert "gn3.state NOT IN ('MERGED', 'ABANDONED')" in clause, (
        "a finished node would still suppress the read mount")


# ── F4 · the lease goes when the run does ────────────────────────────────

def test_both_sweep_forms_run_on_the_real_schema():
    """The narrow form's `($3 || ' seconds')::interval` is the part most
    likely to be wrong, and its failure mode is a query that matches
    nothing — indistinguishable from "no leases to release"."""
    async def go():
        conn = await _connect_or_skip(search_path="kernel,public")
        try:
            from kernel.credentials.sweep import sweep

            full = await sweep(conn)
            narrow = await sweep(conn, since_seconds=600)
            assert set(full) == {"checked", "released"}
            assert set(narrow) == {"checked", "released"}
            # The narrow form must be a SUBSET — if it checked as much as
            # the full one, the window is not being applied.
            assert narrow["checked"] <= full["checked"]
        finally:
            await conn.close()
    asyncio.run(go())


def test_the_prompt_sweep_runs_every_tick_and_the_full_one_does_not():
    # The loop left the console: it IS the execution plane and was
    # starting as a side effect of a debug port bind.
    # MOVED WITH THE CODE (D2a). `service.py` is now the PROCESS --
    # entrypoint, signals, startup order -- and the tick is its own
    # module. A guard reading the file its subject has left does not
    # fail; it passes on no evidence.
    src = open("kernel/worker/service_tick.py", encoding="utf-8").read()
    i = src.index("credential sweep failed")
    block = src[max(0, i - 1800):i]
    assert "since_seconds=0 if full else" in block, (
        "the sweep still runs in only one form")
    assert "tick % SWEEP_EVERY_TICKS == 0" in block, (
        "the full backstop sweep is gone; a worker killed mid-run would "
        "leave its lease for ever")


def test_the_release_is_not_in_the_control_panel():
    """THE FIX THAT COULD NOT RUN. The lease file is on the WORKER's
    filesystem. A release in `go_wake_events` would find no file, delete
    nothing, log success, and read as fixed."""
    src = open("app/services/go_wake_events.py", encoding="utf-8").read()
    assert "_release_cloud_lease" not in src

    # THE PATH IS FOLLOWED, NOT MERELY PRESENT, and the old version of
    # this assertion is why. It read `assert "kernel/worker/service.py"
    # in src` -- it checked that A path was named, never that it was the
    # RIGHT one. D2a split service.py 299 -> 110 and the sweep moved to
    # `service_tick.py`; the comment went on naming a 110-line facade
    # that no longer contains it, and this guard went on passing. A
    # guard that cannot tell a correct pointer from a stale one is the
    # same vacuous pass 11.5f describes, one layer up: the next person
    # follows the comment, does not find the release, and re-adds it
    # here -- which is the exact outcome the guard exists to prevent.
    #
    # And it lives in a `*_sql.py` file, so `-k "not sql"` -- the run
    # everyone uses, because the sql suites need three DSNs and take 27
    # minutes -- never executed it. It was stale and silent together.
    named = re.findall(r"`(kernel/[\w/]+\.py)`", src)
    assert named, (
        "nothing records WHERE the release actually happens, so the next "
        "person re-adds it here")
    for path in named:
        assert pathlib.Path(path).is_file(), (
            f"the comment sends the next reader to {path}, which does "
            f"not exist -- almost certainly a split that moved the code "
            f"and left the pointer")
    assert any("credential_sweep" in pathlib.Path(p).read_text(encoding="utf-8")
               for p in named), (
        f"{named} is named as where the release happens, but none of "
        f"them runs the credential sweep that releases the lease. The "
        f"pointer is stale, so the comment now argues for re-adding the "
        f"release here.")


def test_the_collapse_never_removes_the_only_checkout():
    """F3's REGRESSION, found by an executor reading its own workspace.

    The collapse is a DEDUPE: it removes a second checkout of content the
    working clone already has. With no other grant on that repo it
    removed the ONLY one, and the run got a workspace containing nothing
    but WORKSPACE.md.

    Errand 2bc47dfb held exactly one grant -- `needs` on the cal_kernel
    lane -- and its engine node existed but was still PLANNED, so no
    feature-branch grant had been made yet. Both halves of the collapse
    condition matched and the mount vanished. The executor opened its
    plan with "clone the repository", which read like a model ignoring
    its grants and was in fact the only correct thing it could do.

    So the collapse now also requires that something else of that repo
    survives."""
    src = open("kernel/workspace/mounts.py", encoding="utf-8").read()
    i = src.index("F3/R3-P2")
    clause = src[i:i + 3600]
    assert "sr4.resource_id <> sr.resource_id" in clause, (
        "the collapse does not check that another grant on the repo "
        "exists, so it can remove the only checkout a run has")
    assert "gr4.kind IN ('repo', 'branch')" in clause


def test_a_lane_only_grant_still_mounts():
    """The live shape that broke: one `needs` on a trunk lane, nothing
    else. It must produce a checkout."""
    async def go():
        conn = await _connect_or_skip()
        try:
            import io as _io

            from kernel.credentials.clouds import (ALL_ENVIRONMENT_KINDS,
                                                   ALL_IDENTITY_KINDS)
            src = _io.open("kernel/workspace/mounts.py",
                           encoding="utf-8").read()
            i = src.index("FROM story_resources sr")
            sql = src[src.rindex('"""', 0, i) + 3:src.index('"""', i)]
            cloud = list(ALL_ENVIRONMENT_KINDS) + list(ALL_IDENTITY_KINDS)

            story = await conn.fetchval(
                """
                SELECT sr.story_id FROM story_resources sr
                  JOIN goal_resources gr ON gr.id = sr.resource_id
                 WHERE sr.relation = 'needs' AND gr.kind = 'branch'
                   AND gr.role = 'trunk'
                   AND NOT EXISTS (
                       SELECT 1 FROM story_resources s2
                         JOIN goal_resources g2 ON g2.id = s2.resource_id
                        WHERE s2.story_id = sr.story_id
                          AND s2.resource_id <> sr.resource_id
                          AND g2.kind IN ('repo', 'branch'))
                 LIMIT 1
                """)
            if not story:
                pytest.skip("no lane-only story in this database")
            goal = await conn.fetchval(
                "SELECT goal_id FROM stories WHERE id = $1", story)
            rows = await conn.fetch(sql, goal, story, cloud)
            assert rows, (
                f"story {story} holds a lane grant and got NO mounts — "
                f"its run would start with an empty workspace")
        finally:
            await conn.close()
    asyncio.run(go())
