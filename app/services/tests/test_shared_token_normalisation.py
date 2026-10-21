"""The shared bearer must survive a round trip through a shell.

Cloud Run mounts a secret's payload verbatim; every other reader of the
same secret is `$(gcloud secrets versions access ...)`, and command
substitution strips trailing newlines. So a secret stored with one gives
this process a different string from the one the gateway, the conductor
and the executors hold -- and `hmac.compare_digest` refuses it.

It failed in ONE direction only: the gateway trims the token it is
handed (resolveSharedConnectAuth), so the control panel's own
connections were accepted while every call back was refused with what
looked like "HTTP 405 Method Not Allowed".
"""
import os

import pytest

from app.config import Settings
from app.internal_auth import _bearer_matches


@pytest.fixture
def env(monkeypatch):
    def _set(value):
        monkeypatch.setenv("OPENCLAW_AUTH_TOKEN", value)
        return Settings()
    return _set


def test_a_trailing_newline_in_the_secret_is_stripped(env):
    assert env("s3cret\n").openclaw_auth_token == "s3cret"


def test_surrounding_whitespace_goes_too(env):
    assert env("  s3cret \r\n").openclaw_auth_token == "s3cret"


def test_a_clean_token_is_untouched(env):
    assert env("s3cret").openclaw_auth_token == "s3cret"


def test_the_shell_read_form_now_authenticates(env):
    # What the gateway actually sends: the value as a shell read it.
    settings = env("s3cret\n")
    assert _bearer_matches("Bearer s3cret", settings.openclaw_auth_token)


def test_a_genuinely_wrong_token_is_still_refused(env):
    settings = env("s3cret\n")
    assert not _bearer_matches("Bearer other", settings.openclaw_auth_token)
    assert not _bearer_matches("Bearer ", settings.openclaw_auth_token)


def test_an_unset_token_never_matches(env):
    # Fail-closed: an empty expected value must not accept "Bearer ".
    assert not _bearer_matches("Bearer ", "")
