"""W2 — `rebuild.environments`' claim, against a real schema.

WHY THIS LOOP AND NOT ANOTHER. Plan 11.5a names it: of everything W2
owes a test, `rebuild.environments` "is the loop that most needs one",
because the failure is not a crash. It claims the right to build an
environment in the DATABASE before it reads the tree, and if that claim
is taken late the next tick judges anyway — against a `.venv` that is
half replaced. In the REMOVE-A-DEPENDENCY direction that is a check
**passing that should fail**, which is the one direction 5.10 forbids.

AND A FAKE CANNOT SEE IT. `claim_build` is a compare-and-set whose whole
correctness lives in the `WHERE` clause of an `ON CONFLICT DO UPDATE` —
a fake that returns an id for any `fetchval` makes the second claimant
look like the first. That is exactly how admission shipped broken twice
through a green CI (LT1's docstring), and it is the same shape here:
the second claim LOOKS like a success.

A COMPARE-AND-SET AND NOT A LOCK, deliberately, and this file is where
that choice is checked. `verdict_lock` is an advisory lock and dies with
its connection; a prepare Job OUTLIVES the connection that submitted it,
so an advisory lock would be released while the Job was still writing to
the disk it guards.

SKIPS when no database is reachable, and every test rolls back — the
same contract LT1 established. `asyncio.run` rather than
`@pytest.mark.asyncio`, because pytest-asyncio is not installed here and
an async test without it ERRORS rather than skipping.
"""
from __future__ import annotations

import asyncio
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

from kernel.verifier import verify_state                     # noqa: E402

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


def _dsns():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn:
            yield dsn


async def _connect_or_skip():
    for dsn in _dsns():
        try:
            return await asyncpg.connect(dsn, timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
    return None


async def _execution(conn) -> int:
    """A row to claim against, inside the caller's transaction.

    `name` and `created_by`, not `title` -- the first draft of this
    fixture guessed the column and every test in the file died on
    `UndefinedColumnError` the first time it met a real schema. Which is
    the argument for the file: a fake would have accepted `title`
    happily, and so would a reviewer.
    """
    goal = await conn.fetchval(
        "INSERT INTO goals (id, name, created_by) VALUES "
        "(gen_random_uuid()::text, 'w2-rebuild', 'w2') RETURNING id")
    return await conn.fetchval(
        "INSERT INTO executions (goal_id, kind, domain, state) "
        "VALUES ($1, 'story', 'test', 'VERIFYING') RETURNING id", goal)


def _run(body):
    """Connect, open a transaction, run `body`, roll back. Or skip."""
    async def main():
        conn = await _connect_or_skip()
        if conn is None:
            pytest.skip("no local database — start "
                        "local-development/scripts/, or set "
                        "INTERLOCK_TEST_DSN")
        # THE SAME `search_path` `make_pool` SETS. Every statement in
        # `kernel/` is written unqualified -- `FROM executions`, not
        # `FROM kernel.executions` -- so a raw asyncpg connection
        # without this fails on `relation "executions" does not exist`
        # and proves nothing about the grant it was testing.
        await conn.execute("SET search_path TO kernel, public")
        tx = conn.transaction()
        await tx.start()
        try:
            return await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    return asyncio.run(main())


# ── the race two ticks can lose ────────────────────────────────────────

def test_only_one_of_two_ticks_may_claim_the_same_build():
    """THE RACE. Two verifier replicas and the conductor all reach
    `adjudicate_pass`. Both read the state, both see the manifest has
    moved, both try to build. Two prepare Jobs on one ReadWriteOncePod
    disk is what `pod.pvc_manifest` chose RWOP to make the API server
    refuse — but the second Job would sit Pending until its deadline,
    and the execution would be judged in between against a half-written
    environment."""
    async def body(conn):
        eid = await _execution(conn)

        first = await verify_state.claim_build(conn, eid, "key-abc")
        second = await verify_state.claim_build(conn, eid, "key-abc")

        assert first is True, "the first claimant was refused"
        assert second is False, (
            "BOTH ticks were allowed to build. The second prepare Job "
            "writes to the same RWOP claim the first is using, and the "
            "execution is judged against a half-replaced .venv")
    _run(body)


def test_a_claim_is_taken_BEFORE_the_tree_is_read():
    """5.10's rule, and 11.5a says it was understated. Once claimed, the
    state must ALREADY say a build is in flight — a claim recorded after
    the tree is read leaves a window in which the next tick judges."""
    async def body(conn):
        eid = await _execution(conn)
        await verify_state.claim_build(conn, eid, "key-abc")

        state = await verify_state.current(conn, eid)

        assert state["building_key"] == "key-abc"
        assert verify_state.building(state) == "key-abc", (
            "the state does not report a build in flight, so the next "
            "tick has nothing to skip on")
    _run(body)


def test_a_different_key_still_cannot_jump_a_live_claim():
    """A manifest that moves twice in quick succession. The second key
    is newer and it still must not start while the first Job is writing
    to the disk."""
    async def body(conn):
        eid = await _execution(conn)
        assert await verify_state.claim_build(conn, eid, "key-abc") is True

        assert await verify_state.claim_build(conn, eid, "key-def") is False
    _run(body)


def test_a_finished_build_releases_the_claim():
    """`built` clears `building_key`, or the execution is never
    buildable again and every later manifest change is silently
    skipped."""
    async def body(conn):
        eid = await _execution(conn)
        await verify_state.claim_build(conn, eid, "key-abc")

        await verify_state.built(conn, eid, "key-abc", "c0ffee", {})
        state = await verify_state.current(conn, eid)

        assert state["building_key"] is None
        assert state["env_key"] == "key-abc"
        assert await verify_state.claim_build(conn, eid, "key-def") is True
    _run(body)


def test_a_failed_build_releases_the_claim_but_keeps_the_key():
    """`build_failed` leaves `env_key` alone deliberately: the
    environment on the volume is still whatever it was, and forgetting
    that would make the next tick trust an unknown one."""
    async def body(conn):
        eid = await _execution(conn)
        await verify_state.built(conn, eid, "old-key", "c0ffee", {})
        await verify_state.claim_build(conn, eid, "new-key")

        await verify_state.build_failed(conn, eid, "c0ffee", "pip exploded")
        state = await verify_state.current(conn, eid)

        assert state["building_key"] is None, "the claim was not released"
        assert state["env_key"] == "old-key", (
            "the failed build forgot which environment is on the volume, "
            "so the next tick cannot tell whether it is current")
    _run(body)


# ── and the column E19 added, which a fake would not have ──────────────

def test_mounts_env_survives_the_round_trip():
    """E19 put `mounts_env` beside `env_key` so `verify_claim` knows HOW
    to use the environment, not only WHICH it is. A `jsonb` column that
    silently stored a string would give every criterion an empty PATH
    prepend — and `cd repos/B && pytest` would resolve A's pytest, the
    shadowing `criterion_env` exists to end."""
    async def body(conn):
        eid = await _execution(conn)
        mounts = {"repos/a": {"prepend": ["/verify/tree/repos/a/.venv/bin"],
                              "env": {"VIRTUAL_ENV": "/verify/tree/repos/a"}}}

        await verify_state.built(conn, eid, "key-abc", "c0ffee", mounts)
        state = await verify_state.current(conn, eid)

        import json
        got = state["mounts_env"]
        assert json.loads(got) == mounts if isinstance(got, str) else \
            got == mounts
    _run(body)
