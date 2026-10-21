"""A failed clone must not strand a run for ever — against a real PostgreSQL.

THE BUG THIS PINS. `dispatch_pass` prepared workspaces for `state = 'PENDING'`
rows only, and admitted rows unconditionally in the same pass. So an execution
whose clone failed was admitted anyway, the dispatch guard correctly refused to
hand a model a workspace that did not exist (`workspace_path IS NULL`), and the
one loop that could have re-tried the clone never looked at it again. Permanent,
in a legitimate-looking state, with a stall string nobody was watching.

Observed on KER15: two stories admitted at 10:11 and 10:12 with `repos/` empty,
still empty hours later. The trigger was a 404 from the credential broker — a
separate fix — but fixing the trigger alone would not have moved either run,
because nothing was going to ask again.

`workspace_incomplete` is the recovery set, and it is defined by the SAME
predicate as the dispatch guard so the two cannot drift into disagreeing about
which runs are stranded. These tests pin that agreement, which is the part that
would rot silently.

**How to run.** Needs the local stack's PostgreSQL with the `kernel` schema;
SKIPS when none is reachable. All work happens in one transaction that is
always rolled back.
"""
from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from kernel.worker.workspace_retry import workspace_incomplete

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
GOAL = "zz-wsretry-goal"


def _candidate_dsns() -> list[str]:
    seen, out = set(), []
    for dsn in (os.environ.get("KERNEL_TEST_DSN"),
                os.environ.get("KERNEL_DATABASE_URL"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


async def _connect_or_skip():
    """Resolved at TEST time, not import — a sibling conftest sets a dummy
    DATABASE_URL at import, and an import-time probe would make this skip
    silently in a full-suite run while passing on its own."""
    attempts: list[str] = []
    for dsn in _candidate_dsns():
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            attempts.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
            continue
        n = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='kernel' "
            "AND table_name IN ('executions','execution_mounts')")
        if n == 2:
            await conn.execute("SET search_path TO kernel, public")
            return conn
        await conn.close()
        attempts.append(f"{dsn.rsplit('@', 1)[-1]}: only {n}/2 kernel tables")
    pytest.skip("no local kernel database — start the local stack. Tried: "
                + "; ".join(attempts))


@contextlib.asynccontextmanager
async def _rolled_back():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        leaked = await conn.fetchval(
            "SELECT count(*) FROM kernel.executions WHERE goal_id = $1", GOAL)
        await conn.close()
        assert leaked == 0, "fixture rows survived the rollback — dev DB dirtied"


async def _execution(conn, state: str, *, mounts: bool,
                     workspace_path=None, container_id=None) -> int:
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions "
        "(kind, domain, state, goal_id, workspace_path, container_id) "
        "VALUES ('story','code',$1,$2,$3,$4) RETURNING id",
        state, GOAL, workspace_path, container_id)
    if mounts:
        await conn.execute(
            "INSERT INTO kernel.execution_mounts "
            "(execution_id, at, binding, source, writable, kind) "
            "VALUES ($1,'repos/acme__svc','git','https://x/y',true,'branch')",
            eid)
    return eid


def test_an_admitted_run_whose_clone_failed_is_offered_for_retry():
    """The KER15 shape: admitted, mounts recorded, nothing on disk."""
    async def run():
        async with _rolled_back() as conn:
            eid = await _execution(conn, "ADMITTED", mounts=True)
            ids = [r["id"] for r in await workspace_incomplete(conn, GOAL, 8)]
            assert eid in ids, ids
    asyncio.run(run())


def test_a_run_that_already_has_its_workspace_is_left_alone():
    """Re-acquiring under a working run is what adopt-don't-clobber forbids."""
    async def run():
        async with _rolled_back() as conn:
            eid = await _execution(conn, "ADMITTED", mounts=True,
                                   workspace_path="/workspace/exec-1")
            ids = [r["id"] for r in await workspace_incomplete(conn, GOAL, 8)]
            assert eid not in ids
    asyncio.run(run())


def test_a_live_sandbox_is_never_re_acquired():
    """`container_id` set means a model is working in that tree right now."""
    async def run():
        async with _rolled_back() as conn:
            eid = await _execution(conn, "ADMITTED", mounts=True,
                                   container_id="deadbeef")
            ids = [r["id"] for r in await workspace_incomplete(conn, GOAL, 8)]
            assert eid not in ids
    asyncio.run(run())


def test_a_run_with_no_mounts_needs_no_workspace():
    """An errand mounts nothing, so a null `workspace_path` is not a failure —
    and the dispatch guard lets it through for the same reason. If this one
    drifted, every errand would be retried for ever instead of running."""
    async def run():
        async with _rolled_back() as conn:
            eid = await _execution(conn, "ADMITTED", mounts=False)
            ids = [r["id"] for r in await workspace_incomplete(conn, GOAL, 8)]
            assert eid not in ids
    asyncio.run(run())


def test_the_retry_set_is_exactly_what_dispatch_refuses_to_run():
    """The invariant that keeps the guard and the recovery honest.

    `dispatch_pass` picks up ADMITTED rows with no container that either have a
    workspace or need none. Everything it leaves behind must be in the retry
    set, or that run is stranded — which is the entire bug. Asserted against
    the guard's own SQL rather than restated, so editing one and not the other
    fails here instead of in production six weeks later.
    """
    async def run():
        async with _rolled_back() as conn:
            stranded = await _execution(conn, "ADMITTED", mounts=True)
            ready_ok = await _execution(conn, "ADMITTED", mounts=True,
                                        workspace_path="/workspace/exec-2")
            errand = await _execution(conn, "ADMITTED", mounts=False)

            dispatchable = {r["id"] for r in await conn.fetch(
                "SELECT e.id FROM kernel.executions e "
                "WHERE e.goal_id = $1 "
                "AND e.state = 'ADMITTED' AND e.container_id IS NULL "
                "AND (e.workspace_path IS NOT NULL "
                "     OR NOT EXISTS (SELECT 1 FROM kernel.execution_mounts m "
                "                     WHERE m.execution_id = e.id))", GOAL)}
            retried = {r["id"] for r in await workspace_incomplete(conn, GOAL, 8)}

            assert dispatchable == {ready_ok, errand}
            assert retried == {stranded}
            # No admitted run is both, and none is neither.
            assert not (dispatchable & retried)
            assert dispatchable | retried == {stranded, ready_ok, errand}
    asyncio.run(run())
