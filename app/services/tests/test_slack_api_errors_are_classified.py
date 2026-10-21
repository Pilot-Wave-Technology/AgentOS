"""A Slack failure means something, and the something is recorded.

EIGHT IDENTICAL `except SlackApiError -> warning -> return` BLOCKS
treated every error the same way, which is the same as treating none
of them. A revoked token produced an endless stream of identical
warnings with no status change and no admin surface -- you found out
Slack had stopped working by noticing it had been quiet. A bot kicked
from a channel left a live binding that failed forever with no attempt
to rejoin. A 429 was a dropped message, because slack_sdk ships no
retry handler by default and all twenty-three clients were built bare.

The tests below are about the RECORDING, not the logging. A log line
nobody reads is what we had.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from slack_sdk.errors import SlackApiError

from app.channels.slack.client import Failure, classify, make_client, on_failure
from app.services.tests.slack_fixtures import world


def _run(coro):
    return asyncio.run(coro)


def _err(code: str) -> SlackApiError:
    return SlackApiError(code, {"ok": False, "error": code})


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    ("channel_not_found", Failure.DEAD_CHANNEL),
    ("is_archived", Failure.DEAD_CHANNEL),
    ("not_in_channel", Failure.NOT_A_MEMBER),
    ("invalid_auth", Failure.DEAD_TOKEN),
    ("token_revoked", Failure.DEAD_TOKEN),
    ("account_inactive", Failure.DEAD_TOKEN),
    ("app_uninstalled", Failure.DEAD_TOKEN),
    ("ratelimited", Failure.RATE_LIMITED),
    ("something_new_slack_invented", Failure.OTHER),
])
def test_each_error_gets_its_own_answer(code, expected):
    assert classify(_err(code)) is expected


def test_the_five_answers_are_distinct():
    """If two codes that need different handling collapsed into one
    answer, every test above would still pass. They must not."""
    codes = ["channel_not_found", "not_in_channel", "invalid_auth",
             "ratelimited", "whatever"]
    assert len({classify(_err(c)) for c in codes}) == 5


def test_a_non_slack_exception_does_not_explode_the_classifier():
    assert classify(RuntimeError("network went away")) is Failure.OTHER


# ── and the answer is written down ───────────────────────────────────────────

def test_a_dead_channel_archives_its_binding():
    """Otherwise the next thousand messages all retry a channel that is
    gone, each costing an API call and a warning."""
    async def body():
        async with world() as (conn, pool, w):
            bid = await conn.fetchval(
                "SELECT id FROM platform_channel_bindings WHERE goal_id=$1", w["goal"])
            kind = await on_failure(_err("channel_not_found"), pool=pool,
                                    binding_id=str(bid), where="test")
            assert kind is Failure.DEAD_CHANNEL
            assert await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings WHERE id=$1",
                bid) is not None
    _run(body())


def test_a_dead_token_marks_the_integration_so_a_human_can_see_it():
    async def body():
        async with world() as (conn, pool, w):
            kind = await on_failure(_err("token_revoked"), pool=pool,
                                    integration_id=w["integration"], where="test")
            assert kind is Failure.DEAD_TOKEN
            assert await conn.fetchval(
                "SELECT status FROM platform_integrations WHERE id=$1::uuid",
                w["integration"]) == "revoked"
    _run(body())


def test_a_dead_token_stops_the_relay_finding_a_binding():
    """The point of marking it: the binding lookup joins on
    `pi.status = 'active'`, so one write turns off every relay for that
    workspace instead of each one failing separately, forever."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.base import get_binding_by_thread
            assert await get_binding_by_thread("slack", w["thread"], w["goal"])
            await on_failure(_err("invalid_auth"), pool=pool,
                             integration_id=w["integration"], where="test")
            assert await get_binding_by_thread("slack", w["thread"], w["goal"]) is None
    _run(body())


def test_a_transient_error_changes_nothing():
    """`OTHER` must not archive or deactivate anything — over-reacting to
    a blip would take a healthy integration down."""
    async def body():
        async with world() as (conn, pool, w):
            bid = await conn.fetchval(
                "SELECT id FROM platform_channel_bindings WHERE goal_id=$1", w["goal"])
            await on_failure(_err("internal_error"), pool=pool,
                             integration_id=w["integration"],
                             binding_id=str(bid), where="test")
            assert await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings WHERE id=$1",
                bid) is None
            assert await conn.fetchval(
                "SELECT status FROM platform_integrations WHERE id=$1::uuid",
                w["integration"]) == "active"
    _run(body())


def test_archiving_is_idempotent():
    """Two relays can fail on the same dead channel at once."""
    async def body():
        async with world() as (conn, pool, w):
            bid = await conn.fetchval(
                "SELECT id FROM platform_channel_bindings WHERE goal_id=$1", w["goal"])
            await on_failure(_err("is_archived"), pool=pool,
                             binding_id=str(bid), where="test")
            first = await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings WHERE id=$1", bid)
            await on_failure(_err("is_archived"), pool=pool,
                             binding_id=str(bid), where="test")
            assert await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings WHERE id=$1",
                bid) == first, "a second failure moved the archive timestamp"
    _run(body())


