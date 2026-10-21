"""Every tool a profile offers has an effect class — checked on the host.

WHY HERE. The rule already had a check, `test_effects_cover_profiles.py`,
and the check could not run: it hardcoded `/app/profiles` and imported
`fb_harness`, which pulls in the whole harness and therefore `anthropic`.
So it executed only inside one container target, and a rule written to
stop a THIRD omission did not stop the fifth — `goal_knowledge` shipped
unclassified on all three profile surfaces and was denied at chain stage 3
on every call to it.

These tests import `kernel.profiles.effect_coverage`, which imports the
standard library and `effects`, which imports the standard library and the
catalog. Nothing in that chain reaches a provider SDK, so they run wherever
pytest runs. The container check and the pre-push hook call the same
module: one rule, three doors.

THE RULE IS NOW MOSTLY STRUCTURAL. `effects.py` reads its three sets off
the catalog's `effect=` declaration, so a tool cannot be offered to a model
and left unclassified — the two facts are one line. These tests stay
because "cannot be written down" is a property of today's arrangement, and
the arrangement is what they are guarding.
"""
from __future__ import annotations

from kernel.harness.chain import InterceptorContext, ToolCall, run_chain
from kernel.harness.effects import (CONTAINED, IRREVERSIBLE, REVERSIBLE,
                                    classify)
from kernel.profiles.effect_coverage import (classified, surfaces,
                                             unclassified)


def _ctx(**kw):
    base = dict(writable_scope=["/workspace"], allowed_tools=None,
                budget_remaining=1000, execution_id=1, depth=0)
    base.update(kw)
    return InterceptorContext(**base)


def test_every_offered_tool_has_an_effect_class():
    """The join nobody was making: profiles decide what may be called,
    `effects.py` decides what each call costs, and until this existed
    nothing checked the second covered the first."""
    gaps = unclassified()
    assert not gaps, (
        f"unclassified: {gaps}. Unclassified means IRREVERSIBLE, so stage 3 "
        f"denies every call to these — on every profile listed beside them.")


def test_the_checker_actually_found_the_profiles():
    """A coverage check that reads zero profiles passes vacuously. That is
    exactly how the container version stayed green while missing a live
    omission — it was pointed at a path that did not exist here."""
    per = surfaces()
    assert per, "no profiles found — the check would pass vacuously"
    assert len(per) >= 4, f"expected base + 3 profiles, found {sorted(per)}"
    assert len(classified()) >= 20


def test_goal_knowledge_is_reachable():
    """THE FIFTH OMISSION, kept as a regression.

    It reads approved decisions for the goal — strictly less reaching than
    `read`. Unlisted it was irreversible, so stage 3 denied it on
    errand-ops, story-coding and story-business alike.
    """
    assert classify("goal_knowledge", {}) == CONTAINED
    call = ToolCall(tool="goal_knowledge", args={}, target=None)
    assert run_chain(call, _ctx()).proceed, (
        "goal_knowledge is denied at the chain — it is on all three "
        "profile surfaces, so this breaks every executor")


def test_subagent_is_classified_by_a_flavour_that_exists():
    """The classifier used to branch on `flavour == "effect"`, which no
    valid call can produce — the enum is {investigate, build}. So every
    real spawn classified `contained`, including a `build` child that
    keeps the shell, and a test asserted the dead branch worked."""
    assert classify("subagent", {"flavour": "investigate"}) == CONTAINED
    assert classify("subagent", {"flavour": "build"}) == REVERSIBLE
    # Absent or unknown is treated as the wider of the two, not the safer
    # reading — a spawn whose flavour we cannot see may hold a shell.
    assert classify("subagent", {}) == REVERSIBLE


def test_the_classifier_has_not_gone_soft():
    """The opposite failure. Classifying `bash` by name would make
    `rm -rf /` contained."""
    assert classify("bash", {"command": "rm -rf /"}) == IRREVERSIBLE
    assert classify("some_tool_nobody_declared", {}) == IRREVERSIBLE
    destructive = ToolCall(tool="bash", args={"command": "rm -rf /"},
                           target=None)
    assert not run_chain(destructive, _ctx()).proceed


def test_the_effect_sets_are_derived_not_restated():
    """What Ec1f actually changed, asserted so it is not quietly undone.

    Five tools shipped unclassified because the sets were literals in this
    file and the catalog was in another. They are `tools_by_effect(...)`
    now. If somebody types a set literal back in, the sixth omission
    becomes possible again and this is the only thing that would say so.
    """
    import io
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/harness/effects.py", encoding="utf-8").read()
    for name in ("_CONTAINED", "_REVERSIBLE", "_BY_ARGUMENT"):
        m = re.search(rf"^{name}\s*=\s*(.+)$", src, re.M)
        assert m, f"{name} is gone — effect_coverage imports it"
        assert "tools_by_effect" in m.group(1), (
            f"{name} is a hand-kept literal again: {m.group(1)!r}")


def test_declaring_a_tool_wrongly_fails_at_import_not_at_stage_three():
    """The cost of an omission was that it surfaced far away, as an
    approval demand nobody could satisfy. A typo in `effect=` is now an
    error naming the tool, raised while the catalog is being built."""
    import pytest

    from kernel.tools.catalog import _schema

    with pytest.raises(ValueError, match="contaned"):
        _schema("t", "d", {}, [], side="sandbox", effect="contaned")
    with pytest.raises(ValueError, match="sandbx"):
        _schema("t", "d", {}, [], side="sandbx", effect="contained")
    with pytest.raises(TypeError):
        _schema("t", "d", {}, [])          # side is not optional


def test_the_provider_never_sees_our_private_keys():
    """`_side` and `_effect` ride along in the catalog entry, and a key the
    wire protocol has never heard of comes back from the vendor as a 400
    naming a field the author never wrote. `schemas_for` is the seam."""
    from kernel.tools.catalog import CATALOG, schemas_for

    assert "_side" in CATALOG["read"] and "_effect" in CATALOG["read"]
    for schema in schemas_for(["read", "bash", "story_commit"]):
        assert set(schema) == {"name", "description", "parameters"}, schema


def test_no_story_executor_tool_needs_approval_to_run():
    """A guard on the fix, not on the bug.

    Fixing a classification can widen OR narrow what runs freely, and the
    story profiles are the ones in daily use. Nothing on their surfaces
    may demand a human for an ordinary call — if it does, that executor
    stops being able to work.
    """
    from kernel.harness.effects import requires_approval

    per = surfaces()
    for profile in ("story-coding", "story-business", "errand-ops"):
        tools = per.get(profile, set()) | per.get("base", set())
        blocked = sorted(t for t in tools
                         if requires_approval(classify(t, {})))
        assert not blocked, f"{profile}: {blocked} would need approval"
