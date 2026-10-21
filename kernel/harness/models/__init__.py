"""Model adapter registry — E5b increment of Stage E.

Exports `get_adapter(name)` and `adapter_for_model(model)`, both returning a
`ModelAdapter`. Two adapters: anthropic (reasoning="reported") and gemini
(reasoning="unreported").

THE ADAPTER CLASSES ARE IMPORTED LAZILY, and that is not a micro-optimisation.

`anthropic.py` does `import anthropic` at module scope and `gemini.py` does
the same for `google.genai`. Importing them HERE meant that
`from kernel.harness.models.types import ToolResult` — a dataclass whose own
imports are `dataclasses` and `typing` — ran the package `__init__` first and
pulled in both provider SDKs. Neither is installed on a dev machine.

The cost was paid in tests. `ToolResult` is the return type of every tool, so
every worker-side tool module was unimportable on a laptop, so the guards that
should have exercised them read source with `ast` instead — and an AST test
checks that code was WRITTEN, never that it works. `web_fetch` was dispatched
to a function that did not exist for months, and the test that could have
caught it asserted set membership because importing the real thing was
impossible.

Deferring these two lines means a tool implementation can be imported, called
and asserted against a real database on any machine. The SDK is imported when
somebody actually asks for an adapter, which is the only time it is needed.
"""

from kernel.harness.models.types import ModelAdapter  # noqa: F401

_ADAPTERS = ("anthropic", "gemini")


def get_adapter(name: str, **kwargs) -> ModelAdapter:
    """Get a model adapter by name.

    Args:
        name: 'anthropic' or 'gemini'.
        **kwargs: passed to the adapter constructor (e.g. model=...).

    Raises:
        ValueError: if name is not a registered adapter.
    """
    if name == "anthropic":
        from kernel.harness.models.anthropic import AnthropicAdapter as cls
    elif name == "gemini":
        from kernel.harness.models.gemini import GeminiAdapter as cls
    else:
        raise ValueError(
            f"Unknown adapter {name!r}. Available: {sorted(_ADAPTERS)}"
        )
    return cls(**kwargs)


def adapter_for_model(model: str, **kwargs):
    """The adapter that serves this model. THE ENTRY POINT FOR AN AGENT.

    A user picks a MODEL. Which seam serves it is our problem, not
    theirs: asking somebody to name a provider AND a model is asking
    them to keep two facts in step and to know which adapters we happen
    to have built. `claude-opus-5` is enough.

    It also removes a whole class of mistake — a model handed to the
    wrong adapter reaches an SDK that has never heard of it and comes
    back as a vendor 404, several seconds and one confusing log line
    later.
    """
    from kernel.harness.models.registry import provider_of

    return get_adapter(provider_of(model), model=model, **kwargs)
