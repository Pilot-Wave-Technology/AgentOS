"""The scaffolding every other Slack test stands on, tested itself.

A FIXTURE THAT QUIETLY DOES NOT BUILD IS WORSE THAN NO FIXTURE. The
whole-file skip on a missing database reports as a pass, so a `world()`
that inserted nothing, or a `FakeSlack` that recorded nothing, would
leave twenty green test files proving nothing at all. That has already
happened in this repo once, with a suite that stayed green on a module
whose source had been deleted.

So this file checks the scaffolding does what the other files assume:
the rows exist, the rollback is real, and the fake records what it is
told and raises what it is asked to.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.tests.slack_fixtures import FakeSlack, Pool, world


def _run(coro):
    return asyncio.run(coro)


# ── the fake ─────────────────────────────────────────────────────────────────

def test_the_fake_records_every_call_with_its_arguments():
    c = FakeSlack()
    c.chat_postMessage(channel="C1", text="hi")
    c.conversations_create(name="x", is_private=True)
    assert [n for n, _ in c.calls] == ["chat_postMessage", "conversations_create"]
    assert c.of("chat_postMessage")[0]["text"] == "hi"
    assert c.of("conversations_create")[0]["is_private"] is True


def test_the_fake_returns_a_distinct_ts_per_post():
    """Phase 8 stores the ts to edit the message later; two posts that share
    a ts would make that test pass while the real thing overwrote one card."""
    c = FakeSlack()
    a = c.chat_postMessage(channel="C1", text="one")["ts"]
    b = c.chat_postMessage(channel="C1", text="two")["ts"]
    assert a != b


def test_the_fake_raises_the_error_code_it_is_given():
    from slack_sdk.errors import SlackApiError

    c = FakeSlack(fail_with={"chat_postMessage": "channel_not_found"})
    with pytest.raises(SlackApiError) as e:
        c.chat_postMessage(channel="C1", text="hi")
    assert e.value.response["error"] == "channel_not_found"
    # and only that method
    assert c.conversations_join(channel="C1")["ok"] is True


def test_a_rate_limit_carries_retry_after():
    """Phase 4 asserts the handler honours it, so the fake must supply it."""
    from slack_sdk.errors import SlackApiError

    c = FakeSlack(fail_with={"chat_postMessage": "ratelimited"})
    with pytest.raises(SlackApiError) as e:
        c.chat_postMessage(channel="C1", text="hi")
    assert e.value.response.status_code == 429
    assert e.value.response.headers["Retry-After"] == "1"


# ── the world ────────────────────────────────────────────────────────────────

def test_the_world_actually_inserts_what_it_promises():
    async def body():
        async with world() as (conn, pool, w):
            assert await conn.fetchval(
                "SELECT count(*) FROM goals WHERE id=$1", w["goal"]) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM stories WHERE id=$1", w["story"]) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM threads WHERE id=$1", w["thread"]) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM tasks WHERE id=$1", w["task"]) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM approvals WHERE id=$1::uuid", w["approval"]) == 1
    _run(body())


def test_the_member_is_a_member_and_the_outsider_is_not():
    """The distinction every authorization test depends on."""
    async def body():
        async with world() as (conn, pool, w):
            from app.auth import get_goal_role
            assert await get_goal_role(w["member"], w["goal"]) is not None
            assert await get_goal_role(w["outsider"], w["goal"]) is None
    _run(body())


def test_slack_is_wired_up_when_asked_and_absent_when_not():
    async def body():
        async with world(with_slack=True) as (conn, pool, w):
            n = await conn.fetchval(
                "SELECT count(*) FROM platform_channel_bindings WHERE goal_id=$1",
                w["goal"])
            assert n == 1 and "integration" in w
        async with world(with_slack=False) as (conn, pool, w):
            n = await conn.fetchval(
                "SELECT count(*) FROM platform_channel_bindings WHERE goal_id=$1",
                w["goal"])
            assert n == 0 and "integration" not in w
    _run(body())


def test_the_binding_is_findable_the_way_the_code_finds_it():
    """`get_binding_by_thread` is the real outbound lookup. If the fixture's
    rows do not satisfy its joins — which require an active integration AND
    an enabled goal integration — every relay test would silently take the
    no-binding branch and pass."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.base import get_binding_by_thread
            b = await get_binding_by_thread("slack", w["thread"], w["goal"])
            assert b is not None, (
                "the fixture's binding is invisible to the real lookup — "
                "every outbound test built on it would pass for the wrong reason")
            assert b["external_channel_id"] == w["channel"]
    _run(body())


def test_participants_are_settable_because_phase_6_needs_them():
    async def body():
        async with world(participants=["a", "b"]) as (conn, pool, w):
            p = await conn.fetchval(
                "SELECT participants FROM threads WHERE id=$1", w["thread"])
            assert list(p) == ["a", "b"]
    _run(body())


def test_nothing_survives_the_rollback():
    """The guarantee that lets these run against a shared database."""
    async def body():
        async with world() as (conn, pool, w):
            goal = w["goal"]
        # New connection: the transaction above is gone either way.
        from app.services.tests.slack_fixtures import connect_or_skip
        c2 = await connect_or_skip()
        try:
            assert await c2.fetchval(
                "SELECT count(*) FROM goals WHERE id=$1", goal) == 0, (
                "the fixture leaked rows into the database")
        finally:
            await c2.close()
    _run(body())


def test_the_pool_wrapper_supports_what_the_code_calls():
    async def body():
        async with world() as (conn, pool, w):
            assert isinstance(pool, Pool)
            assert await pool.fetchval("SELECT 1") == 1
            assert (await pool.fetchrow("SELECT 1 AS n"))["n"] == 1
            assert len(await pool.fetch("SELECT 1")) == 1
            async with pool.acquire() as c:
                assert await c.fetchval("SELECT 2") == 2
    _run(body())
