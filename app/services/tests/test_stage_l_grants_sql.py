"""Stage L's writes must be reachable from the role that performs them.

THE BUG THIS PINS. `kernel/loopbridge.py` puts every pooled kernel connection on
`agentos_worker` — F1's privilege separation, and the reason it is real rather
than decorative is that `agentos` is a superuser who would bypass every grant.
Stage L then added four writes to tables that role held SELECT on and nothing
more, and its migrations carried no GRANT statements at all:

    stories.knowledge        record_cycle / record_finding
    goals.brief              update_brief
    goal_decisions           insert + retire
    kernel.alignment_flags   raise_alignment       (a new table, no grants)

Every one raised `permission denied`. All four call sites are best-effort by
design — a story must not fail to complete because a note about it could not be
filed — so each failure was caught, logged, and never surfaced anywhere a person
looks. Observed on KER15: two stories planned, ran, landed and were reviewed
with EVERY Stage L surface empty and six swallowed exceptions in a container log.

That is why this is a test about grants and not about knowledge. The feature was
correct; it was simply not permitted to happen, and nothing said so.

BOTH HALVES MATTER. The writes must work, and the column scoping must still
refuse everything else — a table-level `GRANT UPDATE ON stories` would make the
first half pass while handing the worker `state`, `assigned_to` and the rest.
The refusals are the reason the grant is written per column.

**How to run.** Needs the local stack's PostgreSQL with the `agentos_*` roles;
SKIPS when none is reachable. Everything happens in one transaction that is
always rolled back, so no probe row survives.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

asyncpg = pytest.importorskip("asyncpg")

# Any real story/goal will do — nothing is committed. Resolved at run time so
# the test does not depend on a fixture the dev database may not have.
_ANY_STORY = "SELECT id FROM stories LIMIT 1"
_ANY_GOAL = "SELECT id FROM goals LIMIT 1"


@contextlib.asynccontextmanager
async def _as_worker():
    """A connection wearing `agentos_worker`, always rolled back."""
    conn = await _connect_or_skip()
    if not await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = 'agentos_worker'"):
        await conn.close()
        pytest.skip("no agentos_worker role — this database predates F1")
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("SET LOCAL ROLE agentos_worker")
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _try(conn, sql: str) -> tuple[bool, str]:
    """Run one statement in a savepoint so a refusal does not abort the tx.

    A constraint or privilege violation aborts the WHOLE transaction in
    Postgres, so without the savepoint the first refusal would make every
    later probe report a meaningless "current transaction is aborted".
    """
    await conn.execute("SAVEPOINT sp")
    try:
        await conn.execute(sql)
        ok, msg = True, "OK"
    except Exception as exc:                                   # noqa: BLE001
        ok, msg = False, str(exc)[:70]
    await conn.execute("ROLLBACK TO SAVEPOINT sp")
    return ok, msg


def test_the_worker_can_perform_every_stage_l_write():
    async def run():
        async with _as_worker() as conn:
            sid = await conn.fetchval(_ANY_STORY)
            gid = await conn.fetchval(_ANY_GOAL)
            if not sid or not gid:
                pytest.skip("no stories/goals in this database to write against")

            writes = [
                # The row lock `_apply` takes before its read-modify-write. A
                # column grant DOES satisfy this — verified, not assumed.
                ("stories.knowledge FOR UPDATE",
                 f"SELECT knowledge FROM stories WHERE id='{sid}' FOR UPDATE"),
                ("stories.knowledge UPDATE",
                 f"UPDATE stories SET knowledge='[]'::jsonb, updated_at=now() "
                 f"WHERE id='{sid}'"),
                ("stories.result_summary UPDATE",
                 f"UPDATE stories SET result_summary='probe' WHERE id='{sid}'"),
                ("goals.brief UPDATE",
                 f"UPDATE goals SET brief='probe', brief_updated=now() "
                 f"WHERE id='{gid}'"),
                ("goal_decisions INSERT",
                 f"INSERT INTO goal_decisions (id, goal_id, decision) "
                 f"VALUES ('zz-probe','{gid}','probe')"),
                ("goal_decisions retire",
                 f"UPDATE goal_decisions SET retired_at=now() "
                 f"WHERE goal_id='{gid}'"),
                ("alignment_flags INSERT",
                 f"INSERT INTO kernel.alignment_flags (goal_id, subject_kind,"
                 f" subject_id, question, evidence, fingerprint) VALUES "
                 f"('{gid}','story','{sid}','probe?','[{{\"k\":1}}]'::jsonb,"
                 f"'zz-probe')"),
                ("alignment_flags resolve",
                 f"UPDATE kernel.alignment_flags SET status='RESOLVED' "
                 f"WHERE goal_id='{gid}'"),
            ]
            failed = []
            for label, sql in writes:
                ok, msg = await _try(conn, sql)
                if not ok:
                    failed.append(f"{label}: {msg}")
            assert not failed, (
                "Stage L cannot write as agentos_worker — the feature is off "
                "and every call site swallows the error:\n  "
                + "\n  ".join(failed))
    asyncio.run(run())


def test_the_grant_does_not_widen_past_stage_l():
    """The column scoping is the point, not an implementation detail.

    A table-level `GRANT UPDATE ON stories` would satisfy the test above while
    letting the worker move a story to `done` or reassign it. These are the
    writes the kernel must never make through this role.
    """
    async def run():
        async with _as_worker() as conn:
            sid = await conn.fetchval(_ANY_STORY)
            gid = await conn.fetchval(_ANY_GOAL)
            if not sid or not gid:
                pytest.skip("no stories/goals in this database to write against")

            must_refuse = [
                ("stories.state", f"UPDATE stories SET state='done' WHERE id='{sid}'"),
                ("stories.assigned_to",
                 f"UPDATE stories SET assigned_to='x' WHERE id='{sid}'"),
                ("stories DELETE", f"DELETE FROM stories WHERE id='{sid}'"),
                ("goals.objective",
                 f"UPDATE goals SET objective='x' WHERE id='{gid}'"),
                # What a decision SAID is immutable; only its lifecycle moves.
                ("goal_decisions.decision",
                 f"UPDATE goal_decisions SET decision='x' WHERE goal_id='{gid}'"),
            ]
            widened = [label for label, sql in must_refuse
                       if (await _try(conn, sql))[0]]
            assert not widened, (
                "agentos_worker can now write columns Stage L never needed: "
                + ", ".join(widened))
    asyncio.run(run())
