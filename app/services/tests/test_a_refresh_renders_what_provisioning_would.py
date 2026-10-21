"""A refresh must render what CREATION renders, oddities included.

THE FAILURE THIS PREVENTS is subtle and permanent: if the refresh
renders content a freshly-created agent would never receive, then
existing agents drift onto a second, undocumented configuration that only
they have -- and it looks like the refresh working, because something
changed.

So these tests assert the copied rules, including the two that are odd:

  * an SHCA's per-agent `agent_files` overlay WINS over its template
  * `kernel-assistant` and `go-v2` get ZERO skills, not few

and the boundary that is not copied from anywhere, because provisioning
has no equivalent: a refresh renders only `AGENTS.md` and `SOUL.md`, so a
template carrying anything else must not produce it.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from app.services.agent_refresh.eligibility import Refreshable
from app.services.agent_refresh.render import desired_for

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


def _uid(p: str) -> str:
    return f"{p}-{uuid.uuid4().hex[:8]}"


async def _template(conn, tid: str, files: dict, *, allow=None, deny=None):
    await conn.execute(
        "INSERT INTO templates (id, type, name, tools_allow, tools_deny) "
        "VALUES ($1, 'SHCA', $1, $2, $3)", tid, allow or [], deny or [])
    for name, content in files.items():
        await conn.execute(
            "INSERT INTO template_files (template_id, filename, content) "
            "VALUES ($1, $2, $3)", tid, name, content)


async def _base_agent(conn, template_id: str, *, agent_type="SHCA",
                      allow=None, deny=None) -> str:
    aid = _uid("base")
    await conn.execute(
        "INSERT INTO agents (id, name, type, status, is_template, "
        "                    template_id, created_by, tools_allow, tools_deny) "
        "VALUES ($1, $1, $2, 'active', TRUE, $3, 'test', $4, $5)",
        aid, agent_type, template_id, allow or [], deny or [])
    return aid


async def _skill(conn, name: str, applicable_to: list[str]):
    await conn.execute(
        "INSERT INTO skills (name, title, description, content, "
        "                    content_hash, applicable_to, status) "
        "VALUES ($1, $1, $1, $2, md5($2), $3, 'active')",
        name, f"body of {name}", applicable_to)


def _shca(base_agent_id: str) -> Refreshable:
    return Refreshable(gateway_agent_id=_uid("gw"), agent_type="SHCA",
                       template_id=base_agent_id, goal_id="G1",
                       thread_id="t1", workspace=None)


@pytest.mark.asyncio
async def test_the_per_agent_overlay_wins():
    """Copied from `app/openclaw.py:3155-3165`. A per-agent AGENTS.md
    beats the template's, or a refresh would silently revert an agent
    whose prompt was deliberately customised."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        tid = _uid("tpl")
        await _template(conn, tid, {"AGENTS.md": "FROM TEMPLATE",
                                    "SOUL.md": "soul"})
        base = await _base_agent(conn, tid)
        await conn.execute(
            "INSERT INTO agent_files (agent_id, filename, content) "
            "VALUES ($1, 'AGENTS.md', 'FROM OVERLAY')", base)

        got = await desired_for(conn, _shca(base))
        assert got.files["AGENTS.md"] == "FROM OVERLAY"
        assert got.files["SOUL.md"] == "soul"
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_template_carrying_memory_does_not_render_it():
    """THE BOUNDARY. `template_files` can hold anything; a refresh
    renders two filenames. If this fails, the applier is the only thing
    left between a template edit and an agent's memory."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        tid = _uid("tpl")
        await _template(conn, tid, {
            "AGENTS.md": "a", "SOUL.md": "s",
            "MEMORY.md": "DO NOT WRITE THIS",
            "IDENTITY.md": "nor this",
            "USER.md": "nor this either"})
        base = await _base_agent(conn, tid)
        got = await desired_for(conn, _shca(base))
        assert set(got.files) == {"AGENTS.md", "SOUL.md"}
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_an_overlay_cannot_smuggle_memory_in_either():
    """The overlay is per-agent and writable from the admin UI, so it is
    the likelier route for an unexpected filename than the template."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        tid = _uid("tpl")
        await _template(conn, tid, {"AGENTS.md": "a", "SOUL.md": "s"})
        base = await _base_agent(conn, tid)
        await conn.execute(
            "INSERT INTO agent_files (agent_id, filename, content) "
            "VALUES ($1, 'MEMORY.md', 'accumulated memory')", base)
        got = await desired_for(conn, _shca(base))
        assert "MEMORY.md" not in got.files
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_the_kernel_assistant_gets_no_skills():
    """Zero by design, not few. A skills/ directory it never reads once
    routed it into announcing a phase for a state machine that does not
    exist here."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        await _skill(conn, _uid("sk").replace("-", "_"), ["SHCA"])
        # `kernel-assistant` is a real seeded template, not a fixture --
        # using the actual id is the point of the test.
        base = await _base_agent(conn, "kernel-assistant")
        got = await desired_for(conn, _shca(base))
        assert got.skills == {}
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_an_ordinary_assistant_gets_its_applicable_skills():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        mine = _uid("sk").replace("-", "_")
        theirs = _uid("sk").replace("-", "_")
        await _skill(conn, mine, ["SHCA"])
        await _skill(conn, theirs, ["GO"])
        tid = _uid("tpl")
        await _template(conn, tid, {"AGENTS.md": "a"})
        base = await _base_agent(conn, tid)
        got = await desired_for(conn, _shca(base))
        assert mine in got.skills
        assert theirs not in got.skills, "applicable_to must be honoured"
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_shca_tools_come_from_the_templates_table():
    """Mirrors `app/openclaw.py:2966-2986`, including that an SHCA's
    allowlist is NOT put through the registry resolver. Copied rather
    than unified: resolving it here would change which tools existing
    assistants are granted."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        tid = _uid("tpl")
        await _template(conn, tid, {"AGENTS.md": "a"},
                        allow=["read", "write"], deny=["bash"])
        base = await _base_agent(conn, tid, allow=["ignored"])
        got = await desired_for(conn, _shca(base))
        assert got.tools_allow == ["read", "write"]
        assert got.tools_deny == ["bash"]
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_tools_fall_back_to_the_base_agent_when_the_template_is_empty():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        tid = _uid("tpl")
        await _template(conn, tid, {"AGENTS.md": "a"}, allow=[])
        base = await _base_agent(conn, tid, allow=["from_agent"])
        got = await desired_for(conn, _shca(base))
        assert got.tools_allow == ["from_agent"]
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_missing_template_is_reported_not_silently_empty():
    """A render that returns nothing must say why, or "no changes" and
    "misconfigured" look identical in the plan."""
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        base = _uid("orphan")
        await conn.execute(
            "INSERT INTO agents (id, name, type, status, is_template, "
            "                    created_by) "
            "VALUES ($1, $1, 'SHCA', 'active', TRUE, 'test')", base)
        got = await desired_for(conn, _shca(base))
        assert got.files == {}
        assert got.notes, "a thin render must explain itself"
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_go_tools_come_from_the_templates_table():
    """THE BUG THIS CAUGHT, before it ever ran.

    `go-v2` exists in `templates` and NOT in `agents` -- provisioning
    reads it at `app/services/go_agent.py:417`. An earlier draft of the
    renderer queried `agents`, found nothing, and returned empty tools.
    Because an empty allow list is deliberately never pushed (to the
    gateway it means "all tools permitted"), every GO would have been
    silently skipped for tools while the plan reported a clean refresh.

    A quiet under-delivery that reads as success is the worst shape a
    bug in this tool can take.
    """
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        from app.services.go_agent import GO_TEMPLATE_ID
        n = await conn.fetchval(
            "SELECT coalesce(array_length(tools_allow, 1), 0) FROM templates "
            " WHERE id = $1 AND status = 'active'", GO_TEMPLATE_ID)
        if not n:
            pytest.skip(f"{GO_TEMPLATE_ID} not seeded in this database")

        go = Refreshable(gateway_agent_id="go-test", agent_type="GO",
                         template_id="go-test", goal_id="G1",
                         thread_id=None, workspace=None)
        got = await desired_for(conn, go)
        assert got.tools_allow, (
            "a GO must resolve tools from `templates`; an empty result "
            "here is silently skipped at push time")
        assert got.skills == {}, "go-v2 has zero skills by design"
    finally:
        await tx.rollback()
        await conn.close()
