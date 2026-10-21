"""The model a person picks is the model that answers.

FOUND BY TRACING THE FEATURE END TO END rather than testing its halves.
Each half had tests and each half passed:

    the UI writes goals.executor_model / stories.executor_model   tested
    dispatch resolves and freezes executions.executor_model       tested
    the leg reads it                                              -------

`choice.for_execution` was written to be that third step and NOTHING
EVER CALLED IT. `pod_main` built one adapter from the deployment default
and handed it to every leg of every run, so a story set to Anthropic was
recorded as Anthropic, frozen as Anthropic, and executed on Gemini --
with nothing in the journal naming which model answered, and the bill
the only place it would ever have shown. `pod_main`'s own comment
claimed the opposite, which is why reading the code did not find it.

THE SHAPE IS THE ONE THIS PROJECT KEEPS PAYING FOR: every unit passes and
the wire between them is missing. So these tests are about the WIRE.

AND THE PRECEDENCE IS PART OF THE FIX, not a detail. `freeze` used to
write the RESOLVED model -- story, then goal, then the deployment
default -- so every run carried a non-NULL choice and a leg reading it
would override `profile.model` with a default nobody picked. It now
freezes only what a person actually chose, and NULL means "fall through"
exactly as it did before the column existed.
"""
from __future__ import annotations

import asyncio
import ast
import pathlib

import pytest

from kernel.worker import model_choice

ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _no_cached_adapters():
    model_choice.forget()
    yield
    model_choice.forget()


class _Adapter:
    """Stands in for a vendor client, so these tests are about the
    PRECEDENCE and not about which SDKs happen to be installed here --
    `anthropic` is not, and a test that needed it would skip on this
    machine and in CI while claiming to cover the wire."""

    def __init__(self, model):
        self._model = model

    def __repr__(self):
        return f"<adapter {self._model}>"


@pytest.fixture(autouse=True)
def _fake_models(monkeypatch):
    """Resolve any model the REGISTRY knows; refuse anything else.

    Keeps `test_a_model_that_cannot_be_served_is_loud` honest: the
    refusal it checks is the registry's, which is the one that fires in
    production, not an import error peculiar to this laptop.
    """
    from kernel.harness.models import registry
    import kernel.harness.models as models

    def fake(model_id):
        if model_id not in registry.SUPPORTED:
            raise ValueError(f"{model_id!r} is not a model anything serves")
        return _Adapter(model_id)

    monkeypatch.setattr(models, "adapter_for_model", fake)


class _Profile:
    kind, domain = "story", "coding"

    def __init__(self, model=""):
        self.model = model


class _Conn:
    """Answers `frozen_for`'s one query and records every SQL it saw."""

    def __init__(self, frozen=None):
        self._frozen = frozen
        self.seen: list[str] = []

    async def fetchval(self, sql, *args):
        self.seen.append(" ".join(sql.split()))
        return self._frozen


# ── the wire that was missing ──────────────────────────────────────────

def test_the_frozen_choice_beats_the_deployment_default():
    """THE BUG. A story dispatched with a model must run on it."""
    got = asyncio.run(model_choice.adapter_for_execution(
        _Conn(frozen="claude-sonnet-5"), 214, _Profile(), "DEFAULT"))

    assert got != "DEFAULT", (
        "the leg ran on the deployment default even though this run was "
        "dispatched with a model of its own -- the feature is recorded, "
        "frozen, and ignored")
    assert got._model == "claude-sonnet-5"


def test_the_frozen_choice_also_beats_a_profile():
    """A person's explicit pick for THIS story is more specific than the
    system's opinion about this kind of work."""
    got = asyncio.run(model_choice.adapter_for_execution(
        _Conn(frozen="claude-sonnet-5"),
        214, _Profile(model="gemini-3.8-flash"), "DEFAULT"))

    assert got._model == "claude-sonnet-5"


def test_a_profile_keeps_its_model_when_nobody_chose():
    """THE REGRESSION THE FREEZE CHANGE PREVENTS. `adapter_for_profile`
    promises "nothing about story-coding changes until somebody writes a
    model into its toml". Freezing a resolved default would have broken
    that promise silently for every run."""
    got = asyncio.run(model_choice.adapter_for_execution(
        _Conn(frozen=None), 214, _Profile(model="claude-sonnet-5"),
        "DEFAULT"))

    assert got._model == "claude-sonnet-5", (
        "the profile's own model was ignored")


