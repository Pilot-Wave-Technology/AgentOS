"""Model choice: per goal, per story, frozen at dispatch.

WHAT WAS BROKEN BEFORE ANY OF THIS. `AGENTOS_ASSIST_MODEL` was one
deployment-wide variable read as TWO INCOMPATIBLE THINGS --
`service_tick` handed it to `get_adapter`, which takes a PROVIDER and
raises on anything else, while `quick.ASSIST_MODEL` used it as a MODEL
ID defaulting to `gemini-3.8-flash`. Setting it to the shape either one
documented broke the other, so nobody could choose a model at all.

And `assess.py` built `GeminiAdapter(ASSIST_MODEL)` outright, so a goal
choosing an Anthropic judge would have reached a Gemini SDK with a
Claude model id -- a vendor 404 several seconds later.

ONE FIELD, NOT TWO. `models/__init__.py` argues it: "A user picks a
MODEL. Which seam serves it is our problem, not theirs." A `provider`
column could disagree with the model, and the disagreement surfaces
mid-leg.
"""
from __future__ import annotations

import pytest

from kernel.harness.models import choice


# ── the menu, and what is refused ──────────────────────────────────────

def test_the_dropdown_and_the_validator_are_the_same_list():
    """A menu that can offer a model the validator refuses is a form
    that rejects its own options."""
    from kernel.harness.models.registry import SUPPORTED, available

    offered = {m.id for m in available()}
    assert offered == set(SUPPORTED)
    assert len(offered) > 1


@pytest.mark.parametrize("model", ["claude-opus-5", "gemini-3.8-flash"])
def test_a_supported_model_is_accepted(model):
    assert choice.validate(model) == model


def test_an_unknown_model_is_refused_where_it_is_set():
    """A story dispatched with a model nobody can serve runs, reaches a
    vendor, and comes back 404 -- with an admission slot and a pod
    spent. The refusal names the menu."""
    with pytest.raises(choice.UnknownModel) as caught:
        choice.validate("gpt-4")

    assert "claude-opus-5" in str(caught.value), "the refusal hides the menu"


def test_blank_means_inherit_and_not_a_choice():
    """A cleared dropdown is "use the goal's", not "use the empty
    string" -- which would otherwise reach an adapter as a model id."""
    assert choice.validate(None) is None
    assert choice.validate("   ") is None


def test_the_default_is_a_model_id_and_not_a_provider():
    """THE BUG THIS FEATURE HAD TO FIX FIRST. `service_tick` defaulted
    to "gemini" -- a provider -- and `quick` to `gemini-3.8-flash`, a
    model. One variable, two meanings, and `get_adapter` RAISES on the
    second."""
    from kernel.harness.models.registry import SUPPORTED

    assert choice.default_model() in SUPPORTED, (
        f"{choice.default_model()!r} is not a model id; if it is a "
        f"provider name the old conflation is back")


# ── the resolution order ───────────────────────────────────────────────

def test_the_story_wins_over_the_goal_and_the_goal_over_the_default():
    """story -> goal -> deployment default, the same order
    `base_image.for_goal` established."""
    assert choice._first("s", "g") == "s"
    assert choice._first(None, "g") == "g"
    assert choice._first(None, None) == choice.default_model()
    assert choice._first("", "  ", None) == choice.default_model(), (
        "a blank is treated as a choice rather than as inherit")


# ── the freeze ─────────────────────────────────────────────────────────

def test_the_freeze_cannot_overwrite_a_running_story():
    """"Once dispatched for a story it cannot be changed." The guard is
    in the SQL -- a second dispatch pass must not rewrite the model a
    leg is already using, or a story's journal is half one model's
    reasoning and half another's."""
    import inspect

    src = inspect.getsource(choice.freeze)
    assert "executor_model IS NULL" in src, (
        "freeze can overwrite a model already in use")


def test_dispatch_freezes_and_never_fails_the_run_over_it():
    """A settings lookup must not become an outage. A run that cannot
    resolve a model uses the deployment default -- what it would have
    used before this existed."""
    import inspect

    from kernel.conductor import sandbox_spec

    src = inspect.getsource(sandbox_spec)
    assert "choice.freeze" in src
    i = src.index("choice.freeze")
    assert "except Exception" in src[i:i + 900], (
        "a failed model lookup can stop a dispatch")


# ── executor and verifier are separable ────────────────────────────────

