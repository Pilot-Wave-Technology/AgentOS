"""Pin the canonical native-provider set against silent drift.

The registered native providers are exposed as
`app.services.model_registry.management.NATIVE_PROVIDERS`. This file
asserts that value literally so any future edit that widens or narrows
the set fails a unit test rather than merging silently.

`openai`/`codex-cli` are deliberately excluded for now — deferred, not
yet wanted as registered providers (see NATIVE_PROVIDERS's docstring).
"""


def test_native_providers_pinned_set():
    from app.services.model_registry.management import NATIVE_PROVIDERS

    assert NATIVE_PROVIDERS == frozenset({
        "anthropic", "google", "claude-cli",
    })


def test_native_providers_is_frozenset():
    from app.services.model_registry.management import NATIVE_PROVIDERS

    assert isinstance(NATIVE_PROVIDERS, frozenset)