def test_nobody_choosing_anything_changes_nothing():
    """The property that makes this safe to ship: a deployment where no
    goal or story has ever opened the dropdown behaves exactly as it
    did."""
    got = asyncio.run(model_choice.adapter_for_execution(
        _Conn(frozen=None), 214, _Profile(), "DEFAULT"))

    assert got == "DEFAULT"


# ── the grant hazard ───────────────────────────────────────────────────

def test_the_leg_reads_executions_and_nothing_else():
    """`agentos_leg` holds SELECT on `kernel.executions` and NOT on
    `goals`. The version of this that fell through to the goal would be
    `permission denied for table goals` inside the execution pod, on a
    path with no fallback -- and only ever for a run whose model was
    never frozen, which is the rarest case and so the last to be
    noticed."""
    conn = _Conn(frozen=None)
    asyncio.run(model_choice.adapter_for_execution(
        conn, 214, _Profile(), "DEFAULT"))

    assert conn.seen, "the leg asked nothing at all"
    for sql in conn.seen:
        assert "goals" not in sql.lower(), (
            f"the leg reads `goals`, which agentos_leg cannot: {sql}")


def test_a_database_that_cannot_answer_does_not_end_the_leg():
    """A settings lookup must never be the thing that fails a run."""
    class Broken:
        async def fetchval(self, sql, *args):
            raise RuntimeError("connection reset")

    got = asyncio.run(model_choice.adapter_for_execution(
        Broken(), 214, _Profile(), "DEFAULT"))

    assert got == "DEFAULT"


def test_a_model_that_cannot_be_served_is_loud():
    """The other direction, and `adapter_for_profile` argues it: running
    on a model nobody asked for and reporting success hides a cost and
    quality difference that never shows up in the journal."""
    with pytest.raises(model_choice.UnrunnableProfile):
        asyncio.run(model_choice.adapter_for_execution(
            _Conn(frozen="no-such-model-9000"), 214, _Profile(), "DEFAULT"))


# ── freeze records a CHOICE, not a resolution ──────────────────────────

def test_freeze_writes_nothing_when_nobody_chose():
    class Recording:
        def __init__(self):
            self.writes = []

        async def execute(self, sql, *args):
            self.writes.append(args)

    from kernel.harness.models import choice

    conn = Recording()
    asyncio.run(choice.freeze(conn, 214, None))
    assert conn.writes == [], (
        "a run nobody configured was frozen anyway, so it now looks "
        "deliberately set and will override its profile's model")

    asyncio.run(choice.freeze(conn, 214, "claude-sonnet-5"))
    assert len(conn.writes) == 1


def test_dispatch_freezes_the_choice_and_not_the_default():
    """A source guard, because the behavioural half needs a schema. The
    difference between `for_story` and `chosen_for_story` here is the
    whole of the precedence fix."""
    src = (ROOT / "kernel/conductor/sandbox_spec.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "chosen_for_story":
            break
    else:
        raise AssertionError(
            "dispatch no longer calls `chosen_for_story`; if it went back "
            "to `for_story` it freezes the deployment default as though a "
            "person had picked it")


def test_for_execution_is_gone():
    """It was the dead third step. Leaving it would leave the trap: a
    function that looks like the wire, is not called, and whose
    fall-through reads a table the leg has no grant on."""
    from kernel.harness.models import choice

    assert not hasattr(choice, "for_execution"), (
        "`for_execution` is back; it has no caller, and its goal "
        "fall-through is `permission denied` in the execution pod")
    assert hasattr(choice, "frozen_for")


def test_the_leg_actually_calls_it():
    """THE WIRE ITSELF, and the only check here that would have caught
    the original bug. Every other test in this file passes against a
    `_take_leg` that never calls any of it."""
    tree = ast.parse((ROOT / "kernel/worker/loop.py").read_text(
        encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_take_leg":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                fn = inner.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else "")
                if name == "adapter_for_execution":
                    return
        raise AssertionError(
            "_take_leg does not resolve the execution's model, so the "
            "story's and the goal's choice are written, frozen, and "
            "never read -- the bug this file exists for")
    raise AssertionError("_take_leg is gone; re-point this check")
