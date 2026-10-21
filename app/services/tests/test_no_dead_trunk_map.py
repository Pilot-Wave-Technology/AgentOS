"""The trunk map GO was told to read is gone, and so are its pointers.

`go_agent._build_trunk_branch_map` rendered a "Working trunks (per
repo)" section for GO's per-turn context. `build_go_context` was then
slimmed to goal identity plus the team roster, and its docstring lists
"Trunk-branch map (in RESOURCES.md)" under WHAT WAS REMOVED. The
function stayed. Nothing called it.

THAT IS NOT MERELY UNTIDY, and this is the part that made it a defect
rather than a cleanup:

  the seeded GO skill still says "see the 'Working trunks (per repo)'
  section of your current context", and the sentence the reader lands
  on after failing to find it is "Only fall back to `main`"

  `resolve_repo_trunk`'s docstring justified its own precedence by
  being kept "in step" with this function "so the engine and the agent
  never disagree on the trunk" -- a promise about code that had not run
  in months, cited in two more docstrings after it

So the tree contained a dead resolver, a skill pointing at its output,
and three comments describing it as authoritative. Deleted, along with
`_build_task_snapshot` and `_build_resource_snapshot` beside it: both
equally uncalled, both answering questions that are now tools
(`goal_state`, `list_resources`).

The RESOURCES.md the docstring redirects to did not show lanes either --
see `test_resources_md_shows_the_lanes`. That half is fixed; this half
is the pointer that survived it.
"""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DEAD = ("_build_trunk_branch_map", "_build_task_snapshot",
         "_build_resource_snapshot")


def _sources():
    for base in ("app", "kernel"):
        for p in (_ROOT / base).rglob("*.py"):
            if "__pycache__" in p.parts or p.name == pathlib.Path(__file__).name:
                continue
            yield p


def test_the_dead_helpers_are_gone():
    """No definition and no call. Prose that records the deletion is
    fine -- two docstrings say the function is gone, and saying so is
    the point; what must not come back is code."""
    hits = []
    for p in _sources():
        text = p.read_text(encoding="utf-8")
        for name in _DEAD:
            if f"def {name}(" in text or f"{name}(" in text.replace(
                    f"def {name}(", ""):
                hits.append((p.relative_to(_ROOT).as_posix(), name))
    assert not hits, (
        f"a deleted function is defined or called again: {hits}")


def test_and_nothing_still_calls_one_authoritative():
    """The docstrings that cited it as the resolver kept 'in step' with
    the engine, so the engine and the agent 'never disagree on the
    trunk'. Three of them, about code that had not run in months."""
    claims = [p.relative_to(_ROOT).as_posix() for p in _sources()
              if "_build_trunk_branch_map" in p.read_text(encoding="utf-8")
              and "gone" not in p.read_text(encoding="utf-8")
              and "deleted" not in p.read_text(encoding="utf-8")]
    assert not claims, (
        f"these still describe the dead map as live: {claims}")


def test_the_context_builder_still_says_what_it_dropped():
    from app.services import go_agent

    assert "Trunk-branch map" in go_agent.build_go_context.__doc__


def test_the_skill_no_longer_points_at_the_section():
    """The migration is a text replacement, so it fixes whichever
    revision of the skill a given database landed on."""
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, SEED, seed_text

    if IS_DISTRIBUTION:
        # No migration ships: the seeded skills already carry the
        # replaced text, so none may point at the dead section.
        stale = [p.name for p in (SEED / "skills").glob("*.md")
                 if "Working trunks (per repo)" in p.read_text(encoding="utf-8")]
        assert not stale, f"seeded skills still point at the dead section: {stale}"
        body = seed_text("skills/branch_management.md")
        assert "trunk LANE the story lands on" in body
        assert "code_state" in body
        return
    mig = _ROOT / "migrations" / "go_skill_trunk_lanes.sql"
    body = mig.read_text(encoding="utf-8")
    assert "Working trunks (per repo)" in body, "nothing to replace"
    assert "trunk LANE the story lands on" in body
    assert "code_state" in body, (
        "the replacement has to name something that is actually "
        "generated, or it repeats the original fault")


def test_the_migration_is_in_sequence():
    names = [l.strip() for l in
             (_ROOT / "migrations" / "SEQUENCE").read_text(
                 encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert "go_skill_trunk_lanes" in names
