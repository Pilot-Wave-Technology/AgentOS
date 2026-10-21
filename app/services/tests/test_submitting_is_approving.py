"""In the local lane, submitting the diff IS approving it.

WHY THE SECOND GATE WENT. The cloud lane never had a diff review: its
executor claims completion, a pull request opens, and a human merges on
GitHub. Local grew a diff card that cloud does not have, so the same
work was approved twice — once as a diff, once as a merge — and the two
lanes diverged for no reason anybody could name. Agreed with Abhishek
on 2026-10-14: the plan is the gate before code is written, the pull
request is the gate before it lands, and there is nothing in between.

WHAT THAT MAKES TRUE, and these are the three things worth pinning:
the story lands in `awaiting_merge` (the cloud's own state, not a local
invention), the diff approval is DECIDED rather than left waiting, and
the plan cycle is spent by the submit — which is what makes "more work
from here needs a new plan" true without a second gate to hang it on.
"""
from __future__ import annotations

import pytest

from app.services.tests._source import source as src


def _parked():
    return src("app/services/local_diff.py")


# ── what the submit now does ─────────────────────────────────────────

def test_the_story_lands_in_awaiting_merge():
    """`awaiting_review` meant "a human is reading the diff". Nobody is;
    they are looking at a pull request. `awaiting_merge` is what the
    cloud lane already calls this, and everything that understands it
    there — reclaim leaving the checkout alone, dependents staying
    blocked, Mark Done refusing — understands this story too."""
    body = _parked()
    assert "state = 'awaiting_merge'" in body
    assert "current_phase = 'awaiting_merge'" in body


def test_the_diff_approval_is_decided_not_left_waiting():
    """An undecided approval is what the panel reads as `pending_diff`,
    which is what puts a "Review & Approve Diff" button on screen. Left
    undecided, the gate would still be there in the UI even though
    nothing enforces it."""
    body = _parked()
    assert "decided_at = NOW()" in body
    assert "decision = 'approved'" in body
    assert "decided_at IS NULL" in body, (
        "the settle must be conditional, or a retried submit overwrites "
        "a decision somebody already made")


def test_the_submit_spends_the_cycle():
    """More work from `awaiting_merge` is a NEW plan, and that rule needs
    a moment where the old one stops counting. With no diff approval,
    the submit is that moment."""
    body = _parked()
    assert "status = 'executed'" in body
    assert "status = 'approved'" in body


def test_the_approval_row_survives_because_it_is_the_record():
    """Deleting it would take the diff away along with the gate — it is
    what the panel shows and what the ledger points at."""
    body = _parked()
    assert "DELETE FROM approvals" not in body


def test_a_refused_submission_changes_nothing():
    """An errand, an unverified push, a spent plan. Landing those in
    `awaiting_merge` would park a story waiting on a merge of work that
    was never accepted."""
    body = _parked()
    fn = body[body.index("async def _park_for_review("):]
    fn = fn[:fn.index("async def submit_local_diff(")]
    guard = fn.index('if not result.get("ok"):')
    # The SQL, not the word -- `awaiting_merge` appears in the docstring
    # above the guard, which made the first version of this test pass for
    # the wrong reason.
    assert guard < fn.index("UPDATE stories SET state = 'awaiting_merge'"), (
        "the success guard must come before anything is written")


def test_nothing_still_parks_the_story_for_a_diff_review():
    """The old phase is retired. A stray writer would put a story into a
    state the extension no longer acts on — which is exactly the stall
    this whole change exists to remove."""
    body = _parked()
    assert "'awaiting_review'" not in body


# ── and the behaviour, end to end ────────────────────────────────────

@pytest.mark.asyncio
async def test_a_submitted_story_is_awaiting_merge_with_a_settled_diff():
    from app.services.local_diff import _park_for_review
    from app.services.tests.local_http import local_http

    async with local_http() as h:
        goal, story = await h.seed_story(phase="submitting")
        await h.conn.execute(
            "INSERT INTO plan_versions (id, story_id, version, plan_doc_path, "
            "   plan_json, proposed_by, status, approved_at, approved_by) "
            "VALUES (gen_random_uuid(), $1, 1, 'p.md', '{}'::jsonb, 'local', "
            "        'approved', now(), 'person')", story)
        aid = await h.conn.fetchval(
            "INSERT INTO approvals (id, story_id, kind, payload, proposed_at) "
            "VALUES (gen_random_uuid(), $1, 'diff', '{}'::jsonb, now()) "
            "RETURNING id", story)

        from app.services.tests.local_http import PoolOnConn
        await _park_for_review(PoolOnConn(h.conn), story,
                               {"ok": True, "approval_id": str(aid)})

        row = await h.conn.fetchrow(
            "SELECT state, current_phase FROM stories WHERE id = $1", story)
        assert row["state"] == "awaiting_merge"
        decided = await h.conn.fetchrow(
            "SELECT decision, decided_at FROM approvals WHERE id = $1", aid)
        assert decided["decision"] == "approved"
        assert decided["decided_at"] is not None
        assert await h.conn.fetchval(
            "SELECT status FROM plan_versions WHERE story_id = $1", story) == "executed"


@pytest.mark.asyncio
async def test_a_refusal_leaves_the_story_alone():
    from app.services.local_diff import _park_for_review
    from app.services.tests.local_http import local_http

    async with local_http() as h:
        goal, story = await h.seed_story(phase="submitting")
        from app.services.tests.local_http import PoolOnConn
        await _park_for_review(PoolOnConn(h.conn), story,
                               {"ok": False, "reason": "errand"})
        assert await h.conn.fetchval(
            "SELECT state FROM stories WHERE id = $1", story) == "in_progress"
