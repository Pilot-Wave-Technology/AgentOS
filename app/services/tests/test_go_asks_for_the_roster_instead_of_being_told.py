"""The per-turn block got smaller, and a tool took over.

WHAT WAS WRONG. `build_go_context` rendered the objective, the
description and a full team roster, and `_compose_go_turn` prepended the
result to every user message. Three consequences, and the third decided
it:

  SIZE. ~1,840 tokens on a twenty-person goal with a real description.
  The one-person goals it was written against hid that completely.

  IT IS TRANSCRIPT, NOT CONTEXT. Prepended to the USER MESSAGE, so each
  copy is written into the session permanently -- ~74,000 tokens across
  forty turns, for facts unchanged since the first.

  IT WAS ALREADY ASKABLE. `goal_state` returns the objective AND the
  description (kernel/go/state.py:137). Only the roster had no tool,
  which is the whole reason it travelled on every message.

These assert the shape of the replacement, because the failure mode is
silent: a block that quietly grows back costs tokens nobody attributes
to it.
"""

from __future__ import annotations

import inspect
import io
import json

import pytest

from app.services.tests.shipped_tree import IS_DISTRIBUTION
from kernel.go.members import goal_members


def _context_src() -> str:
    from app.services import go_agent
    return inspect.getsource(go_agent.build_go_context)


def test_the_block_no_longer_carries_the_roster():
    src = _context_src()
    assert "_build_team_roster" not in src, (
        "the roster is a tool now; rendering it per turn puts a copy in "
        "the transcript for every message")


def test_the_block_no_longer_carries_the_description():
    """`goal_state` returns it. Sending it per turn is duplication of a
    tool the GO already holds, and it is the single largest field."""
    src = _context_src()
    assert "desc_line" not in src
    assert "**Description:**" not in src


def test_the_block_still_carries_what_a_tool_call_needs():
    """The id is the argument to nearly every GO tool, and the name
    saves a round trip to learn which goal this is."""
    src = _context_src()
    assert "**Id:**" in src
    assert "**Name:**" in src


def test_the_block_says_which_tool_answers_what():
    """Removing a fact GO used to be handed only works if it fetches it.
    Silence would leave the model to notice the absence by itself."""
    src = _context_src()
    assert "goal_state" in src
    assert "goal_members" in src


def test_the_prompt_forbids_naming_an_unread_member():
    """The one rule replacing a fact that used to be given. Without it
    the model will confidently name a colleague who is not on the goal,
    and `decide_plan` will refuse the uid anyway."""
    doc = io.open("kernel/go/AGENTS.md", encoding="utf-8").read()
    body = " ".join(doc.split())
    assert "Never name a person you have not read from `goal_members`" in body


def test_the_tool_is_on_the_go_surface():
    """`GO_ROUTES` is asserted equal to `GO_TOOLS` at startup, so a tool
    added to one and not the other fails the boot rather than going
    quietly missing."""
    from kernel.assistant.api import GO_READS
    from kernel.go import TOOLS as GO_TOOLS

    assert "goal_members" in GO_TOOLS
    assert "goal_members" in GO_READS, "a read, and it must be routed"


def _seeded_tools(template: str) -> list:
    """tools_allow as the distribution's seed writes it for `template`."""
    meta = io.open(f"seed/templates/{template}/meta.json", encoding="utf-8").read()
    return json.loads(meta)["tools_allow"]


def test_the_migration_grants_it_to_go_v2_only():
    if IS_DISTRIBUTION:
        # The seed is the distribution's only source of templates.tools_allow.
        assert "goal_members" in _seeded_tools("go-v2")
        # The legacy `go` template is retired and no longer seeded at all,
        # which grants it nothing; if it is ever seeded again it must not.
        import os
        if os.path.exists("seed/templates/go/meta.json"):
            assert "goal_members" not in _seeded_tools("go")
        return
    sql = io.open("migrations/kernel_go_goal_members.sql",
                  encoding="utf-8").read()
    assert "goal_members" in sql
    assert "id = 'go-v2'" in sql


def test_it_is_in_the_sequence():
    if IS_DISTRIBUTION:
        # Applied means seeded: 0002 is in SEQUENCE and grants it.
        seq = io.open("migrations/SEQUENCE", encoding="utf-8").read().split()
        assert "0002_baseline_seed" in seq
        assert "goal_members" in _seeded_tools("go-v2")
        return
    seq = io.open("migrations/SEQUENCE", encoding="utf-8").read()
    assert "kernel_go_goal_members" in seq


@pytest.mark.asyncio
async def test_the_tool_returns_roles_and_lifts_out_admins():
    """`decide_plan` refuses a uid that is not a goal admin, so "who can
    approve this" is the question the GO actually has. Leaving it to be
    derived from a role field is how it ends up guessing."""
    class Conn:
        async def fetch(self, *_a):
            class R(dict):
                def __getitem__(self, k):
                    return dict.get(self, k)
            return [
                R(uid="u1", role="admin", added_at=None,
                  display_name="Ada", email="a@b.c", assistant_id="asst-1"),
                R(uid="u2", role="member", added_at=None,
                  display_name="", email="b@b.c", assistant_id=None),
            ]

    got = await goal_members(Conn(), "G1")
    assert got["count"] == 2
    assert got["admins"] == ["u1"], "admins must be lifted out, not derived"
    assert got["members"][1]["name"] == "b@b.c", "fall back to email"
    assert got["members"][0]["assistant"] == "asst-1"


@pytest.mark.asyncio
async def test_an_empty_goal_says_so_rather_than_implying_an_owner():
    class Conn:
        async def fetch(self, *_a):
            return []

    got = await goal_members(Conn(), "G1")
    assert got["count"] == 0
    assert "no members" in got["detail"].lower()


def test_the_tool_does_not_return_tasks():
    """Tasks were ruled out of GO's per-turn context and came back in
    through the roster's side door as a per-member subquery -- which is
    what made the whole block change on every task transition. Stories
    with their assignees are `goal_state`'s job."""
    # THE QUERY, not the file. The module docstring says "tasks" several
    # times explaining why there are none, so a substring search over the
    # source tests the explanation rather than the code -- which is how
    # a guard comes to forbid the very thing it documents.
    from kernel.go import members

    assert "tasks" not in members._SQL.lower(), (
        "no task subquery here -- a per-member open-task count is what "
        "made the old roster change on every task transition, and so "
        "impossible to cache or send only when it changed")
