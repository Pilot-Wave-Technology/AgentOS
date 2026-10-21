"""I5f · a grant is a human act, checked as a PRIVILEGE and not a habit.

`kernel_autonomous_errands.sql` wrote the security argument down like
this:

    INSERT stays `agentos_human` only ... NO AGENT, GO INCLUDED, CAN MINT
    ITS OWN GRANT. That is a database privilege, not a code path somebody
    can find a way around.

The first sentence was true. The second was not, and this file is the
reason I know: `kernel_errand_schedules.sql` grants the worker UPDATE at
TABLE level, because the sweep must move `next_run_at`. Table-level
UPDATE covers `approved_by` too. So minting a grant on a NEW row was
refused and minting one on an EXISTING row was permitted:

    SET ROLE agentos_worker;
    UPDATE kernel.errand_schedules SET approved_by = 'alice', ...

AND THAT ROLE IS THE ONE EVERY TOOL CALL WEARS. `kernel/loopbridge.py`
puts `SET ROLE agentos_worker` on every pooled connection, which is what
makes the grants bind the assistant and GO surfaces at all.

No tool runs arbitrary SQL, so nothing could reach it. But a guarantee
that holds only because of what is ABSENT from a tool list stops being
true the first time somebody adds one, and nothing would say so.

EACH ATTEMPT GETS ITS OWN SAVEPOINT. A failed statement poisons the
transaction, so a second attempt in the same one fails with
`InFailedSQLTransactionError` — which looks exactly like a refusal and is
how a guard like this passes while testing nothing. `pytest.raises` sits
outside the savepoint for the same reason.
"""
from __future__ import annotations

import asyncio
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-priv-test"

# The five that mint or widen authority. Each is a complete self-approval
# on its own: a uid the run did not earn, a driver it did not get, a
# ceiling nobody agreed to, a revocation undone, or the same grant
# pointed at different work.
FORBIDDEN = {
    "mint a grant": "SET approved_by = 'alice', approved_at = now()",
    "pin a different plan": "SET approved_plan_hash = 'whatever-i-just-ran'",
    "repoint the driver": "SET driving_thread_id = 'somebody-elses-thread'",
    "widen the ceiling": "SET max_runs = 999999",
    "extend the expiry": "SET expires_at = now() + interval '10 years'",
    "undo a revocation": "SET revoked_at = NULL",
    "rewrite the task": "SET task = 'something else entirely'",
    "claim it was proposed by a person": "SET proposed_by = 'alice'",
}

# What `kernel/worker/schedules.py` actually writes. If one of these ever
# refuses, the sweep is dead and every scheduled errand stops silently —
# which is the Stage L failure shape and cost 3,850 denials once.
SWEEP = {
    "claim (due)": "SET next_run_at = NULL, last_run_at = now()",
    "advance": ("SET next_run_at = now(), last_execution_id = 1, "
                "run_count = run_count + 1"),
    "retire": "SET paused = true, retired_reason = 'ran its course'",
}


async def _connect_or_skip():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
        if await conn.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = 'agentos_worker'"):
            return conn
        await conn.close()
    pytest.skip("no local database with the agentos_worker role")


def _run(body):
    async def _go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(_go())


async def _schedule(conn):
    """A hand-made schedule — no grant on it, which is the point.

    The attack is not creating a grant; it is turning a schedule somebody
    already made into one.
    """
    goal = f"{PREFIX}-{os.urandom(4).hex()}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')",
        goal)
    return await conn.fetchval(
        "INSERT INTO kernel.errand_schedules (goal_id, task, cron, max_runs) "
        "VALUES ($1,'read the log','0 9 * * 1',5) RETURNING id", goal)


async def _as_worker(conn, sql, sid):
    """One statement as the worker, in its own savepoint. True if allowed."""
    sp = conn.transaction()
    await sp.start()
    try:
        await conn.execute("SET LOCAL ROLE agentos_worker")
        await conn.execute(sql, sid)
        return True
    except asyncpg.exceptions.InsufficientPrivilegeError:
        return False
    finally:
        await sp.rollback()


@pytest.mark.parametrize("what", sorted(FORBIDDEN))
def test_the_worker_role_cannot_mint_or_widen_a_grant(what):
    """The role every kernel tool call wears, against every column that
    decides who authorised what."""
    async def body(conn):
        sid = await _schedule(conn)
        allowed = await _as_worker(
            conn, f"UPDATE kernel.errand_schedules {FORBIDDEN[what]} "
                  f"WHERE id = $1", sid)
        assert not allowed, (
            f"agentos_worker can {what} — so a self-approval is a privilege "
            f"away, and the only thing stopping it is that no tool runs SQL")
    _run(body)


@pytest.mark.parametrize("what", sorted(SWEEP))
def test_the_sweep_can_still_do_its_job(what):
    """The other half, and the half that fails SILENTLY. A revoke that
    went one column too far would stop every scheduled errand with no
    error anybody sees — `advance` is called inside a `finally` and the
    sweep catches per row."""
    async def body(conn):
        sid = await _schedule(conn)
        allowed = await _as_worker(
            conn, f"UPDATE kernel.errand_schedules {SWEEP[what]} "
                  f"WHERE id = $1", sid)
        assert allowed, (
            f"the sweep cannot {what}; every scheduled errand has stopped "
            f"and nothing will say so")
    _run(body)


def test_insert_is_still_refused_outright():
    """The half that was already true, kept true: a grant on a NEW row."""
    async def body(conn):
        goal = f"{PREFIX}-ins-{os.urandom(4).hex()}"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','a')",
            goal)
        sp = conn.transaction()
        await sp.start()
        try:
            await conn.execute("SET LOCAL ROLE agentos_worker")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO kernel.errand_schedules "
                    "(goal_id, task, cron, approved_by, approved_at, max_runs)"
                    " VALUES ($1,'t','0 9 * * 1','alice',now(),1)", goal)
        finally:
            await sp.rollback()
    _run(body)


def test_every_column_the_sweep_writes_is_granted():
    """THE JOIN, so this file cannot rot into a list somebody maintains by
    memory. The privilege is derived from what the code writes, and if the
    sweep grows a column the grant has to grow with it."""
    import io
    import pathlib
    import re

    src = io.open(pathlib.Path(__file__).resolve().parents[3]
                  / "kernel/worker/schedules.py", encoding="utf-8").read()
    granted = {"next_run_at", "last_run_at", "last_execution_id",
               "run_count", "paused", "retired_reason"}

    written = set()
    for stmt in re.findall(
            r"UPDATE kernel\.errand_schedules(.*?)WHERE", src, re.S):
        for col in re.findall(r"(\w+)\s*=", stmt):
            if col not in ("SET", "id"):
                written.add(col)
    assert written, "no UPDATE found — this test is asserting nothing"
    assert written <= granted, (
        f"the sweep writes {sorted(written - granted)}, which "
        f"kernel_errand_grant_columns.sql does not grant")
