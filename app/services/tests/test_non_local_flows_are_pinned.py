"""Today's behaviour for everything that is NOT a VS Code story, pinned.

RULE ZERO of the local-lane build (plans/vscode-coding-story-build-plan.md):
the web UI, the board, Slack, GO and the cloud (pod) flow work today and must
behave exactly as they do now. Every change for local stories is gated by
`execution_mode = 'local'`; these tests are what notices if one is not.

Written BEFORE any of that work, against the code as it was, and never edited
to make a later change pass. If one of these fails, the change is wrong.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.services.tests.local_http import local_http


def test_the_story_transition_table_is_unchanged():
    from app.models.story import STORY_VALID_TRANSITIONS
    assert STORY_VALID_TRANSITIONS == {
        "proposed": ["accepted", "rejected"],
        "accepted": ["in_progress", "rejected"],
        "in_progress": ["blocked", "done"],
        "blocked": ["in_progress", "done"],
        "done": ["reviewed"],
        "reviewed": [],
        "rejected": [],
        "cancelled": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["in_progress", "blocked"])
@pytest.mark.parametrize("mode", ["operator", "ia_dispatch"])
async def test_a_started_cloud_story_can_be_cancelled(state, mode):
    """IT USED TO BE REFUSED, and that was the point of this test.

    The rule was that a started story has a cut branch, so cancelling
    would strand commits -- true, and it left a story nobody was
    working on running for ever. The lane is torn down instead, and
    `tear_down_nodes` leaves a MERGED node alone, so what is stopped
    is the work still to come.
    """
    from app.routers.stories import _cancel_story
    async with local_http() as h:
        goal, story = await h.seed_story(state=state, mode=mode, held_by=None)
        r = await _cancel_story(goal, story, "x", actor_uid="person")
        assert r.status_code != 409, r.body
        assert (await h.story(story))["state"] == "cancelled"


async def _diff_approval(h, mode="operator"):
    goal, story = await h.seed_story(mode=mode, phase="awaiting_review", held_by=None)
    aid = str(uuid.uuid4())
    await h.conn.execute(
        "INSERT INTO approvals (id, story_id, kind, payload) VALUES ($1, $2, 'diff', $3)",
        aid, story, json.dumps({"dag_task_id": f"{story}-dag", "commit_sha": "abc123"}))
    return goal, story, aid


@pytest.fixture
def gateway_push(monkeypatch):
    """The cloud lane pushes through the gateway; pretend it pushed."""
    calls = []

    async def call(method, params, timeout=None):
        calls.append((method, params))
        return {"pushed": True, "commit_sha": "def456"}

    async def refresh(_story):
        return True, None

    import app.services.diff_review as dr
    import app.services.token_refresh as tr
    monkeypatch.setattr(dr.gateway_admin, "call", call)
    monkeypatch.setattr(tr, "refresh_story_token", refresh)
    return calls


@pytest.mark.asyncio
async def test_approving_a_cloud_diff_pushes_and_waits_for_the_next_step(gateway_push):
    from app.services.diff_review import decide_diff_review
    async with local_http() as h:
        goal, story, aid = await _diff_approval(h)
        r = await decide_diff_review(approval_id=aid, decision="approve", feedback="",
                                     decided_by_uid="person")
        assert r == {"ok": True, "decision": "approve", "pushed": True, "commit_sha": "def456"}
        assert gateway_push[0][0] == "shca.workspace.gitCommit"
        row = await h.story(story)
        assert (row["state"], row["current_phase"]) == ("in_progress", "awaiting_next")
        ap = await h.conn.fetchrow("SELECT decision, decided_by FROM approvals WHERE id = $1", aid)
        assert (ap["decision"], ap["decided_by"]) == ("approved", "person")
        events = [e for e, _, _ in await h.ledger(goal)]
        assert "diff.approved_and_pushed" in events


@pytest.mark.asyncio
async def test_approving_a_cloud_diff_twice_says_it_is_already_decided(gateway_push):
    from app.services.diff_review import decide_diff_review
    async with local_http() as h:
        _, _, aid = await _diff_approval(h)
        await decide_diff_review(approval_id=aid, decision="approve", feedback="", decided_by_uid="person")
        r = await decide_diff_review(approval_id=aid, decision="approve", feedback="", decided_by_uid="person")
        assert r == {"ok": False, "error": "approval already approved"}
        assert len(gateway_push) == 1


@pytest.mark.asyncio
async def test_rejecting_a_cloud_diff_sends_the_story_back_to_planning(gateway_push):
    from app.services.diff_review import decide_diff_review
    async with local_http() as h:
        goal, story, aid = await _diff_approval(h)
        await h.conn.execute("UPDATE stories SET current_plan_path = 'p.md' WHERE id = $1", story)
        r = await decide_diff_review(approval_id=aid, decision="reject", feedback="redo",
                                     decided_by_uid="person")
        assert r == {"ok": True, "decision": "reject"}
        row = await h.story(story)
        assert (row["state"], row["current_phase"], row["current_plan_path"]) == ("in_progress", "planning", None)
        assert gateway_push == []
        assert "diff.rejected" in [e for e, _, _ in await h.ledger(goal)]
