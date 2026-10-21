"""A refresh sweeps the fleet, so who it sweeps is the whole safety story.

WHAT THIS GUARDS. `refreshable_agents` decides which agents a fleet-wide
write may touch. Two ways it can be wrong, and they fail in opposite
directions:

  TOO WIDE -- it includes agents of deleted goals. A GO session never
  idle-evicts, so a deleted goal leaves an agent that reads as perfectly
  healthy to any query that does not check its goal. The last job in
  this system to sweep the fleet, `skill_sync`'s startup reconcile, is
  on record spamming for exactly that reason.

  TOO NARROW -- it misses every GO. GOs are not in `agent_instances`;
  they are rows in `agents` reached through `agent_contexts`. An
  instance-only query returns every assistant and no orchestrator, and
  nothing about the result looks wrong. That was a live bug on
  2026-06-12 and is why `repush-all` UNIONs two sources.

These run against a real database because the thing under test IS the
query. A fake that returns rows would be asserting on my own SQL
transcribed twice.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid

import asyncpg
import pytest

from app.services.agent_refresh.eligibility import (
    Refreshable, refreshable_agents)

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


def _dsns():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn:
            yield dsn


async def _connect_or_skip():
    tried = []
    for dsn in _dsns():
        try:
            return await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip("no local database — start local-development/scripts/"
                f"stack.ps1. Tried: {'; '.join(tried)}")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _goal(conn, status: str) -> str:
    gid = _uid("G")[:12]
    await conn.execute(
        "INSERT INTO goals (id, name, status, created_by) "
        "VALUES ($1, $2, $3, $4)",
        gid, f"refresh test {gid}", status, "test")
    return gid


async def _template(conn, agent_type: str) -> str:
    tid = _uid("tpl")
    await conn.execute(
        "INSERT INTO agents (id, name, type, status, is_template, "
        "                    created_by) "
        "VALUES ($1, $2, $3, 'active', TRUE, 'test')",
        tid, tid, agent_type)
    return tid


async def _shca(conn, template_id: str, goal_id: str, *,
                status: str = "active") -> str:
    gw = _uid("asst")
    await conn.execute(
        "INSERT INTO agent_instances "
        "  (id, template_id, gateway_agent_id, goal_id, thread_id, status) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        uuid.uuid4(), template_id, gw, goal_id, _uid("th"), status)
    return gw


async def _go(conn, goal_id: str, *, status: str = "active") -> str:
    gw = f"go-{goal_id.lower()}"
    await conn.execute(
        "INSERT INTO agents (id, name, type, status, is_template, "
        "                    gateway_agent_id, created_by) "
        "VALUES ($1, $1, 'GO', $2, FALSE, $1, 'test')", gw, status)
    await conn.execute(
        "INSERT INTO agent_contexts (goal_id, agent_id, added_by) "
        "VALUES ($1, $2, 'test')", goal_id, gw)
    return gw


def _ids(agents) -> set[str]:
    return {a.gateway_agent_id for a in agents}


@pytest.mark.asyncio
async def test_a_deleted_goal_takes_its_agents_with_it():
    """THE ZOMBIE CASE. Both an assistant and an orchestrator."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        live, dead = await _goal(conn, "active"), await _goal(conn, "deleted")
        tpl = await _template(conn, "SHCA")
        keep_a = await _shca(conn, tpl, live)
        keep_g = await _go(conn, live)
        drop_a = await _shca(conn, tpl, dead)
        drop_g = await _go(conn, dead)

        got = _ids(await refreshable_agents(conn))
        assert keep_a in got and keep_g in got
        assert drop_a not in got, "an assistant of a deleted goal is a zombie"
        assert drop_g not in got, "a GO of a deleted goal never idle-evicts"
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_the_orchestrator_is_found_at_all():
    """THE COVERAGE CASE, and the one that fails silently.

    A GO is not in `agent_instances`. Drop the second arm of the UNION
    and this returns every assistant, no orchestrator, and no error.
    """
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal = await _goal(conn, "active")
        go = await _go(conn, goal)
        got = await refreshable_agents(conn)
        assert go in _ids(got)
        assert any(a.is_go for a in got)
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_destroyed_instance_is_not_refreshed():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        goal = await _goal(conn, "active")
        tpl = await _template(conn, "SHCA")
        gone = await _shca(conn, tpl, goal, status="destroyed")
        assert gone not in _ids(await refreshable_agents(conn))
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_scoping_to_one_goal_is_the_blast_radius_control():
    """Prove a refresh on one goal before running it on the fleet."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        a, b = await _goal(conn, "active"), await _goal(conn, "active")
        tpl = await _template(conn, "SHCA")
        mine, theirs = await _shca(conn, tpl, a), await _shca(conn, tpl, b)
        got = _ids(await refreshable_agents(conn, goal_id=a))
        assert mine in got and theirs not in got
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_an_unknown_type_is_refused_rather_than_silently_empty():
    """A typo must not read as "nothing to do"."""
    conn = await _connect_or_skip()
    try:
        with pytest.raises(ValueError):
            await refreshable_agents(conn, agent_type="ASSISTANT")
    finally:
        await conn.close()


def test_liveness_is_status_not_state():
    """`goals.state` is the lifecycle a person drives -- planning,
    paused, done. `goals.status` is the soft-delete marker. Gating on
    `state` would skip PAUSED goals, whose agents are exactly the ones a
    refresh should reach, and would still sweep deleted ones."""
    src = inspect.getsource(refreshable_agents.__module__ and
                            __import__("app.services.agent_refresh.eligibility",
                                       fromlist=["x"]))
    code = "\n".join(line.split("--")[0] for line in src.splitlines())
    assert "gl.status = 'active'" in code
    assert "gl.state" not in code


def test_it_carries_what_a_render_needs():
    """template_id and goal_id, or the caller has to go back to the
    database per agent to work out what to push."""
    fields = Refreshable.__dataclass_fields__
    for needed in ("gateway_agent_id", "agent_type", "template_id",
                   "goal_id", "workspace"):
        assert needed in fields, needed