def test_the_verifier_is_goal_level_and_the_executor_is_not_only():
    """Which model JUDGES is a standard the goal sets -- a story that
    could pick its own judge could pick a lenient one. The executor is
    the work, and work varies story by story."""
    import inspect

    assert "goal_id" in inspect.signature(choice.for_verifier).parameters
    params = inspect.signature(choice.for_story).parameters
    assert "story_id" in params and "goal_id" in params


def test_the_assessors_adapter_follows_its_model():
    """It was `GeminiAdapter(ASSIST_MODEL)`. An Anthropic judge would
    have reached a Gemini SDK with a Claude id."""
    import inspect

    from kernel.verifier import assess

    src = inspect.getsource(assess)
    assert "adapter_for_model" in src
    # COMMENT LINES STRIPPED. The note above the fix EXPLAINS what the
    # line used to be, and an absence assertion will happily fire on the
    # sentence saying why it is absent -- which is how
    # test_the_capture_job_can_reach_its_identity learned the same
    # lesson one wave earlier.
    code = chr(10).join(line for line in src.splitlines()
                        if not line.lstrip().startswith("#"))
    assert "GeminiAdapter(ASSIST_MODEL)" not in code, (
        "the assessor is hardcoded to one vendor again")


def test_neither_leg_caller_hands_a_model_id_to_get_adapter():
    """`get_adapter` takes a PROVIDER and raises on a model id. Both
    callers used to hand it `AGENTOS_ASSIST_MODEL`, which means a model
    id everywhere else."""
    import pathlib

    for rel in ("kernel/worker/service_tick.py", "kernel/worker/pod_main.py"):
        src = pathlib.Path(rel).read_text(encoding="utf-8")
        assert "adapter_for_model" in src, f"{rel} does not resolve by model"
        assert "get_adapter(model)" not in src, (
            f"{rel} hands a model id to get_adapter, which raises on one")


# ── the UI, and the registry it must offer ─────────────────────────────

def test_the_goal_menu_comes_from_the_KERNELS_registry():
    """TWO REGISTRIES, and picking the wrong one is the bug this guards.

    The GO runs on the GATEWAY and its dropdown reads the `models` TABLE
    (`app/services/model_registry/`). The executor and verifier run in
    the KERNEL, whose adapters serve `registry.SUPPORTED` — a different,
    smaller, hardcoded set. Offering the gateway's list here would let
    somebody choose a model `choice.validate` then refuses: a form that
    rejects its own options.
    """
    from kernel.harness.models.registry import SUPPORTED

    from app.routers.goals import _kernel_model_menu

    offered = {m for g in _kernel_model_menu() for m in g["models"]}
    # A SUBSET, not equal. The menu is curated (Gemini 3.8 only) while
    # `validate` accepts anything the adapters serve, so a goal
    # configured before the curation keeps working -- a narrowing menu
    # must not turn an existing setting into an error nobody can clear.
    # WIDER would be the bug: a form offering what it then refuses.
    assert offered <= set(SUPPORTED)
    assert offered, "the menu is empty"
    for m in offered:
        assert choice.validate(m) == m


def test_the_menu_is_grouped_by_vendor_but_stores_only_a_model():
    """Grouping is presentation. Nothing stores a provider — one field,
    and `provider_of` derives the seam."""
    from app.routers.goals import _kernel_model_menu

    groups = _kernel_model_menu()
    assert {g["provider"] for g in groups} == {"anthropic", "gemini"}
    for g in groups:
        assert g["models"] == sorted(g["models"])


def test_the_story_override_is_locked_once_the_story_is_picked_up():
    """"Once dispatched for a story it cannot be changed." The value is
    frozen onto the execution at dispatch, so a later edit would be a
    setting that silently does nothing — worse than a refusal, because
    the form would look like it worked."""
    import inspect

    from app.routers.stories import story_executor_model

    src = inspect.getsource(story_executor_model)
    assert '("proposed", "accepted")' in src, (
        "the story's model can be changed after pickup, where it has no "
        "effect")


def test_the_story_cannot_choose_its_own_judge():
    """The verifier is goal-level only. `assess.py`: a model that can
    pass work makes verification unreproducible — a story that could
    pick its own judge could pick a lenient one."""
    import inspect

    from app.routers.stories import story_executor_model

    assert "verifier_model" not in inspect.getsource(story_executor_model)
