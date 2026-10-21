"""Naming the person an agent acted for — and refusing to invent one.

An assistant message attributed only to the assistant is attributable to a
robot and to nobody, which is the same dead end `system` attribution was.
So agent events now carry `on_behalf_of`.

THE RISK IN DOING THAT IS OVER-CLAIMING, and it is the whole subject of this
file. `threads.created_by` looks like a uid and holds 'engine' on 33 threads,
'go-KER15' and friends on 34 more, and 'system' on one. A resolver that
trusted it would put 68 false principals into an APPEND-ONLY table, where
they cannot be corrected. Refusing to answer is the correct behaviour, and
these tests exist to keep it that way.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

# What the resolver runs. Kept here as a copy of the SQL rather than by
# importing the function, because the function needs the app's pool while
# these tests hold a plain connection — the QUERY is what is under test.
_SQL = """
    SELECT COALESCE(owner.id, creator.id)
      FROM threads t
      LEFT JOIN agent_instances ai ON ai.gateway_agent_id = t.instance_id
      LEFT JOIN actors owner   ON owner.id = ai.owner_actor_id AND owner.type = 'user'
      LEFT JOIN actors creator ON creator.uid = t.created_by  AND creator.type = 'user'
     WHERE t.id = $1 AND COALESCE(owner.id, creator.id) IS NOT NULL
     LIMIT 1
"""


def _run(fn):
    async def go():
        conn = await _connect_or_skip()
        try:
            if not await conn.fetchval(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'agent_instances'"):
                pytest.skip("no agent_instances here")
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


def test_a_machine_owned_thread_resolves_to_nobody():
    """THE ONE THAT MATTERS.

    `engine` and `go-KER15` are thread owners and neither is a person. The
    join goes through `actors` with type='user', so they yield NULL rather
    than a plausible-looking principal. On an append-only ledger a false
    name is permanent.
    """
    async def check(conn):
        rows = await conn.fetch(
            """SELECT t.id, t.created_by FROM threads t
                WHERE t.created_by IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM actors a
                                   WHERE a.type = 'user' AND a.uid = t.created_by)
                  AND NOT EXISTS (SELECT 1 FROM agent_instances ai
                                    JOIN actors o ON o.id = ai.owner_actor_id
                                                 AND o.type = 'user'
                                   WHERE ai.gateway_agent_id = t.instance_id)""")
        if not rows:
            pytest.skip("no machine-owned threads in this database")
        for r in rows:
            got = await conn.fetchval(_SQL, r["id"])
            assert got is None, (
                f"thread owned by {r['created_by']!r} resolved to a "
                f"principal — that name would be permanent and wrong")
    _run(check)


def test_an_assistant_thread_resolves_to_its_owner():
    """Structural, not inferred: an SHCA is per-(goal, user), and
    `agent_instances.owner_actor_id` is the binding written at provisioning
    time rather than a guess about who last spoke."""
    async def check(conn):
        rows = await conn.fetch(
            """SELECT t.id, ai.owner_actor_id FROM threads t
                 JOIN agent_instances ai ON ai.gateway_agent_id = t.instance_id
                 JOIN actors o ON o.id = ai.owner_actor_id AND o.type = 'user'
                LIMIT 20""")
        if not rows:
            pytest.skip("no owned assistant threads here")
        for r in rows:
            assert await conn.fetchval(_SQL, r["id"]) == r["owner_actor_id"]
    _run(check)


def test_the_resolver_only_ever_returns_a_user():
    """Not an agent, not the system. `on_behalf_of` names a PERSON or
    nothing — pointing it at the system actor would restate the
    over-attribution this replaced, in a field that reads as a fact."""
    async def check(conn):
        rows = await conn.fetch("SELECT id FROM threads LIMIT 200")
        if not rows:
            pytest.skip("no threads")
        for r in rows:
            got = await conn.fetchval(_SQL, r["id"])
            if got is None:
                continue
            kind = await conn.fetchval("SELECT type FROM actors WHERE id = $1", got)
            assert kind == "user", f"resolved to a {kind}, not a person"
    _run(check)


def test_the_go_is_not_given_a_principal():
    """The orchestrator is goal-level and serves every member, so there is
    no one person it acts for. `context_goal_id` records the true scope.

    Asserted against the CODE rather than the data: the data here is a
    single-user database where every goal thread happens to resolve to the
    same person, so it would pass a wrong implementation.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[3].joinpath(
        "app", "routers", "goal_thread.py").read_text(encoding="utf-8")
    assert "human_principal_for_thread" not in src, (
        "the GO's messages must not be attributed to the thread's creator — "
        "it serves every member of the goal, so that is a guess dressed as "
        "a fact")


def test_the_strict_uid_resolver_never_falls_back_to_system():
    """`human_actor` returns the SYSTEM actor for an unknown uid. That is
    right for `actor_id`, where something must be named, and wrong for
    `on_behalf_of`: "on behalf of the system" asserts nothing about
    anybody."""
    import inspect

    from app.ledger.actors import human_principal_for_uid

    src = inspect.getsource(human_principal_for_uid)
    assert "system_actor" not in src and "SYSTEM_ACTOR_ID" not in src
    assert "type = 'user'" in src, "it must verify the actor IS a person"

    async def check(conn):
        assert await conn.fetchval(
            "SELECT id FROM actors WHERE type='user' AND uid=$1 LIMIT 1",
            "definitely-not-a-real-uid") is None
    _run(check)
