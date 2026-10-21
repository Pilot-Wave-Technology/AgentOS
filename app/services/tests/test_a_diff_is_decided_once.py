"""S4: a diff approval is decided once, even when two decisions race.

`decide_diff_review` read `decided_at`, then awaited (the freeze check, the
push), then wrote the decision without re-checking -- so two clicks arriving
together both passed and both wrote the ledger. The write is now conditional on
the approval still being undecided; the loser gets the same reply a later click
gets. The race is reproduced deterministically: the "other click" lands inside
the freeze check, after the first read and before the write.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.services.tests.local_http import local_http


async def _local_approval(h):
    goal, story = await h.seed_story(phase="awaiting_review")
    aid = str(uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO approvals (id, story_id, kind, payload) VALUES ($1, $2, 'diff', $3)",
        aid, story, json.dumps({"dag_task_id": "", "commit_sha": "abc", "push_verified": True}))
    return goal, story, aid


@pytest.fixture
def other_click_wins(monkeypatch):
    """Make the freeze check -- which runs between the read and the write -- be the
    moment another request decides the same approval."""
    import app.services.execution_control as ec
    state = {"aid": None, "conn": None, "decision": "approved"}

    async def frozen_response(_goal):
        if state["aid"]:
            await state["conn"].execute(
                "UPDATE approvals SET decision = $2, decided_at = now(), decided_by = 'other' WHERE id = $1",
                state["aid"], state["decision"])
        return None
    monkeypatch.setattr(ec, "frozen_response", frozen_response)
    return state


@pytest.mark.asyncio
async def test_an_approval_that_lost_the_race_changes_nothing(other_click_wins):
    from app.services.diff_review import decide_diff_review
    async with local_http() as h:
        goal, story, aid = await _local_approval(h)
        other_click_wins.update(aid=aid, conn=h.conn)
        r = await decide_diff_review(approval_id=aid, decision="approve", feedback="", decided_by_uid="person")
        assert r == {"ok": False, "error": "approval already approved"}
        assert await h.conn.fetchval("SELECT decided_by FROM approvals WHERE id = $1", aid) == "other"
        assert [e for e, _, _ in await h.ledger(goal) if e.startswith("diff.")] == []
        assert (await h.story(story))["current_phase"] == "awaiting_review"


@pytest.mark.asyncio
async def test_a_rejection_that_lost_the_race_changes_nothing(other_click_wins):
    from app.services.diff_review import decide_diff_review
    async with local_http() as h:
        goal, story, aid = await _local_approval(h)
        other_click_wins.update(aid=aid, conn=h.conn, decision="rejected")
        r = await decide_diff_review(approval_id=aid, decision="reject", feedback="no", decided_by_uid="person")
        assert r == {"ok": False, "error": "approval already rejected"}
        assert [e for e, _, _ in await h.ledger(goal) if e.startswith("diff.")] == []


@pytest.mark.asyncio
async def test_a_local_approval_records_once_with_the_person_as_actor():
    from app.services.diff_review import decide_diff_review
    async with local_http() as h:
        await h.ensure_user("person")
        goal, story, aid = await _local_approval(h)
        r = await decide_diff_review(approval_id=aid, decision="approve", feedback="", decided_by_uid="person")
        assert r["ok"] is True
        again = await decide_diff_review(approval_id=aid, decision="approve", feedback="", decided_by_uid="person")
        assert again == {"ok": False, "error": "approval already approved"}
        events = [(e, a) for e, a, _ in await h.ledger(goal) if e.startswith("diff.")]
        assert events == [("diff.approved_and_pushed", "human")]


@pytest.mark.asyncio
async def test_a_second_submit_reuses_the_pending_approval_and_releases_its_lock():
    from app.services.local_diff import submit_local_diff
    async with local_http() as h:
        goal, story, aid = await _local_approval(h)
        await h.conn.execute(
            "INSERT INTO plan_versions (id, story_id, version, status) VALUES ($1, $2, 1, 'approved')",
            str(uuid.uuid4()), story)
        r = await submit_local_diff(story, "diff", "stat", False, "abc")
        assert r == {"ok": True, "approval_id": aid, "deduped": True}
        held = await h.conn.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
        assert held == 0, "the per-story submit lock is released"