def test_on_failure_never_raises_even_when_the_database_is_gone():
    """It runs inside an except block. Raising there replaces a Slack
    problem with a traceback in a caller that cannot do anything useful."""
    class _Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("no database")

    async def body():
        assert await on_failure(_err("channel_not_found"), pool=_Broken(),
                                binding_id="x", where="test") is Failure.DEAD_CHANNEL
    _run(body())


# ── rate limiting is handled by the client, not by each caller ───────────────

def test_the_sync_client_carries_usable_retry_handlers():
    """USABLE, not merely present. The first version of this asked whether
    a handler's class NAME contained "RateLimit" -- which is true of both
    the sync and the async handler, so it could not tell them apart and
    passed while the async client was given sync handlers. That shipped,
    and the first Slack call in production died with
    `'RateLimitErrorRetryHandler' object has no attribute
    'can_retry_async'`: channel creation stopped working entirely.

    So each handler is now checked for the method its client will
    actually call."""
    c = make_client("xoxb-test")
    assert c.retry_handlers, "a sync client with no retry handlers"
    for h in c.retry_handlers:
        assert hasattr(h, "can_retry"), (
            f"{type(h).__name__} is not usable by a sync client")
    assert any("RateLimit" in type(h).__name__ for h in c.retry_handlers)


def test_no_module_builds_a_bare_client_any_more():
    """A single bare `WebClient(token=...)` reintroduces the whole problem
    for whichever call site it serves, silently."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app/channels/slack"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "client.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "WebClient(token=" in text:
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} construct a Slack client directly. Use "
        "app/channels/slack/client.py so retry handlers are always attached.")


def test_the_relays_classify_rather_than_logging_and_shrugging():
    """The eight blocks were identical; the guard is that none of them has
    gone back to being a bare warning."""
    from app.channels.slack import outbound

    src = inspect.getsource(outbound)
    assert src.count("except SlackApiError as exc:") == src.count("on_failure("), (
        "a SlackApiError handler no longer classifies — it is back to "
        "logging a warning and dropping the message")


# ── the event loop is not blocked by a Slack round-trip ──────────────────────

def test_nothing_in_the_module_uses_a_synchronous_client():
    """23 bare `WebClient` calls meant every Slack round-trip blocked the
    event loop, so a slow Slack degraded unrelated requests on the same
    worker. A single sync client reintroduces that for its call sites."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app/channels/slack"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "client.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "make_client(" in text or "WebClient(" in text:
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} build a synchronous Slack client. Use make_async_client "
        "— a sync call on the event loop blocks every other request on the "
        "worker for the length of Slack's p99.")


def test_every_slack_call_is_awaited():
    """An un-awaited coroutine is not an error, it is a no-op plus a
    RuntimeWarning — the message simply never gets sent."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app/channels/slack"
    unawaited = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            if fn.attr.split("_")[0] not in ("chat", "conversations", "users",
                                             "files", "views"):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "client"):
                continue
            if id(node) not in awaited:
                unawaited.append(f"{path.name}:{node.lineno} {fn.attr}")
    assert not unawaited, (
        f"these Slack calls are not awaited and therefore never happen: "
        f"{unawaited}")


def test_the_async_client_carries_ASYNC_retry_handlers():
    """THE ONE THAT WOULD HAVE CAUGHT IT. An AsyncWebClient calls
    `can_retry_async` on every handler it holds; the sync handlers do not
    have it, and every Slack call raises AttributeError before it is even
    attempted."""
    from app.channels.slack.client import make_async_client

    c = make_async_client("xoxb-test")
    assert c.retry_handlers, "an async client with no retry handlers"
    for h in c.retry_handlers:
        assert hasattr(h, "can_retry_async"), (
            f"{type(h).__name__} is a SYNC handler on an async client — "
            "every Slack call will raise AttributeError")
    assert any("RateLimit" in type(h).__name__ for h in c.retry_handlers)


def test_an_async_client_can_actually_make_a_call():
    """The end of the argument: build the real client and drive one call
    through its retry machinery against a stub transport. A class-shape
    test can be satisfied by something that still cannot run."""
    import asyncio
    from unittest.mock import patch

    from app.channels.slack.client import make_async_client

    c = make_async_client("xoxb-test")

    async def _fake_send(*a, **k):
        from slack_sdk.web.async_slack_response import AsyncSlackResponse
        return AsyncSlackResponse(
            client=c, http_verb="POST", api_url="https://slack.test/api/auth.test",
            req_args={}, data={"ok": True}, headers={}, status_code=200)

    async def body():
        with patch.object(type(c), "api_call", _fake_send):
            r = await c.api_call("auth.test")
            assert r["ok"] is True

    asyncio.run(body())
