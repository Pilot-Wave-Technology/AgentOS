"""A local coding story is reviewed before it reaches `reviewed`.

MIRRORS THE POD, where `kernel/worker/finish.py` keeps a finished run alive
with `phase='review'` so a later leg reads the tree the executor wrote. What
carries over is the WORKSPACE being intact and the RULES being identical --
same verdicts, same minimum summary, same downgrade, same writer.

What does NOT carry over is recorded in `local_review`'s docstring and in the
profile: locally the review runs in the author's own session, so it remembers
the work and still holds every writing tool. The prompt asks for what the
pod's tool surface guarantees.

WHAT THESE PIN is the ordering the feature exists for: memory, then the result
and the GO wake, then the review, and only then is the story finished with. A
story that owes a review must not take the `skipped_local` exemption, because
that moves it out of `done` before anybody has read anything.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


class _OneConnPool:
    """Pool-shaped, with `acquire()` and deliberately no `.transaction()`."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        @contextlib.asynccontextmanager
        async def _cm():
            yield conn
        return _cm()

    def __getattr__(self, name):
        if name == "transaction":
            raise AttributeError("'Pool' object has no attribute 'transaction'")
        return getattr(self._conn, name)


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _story(conn, sid: str, *, work_kind="story", state="done"):
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       f"{sid}-goal", "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,execution_mode,"
        "work_kind,state,story_kind) VALUES ($1,$2,$3,$4,'local',$5,$6,'code')",
        sid, f"{sid}-goal", "review fixture", f"{PREFIX}-user", work_kind, state)
    return f"{sid}-goal"


LONG = ("I read every changed file whole against the approved plan. The four "
        "endpoints match what was agreed, the error path returns 400 as "
        "specified, and nothing outside the plan was touched. Tests exist for "
        "each case and I did not run them.")


@pytest.mark.asyncio
async def test_a_coding_story_owes_a_review():
    from app.services.local_review import owes_a_review

    async with _conn() as conn:
        sid = f"{PREFIX}-rv-owes"
        await _story(conn, sid)
        assert await owes_a_review(conn, sid) is True


@pytest.mark.asyncio
async def test_an_errand_does_not():
    # The same exclusion the pod makes: the deliverable is an artifact
    # already measured, and its branch is scaffolding that goes at `done`.
    from app.services.local_review import owes_a_review

    async with _conn() as conn:
        sid = f"{PREFIX}-rv-errand"
        await _story(conn, sid, work_kind="errand")
        assert await owes_a_review(conn, sid) is False


@pytest.mark.asyncio
async def test_a_story_owing_a_review_does_not_take_the_exemption():
    """The ordering guarantee. Taking `skipped_local` here would move the
    story out of `done` before anybody read anything."""
    from app.services.local_completion import finish_local_story

    async with _conn() as conn:
        sid = f"{PREFIX}-rv-noexempt"
        goal = await _story(conn, sid)
        out = await finish_local_story(_OneConnPool(conn), sid, goal)
        state = await conn.fetchval("SELECT state FROM stories WHERE id=$1", sid)
        rows = await conn.fetchval(
            "SELECT count(*) FROM story_reviews WHERE story_id=$1", sid)
    assert out["owes_review"] is True
    assert out["reviewed"] is False
    assert state == "done", "the story reached `reviewed` with no review"
    assert rows == 0


@pytest.mark.asyncio
async def test_an_errand_still_finishes_in_one_step():
    from app.services.local_completion import finish_local_story

    async with _conn() as conn:
        sid = f"{PREFIX}-rv-errand2"
        goal = await _story(conn, sid, work_kind="errand")
        out = await finish_local_story(_OneConnPool(conn), sid, goal)
        state = await conn.fetchval("SELECT state FROM stories WHERE id=$1", sid)
    assert out["reviewed"] is True
    assert state == "reviewed", "an exempt kind is now stuck at `done`"


