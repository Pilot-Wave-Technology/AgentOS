"""A story assigned to you can be started without a trip to the board.

THE OLD RULE AND WHY IT WENT. `_import_readiness` refused anything but
`accepted` -- "Accept this story on the board before importing it
locally" -- on the stated policy that there be no local auto-accept of
a proposed story. In practice that is a round trip to the web board to
press one button, for a story the person asking is already assigned.

And the rest of the system did not hold that line anyway: Slack's
`/work` accepts and imports in one step, and
`provision_assistant_thread_for_story` has always moved
`proposed/accepted -> in_progress` with its UPDATE guarded on BOTH
states. The machinery was there; only this one gate stood in front of
it, and only for the extension.

WHAT ACTUALLY PROTECTED THE STORY IS UNCHANGED. Only the assignee may
check a story out, and `local_checkout` enforces that BEFORE readiness
is consulted -- so `accepted` was never the thing stopping somebody
taking work that was not theirs. That check is pinned here too,
because this change leans on it.
"""
from __future__ import annotations

import inspect

import pytest

from app.services.tests.local_http import local_http


async def _story(h, state: str, *, mode: str = "operator",
                 assigned: str = "u1"):
    """`execution_mode` has a CHECK constraint, so "" is not a value —
    `operator` is the not-yet-local default these stories carry."""
    goal, story = await h.seed_story(state=state)
    await h.conn.execute(
        "UPDATE stories SET execution_mode = $2, assigned_to = $3 "
        " WHERE id = $1", story, mode, assigned)
    return goal, story


# ── the gate ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_proposed_story_is_importable():
    from app.services.local_checkout import _import_readiness

    async with local_http() as h:
        goal, story = await _story(h, "proposed")
        assert await _import_readiness(h.conn, story, goal) is None, (
            "a proposed story assigned to the caller is still refused; "
            "they have to go to the board to press Accept first")


@pytest.mark.asyncio
async def test_an_accepted_story_is_still_importable():
    """The change adds a state; it must not have replaced one."""
    from app.services.local_checkout import _import_readiness

    async with local_http() as h:
        goal, story = await _story(h, "accepted")
        assert await _import_readiness(h.conn, story, goal) is None


@pytest.mark.asyncio
async def test_a_proposed_story_with_unmet_deps_is_still_blocked():
    """Accepting early must not skip the dependency gate — that one is
    about whether the work CAN start, not about permission."""
    from app.services.local_checkout import _import_readiness

    async with local_http() as h:
        # The goal must be the DEPENDENT story's own: `_import_readiness`
        # is keyed on (story, goal), and each seed makes a fresh goal.
        _, dep = await _story(h, "in_progress")
        goal, story = await _story(h, "proposed")
        await h.conn.execute(
            "UPDATE stories SET depends_on = ARRAY[$2]::text[] WHERE id = $1",
            story, dep)
        out = await _import_readiness(h.conn, story, goal)
        assert out is not None and out[0] == "blocked_by_deps"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["done", "reviewed", "cancelled",
                                   "rejected"])
async def test_a_finished_story_is_still_refused(state):
    from app.services.local_checkout import _import_readiness

    async with local_http() as h:
        goal, story = await _story(h, state)
        out = await _import_readiness(h.conn, story, goal)
        assert out is not None and out[0] == "not_importable", state


# ── the listing ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_proposed_story_shows_in_the_extension_list():
    """The list is the extension's entry point. A story it cannot see
    is a story that cannot be imported, whatever the gate allows."""
    from app.services.local_hydrate import list_local_stories

    async with local_http() as h:
        _, story = await _story(h, "proposed", assigned="u1")
        ids = [s["id"] for s in await list_local_stories("u1")]
        assert story in ids


@pytest.mark.asyncio
async def test_a_proposed_story_is_marked_ready():
    """`ready` is what decides whether the sidebar offers a button.
    Listed but never ready would be worse than not listed — it looks
    broken rather than absent."""
    from app.services.local_hydrate import list_local_stories

    async with local_http() as h:
        _, story = await _story(h, "proposed", assigned="u1")
        row = [s for s in await list_local_stories("u1") if s["id"] == story]
        assert row and row[0]["ready"] is True


@pytest.mark.asyncio
async def test_somebody_elses_proposed_story_is_not_listed():
    """The listing is keyed on `assigned_to`, and that is the whole of
    the permission story here."""
    from app.services.local_hydrate import list_local_stories

    async with local_http() as h:
        _, story = await _story(h, "proposed", assigned="someone-else")
        ids = [s["id"] for s in await list_local_stories("u1")]
        assert story not in ids


# ── what this change leans on ────────────────────────────────────────

def test_only_the_assignee_can_check_out():
    """`accepted` was never what stopped somebody taking work that was
    not theirs — this is. Allowing `proposed` leans on it entirely, so
    it is pinned here rather than only where it lives."""
    from app.services import local_checkout
    src = inspect.getsource(local_checkout)
    i = src.index("not_assignee")
    window = src[max(0, i - 600):i]
    assert "SELECT assigned_to FROM stories" in window
    # And before the readiness gate, or a not-yet-accepted story could
    # be imported by anyone holding a token.
    assert i < src.index("_import_readiness(pool,")


def test_the_provisioner_moves_proposed_as_well_as_accepted():
    """If its UPDATE were guarded on `accepted` alone, a proposed story
    would pass the gate, report a successful checkout, and sit in
    `proposed` for ever with a branch cut against it."""
    from app.routers import assistant
    src = inspect.getsource(assistant.provision_assistant_thread_for_story)
    assert "state IN ('proposed', 'accepted')" in src


def test_the_sidebar_and_the_gate_name_the_same_states():
    """Two copies of "can this be imported", and they disagreeing is
    how a story gets a button that always refuses."""
    from app.services import local_checkout, local_hydrate
    gate = inspect.getsource(local_checkout._import_readiness)
    listing = inspect.getsource(local_hydrate.list_local_stories)
    for state in ("accepted", "proposed"):
        assert state in gate, state
        assert state in listing, state
