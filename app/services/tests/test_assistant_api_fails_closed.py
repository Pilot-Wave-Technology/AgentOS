"""The assistant API must not treat "no token" as "open".

WHAT WENT WRONG. `_authorised` returned `not want or header == want`, so
an unset ASSISTANT_API_TOKEN authorised everybody, and the startup line
said so cheerfully: "(NO TOKEN SET - writes are open)". The variable
appears in no compose file and no cloudbuild step, so that was the state
everywhere. This surface carries start_story, steer, amend, cancel_story
and every GO write, each addressed by a goal id in the URL path with no
other scoping -- so open means any caller that can reach the port can
drive any goal.

The fix is the one the credential sidecar already uses: refuse at start,
not at the first write. A service that accepts connections and only then
discovers it cannot authenticate anybody is one somebody has already
pointed an agent at.
"""

from __future__ import annotations

import pytest

from kernel.assistant import api


def test_no_token_and_no_opt_out_refuses_to_start(monkeypatch):
    monkeypatch.delenv("ASSISTANT_API_TOKEN", raising=False)
    monkeypatch.delenv(api.OPEN_ENV, raising=False)
    with pytest.raises(RuntimeError) as caught:
        api._require_auth_decision()
    message = str(caught.value)
    assert "ASSISTANT_API_TOKEN" in message
    assert api.OPEN_ENV in message, (
        "the refusal should name the way out, or the next person sets a "
        "token they do not want in order to get a stack running")


def test_a_token_means_enforced_not_open(monkeypatch):
    monkeypatch.setenv("ASSISTANT_API_TOKEN", "s3cret")
    monkeypatch.delenv(api.OPEN_ENV, raising=False)
    assert api._require_auth_decision() is False


def test_the_opt_out_is_explicit_and_only_that_value(monkeypatch):
    monkeypatch.delenv("ASSISTANT_API_TOKEN", raising=False)
    for value in ("", "0", "true", "yes", "TRUE"):
        monkeypatch.setenv(api.OPEN_ENV, value)
        with pytest.raises(RuntimeError):
            api._require_auth_decision()
    monkeypatch.setenv(api.OPEN_ENV, "1")
    assert api._require_auth_decision() is True


def test_a_token_wins_over_the_opt_out(monkeypatch):
    """Belt and braces: a stack with both set is authenticated, not open."""
    monkeypatch.setenv("ASSISTANT_API_TOKEN", "s3cret")
    monkeypatch.setenv(api.OPEN_ENV, "1")
    assert api._require_auth_decision() is False


def test_authorised_still_rejects_a_wrong_token(monkeypatch):
    """The request-time check is unchanged and must stay strict."""
    monkeypatch.setenv("ASSISTANT_API_TOKEN", "s3cret")

    class _H:
        headers = {"X-Assistant-Token": "wrong"}
        _authorised = api.Handler._authorised

    assert _H()._authorised() is False
    _H.headers = {"X-Assistant-Token": "s3cret"}
    assert _H()._authorised() is True
