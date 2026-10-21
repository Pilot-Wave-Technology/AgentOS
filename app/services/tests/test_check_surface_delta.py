"""A re-plan must show what it changed about the definition of done.

WHAT HAPPENED ON KER25. Story 4a975a86 parked twice for plan approval,
ten minutes apart. The first card carried three goal checks exercising
the HTTP layer:

    POST /add     {"a": "inf", "b": 1.0}        -> 422
    POST /multiply {"a": 1e200, "b": 1e200}     -> 400
    POST /add     {"a": true,  "b": 1.0}        -> 422

The second carried two schema-only checks against Pydantic directly. The
overflow check was gone entirely and nothing exercised an endpoint. Both
were approved, and the story verified green against the weaker surface.

Goal checks ARE what done means. A model that finds a check inconvenient
can re-plan with an easier one, and noticing required diffing two cards
nobody had reason to compare.

NOT A REFUSAL. Narrowing is sometimes right, and this executor went on
to do the stronger work anyway -- all three dropped checks pass against
the tree it committed. The defect is that an approver had no way to see
the delta.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


async def _connect_or_skip():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
    pytest.skip("no local database")


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


async def _execution(conn):
    tag = os.urandom(4).hex()
    goal = f"zz-cd-{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')", goal)
    return await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, state) "
        "VALUES ('story','coding',$1,'RUNNING') RETURNING id", goal)


async def _plan(conn, eid, checks):
    # ANSWERED rows must carry their answer -- `questions_answer_present`
    # is a CHECK constraint, and a fixture that skips it fails in a way
    # that looks like the code under test.
    await conn.execute(
        """INSERT INTO kernel.questions
               (execution_id, kind, question_text, status, payload,
                generation, answer_text, answered_by, answered_at)
           VALUES ($1,'authorization','plan approval','ANSWERED',$2::jsonb,
                   0,'approved','alice', now())""",
        eid, json.dumps({"steps": ["s"], "checks": checks}))


def _c(run, label="goal"):
    return {"run": run, "label": label, "kind": "command", "spec": {}}


def test_a_narrowed_surface_is_reported_with_a_count():
    """The KER25 shape: three goal checks become two."""
    async def body(conn):
        from kernel.harness.tools_worker.plan import _check_delta

        eid = await _execution(conn)
        await _plan(conn, eid, [_c("http inf 422"), _c("http overflow 400"),
                                _c("http bool 422"), _c("pytest", "guard")])
        delta = await _check_delta(
            conn, eid, [_c("schema inf"), _c("schema extra"),
                        _c("pytest", "guard")])
        assert delta, "a narrowed surface produced no delta"
        assert delta["goal_count"] == [3, 2]
        assert "http overflow 400" in delta["removed"]
        # And it says so in words, not only as two lists to diff.
        assert "note" in delta and "fewer goal checks" in delta["note"].lower()
    _run(body)


def test_an_unchanged_surface_is_silent():
    """A re-plan that keeps its checks should not add noise to the card."""
    async def body(conn):
        from kernel.harness.tools_worker.plan import _check_delta

        eid = await _execution(conn)
        same = [_c("a"), _c("pytest", "guard")]
        await _plan(conn, eid, same)
        assert await _check_delta(conn, eid, list(same)) is None
    _run(body)


def test_a_widened_surface_is_reported_without_the_warning():
    """Adding checks is reported -- the approver should see any change --
    but only a NARROWING gets the sentence about done meaning less."""
    async def body(conn):
        from kernel.harness.tools_worker.plan import _check_delta

        eid = await _execution(conn)
        await _plan(conn, eid, [_c("a"), _c("pytest", "guard")])
        delta = await _check_delta(conn, eid,
                                   [_c("a"), _c("b"), _c("pytest", "guard")])
        assert delta["goal_count"] == [1, 2]
        assert delta["added"] == ["b"]
        assert "note" not in delta
    _run(body)


def test_the_first_plan_has_nothing_to_compare():
    async def body(conn):
        from kernel.harness.tools_worker.plan import _check_delta

        eid = await _execution(conn)
        assert await _check_delta(conn, eid, [_c("a")]) is None
    _run(body)
