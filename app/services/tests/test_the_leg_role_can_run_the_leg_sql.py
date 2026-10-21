"""The leg's grants, exercised BY RUNNING THE LEG'S OWN READS as it.

WHY THE OTHER GRANT TEST IS NOT THIS ONE.
`test_the_leg_role_grants_hold.py` reads the migration TEXT -- it
asserts the statements a human wrote are present. That catches a
deleted grant and cannot catch a MISSING one, because a grant nobody
thought of is absent from the file and from the test equally.

Three derivations of this role each missed something a different way:
the first described the lock connection rather than the leg; the second
forgot that a leg is also every TOOL it dispatches; the third scoped its
sweep BY DIRECTORY, so two worker tools with bodies in
`kernel/verifier/` and `kernel/knowledge/` were invisible. Every one was
a careful reading of the code. The code is not the thing that answers
this question -- Postgres is.

So this file connects, becomes `agentos_leg`, and calls the functions
the pod's leg loop actually calls. A missing grant is an
`InsufficientPrivilegeError` here rather than mid-leg in the cluster,
where `loop_claim.py:137` would swallow it with "the claim stands" and
the story would finish having recorded nothing it learned.

`SET LOCAL ROLE`, NOT A PASSWORD. The role ships with no password by
design -- a human sets one per environment -- and a test that issued
`ALTER ROLE ... PASSWORD` would be a test that mutates a shared database
to run. `SET LOCAL` is scoped to the transaction and the transaction
rolls back.

SKIPS when no database is reachable, or when the connected user may not
become the role. Both are ordinary: a developer's laptop has neither.
"""
from __future__ import annotations

import asyncio
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
ROLE = "agentos_leg"


async def _connect_or_skip():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncpg.connect(
                dsn, timeout=5,
                server_settings={"search_path": "kernel,public"})
        except Exception:                                   # noqa: BLE001
            continue
    return None


def _as_the_leg(body):
    """Run `body(conn, execution_id)` as `agentos_leg`, then roll back."""
    async def main():
        conn = await _connect_or_skip()
        if conn is None:
            pytest.skip("no database; set INTERLOCK_TEST_DSN")
        tx = conn.transaction()
        await tx.start()
        try:
            goal = await conn.fetchval(
                "INSERT INTO goals (id, name, created_by) VALUES "
                "(gen_random_uuid()::text, 'grant-probe', 'p') RETURNING id")
            eid = await conn.fetchval(
                "INSERT INTO executions (goal_id, kind, domain, state) "
                "VALUES ($1, 'story', 'test', 'RUNNING') RETURNING id", goal)
            try:
                await conn.execute(f"SET LOCAL ROLE {ROLE}")
            except Exception as exc:                        # noqa: BLE001
                pytest.skip(f"cannot become {ROLE} here: {exc}")
            return await body(conn, eid)
        finally:
            await tx.rollback()
            await conn.close()
    return asyncio.run(main())


def _denied(exc) -> bool:
    return isinstance(exc, asyncpg.exceptions.InsufficientPrivilegeError)


@pytest.mark.parametrize("name", [
    "resolve_identity", "plan_is_approved", "mounts_for",
])
def test_the_leg_may_run_what_every_leg_runs(name):
    """`leg_body` and `_take_leg` call these on EVERY leg. A missing
    grant on any one is a story that cannot start, or -- worse, for
    `resolve_identity` -- a commit attributed to nobody."""
    async def body(conn, eid):
        from kernel.harness import park_approval
        from kernel.harness.identity import resolve as resolve_identity
        from kernel.workspace.mounts import mounts_for

        fn = {"resolve_identity": lambda: resolve_identity(conn, eid),
              "plan_is_approved": lambda: park_approval.plan_is_approved(
                  conn, eid),
              "mounts_for": lambda: mounts_for(conn, eid)}[name]
        try:
            await fn()
        except Exception as exc:                            # noqa: BLE001
            if _denied(exc):
                pytest.fail(f"{name} is denied to {ROLE}: {exc}")
            raise
    _as_the_leg(body)


def test_the_leg_may_write_its_own_journal():
    """`step_journal` is where a leg records what it did, and where its
    spend goes before the conductor drains it to `token_calls`. Denied,
    a leg runs and leaves no trace of having run."""
    async def body(conn, eid):
        try:
            await conn.execute(
                "INSERT INTO step_journal (execution_id, seq, generation, "
                "idempotency_key) VALUES ($1, 1, 0, $2)",
                eid, f"probe-{eid}")
        except asyncpg.exceptions.InsufficientPrivilegeError as exc:
            pytest.fail(f"step_journal is denied to {ROLE}: {exc}")
        except asyncpg.exceptions.UndefinedColumnError:
            pytest.skip("step_journal's columns have moved; re-point this")
    _as_the_leg(body)


def test_the_leg_may_not_write_a_verdict():
    """THE REFUSAL THAT MATTERS MOST. A pod that could INSERT a verdict
    could pass its own work. Section 10a: the grants ARE the security
    model, and this is the one they exist for."""
    async def body(conn, eid):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO verdicts (execution_id, claim_seq, passed) "
                "VALUES ($1, 1, true)", eid)
    _as_the_leg(body)


def test_the_leg_may_not_become_the_worker():
    """10a: `agentos_leg` is deliberately NOT a member of
    `agentos_worker`. Granting it would silence the boundary along with
    the error -- the pod's first failure would read "permission denied
    to set role" rather than naming the table.

    CHECKED ON THE CATALOGUE, NOT BY TRYING IT, and the first draft of
    this test did try it and PASSED WRONGLY. `SET LOCAL ROLE` changes
    the CURRENT role; Postgres checks `SET ROLE` against the SESSION
    role, which here is still the superuser that connected. So
    `SET ROLE agentos_worker` succeeded from inside a leg-role block and
    the test asserting it would fail reported no problem at all.

    That is worth leaving written down: every other test in this file is
    sound under `SET LOCAL ROLE` because it exercises TABLE privileges,
    which are checked against the current role. Role MEMBERSHIP is not.
    """
    async def body(conn, eid):
        member = await conn.fetchval(
            "SELECT pg_has_role($1, $2, 'MEMBER')", ROLE, "agentos_worker")
        assert member is False, (
            f"{ROLE} is a member of agentos_worker, so a compromised pod "
            f"has the worker's whole surface and the boundary 10a "
            f"describes does not exist")
    _as_the_leg(body)
