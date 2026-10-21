"""Both providers are reachable, and a missing key says so early.

THE REQUIREMENT: every executor agent may run on Anthropic or Gemini.
Ec1g built the mechanism — a profile names a model, `provider_of` picks the
seam, `adapter_for_profile` resolves it per execution. This is the other
half: the mechanism is only true end to end if the credential is there, and
the failure when it is not has to name the profile rather than the vendor.

WHY AN EMPTY KEY IS THE CASE THAT MATTERS. `docker-compose.yml` writes
`ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}`, so an unset variable arrives as
the empty string, not absent. `os.environ["ANTHROPIC_API_KEY"]` then returns
`""`, the adapter constructs happily, and the leg dies several seconds later
on a 401 that `faults.py` classifies AUTH and does not retry. The operator
sees a vendor error on a story, not "this profile asks for a provider this
stack has no key for".

Read from source where importing would pull in a provider SDK.
"""
from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import re
import sys

import pytest

from app.services.tests.shipped_tree import IS_DISTRIBUTION

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _source(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _config():
    """`config.py` alone, without `models/__init__` and its SDK imports."""
    spec = importlib.util.spec_from_file_location(
        "_models_config", ROOT / "kernel/harness/models/config.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ── the credential itself ────────────────────────────────────────────────

@pytest.mark.parametrize("provider,var", [("anthropic", "ANTHROPIC_API_KEY"),
                                          ("gemini", "GEMINI_API_KEY")])
def test_a_present_key_is_returned(clean_env, provider, var):
    clean_env.setenv(var, "sk-real")
    assert _config().api_key(provider) == "sk-real"


@pytest.mark.parametrize("value", [None, "", "   "])
@pytest.mark.parametrize("provider,var", [("anthropic", "ANTHROPIC_API_KEY"),
                                          ("gemini", "GEMINI_API_KEY")])
def test_absent_empty_and_blank_all_refuse(clean_env, provider, var, value):
    """Three states, one answer. The compose file produces the second."""
    if value is not None:
        clean_env.setenv(var, value)
    with pytest.raises(RuntimeError) as caught:
        _config().api_key(provider)
    assert var in str(caught.value), "the error must name the variable"


def test_an_unknown_provider_is_a_different_error(clean_env):
    """A typo'd provider is a programming mistake, not a missing key, and
    conflating them would send somebody looking for an OPENAI_API_KEY."""
    with pytest.raises(ValueError):
        _config().api_key("openai")


# ── the adapters actually use it ─────────────────────────────────────────

@pytest.mark.parametrize("rel", ["kernel/harness/models/anthropic.py",
                                 "kernel/harness/models/gemini.py"])
def test_no_adapter_reads_the_variable_itself(rel):
    """Two adapters reading their own variable name is two chances to
    disagree with the compose file, and neither would check for empty."""
    src = _source(rel)
    assert "api_key(" in src
    assert "os.environ" not in src, (
        f"{rel} reads the environment directly again; the check in "
        f"config.api_key is then bypassed")


def test_the_key_names_match_the_compose_file():
    """The variable names live in one dict; this is the join to the file
    that actually sets them. A rename on one side is silent otherwise."""
    src = _source("kernel/harness/models/config.py")
    declared = set(re.findall(r'"(\w+_API_KEY)"', src))
    assert declared == {"ANTHROPIC_API_KEY", "GEMINI_API_KEY"}, declared

    if IS_DISTRIBUTION:
        # No E0 compose here; the execution plane's model-key secret is
        # what sets these names for the pods that read them.
        deploy = _source("scripts/infra/deploy_execution_plane.sh")
        for name in declared:
            assert re.search(rf"--from-literal={name}=", deploy), (
                f"{name} is read by the adapters and never set in the "
                f"execution plane's secret, so that provider is unreachable")
        return

    compose = _source("infra/e0/docker-compose.yml")
    for name in declared:
        assert re.search(rf"^\s+{name}:", compose, re.M), (
            f"{name} is read by the adapters and never set in "
            f"docker-compose.yml, so that provider is unreachable in E0")


# ── and the profile really can ask for either ────────────────────────────

def _registry():
    """`registry.py` alone. Importing it normally runs `models/__init__`,
    which imports both SDKs; loading the file directly does not."""
    spec = importlib.util.spec_from_file_location(
        "_models_registry", ROOT / "kernel/harness/models/registry.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("model,provider", [
    ("claude-sonnet-5", "anthropic"),
    ("claude-opus-5", "anthropic"),
    ("claude-haiku-4-5-20251001", "anthropic"),
    ("gemini-3.7-flash", "gemini"),
    ("gemini-3.8-flash", "gemini"),
])
def test_the_seam_is_derived_from_the_model(model, provider):
    """A profile names a model and never a provider. Run for real — the
    registry is pure data and needs no SDK."""
    assert _registry().provider_of(model) == provider


def test_a_model_nobody_supports_is_refused_by_name():
    """Not defaulted to a seam. A guessed provider sends the id to an SDK
    that has never heard of it, and comes back as a vendor 404."""
    with pytest.raises(Exception):
        _registry().provider_of("gpt-9-turbo")


def test_a_profile_may_declare_either_and_the_loader_accepts_it(tmp_path):
    """The end of the chain Ec1g opened: `model` was read by `_build` and
    refused by `_KNOWN`, so a profile that named one failed to load."""
    from kernel.profiles.loader import load_all

    (tmp_path / "base.toml").write_text('tools = ["read"]\n', encoding="utf-8")
    for name, model in (("a-anthropic", "claude-sonnet-5"),
                        ("b-gemini", "gemini-3.7-flash"),
                        ("c-default", "")):
        d = tmp_path / name
        d.mkdir()
        line = f'model = "{model}"\n' if model else ""
        (d / "profile.toml").write_text(
            f'kind = "errand"\ndomain = "{name}"\n'
            f'workspace_driver = "none"\n{line}', encoding="utf-8")
        (d / "prompt.md").write_text("You are a test profile.", encoding="utf-8")

    loaded = load_all(str(tmp_path))
    assert loaded[("errand", "a-anthropic")].model == "claude-sonnet-5"
    assert loaded[("errand", "b-gemini")].model == "gemini-3.7-flash"
    # And the one that says nothing keeps the worker's adapter.
    assert loaded[("errand", "c-default")].model == ""


def test_the_shipped_WORK_profiles_still_name_no_model():
    """DELIBERATE, and the reason story executors are unaffected.

    An empty `model` returns the worker's default, so every work profile
    behaves exactly as it did before any of this existed. Which model each
    agent kind SHOULD run on is a cost and quality decision for a person,
    and writing one here would change what story-coding runs on today.

    A PHASE PROFILE MAY NAME ONE, and story/coding/review does. The
    hazard this guards is silently changing what an EXISTING executor
    runs on; a phase that did not exist before changes nothing by
    naming its own. It names `gemini-3.8-flash`, which is also the
    harness fallback today -- so it is currently a no-op, and the point
    of writing it is that a deployment retuning AGENTOS_ASSIST_MODEL for
    executors does not silently retune the thing that checks them.
    """
    from kernel.profiles.loader import load_all

    named = {f"{p.kind}/{p.domain}": p.model
             for p in load_all().values() if p.model and not p.phase}
    assert not named, (
        f"{named} now pins a model. That is a policy change, not a "
        f"refactor — make it deliberately, and re-measure the corpus.")


def test_the_reviewer_names_its_own_model():
    """The other half: it must not quietly go back to inheriting.

    A reviewer that follows the executors' model setting is one a cost
    change can retune without anybody deciding to.
    """
    from kernel.profiles.registry import resolve

    assert resolve("story", "coding", "review").model == "gemini-3.8-flash"


def test_a_missing_key_stops_the_leg_naming_the_profile():
    """The end-to-end shape, with the SDK layer stubbed out.

    `adapter_for_profile` catches anything the adapter raises and re-raises
    `UnrunnableProfile` with the profile's kind/domain and the model in the
    message — so a stack with no Anthropic key tells the operator which
    profile to change, not which vendor rejected them.

    HONEST ABOUT WHAT IT PROVES HERE. On a host without the `anthropic`
    package the raise comes from the import rather than from the missing
    key, so what this pins on a laptop is the WRAPPING — that whatever the
    adapter layer fails with, the profile is named. The credential branch
    itself is covered above, directly, and this case exercises it for real
    only in the container.
    """
    from kernel.worker.model_choice import (UnrunnableProfile,
                                            adapter_for_profile, forget)

    forget()

    class _P:
        kind, domain, model = "errand", "ops", "claude-sonnet-5"

    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        with pytest.raises(UnrunnableProfile) as caught:
            adapter_for_profile(_P(), object())
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
        forget()

    said = str(caught.value)
    assert "errand/ops" in said and "claude-sonnet-5" in said
