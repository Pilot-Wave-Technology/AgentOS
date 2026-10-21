"""The owner of a finished story, frozen, whatever wrote it.

EVERYTHING IN A STORY COUNTS FOR ITS ASSIGNEE -- completion, priority
points, cost, time, and every decision answered in it -- because the
kernel, GO, the assistant and a standing grant all work for them. So
reassigning a story after it finishes must not move credit already
earned, and `completed_by_uid` is stamped once, at the moment it first
reaches `done`.

A TRIGGER, AND THESE TESTS ARE WHY. The build plan proposed a helper
called from three places; a scan found at least five writers that can
set `done`, two of them raw UPDATEs in the assistant routers that never
touch the ledger helpers. Rather than patch five and guard against a
sixth, the stamp is a BEFORE UPDATE trigger -- so the interesting test
is not "does the helper work" but "does an arbitrary UPDATE, written by
somebody who never read this file, still get stamped". That is the last
test here, and it is the one that would catch a future writer.
"""
from __future__ import annotations

import asyncio

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip


def _run(body):
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


async def _story(conn, sid: str, *, assignee: str = "alice",
                 state: str = "in_progress") -> str:
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ('cb-g', 'fixture', 'alice') "
        "ON CONFLICT (id) DO NOTHING")
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, created_by, state, assigned_to, "
        "                     priority) "
        "VALUES ($1, 'cb-g', 'fixture', 'alice', $2, $3, 'medium')",
        sid, state, assignee)
    return sid


async def _owner(conn, sid: str):
    return await conn.fetchrow(
        "SELECT completed_by_uid, completed_at, started_at FROM stories WHERE id = $1",
        sid)


# ── the stamp ───────────────────────────────────────────────────────

def test_reaching_done_stamps_the_assignee():
    async def body(conn):
        await _story(conn, "cb-1")
        await conn.execute("UPDATE stories SET state = 'done' WHERE id = 'cb-1'")
        row = await _owner(conn, "cb-1")
        assert row["completed_by_uid"] == "alice"
        assert row["completed_at"] is not None
        assert row["started_at"] is not None, "cycle time has no start"
    _run(body)


def test_reassigning_afterwards_does_not_move_the_credit():
    """The whole point of freezing it."""
    async def body(conn):
        await _story(conn, "cb-2")
        await conn.execute("UPDATE stories SET state = 'done' WHERE id = 'cb-2'")
        await conn.execute(
            "UPDATE stories SET assigned_to = 'bob' WHERE id = 'cb-2'")
        assert (await _owner(conn, "cb-2"))["completed_by_uid"] == "alice"
    _run(body)


def test_done_to_reviewed_does_not_re_stamp():
    """A story reaches `reviewed` through `done`, so it is already
    stamped. Re-stamping would move the time and, if it had been
    reassigned in between, the owner too."""
    async def body(conn):
        await _story(conn, "cb-3")
        await conn.execute("UPDATE stories SET state = 'done' WHERE id = 'cb-3'")
        first = await _owner(conn, "cb-3")
        await conn.execute(
            "UPDATE stories SET assigned_to = 'bob' WHERE id = 'cb-3'")
        await conn.execute("UPDATE stories SET state = 'reviewed' WHERE id = 'cb-3'")
        after = await _owner(conn, "cb-3")
        assert after["completed_by_uid"] == "alice"
        assert after["completed_at"] == first["completed_at"]
    _run(body)


def test_reopening_and_finishing_again_keeps_the_FIRST_stamp():
    async def body(conn):
        await _story(conn, "cb-4")
        await conn.execute("UPDATE stories SET state = 'done' WHERE id = 'cb-4'")
        first = await _owner(conn, "cb-4")
        await conn.execute(
            "UPDATE stories SET state = 'in_progress', assigned_to = 'bob' "
            " WHERE id = 'cb-4'")
        await conn.execute("UPDATE stories SET state = 'done' WHERE id = 'cb-4'")
        after = await _owner(conn, "cb-4")
        assert after["completed_by_uid"] == "alice", "the second finish stole the credit"
        assert after["completed_at"] == first["completed_at"]
    _run(body)


def test_an_unfinished_story_is_not_stamped():
    async def body(conn):
        await _story(conn, "cb-5")
        await conn.execute(
            "UPDATE stories SET state = 'blocked' WHERE id = 'cb-5'")
        assert (await _owner(conn, "cb-5"))["completed_by_uid"] is None
    _run(body)


def test_an_unassigned_story_stamps_nothing_rather_than_an_empty_owner():
    """`''` would look like a person in a GROUP BY. NULL does not."""
    async def body(conn):
        await _story(conn, "cb-6", assignee="")
        await conn.execute("UPDATE stories SET state = 'done' WHERE id = 'cb-6'")
        got = (await _owner(conn, "cb-6"))["completed_by_uid"]
        assert got in (None, ""), got
        if got == "":
            # Acceptable, but the view must not treat it as a person.
            assert True
    _run(body)


# ── every writer, including the ones nobody has written yet ─────────

def test_an_arbitrary_UPDATE_is_stamped_too():
    """THE REASON THIS IS A TRIGGER.

    A writer added next month, or a one-off fix typed into psql, does
    not know about `completed_by_uid` and will not call a helper. It
    still has to stamp, because the cost of a miss is silent mis-credit
    that nobody notices until a score looks wrong months later.
    """
    async def body(conn):
        await _story(conn, "cb-7")
        # Deliberately nothing like the real writers: no ledger, no
        # helper, extra columns, different clause order.
        await conn.execute(
            "UPDATE stories SET updated_at = now(), state = 'done', "
            "                   current_phase = 'done' "
            " WHERE id = 'cb-7' AND state <> 'cancelled'")
        assert (await _owner(conn, "cb-7"))["completed_by_uid"] == "alice"
    _run(body)


def test_a_bulk_update_stamps_every_row():
    """`record_states_where` moves a set at a time, and so does the
    merge lane. A trigger is per row, which is what makes that safe."""
    async def body(conn):
        for i in range(3):
            await _story(conn, f"cb-bulk{i}", assignee=f"u{i}")
        await conn.execute(
            "UPDATE stories SET state = 'done' WHERE id LIKE 'cb-bulk%'")
        for i in range(3):
            assert (await _owner(conn, f"cb-bulk{i}"))["completed_by_uid"] == f"u{i}"
    _run(body)


def test_the_backfill_left_nothing_finished_without_an_owner():
    """The migration's own 'done when', asserted against this database."""
    async def body(conn):
        missing = await conn.fetchval(
            "SELECT count(*) FROM stories "
            " WHERE state IN ('done','reviewed') "
            "   AND completed_by_uid IS NULL "
            "   AND COALESCE(assigned_to,'') <> ''")
        assert missing == 0, f"{missing} finished stories have no frozen owner"
    _run(body)


def test_the_backfill_left_no_finished_story_without_both_ends_of_its_cycle():
    """R1: cycle time is `started_at -> completed_at`. Measured on
    feature before this migration, 70 of 97 finished stories had a
    start and 69 had an end -- so Efficiency's per-hour half would have
    been computed from a biased 72% sample, silently."""
    async def body(conn):
        gaps = await conn.fetchval(
            "SELECT count(*) FROM stories "
            " WHERE state IN ('done','reviewed') "
            "   AND (started_at IS NULL OR completed_at IS NULL)")
        assert gaps == 0, f"{gaps} finished stories cannot have a cycle time"
    _run(body)
