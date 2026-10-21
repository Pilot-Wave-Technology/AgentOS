"""A secret that was never provided must read as not configured.

WHY THIS IS A TEST AND NOT A COMMENT.

Secret Manager rejects an empty payload, and Cloud Run refuses to deploy
a service referencing a secret with no version. So `prerequisites.sh`
creates every unprovided secret with a SINGLE SPACE. That part is
forced.

What was not forced was the sentence three deployer-facing documents
then told people: "consumers treat whitespace-only as unset". No
consumer did. Every guard in this codebase is `if not key`, and `" "` is
truthy -- so an unconfigured Slack, GitHub or OpenAI credential read as
CONFIGURED, the feature behind it switched itself on, and it failed
later and somewhere else: a 401 from a vendor, or a webhook verifying
every signature against one space.

The validator makes the sentence true. These tests are what keep it
true, because the failure is silent in both directions: nothing errors
when the normalisation is removed, and nothing errors when it starts
eating values it should not.
"""
from __future__ import annotations

import pytest

from app.config import Settings


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n", " \t\n "])
def test_whitespace_only_is_the_empty_string(monkeypatch, blank):
    """The placeholder, in every shape a shell or an editor can produce."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", blank)
    assert Settings().slack_signing_secret == ""
    assert not Settings().slack_signing_secret


def test_a_real_value_is_not_touched(monkeypatch):
    """Normalising unset must not become trimming everything.

    A secret may legitimately carry surrounding whitespace, and changing
    a provided value is a different decision from recognising an absent
    one. `openclaw_auth_token` has its own stripper for the one case
    that actually bit; nothing else is stripped.
    """
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "  real-value  ")
    assert Settings().slack_signing_secret == "  real-value  "


def test_it_applies_to_every_setting_not_a_list_of_them():
    """The validator is on `"*"` on purpose.

    A list of secret names is a list somebody forgets to extend, and the
    cost of forgetting is exactly the silent degradation this exists to
    remove. If someone narrows it to named fields, this fails.
    """
    validators = Settings.__pydantic_decorators__.field_validators
    wildcards = [name for name, dec in validators.items()
                 if "*" in dec.info.fields]
    assert wildcards == ["_whitespace_is_unset"], validators


def test_non_string_settings_pass_through():
    """`"*"` means ints and bools reach the validator too.

    It returns them unchanged, and that is worth pinning: an `isinstance`
    slip here would turn every numeric setting into a string or a crash
    at import, which is the kind of failure that takes down startup
    rather than one feature.
    """
    s = Settings()
    by_type: dict[str, int] = {}
    for name in Settings.model_fields:
        by_type[type(getattr(s, name)).__name__] = 1 + by_type.get(
            type(getattr(s, name)).__name__, 0)
    assert by_type.get("bool", 0) > 0 and by_type.get("int", 0) > 0, by_type
