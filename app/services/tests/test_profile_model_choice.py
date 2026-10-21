"""An agent type picks its model; the provider follows from it.

WHAT WAS WRONG, in two halves that each made the other invisible.

`Profile.model` existed, `loader._build` read it out of the toml — and
`_KNOWN` refused the key, so any profile that actually wrote one failed to
load with "unknown key(s): model". A field that is declared, plumbed, and
rejected by the validator.

And the worker built ONE adapter at startup, `get_adapter("gemini")`, and
injected it into every leg of every execution for the life of the process.
Both adapters have been built the whole time and
`kernel.harness.models.adapter_for_model` describes itself as "THE ENTRY
POINT FOR AN AGENT" — nothing called it.

So neither half alone would have been enough, and fixing either alone
would have looked like it worked.

Most of this runs without the provider SDKs, which are absent on a dev
host. Anything needing a live adapter is read from source instead.
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest

from kernel.worker.model_choice import (UnrunnableProfile,
                                        adapter_for_profile, forget)

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _source(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


class _Profile:
    def __init__(self, model: str = "", kind: str = "errand",
                 domain: str = "ops"):
        self.model, self.kind, self.domain = model, kind, domain


def setup_function() -> None:
    forget()


# ── the profile may now declare one ──────────────────────────────────────

def test_a_profile_can_declare_a_model_and_its_budgets():
    """These four were read by `_build` and refused by `_KNOWN`."""
    from kernel.profiles.loader import _KNOWN

    for key in ("model", "leg_tokens", "leg_steps", "max_legs"):
        assert key in _KNOWN, f"{key} is read by _build but cannot be set"


def test_the_shipped_profiles_still_load():
    """The loader is strict about unknown keys, which is why this broke in
    the first place. Widening the set must not have widened it wrongly."""
    from kernel.profiles.loader import load_all

    profiles = load_all()
    assert len(profiles) >= 3
    # None of them names a model yet, so all three keep the worker's.
    for key, prof in profiles.items():
        assert isinstance(prof.model, str)


# ── choosing the adapter ─────────────────────────────────────────────────

def test_no_declared_model_keeps_the_workers_default():
    """THE PROPERTY THAT PROTECTS STORY EXECUTORS. A profile with no
    opinion behaves exactly as it did before this existed."""
    sentinel = object()
    assert adapter_for_profile(_Profile(""), sentinel) is sentinel
    assert adapter_for_profile(_Profile("   "), sentinel) is sentinel
    assert adapter_for_profile(_Profile(None), sentinel) is sentinel


def test_an_unservable_model_stops_the_leg_rather_than_falling_back():
    """Falling back would run the work on a model nobody asked for and
    report success. A profile that names `claude-opus-5` and quietly gets
    a flash model is a cost and quality difference invisible in the
    journal."""
    with pytest.raises(UnrunnableProfile) as caught:
        adapter_for_profile(_Profile("not-a-real-model"), object())
    assert "not-a-real-model" in str(caught.value)
    assert "errand/ops" in str(caught.value)


def test_the_worker_asks_per_execution_not_once_at_startup():
    """`_worker_forever` builds one adapter for the life of the process.
    The per-leg override has to happen where the profile is known.

    RE-POINTED, and the name it pinned is why. It asserted
    `adapter_for_profile` -- the function -- where what it is ABOUT is
    "the worker asks per execution". `_take_leg` now calls
    `adapter_for_execution`, which asks a strictly wider question:
    `executions.executor_model` (a person's frozen choice for THIS run)
    first, then `profile.model`, then the injected default. The profile
    branch is still there, one level down, and
    `test_a_profile_keeps_its_model_when_nobody_chose` in
    test_the_chosen_model_reaches_the_leg.py holds it.

    The literal was load-bearing in the wrong direction: the test would
    have kept passing if the call had been left in place and its RESULT
    thrown away, and it failed for a change that made the property it
    describes more true.
    """
    src = _source("kernel/worker/loop.py")
    assert "adapter_for_execution" in src, (
        "the worker still injects one adapter into every execution")
    # After the profile is resolved, and before the leg runs.
    at_resolve = src.index("profile = resolve(row[")
    at_choice = src.index("adapter_for_execution(conn")
    at_leg = src.index("result = await run_leg_fn(")
    assert at_resolve < at_choice < at_leg


# ── both providers are really reachable ──────────────────────────────────

def test_the_registry_serves_anthropic_and_gemini():
    """Read from source: importing the registry pulls in the provider
    SDKs, which a dev host does not have.

    The requirement is that an executor can run on either, so the check is
    that both families are in the supported set with real entries.
    """
    src = _source("kernel/harness/models/registry.py")
    block = src[src.index("SUPPORTED: dict"):]
    gemini = set(re.findall(r'_gemini\("([\w.\-]+)"', block))
    anthropic = set(re.findall(r'_anthropic\("([\w.\-]+)"', block))
    assert len(gemini) >= 3, sorted(gemini)
    assert len(anthropic) >= 2, sorted(anthropic)
    # The two the design names for errands and stories respectively.
    assert "gemini-3.7-flash" in gemini
    assert "claude-sonnet-5" in anthropic


def test_the_provider_is_derived_from_the_model_never_declared():
    """A profile names a model. Asking for a provider too is asking for
    two facts to be kept in step, and a model handed to the wrong adapter
    surfaces as a vendor 404 seconds later."""
    from kernel.profiles.loader import _KNOWN

    assert "provider" not in _KNOWN and "adapter" not in _KNOWN, (
        "a profile must not name a seam")
    # Docstrings stripped before matching. The module's own prose QUOTES
    # `get_adapter("gemini")` while explaining why it must not be used, so
    # a raw grep fails on the text describing the rule it checks — which
    # is a test defeated by its subject being well documented.
    import ast

    tree = ast.parse(_source("kernel/worker/model_choice.py"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    assert "adapter_for_model" in code
    assert 'get_adapter(' not in code, "must not hardcode a provider"


def test_the_adapter_is_cached_per_model():
    """An adapter is a client and a set of limits, not a per-leg object.
    Rebuilding one every leg reopens a connection pool eight times a
    minute under normal concurrency."""
    import inspect

    from kernel.worker import model_choice

    src = inspect.getsource(model_choice)
    assert "_cache" in src and "_cache[wanted]" in src
    assert callable(model_choice.forget)
