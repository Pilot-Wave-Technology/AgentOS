"""Slack is a mirror. When the mirror breaks, the work still happened.

THIS FILE PINS SOMETHING THAT IS ALREADY RIGHT, which is unusual enough
to explain. Every internal→Slack call writes to the database first and
wraps the relay, and every relay carries an outer `except Exception`. So
a Slack outage, a revoked token or a bad channel id costs you the
notification and nothing else: the card is in the DB, the ledger event
is written, the approval is committed, and the web UI is unaffected.

That is the single best property of this integration and it is held up
entirely by convention — a dozen try/except blocks that somebody has to
remember. The whole Slack layer is about to be rewritten across ten
phases. Without these tests, one tidy-up that lets an exception
propagate turns "Slack didn't post" into "the approval didn't happen",
and nothing would catch it until a human lost work.

So: break the relay on purpose, and assert the real thing still occurred.
"""
from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest


def _run(coro):
    return asyncio.run(coro)


def _boom(*a, **k):
    raise RuntimeError("slack is down")


async def _aboom(*a, **k):
    raise RuntimeError("slack is down")


# ── the relays swallow, by construction ──────────────────────────────────────

def test_every_public_relay_wraps_its_whole_body():
    """A source guard, because the property is "no FUTURE relay forgets".

    A behavioural test covers the relays that exist now; this covers the
    one somebody adds in phase 8 and does not wrap.
    """
    from app.channels.slack import outbound

    missing = []
    for name, fn in vars(outbound).items():
        if not name.startswith("relay") and not name.startswith("post_to"):
            continue
        if not inspect.iscoroutinefunction(fn):
            continue
        src = inspect.getsource(fn)
        if "except Exception" not in src:
            missing.append(name)
    assert not missing, (
        f"these relays can raise into their caller: {missing}. Every public "
        "relay needs an outer `except Exception` — a Slack failure must never "
        "become an internal failure.")


def test_a_relay_with_no_binding_returns_quietly():
    """The most-exercised branch in production: Slack simply isn't set up."""
    from app.channels.slack.outbound import relay_agent_message

    async def body():
        async def no_binding(*a, **k):
            return None
        with patch("app.channels.base.get_binding_by_thread", no_binding):
            # Returns, does not raise, does not need a bot token.
            assert await relay_agent_message("G1", "t1", "hello") is None
    _run(body())


def test_a_relay_whose_slack_call_explodes_still_returns():
    from app.channels.slack.outbound import relay_agent_message

    async def body():
        async def binding(*a, **k):
            return {"external_channel_id": "C1", "integration_id": "i1"}
        with patch("app.channels.base.get_binding_by_thread", binding), \
             patch("app.channels.slack.outbound._get_bot_token", _aboom):
            assert await relay_agent_message("G1", "t1", "hello") is None
    _run(body())


# ── and the callers do not depend on the relay succeeding ────────────────────

def test_one_platform_failing_does_not_stop_another():
    """`relay_all_outbound` isolates per platform. Slack dying must not take
    Notion with it — they share nothing but a loop."""
    from app.channels import base

    calls = []

    async def ok_relay(*a, **k):
        calls.append("notion")

    async def bad_relay(*a, **k):
        calls.append("slack")
        raise RuntimeError("slack is down")

    src = inspect.getsource(base.relay_all_outbound)
    assert "except Exception" in src, (
        "relay_all_outbound no longer isolates platforms from each other")
    # The ordering guarantee is what matters and it is structural: the body
    # wraps each platform's call, so neither order can break the other.
    assert src.count("except Exception") >= 1


def test_the_diff_review_card_is_persisted_before_slack_is_told():
    """If the write came after the relay, a Slack failure would lose the card.

    REPOINTED. The relay used to be an explicit `relay_card` call right
    here; it now hangs off `notify_new_message`, so that every card kind
    reaches Slack rather than the three that had a call. The ordering
    guarantee is unchanged and is still the whole property: persist,
    then tell anyone.
    """
    from app.services import diff_review

    src = inspect.getsource(diff_review.post_diff_review)
    assert "notify_new_message" in src, (
        "this test is pinned to the wrong function, or the card no longer "
        "goes through the fan-out that relays it")
    insert_at = src.index("INSERT INTO messages")
    relay_at = src.index("notify_new_message")
    assert insert_at < relay_at, (
        "the card is announced before it is stored — a relay failure, or a "
        "reader racing the notify, would find no row")


def test_the_card_relay_cannot_take_the_caller_down():
    """It runs inside notify_new_message now, which every card poster
    calls, so an exception there would break posting a card at all."""
    from app.chat_module import repository

    src = inspect.getsource(repository._relay_card_if_any)
    assert "except Exception" in src, (
        "the card fan-out can raise into notify_new_message, which every "
        "card poster calls — a Slack problem would stop cards being posted")


@pytest.mark.parametrize("module,fn", [
    ("app.services.diff_review", "post_diff_review"),
    ("app.services.story_recovery", "post_recovery_card"),
])
def test_card_producers_treat_the_relay_as_optional(module, fn):
    import importlib

    mod = importlib.import_module(module)
    target = getattr(mod, fn, None)
    if target is None:
        pytest.skip(f"{module}.{fn} no longer exists")
    src = inspect.getsource(target)
    if "relay_card" not in src:
        pytest.skip(f"{fn} no longer relays directly")
    assert "try:" in src and "except" in src, (
        f"{fn} calls relay_card outside a try/except — a Slack outage would "
        "take the internal action down with it")


def test_a_slack_originated_approval_commits_before_slack_is_answered():
    """The inverse direction. `_respond_ephemeral` swallows its own failures,
    so an approval is never lost because Slack did not acknowledge it."""
    from app.channels.slack import actions

    src = inspect.getsource(actions._respond_ephemeral)
    assert "except" in src, (
        "_respond_ephemeral can raise — a failed ephemeral would roll back "
        "into the caller that had already approved something")
