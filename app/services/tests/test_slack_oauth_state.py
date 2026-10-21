"""The install flow cannot be driven from outside.

WITHOUT A STATE PARAMETER the OAuth callback accepts any code anybody
sends it. Get an admin to follow a link and we exchange the attacker's
code and store the result as this deployment's workspace credentials --
bot token and all. It was a known, deferred gap.

The cases that matter are the ones where a weak implementation still
looks right: a state that is accepted twice, one that never expires,
and one that passes when nothing was ever issued.
"""
from __future__ import annotations

import asyncio
import inspect
import time

from app.channels.slack import oauth_state
from app.services.tests.slack_fixtures import world


def _run(coro):
    return asyncio.run(coro)


def test_a_freshly_issued_state_verifies():
    async def body():
        async with world() as (conn, pool, w):
            token = await oauth_state.issue(pool)
            assert token
            assert await oauth_state.verify(pool, token) is True
    _run(body())


def test_the_same_state_cannot_be_used_twice():
    """Single-use. A replayable token is a slower version of no token."""
    async def body():
        async with world() as (conn, pool, w):
            token = await oauth_state.issue(pool)
            assert await oauth_state.verify(pool, token) is True
            assert await oauth_state.verify(pool, token) is False
    _run(body())


def test_a_state_we_never_issued_is_refused():
    async def body():
        async with world() as (conn, pool, w):
            assert await oauth_state.verify(pool, "handcrafted") is False
    _run(body())


def test_an_empty_state_is_refused():
    """The shape of the bug being fixed: the old flow sent no state at all,
    so the callback saw "" and had nothing to compare it against."""
    async def body():
        async with world() as (conn, pool, w):
            await oauth_state.issue(pool)
            assert await oauth_state.verify(pool, "") is False
    _run(body())


def test_a_failed_attempt_still_consumes_the_token():
    """Otherwise a wrong guess costs the attacker nothing and they can
    keep going against a token that stays valid."""
    async def body():
        async with world() as (conn, pool, w):
            token = await oauth_state.issue(pool)
            assert await oauth_state.verify(pool, "wrong") is False
            assert await oauth_state.verify(pool, token) is False, (
                "the real token survived a failed attempt")
    _run(body())


def test_an_expired_state_is_refused():
    async def body():
        async with world() as (conn, pool, w):
            token = await oauth_state.issue(pool)
            # age it past the TTL in place
            import json
            cfg = json.loads(await conn.fetchval(
                "SELECT config::text FROM platform_integrations WHERE platform='slack'"))
            cfg["pending_oauth_state"]["issued_at"] = (
                int(time.time()) - oauth_state.TTL_SECONDS - 1)
            await conn.execute(
                "UPDATE platform_integrations SET config = $1 WHERE platform='slack'",
                json.dumps(cfg))
            assert await oauth_state.verify(pool, token) is False
    _run(body())


def test_two_installs_do_not_share_a_token():
    async def body():
        async with world() as (conn, pool, w):
            a = await oauth_state.issue(pool)
            b = await oauth_state.issue(pool)
            assert a != b
            # the second supersedes the first, which must then fail
            assert await oauth_state.verify(pool, a) is False
    _run(body())


# ── and it is actually wired into the flow ───────────────────────────────────

def test_the_install_redirect_carries_a_state():
    from app.channels.slack.inbound import SlackInboundAdapter

    src = inspect.getsource(SlackInboundAdapter.oauth_install)
    assert "state=" in src and "issue" in src, (
        "the consent URL does not carry a state parameter")


def test_the_callback_verifies_it_before_exchanging_the_code():
    """Order matters: verifying after the exchange would still have us
    talking to Slack with somebody else's code."""
    from app.channels.slack.inbound import SlackInboundAdapter

    src = inspect.getsource(SlackInboundAdapter.oauth_callback)
    assert "verify" in src, "the callback does not check state at all"
    verify_at = src.index("_verify_state")
    exchange_at = src.index("oauth.v2.access")
    assert verify_at < exchange_at, (
        "the code is exchanged before the state is verified")


def test_both_oauth_routes_require_an_admin():
    """Completing an install writes credentials for the whole deployment.
    The form that saves them next to it was already admin-only."""
    from app.channels import router as chrouter

    src = inspect.getsource(chrouter)
    install = src[src.index("async def oauth_install"):src.index("async def oauth_callback")]
    callback = src[src.index("async def oauth_callback"):]
    callback = callback[:callback.index("@router.post")] if "@router.post" in callback else callback
    for name, seg in (("install", install), ("callback", callback)):
        assert "require_admin" in seg, (
            f"oauth_{name} is open to any signed-in user")
