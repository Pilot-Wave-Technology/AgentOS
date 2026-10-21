"""An unsigned Slack request is refused, including when nothing is configured.

THE FAILURE THIS PREVENTS IS AN OPEN DOOR. `verify_request` used to
return True when no signing secret could be found in either the database
or the environment, logging "skipping verification (dev mode)" -- and
`slack_signing_secret` defaults to "". All three inbound surfaces gate on
that one function, so any deployment that had not finished its Slack
setup, or had lost the DB row, accepted unsigned POSTs from the internet
and dispatched them to real handlers.

The first test here is the whole point of the file: NO SECRET MUST STILL
MEAN NO. Everything else checks the parts that were already right and
must stay right, because a fail-closed verifier that 500s on a DB blip
is its own outage -- Slack disables an endpoint that keeps erroring.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from unittest.mock import patch

from app.channels.slack.inbound import SlackInboundAdapter

SECRET = "8f14e45fceea167a5a36dedd4bea2543"
BODY = b"token=x&command=%2Fgoal&text=status"


def _run(coro):
    return asyncio.run(coro)


def _sign(body: bytes, secret: str, ts: str) -> str:
    base = f"v0:{ts}:{body.decode()}".encode()
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _headers(body: bytes = BODY, secret: str = SECRET, ts: str | None = None,
             sig: str | None = None) -> dict:
    ts = ts if ts is not None else str(int(time.time()))
    return {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": sig if sig is not None else _sign(body, secret, ts),
    }


def _verify(body: bytes, headers: dict, *, db_secret: str | None = SECRET,
            env_secret: str = "", db_raises: bool = False) -> bool:
    """Run the real verifier with the secret sources under our control."""
    async def _get_signing_secret():
        if db_raises:
            raise RuntimeError("connection reset by peer")
        return db_secret

    class _Settings:
        slack_signing_secret = env_secret

    with patch("app.channels.slack.config.get_signing_secret", _get_signing_secret), \
         patch("app.config.get_settings", lambda: _Settings()):
        return _run(SlackInboundAdapter().verify_request(body, headers))


# ── the one that was open ────────────────────────────────────────────────────

def test_no_secret_anywhere_REJECTS():
    """Was `return True`. An unconfigured deployment gets no Slack, not all of it."""
    assert _verify(BODY, _headers(), db_secret=None, env_secret="") is False


def test_no_secret_rejects_even_a_well_formed_signature():
    """There is nothing to check a signature against, so it cannot pass.

    Guards the lazy fix: short-circuiting on "the headers look right"
    would let anyone who can compute sha256 through.
    """
    headers = _headers(secret="whatever-the-attacker-likes")
    assert _verify(BODY, headers, db_secret=None, env_secret="") is False


# ── and the parts that must keep working ─────────────────────────────────────

def test_a_correctly_signed_request_passes():
    assert _verify(BODY, _headers()) is True


def test_the_env_var_is_still_a_fallback_when_the_db_has_nothing():
    headers = _headers(secret=SECRET)
    assert _verify(BODY, headers, db_secret=None, env_secret=SECRET) is True


def test_a_body_changed_after_signing_is_rejected():
    headers = _headers(BODY)
    assert _verify(BODY + b"&evil=1", headers) is False


def test_a_signature_from_a_different_secret_is_rejected():
    headers = _headers(secret="00000000000000000000000000000000")
    assert _verify(BODY, headers) is False


def test_a_stale_timestamp_is_rejected():
    """±5 minutes. Replay protection, and the timestamp is inside the signature."""
    old = str(int(time.time()) - 301)
    assert _verify(BODY, _headers(ts=old)) is False


def test_a_fresh_timestamp_just_inside_the_window_passes():
    recent = str(int(time.time()) - 299)
    assert _verify(BODY, _headers(ts=recent)) is True


def test_missing_headers_are_rejected():
    assert _verify(BODY, {}) is False
    assert _verify(BODY, {"x-slack-signature": "v0=abc"}) is False
    assert _verify(BODY, {"x-slack-request-timestamp": "123"}) is False


def test_a_non_integer_timestamp_is_rejected_not_raised():
    """`int(timestamp)` on junk must not become a 500."""
    assert _verify(BODY, _headers(ts="not-a-number", sig="v0=abc")) is False


# ── the DB must not be able to take the endpoint down ────────────────────────

def test_a_database_error_rejects_rather_than_raising():
    """A 500 is worse than a refusal: Slack disables an endpoint that keeps
    erroring, and turning it back on is a manual trip to api.slack.com."""
    assert _verify(BODY, _headers(), db_raises=True) is False


# ── the invariant, stated so it cannot be quietly undone ─────────────────────

def test_the_verifier_has_no_environment_escape_hatch():
    """An `if APP_ENV != production: return True` would pass every test above
    while reopening the door on every environment that is not production --
    which is where the credentials are least likely to be set."""
    import inspect

    src = inspect.getsource(SlackInboundAdapter.verify_request)
    body = src[src.index('"""', src.index('"""') + 3):]   # drop the docstring
    for forbidden in ("APP_ENV", "app_env", "is_production", "DEBUG"):
        assert forbidden not in body, (
            f"verify_request branches on {forbidden!r} — the verifier must "
            "fail closed in every environment, with no exceptions")
