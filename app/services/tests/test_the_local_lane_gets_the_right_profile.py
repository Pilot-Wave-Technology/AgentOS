"""A local run and a pod run of one story must resolve the SAME profile.

TWO THINGS COULD MAKE THEM DIFFER, and only one is obvious.

The obvious one: a local run creates no `kernel.executions` row, so it never
reached the resolver at all -- the extension shipped a hardcoded tool surface
with no relationship to the profile a pod would have used.

The other: **there is no `stories.domain` column.** The domain is DERIVED,
and it was derived in exactly one place, inside the kernel's
execution-creation path. A second derivation for the local lane would let the
two disagree -- and nobody would look, because both runs work. They would
simply disagree about what the agent may do.

AND WHAT A PROFILE DECIDES LOCALLY IS NARROWER THAN IN A POD. In a pod the
tool surface IS the guarantee. Locally the person is self-operating in their
own editor with a terminal in the same directory, so the agent's Read/Bash/
Grep are its own and a profile has no business narrowing them. What a profile
still decides is which CONTROL-PLANE verbs the bridge must serve.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── the derivation ───────────────────────────────────────────────────

@pytest.mark.parametrize("work_kind,expected", [
    (None, ("story", "coding")),      # the column post-dates the rows
    ("", ("story", "coding")),
    ("story", ("story", "coding")),
    ("errand", ("errand", "ops")),    # errand/ops is the only errand profile
])
def test_the_derivation(work_kind, expected):
    from kernel.profiles.story_keys import derive_kind_domain
    assert derive_kind_domain(work_kind) == expected


def test_null_work_kind_is_a_story_not_an_errand():
    """`!= 'story'` would call every row written before the column existed
    an errand, and errands take a different profile and a different
    completion path."""
    from kernel.profiles.story_keys import derive_kind_domain
    assert derive_kind_domain(None)[0] == "story"


def test_an_explicit_domain_is_not_overridden():
    from kernel.profiles.story_keys import derive_kind_domain
    assert derive_kind_domain("story", "business") == ("story", "business")


def test_there_is_one_derivation_and_both_lanes_use_it():
    assert "derive_kind_domain" in src("kernel/assistant/write.py"), (
        "the kernel derives the domain its own way again")
    assert "derive_kind_domain" in src("app/services/story_profile.py")
    assert 'domain = "ops" if kind == "errand" else "coding"' not in \
        src("kernel/assistant/write.py"), "the old copy is still there"


def test_the_derivation_lives_in_the_kernel():
    """`app/` imports `kernel/` freely; the reverse is a single deliberate
    exception. Putting the shared rule in `app/` would have made it two."""
    assert (ROOT / "kernel/profiles/story_keys.py").exists()
    kernel_importing_app = sum(
        p.read_text(encoding="utf-8").count("from app.")
        for p in (ROOT / "kernel").rglob("*.py"))
    assert kernel_importing_app <= 1, (
        "the kernel now imports app/ in more than one place")


# ── what a profile decides locally ───────────────────────────────────

def test_the_agents_own_tools_are_not_a_profile_decision():
    """Narrowing Read/Bash/Grep would be theatre the agent's own Bash walks
    through, and the only thing it would reliably achieve is breaking work.

    2026-10-01: this asserted the result was EMPTY. It is now the bridge's own
    plumbing and nothing else -- `get_state` and the three recorders, which no
    profile grants because in a pod the harness does them itself. A
    profile-derived list alone left every local session without them, while
    granting four `mcp__agentos__*` names the extension has never implemented.
    The claim under test is unchanged: no EDITOR tool appears either way."""
    from app.services.local_tool_surface import ALWAYS_SERVED, bridge_tools_for

    got = bridge_tools_for(["read", "bash", "grep", "write", "edit", "glob"])
    assert got == sorted(ALWAYS_SERVED), (
        "editor tools must be neither granted nor withheld here")


def test_only_control_plane_verbs_come_back():
    from app.services.local_tool_surface import ALWAYS_SERVED, bridge_tools_for

    # `goal_knowledge` is SUBSTITUTED: memory is seeded at checkout from the
    # same shared store, so what the goal settled is already in the workspace.
    got = bridge_tools_for(["read", "bash", "goal_knowledge"])
    assert got == sorted(ALWAYS_SERVED)

    # `emit_artifact` is NOT: it was substituted on the grounds that a local
    # story's deliverable is its diff, which is a story argument that was also
    # applied to errands -- where the artifact IS the deliverable.
    with_art = bridge_tools_for(["read", "bash", "emit_artifact"])
    assert "mcp__agentos__emit_artifact" in with_art

    with_plan = bridge_tools_for(["read", "bash", "register_plan"])
    assert "mcp__agentos__register_plan" in with_plan
    assert set(with_plan) - {"mcp__agentos__register_plan"} == set(ALWAYS_SERVED)


def test_asking_is_left_to_the_editor():
    """`ask_human` parks a pod's run and waits for a card because the person
    is not there. Locally the IDE already pauses and asks, and the answer
    arrives by them typing it -- routing that through the control plane would
    replace a conversation with a ticket."""
    from app.services.local_tool_surface import ALWAYS_SERVED, bridge_tools_for

    # Neither adds a bridge tool: what comes back is the plumbing every local
    # run gets regardless of profile, and nothing for asking.
    assert bridge_tools_for(["ask_human", "reply_to_human"]) == sorted(ALWAYS_SERVED)


def test_an_unrecognised_tool_refuses_rather_than_being_dropped():
    """A new kernel tool must not be silently assumed to be the agent's own.
    Somebody decides which side it belongs on."""
    from app.services.local_tool_surface import (ProfileNotRunnableLocally,
                                                 bridge_tools_for)
    with pytest.raises(ProfileNotRunnableLocally) as e:
        bridge_tools_for(["read", "a_verb_nobody_mapped"])
    assert "a_verb_nobody_mapped" in str(e.value)


def test_copyin_is_refused_rather_than_degraded_to_git():
    """It hands the sandbox a copy with no remote. Degrading it would give a
    business story a working tree it is not supposed to have and a branch
    nobody asked for."""
    from app.services.local_tool_surface import refuse_driver
    assert refuse_driver("copyin")
    assert refuse_driver("git") is None


# ── every shipped profile has a decided answer ───────────────────────

def test_no_shipped_profile_passes_silently():
    """Each one either maps, or refuses with a reason. A profile that fell
    through to an empty surface would look like it worked."""
    from app.services.local_tool_surface import (ProfileNotRunnableLocally,
                                                 bridge_tools_for,
                                                 refuse_driver)
    from kernel.profiles.loader import load_all

    verdicts = {}
    for key, prof in load_all().items():
        if refuse_driver(prof.workspace_driver):
            verdicts[key] = "refused: driver"
            continue
        try:
            verdicts[key] = f"ok: {len(bridge_tools_for(prof.tools))} bridge tools"
        except ProfileNotRunnableLocally:
            verdicts[key] = "refused: tools"

    assert verdicts[("story", "coding", "")].startswith("ok:")
    assert verdicts[("errand", "ops", "")].startswith("ok:")
    assert verdicts[("story", "business", "")] == "refused: driver"
    # 2026-10-01: this asserted "refused: tools", because
    # `submit_story_review` was in `_NOT_LOCAL` on the grounds that a local
    # story is review-exempt. It is not any more -- a local coding story is
    # reviewed in the session before it reaches `reviewed`, by the same rules
    # and into the same table as a pod's. The claim under test is unchanged:
    # every shipped profile gets a VERDICT rather than passing silently.
    assert verdicts[("story", "coding", "review")].startswith("ok:"), (
        "the review profile no longer resolves, so a local story cannot be "
        "reviewed at all")
