"""Both creation paths hand GO the catalogue, because its prompt says so.

GO's prompt states outright: "`create_story` answers with the goal's
catalogue and the ids to grant, so there is nothing to look up."

That was true of ONE of the two creation paths. The ACP action carried
the block; the HTTP endpoint the gateway's `create_story` tool actually
posts to returned `{ok, story_id, title}`. On the live path GO created a
story, got no catalogue, and had been told it already had one.

It cannot recover from that. GO holds no tool that lists a goal's
resources -- `list_resources` is the assistant's -- and
`resource_brief`, `read_resource` and `grant_resource` all take an id.
So the ids sat in a table GO could not read.

Observed on KER25: asked to plan against a linked repository, GO
reported the goal was "currently listed with no designated branch
assigned yet". `cal_kernel` was linked. GO was being accurate about what
it could see.
"""
from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def test_both_creation_paths_call_the_same_helper():
    """One fact, one place. The block was duplicated-by-omission: it
    existed once and the second path simply never got it, which is the
    failure mode a shared function removes rather than documents."""
    for rel in ("app/services/go_response.py", "app/routers/stories.py"):
        assert "grant_guidance" in _read(rel), (
            f"{rel} builds a create-story response without the catalogue "
            f"guidance GO's prompt promises")


def test_the_gateway_path_returns_it():
    """The path that matters: `story-tools.ts` posts `create_story` to
    `/internal/goals/{pid}/stories/create`."""
    src = _read("app/routers/stories.py")
    body = src[src.index('@router.post("/internal/goals/{pid}/stories/create")'):]
    body = body[:body.index("\n@router.")]
    assert "grant_guidance" in body
    # And a catalogue failure must not lose the story: it already exists
    # by this point, and the guidance is a convenience on top of it.
    i = body.index("grant_guidance")
    assert "except Exception" in body[i - 400:i + 400], (
        "a failed catalogue lookup would lose a story that was already "
        "created")


def test_the_promise_in_the_prompt_is_still_the_one_being_kept():
    """If the prompt's wording changes, this test should be read again
    rather than silently continuing to guard a sentence nobody says."""
    text = re.sub(r"\s+", " ", _read("kernel/go/AGENTS.md"))
    assert "there is nothing to look up" in text


def test_go_still_has_no_way_to_list_resources_itself():
    """THE REASON THIS MATTERS, asserted so it cannot quietly stop being
    true in either direction. If GO ever gains a catalogue-listing tool,
    the guidance becomes a convenience instead of the only road, and
    this test should be deleted deliberately rather than left passing."""
    from kernel.go import TOOLS

    assert "list_resources" not in TOOLS, (
        "GO gained a way to list resources -- revisit whether the "
        "create-story guidance is still load-bearing")
