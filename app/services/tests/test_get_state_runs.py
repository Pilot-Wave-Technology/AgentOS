"""`get_state` runs against a real database, every turn.

IT 500'd ON EVERY CALL. The parked-plan read queried
`FROM messages WHERE story_id = $1` and there is no `messages.story_id` --
a message belongs to a THREAD, and the thread belongs to the story. The same
mistake sat in `approve_local_plan`, where it had never fired only because
nobody had reached the approve button yet.

WHY NOTHING CAUGHT IT. The snapshot tests exercise the pure mapping functions
(`_lifecycle_action`, `_phase_hint`) and the bundle tests never call
`local_state`. Both passed while the one query in it could not execute. A
SELECT is only tested by a database.
"""
from __future__ import annotations

import contextlib
import json
import uuid

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


@contextlib.contextmanager
def _pool_is(conn):
    """Every module `local_state` reaches through holds its OWN `get_pool`.

    `execution_control` is the easy one to miss -- `local_state` asks it
    whether the goal is frozen, which is a database call on a different
    module's handle.
    """
    from app.services import execution_control, local_dispatch, local_report
    mods = [local_dispatch, local_report, execution_control]
    saved = [(m, getattr(m, "get_pool", None)) for m in mods]
    for m in mods:
        if hasattr(m, "get_pool"):
            m.get_pool = lambda: conn
    try:
        yield
    finally:
        for m, fn in saved:
            if fn is not None:
                m.get_pool = fn


async def _story(conn, sid: str, phase: str = ""):
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       f"{sid}-goal", "fixture", f"{PREFIX}-user")
    tid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO threads (id, goal_id, story_id, created_by) "
        "VALUES ($1,$2,$3,$4)",
        tid, f"{sid}-goal", sid, f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,execution_mode,"
        "current_phase,active_thread_id) VALUES ($1,$2,$3,$4,'local',$5,$6)",
        sid, f"{sid}-goal", "state fixture", f"{PREFIX}-user", phase, tid)
    return tid


@pytest.mark.asyncio
async def test_get_state_actually_runs():
    from app.services.local_dispatch import local_state

    async with _conn() as conn:
        sid = f"{PREFIX}-state-plain"
        await _story(conn, sid)
        with _pool_is(conn):
            snap = await local_state(sid)
    assert snap["ok"] is True, snap
    assert snap["next_action"] == "planning"
    assert snap["phase_hint"], "a story with no phase gets no guidance"
    assert snap["pending_plan"] is None


@pytest.mark.asyncio
async def test_the_parked_plan_reaches_the_snapshot():
    # This is what Review Plan approves. Without it the command has nothing to
    # show and falls back to "no plan is waiting".
    from app.services.local_dispatch import local_state

    async with _conn() as conn:
        sid = f"{PREFIX}-state-parked"
        tid = await _story(conn, sid, phase="awaiting_plan_approval")
        await conn.execute(
            "UPDATE stories SET pending_plan_md = $2, pending_plan_path = $3 "
            " WHERE id = $1", sid, "# plan", "agentos_plans/x/plan-01.md")
        await conn.execute(
            """INSERT INTO messages (id, thread_id, sender, role, content, card_data)
               VALUES ($1,$2,'agent','agent','Plan submitted.',$3::jsonb)""",
            str(uuid.uuid4()), tid,
            json.dumps({"kind": "plan_review", "steps": ["one", "two"],
                        "checks": [{"run": "pytest", "label": "goal"}],
                        "plan_hash": "abc123"}))
        with _pool_is(conn):
            snap = await local_state(sid)

    plan = snap["pending_plan"]
    assert plan is not None, "the parked plan never reaches the panel"
    assert plan["steps"] == ["one", "two"]
    assert plan["checks"][0]["label"] == "goal"
    assert plan["plan_hash"] == "abc123"
    assert plan["path"] == "agentos_plans/x/plan-01.md"
    assert snap["next_action"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_the_card_is_found_through_the_legacy_thread_binding_too():
    # `threads.story_id` is the per-story model; `stories.active_thread_id` is
    # what report_message writes through. A story bound before the per-story
    # model has the second and not the first.
    from app.services.local_dispatch import local_state

    async with _conn() as conn:
        sid = f"{PREFIX}-state-legacy"
        tid = await _story(conn, sid, phase="awaiting_plan_approval")
        await conn.execute("UPDATE threads SET story_id = NULL WHERE id = $1", tid)
        await conn.execute(
            "UPDATE stories SET pending_plan_md = 'x' WHERE id = $1", sid)
        await conn.execute(
            """INSERT INTO messages (id, thread_id, sender, role, content, card_data)
               VALUES ($1,$2,'agent','agent','Plan submitted.',$3::jsonb)""",
            str(uuid.uuid4()), tid,
            json.dumps({"kind": "plan_review", "steps": ["only"], "checks": [],
                        "plan_hash": "zz"}))
        with _pool_is(conn):
            snap = await local_state(sid)
    assert snap["pending_plan"]["steps"] == ["only"], (
        "a story whose thread predates the per-story model loses its plan card"
    )
