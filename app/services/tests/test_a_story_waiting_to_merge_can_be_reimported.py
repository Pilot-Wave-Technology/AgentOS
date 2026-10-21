"""A local story waiting on its merge can be imported again.

IT COULD NOT, AND THE REFUSAL WAS NONSENSE. `_import_readiness` lets a
person resume their own local story from `in_progress` or `blocked`,
and anything else must be `accepted`. That list was written before
`awaiting_merge` existed and never revisited -- so a story whose work
is pushed and whose pull request is open was refused with "Accept this
story on the board before importing it locally", about a story nobody
can accept because it is long past `accepted`. Seen on 66317ffc,
2026-10-14.

IT IS NOT AN EDGE CASE. Now the lane waits for the merge, that is the
state a local story spends most of its life in, and it is exactly when
a person needs the checkout back: to mark it done, to ask for more
work, or to take the trunk in when it moves. Losing the window meant
losing the story.

THE SIDEBAR'S OWN ANSWER HAS TO MATCH. `_ready_rows` computes the same
"is this a resume" in its own copy; left behind, the story is listed
as not ready and there is no button to press.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


async def _local(h, state: str, mode: str = "local"):
    goal, story = await h.seed_story(state=state)
    await h.conn.execute(
        "UPDATE stories SET execution_mode = $2 WHERE id = $1", story, mode)
    return goal, story


@pytest.mark.parametrize("state", ["in_progress", "blocked", "awaiting_merge"])
@pytest.mark.asyncio
async def test_my_own_local_story_is_resumable(state):
    from app.services.local_checkout import _import_readiness

    async with local_http() as h:
        goal, story = await _local(h, state)
        assert await _import_readiness(h.conn, story, goal) is None


@pytest.mark.asyncio
async def test_a_NON_local_story_waiting_to_merge_is_still_refused():
    """The exemption is for resuming YOUR OWN local checkout. A cloud
    story at `awaiting_merge` is being landed by the platform and must
    not be importable into somebody's editor halfway through."""
    from app.services.local_checkout import _import_readiness

    async with local_http() as h:
        goal, story = await _local(h, "awaiting_merge", mode="operator")
        out = await _import_readiness(h.conn, story, goal)
        assert out is not None and out[0] == "not_importable"


@pytest.mark.asyncio
async def test_a_finished_story_is_still_refused():
    from app.services.local_checkout import _import_readiness

    async with local_http() as h:
        goal, story = await _local(h, "done")
        assert (await _import_readiness(h.conn, story, goal))[0] == "not_importable"


def test_the_sidebar_agrees_with_the_gate():
    """Two copies of "is this a resume", and the sidebar's is what decides
    whether a button exists at all. They must name the same states."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    gate = (root / "local_checkout.py").read_text(encoding="utf-8")
    side = (root / "local_hydrate.py").read_text(encoding="utf-8")

    def states(text: str) -> set:
        m = re.search(r'state"?\]? in \(\s*((?:"[a-z_]+",?\s*)+)\)', text)
        return set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()

    assert states(gate) == states(side) == {
        "in_progress", "blocked", "awaiting_merge"}, (
        "the import gate and the sidebar's readiness disagree about which "
        "states can be resumed — one of them will offer a button the other "
        "refuses, or hide one it would allow")