@pytest.mark.asyncio
async def test_a_submitted_review_moves_the_story():
    from app.services.local_review import record_local_review

    async with _conn() as conn:
        sid = f"{PREFIX}-rv-submit"
        goal = await _story(conn, sid)
        out = await record_local_review(_OneConnPool(conn), sid, goal, {
            "verdict": "approve", "summary": LONG})
        state = await conn.fetchval("SELECT state FROM stories WHERE id=$1", sid)
        row = await conn.fetchrow(
            "SELECT verdict, reviewer_agent_id, review_text FROM story_reviews "
            " WHERE story_id=$1", sid)
    assert out["verdict"] == "approve"
    assert state == "reviewed"
    assert row["verdict"] == "approve"
    assert row["reviewer_agent_id"] == "local-session"
    assert LONG[:40] in row["review_text"]


@pytest.mark.asyncio
async def test_approve_with_unexamined_churn_is_downgraded():
    """The rule that stops 'nobody looked' being recorded as 'somebody looked
    and it was fine'. Identical to the pod's, and not a refusal."""
    from app.services.local_review import record_local_review

    async with _conn() as conn:
        sid = f"{PREFIX}-rv-down"
        goal = await _story(conn, sid)
        out = await record_local_review(_OneConnPool(conn), sid, goal, {
            "verdict": "approve", "summary": LONG,
            "not_examined": ["app/legacy.py"]})
        row = await conn.fetchrow(
            "SELECT verdict, review_text FROM story_reviews WHERE story_id=$1", sid)
    assert out["verdict"] == "partial"
    assert out["downgraded"] is True
    assert row["verdict"] == "partial"
    assert "Downgraded from" in row["review_text"], (
        "the downgrade is not recorded in the review a person reads"
    )


@pytest.mark.asyncio
async def test_the_refusals_are_actionable():
    from app.services.local_review import ReviewRefused, record_local_review

    async with _conn() as conn:
        sid = f"{PREFIX}-rv-refuse"
        goal = await _story(conn, sid)
        pool = _OneConnPool(conn)
        with pytest.raises(ReviewRefused) as bad_verdict:
            await record_local_review(pool, sid, goal,
                                      {"verdict": "lgtm", "summary": LONG})
        assert "must be one of" in str(bad_verdict.value)
        with pytest.raises(ReviewRefused) as thin:
            await record_local_review(pool, sid, goal,
                                      {"verdict": "approve", "summary": "fine"})
        assert "code_not_reviewed" in str(thin.value), (
            "the refusal does not tell an honest reviewer what to answer instead"
        )


def test_both_lanes_share_the_rules():
    # A review must mean one thing on the board whichever lane produced it.
    from kernel.harness.tools_worker.review import MIN_SUMMARY, VERDICTS

    from app.services import local_review

    assert local_review.VERDICTS is VERDICTS
    assert local_review.MIN_SUMMARY is MIN_SUMMARY


@pytest.mark.asyncio
async def test_a_local_review_is_delivered_by_the_request_itself():
    """a91cac41 (feature, 2026-10-16): the review was recorded and announced
    on `story_review_ready`, but the listener's connection was gone and the
    review never reached the goal. A local review is written by the request,
    so the request delivers it -- and the listener, if it also hears the
    NOTIFY, finds it claimed and sends nothing."""
    from unittest.mock import AsyncMock, patch

    from app.services import story_review_delivery
    from app.services.local_review import record_local_review

    async with _conn() as conn:
        sid = f"{PREFIX}-rv-deliver"
        goal = await _story(conn, sid)
        await conn.execute(
            "UPDATE goals SET go_agent_id='go-x', go_thread_id='t-x' WHERE id=$1", goal)
        card, wake = AsyncMock(return_value=True), AsyncMock()
        with patch.object(story_review_delivery, "get_pool", lambda: conn), \
             patch("app.services.orchestrator_delivery."
                   "persist_review_to_orchestrator", card), \
             patch("app.services.go_events._send_system_message_to_go", wake):
            await record_local_review(_OneConnPool(conn), sid, goal, {
                "verdict": "approve", "summary": LONG})
            delivered = await conn.fetchval(
                "SELECT delivered_at IS NOT NULL FROM story_reviews "
                " WHERE story_id=$1", sid)
            # The listener hearing the same NOTIFY afterwards.
            again = await story_review_delivery.deliver_recorded(goal, sid)
    assert delivered, "the review waits on a listener that may not be there"
    assert card.await_count == 1 and wake.await_count == 1
    assert "[REVIEW:story]" in wake.await_args.args[1]
    assert again is False, "the listener delivered it a second time"
