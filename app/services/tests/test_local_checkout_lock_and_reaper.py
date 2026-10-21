"""The local checkout lock, and the reaper that frees what nobody is holding.

Lock: who can take it, when a held lock can be taken over, and that heartbeat
and release are fenced by the session that took it. Reaper: what it frees and
cleans for a story in EACH state -- the one that matters most being that a story
waiting on its merge is never freed by a missed heartbeat.
"""
from __future__ import annotations

import pytest

from app.services.tests.local_http import local_http


@pytest.fixture
def no_prep(monkeypatch):
    """Branch preparation runs after the lock is taken; these tests are about the lock."""
    import app.services.local_checkout as lc

    async def _skip(*_a, **_k):
        return None
    monkeypatch.setattr(lc, "_prepare_local_story", _skip)


async def _expire(h, story):
    await h.conn.execute(
        "UPDATE stories SET checkout_expires_at = now() - interval '1 minute' WHERE id = $1", story)


@pytest.mark.asyncio
async def test_a_live_lock_held_by_someone_else_is_refused_and_taken_once_it_expires(no_prep):
    from app.services.local_checkout import claim_checkout
    async with local_http() as h:
        # Reassigned while the previous holder still has it checked out.
        goal, story = await h.seed_story(state="accepted", held_by="previous")
        r = await claim_checkout(story, goal, "person")
        assert (r["ok"], r["reason"], r["held_by"]) == (False, "held_by_other", "previous")
        await _expire(h, story)
        r = await claim_checkout(story, goal, "person")
        assert r["ok"] is True
        assert (await h.story(story))["checked_out_to_uid"] == "person"


@pytest.mark.asyncio
async def test_resuming_my_own_checkout_starts_a_new_session_and_fences_the_old_one(no_prep):
    from app.services.local_checkout import claim_checkout, heartbeat_checkout
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", held_by="person")
        r = await claim_checkout(story, goal, "person")
        assert r["ok"] is True and r["checkout_session_id"] != "sess-1"
        assert (await heartbeat_checkout(story, "sess-1"))["reason"] == "not_holder_or_released"
        assert (await heartbeat_checkout(story, r["checkout_session_id"]))["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["done", "reviewed", "cancelled", "rejected"])
async def test_a_finished_story_cannot_be_checked_out(no_prep, state):
    from app.services.local_checkout import claim_checkout
    async with local_http() as h:
        goal, story = await h.seed_story(state=state, held_by=None)
        assert (await claim_checkout(story, goal, "person"))["ok"] is False


@pytest.mark.asyncio
async def test_heartbeat_and_release_are_fenced_by_the_session():
    from app.services.local_checkout import heartbeat_checkout, release_checkout
    async with local_http() as h:
        goal, story = await h.seed_story(held_by="person")
        assert (await heartbeat_checkout(story, "other"))["ok"] is False
        assert (await release_checkout(story, "other"))["ok"] is False
        assert (await heartbeat_checkout(story, "sess-1"))["ok"] is True
        assert (await release_checkout(story, "sess-1"))["ok"] is True
        assert (await release_checkout(story, "sess-1"))["ok"] is False, "released twice"
        assert (await heartbeat_checkout(story, "sess-1"))["ok"] is False, "no heartbeat after release"
        row = await h.story(story)
        assert row["checked_out_to_uid"] is None and row["execution_mode"] == "local", "mode stays local"


@pytest.mark.asyncio
async def test_over_http_a_stale_session_cannot_heartbeat_or_complete():
    async with local_http() as h:
        goal, story = await h.seed_story(held_by="person")
        tok = h.story_token(story, goal)
        for route in ("/heartbeat", "/complete"):
            r = await h.post(route, {"story_id": story, "checkout_session_id": "stale"}, token=tok)
            assert r.status_code == 409, (route, r.text)


async def _reaped(h, story, **kw):
    from app.services.local_checkout import reap_local_checkouts
    out = await reap_local_checkouts()
    return story in out["reclaimed"], story in out["cleaned"], await h.story(story)


@pytest.mark.asyncio
async def test_reaper_frees_an_abandoned_working_story_but_keeps_it_local():
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", held_by="person")
        await _expire(h, story)
        reclaimed, cleaned, row = await _reaped(h, story)
        assert (reclaimed, cleaned) == (True, False)
        assert row["checked_out_to_uid"] is None and row["execution_mode"] == "local"


@pytest.mark.asyncio
async def test_reaper_never_frees_a_story_waiting_for_its_merge():
    async with local_http() as h:
        goal, story = await h.seed_story(state="awaiting_merge", held_by="person")
        await _expire(h, story)
        reclaimed, cleaned, row = await _reaped(h, story)
        assert (reclaimed, cleaned) == (False, False)
        assert row["checked_out_to_uid"] == "person"


@pytest.mark.asyncio
async def test_reaper_leaves_a_done_story_that_still_owes_its_review():
    async with local_http() as h:
        goal, story = await h.seed_story(state="done", held_by="person")
        await _expire(h, story)
        assert (await _reaped(h, story))[:2] == (False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("state,kind", [("reviewed", {}), ("cancelled", {}), ("rejected", {}),
                                        ("done", {"work_kind": "errand"})])
async def test_reaper_cleans_a_finished_story_back_to_the_default_mode(state, kind):
    async with local_http() as h:
        goal, story = await h.seed_story(state=state, held_by="person", **kind)
        reclaimed, cleaned, row = await _reaped(h, story)
        assert cleaned is True
        assert (row["checked_out_to_uid"], row["execution_mode"]) == (None, "operator")


@pytest.mark.asyncio
async def test_reaper_leaves_a_live_checkout_alone():
    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress", held_by="person")
        assert (await _reaped(h, story))[:2] == (False, False)
