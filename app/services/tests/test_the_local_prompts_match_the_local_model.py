"""The local lane has its OWN profiles, and its prompts teach the real model.

TWO DEFECTS, ONE LAYER. Phase 3 removed the task DAG from the endpoints, the
MCP surface and the done gate -- and left it in the only files the agent
actually reads every turn. `local_prompts/claude.md` and `assistant.md` were
last touched before that refactor and still specified `## Task DAG` YAML, the
six-phase registry, `claim_next_task` and the `PHASE_TASK_DONE` sentinel: a
format the parser no longer accepts and a tool that no longer exists.

Underneath it, the tool surface lied. `_BRIDGE` claimed the bridge served
`goal_knowledge`, `propose_completion`, `emit_artifact` and `artifact_read`.
It serves none of them -- the extension exposes `get_state`, `register_plan`,
`record_branch`, `record_checkpoint` and `append_ledger`. A story with a
resolved profile therefore got an allowlist of five names, four imaginary,
with the four real ones dropped because a served profile replaces the
client's fallback list wholesale.

These pin both, plus the separation the two together argue for: a local run
resolves `profiles-local/`, not the pod tree.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
PROMPTS = ROOT / "app" / "services" / "local_prompts"

# Vocabulary that belongs to the removed dispatch model. A local prompt
# mentioning any of it is teaching a machine that is not there.
DEAD = (
    "Task DAG", "task_dag", "claim_next_task", "report_phase_status",
    "PHASE_TASK_DONE", "PHASE_TASK_FAILED", "PHASE_TASK_BLOCKED",
    "code-explorer", "integration-tester", "unit-tester", "code-reviewer",
    "depends_on",
)


@pytest.mark.parametrize("name", ["claude.md", "assistant.md"])
def test_no_local_prompt_teaches_the_task_dag(name):
    body = (PROMPTS / name).read_text(encoding="utf-8")
    found = [d for d in DEAD if d in body]
    assert not found, (
        f"{name} still teaches the removed dispatch model: {found}. The agent "
        "reads this every turn, so a stale instruction here outlives every "
        "endpoint we deleted."
    )


@pytest.mark.parametrize("name", ["claude.md", "assistant.md"])
def test_every_local_prompt_states_the_profile(name):
    body = (PROMPTS / name).read_text(encoding="utf-8")
    assert "[[PROFILE_KEY]]" in body, (
        f"{name} does not say which executor profile the session is running "
        "under, so story/coding and errand/ops read identically"
    )


def test_claude_md_teaches_steps_and_checks():
    body = (PROMPTS / "claude.md").read_text(encoding="utf-8")
    for token in ("register_plan", "`goal`", "`guard`", "cd repos/"):
        assert token in body, f"claude.md no longer explains {token}"


def test_the_bridge_list_is_exactly_what_the_extension_serves():
    # The extension's MCP server (src/mcp/tools.ts) exposes these five. The
    # control plane decides the allowlist, so the two must agree -- and when
    # they did not, four of the five granted names did not exist.
    from app.services.local_tool_surface import ALWAYS_SERVED, _BRIDGE

    served = set(ALWAYS_SERVED) | set(_BRIDGE.values())
    assert served == {
        "mcp__agentos__get_state",
        "mcp__agentos__register_plan",
        # Added 2026-10-01 with `artifact_manifest` taking a story: an errand's
        # deliverable IS an artifact, and a coding story's outputs are not all
        # the tree. Both must exist in src/mcp/tools.ts or the allowlist names
        # tools that do not exist -- the defect this test was written for.
        "mcp__agentos__emit_artifact",
        "mcp__agentos__artifact_list",
        # The reviewer's only verb with an effect. Added with the local story
        # review; it must exist in src/mcp/tools.ts or the allowlist names a
        # tool that is not there -- the defect this test exists for.
        "mcp__agentos__submit_story_review",
        "mcp__agentos__record_branch",
        "mcp__agentos__record_checkpoint",
        "mcp__agentos__append_ledger",
        # Working a catch-up, added when the catch-up stopped being a
        # thing the extension did behind the agent and became a plan the
        # agent works. The plan text -- shared with the cloud lane --
        # has always named these three; until they existed locally the
        # agent read its own plan, found nothing to call, and waited.
        "mcp__agentos__sync_trunk",
        "mcp__agentos__run_check",
        "mcp__agentos__story_commit",
    }, (
        "the control plane grants MCP tools the extension does not implement, "
        "or withholds ones it does: " + ", ".join(sorted(served))
    )


def test_a_local_coding_profile_grants_only_tools_that_exist():
    from app.services.local_tool_surface import bridge_tools_for
    from kernel.profiles.local_registry import resolve_local

    prof = resolve_local("story", "coding")
    granted = bridge_tools_for(prof.tools)
    assert "mcp__agentos__register_plan" in granted
    assert "mcp__agentos__get_state" in granted, (
        "get_state is how every turn learns the story's state; a profile-derived "
        "list that omits it leaves the session blind"
    )


def test_the_local_tree_is_not_the_pod_tree():
    # The whole point of the separation: the same key, a different profile.
    from kernel.profiles.local_registry import resolve_local
    from kernel.profiles.registry import resolve

    local = resolve_local("story", "coding")
    pod = resolve("story", "coding")
    assert local.prompt_preamble != pod.prompt_preamble, (
        "the local profile is serving the pod's role prose, which describes "
        "legs, a step budget and tools that do not exist in an editor"
    )
    assert "story_commit" not in local.tools
    assert "bash_wait" not in local.tools
    assert "propose_completion" not in local.tools, (
        "completion is the person submitting the diff and marking it done"
    )


def test_every_local_profile_is_actually_runnable_locally():
    # A local profile that the local tool surface refuses is a contradiction:
    # nothing else would ever resolve it.
    from app.services.local_tool_surface import bridge_tools_for, refuse_driver
    from kernel.profiles.local_registry import all_local_profiles

    profiles = all_local_profiles()
    assert profiles, "profiles-local/ is empty, so no story can be imported"
    for p in profiles:
        key = f"{p.kind}/{p.domain}"
        assert refuse_driver(p.workspace_driver) is None, (
            f"{key} declares a workspace driver the local lane refuses"
        )
        bridge_tools_for(p.tools)  # raises if a tool has no local home
