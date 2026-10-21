"""Answer Slack inside three seconds, and do the work exactly once.

THESE TWO FAULTS FED EACH OTHER. The interactive endpoint awaited the
whole handler inline -- and inside that window sat a blocking
users_info(), a GitHub fetch, and for diff_approve a real git commit
and push. So approving a diff routinely blew Slack's 3-second budget
and showed the user a timeout on a click that had in fact worked.

Which invites a second click. And the approve path had no CAS: it
checked the state, created the subtask tree, and only then updated --
so two clicks both passed the check and both created subtasks. The
timeout manufactured the double-click that the missing CAS then turned
into duplicated work.

Fixing one without the other leaves the pair intact, so both are here.
"""
from __future__ import annotations

import asyncio
import inspect

from app.services.tests.slack_fixtures import world


def _run(coro):
    return asyncio.run(coro)


# ── ack first ────────────────────────────────────────────────────────────────

def test_the_actions_endpoint_does_not_await_the_handler():
    from app.channels import router

    src = inspect.getsource(router.platform_actions)
    assert "asyncio.create_task" in src, (
        "the actions endpoint still awaits the full handler inline — a diff "
        "approval commits and pushes inside Slack's 3-second budget")
    assert "add_done_callback" in src, (
        "the backgrounded handler's exception is never retrieved")


def test_a_view_submission_is_still_awaited():
    """Slack reads a view_submission's response to decide whether to close
    the modal or show field errors, so this one genuinely cannot be
    backgrounded. Backgrounding it would silently break modal validation."""
    from app.channels import router

    src = inspect.getsource(router.platform_actions)
    assert "view_submission" in src, (
        "view_submission is backgrounded with everything else — its response "
        "is what closes the modal")
    assert src.index("view_submission") < src.index("asyncio.create_task"), (
        "the view_submission branch must come before the background path")


def test_a_slow_handler_does_not_delay_the_ack():
    """The property itself, not its spelling."""
    from app.channels import router

    acked = []

    class _Adapter:
        async def handle_action(self, payload):
            await asyncio.sleep(0.4)
            acked.append("handler finished")
            return None

    async def body():
        task = asyncio.create_task(_Adapter().handle_action({}))
        task.add_done_callback(lambda t: None)
        # the endpoint returns here, with the handler still running
        assert acked == [], "the handler completed before the ack"
        await task
        assert acked == ["handler finished"]

    _run(body())
    assert hasattr(router, "_report_background_failure")


def test_a_background_failure_is_reported_not_swallowed():
    from app.channels.router import _report_background_failure

    async def body():
        async def boom():
            raise RuntimeError("handler exploded")
        t = asyncio.create_task(boom())
        await asyncio.gather(t, return_exceptions=True)
        _report_background_failure(t)        # must not raise
    _run(body())


# ── act once ─────────────────────────────────────────────────────────────────

def test_a_second_click_during_subtask_creation_does_not_build_a_second_tree():
    """THE RACE, REPRODUCED IN ITS ACTUAL WINDOW.

    Two SEQUENTIAL approvals prove nothing: the first leaves the task
    `in_progress`, so the second fails the state check whether or not a
    CAS exists. (That version of this test passed with the CAS reverted,
    which is how it was caught.)

    The real window is the gap between reading the state and writing it.
    With the old order -- check, create subtasks, then update -- the task
    is still `awaiting_approval` for the whole of subtask creation, so a
    click arriving in that window passes the check too. Re-entering from
    inside `create_subtasks_from_planner` lands exactly there.
    """
    from unittest.mock import patch

    created = []

    async def body():
        async with world() as (conn, pool, w):
            await conn.execute(
                "UPDATE tasks SET state='awaiting_approval', result='a plan' "
                " WHERE id=$1", w["task"])

            from app.services.card_responses import handle_card_response

            async def _fake_create(task, result):
                created.append(task["id"])
                if len(created) == 1:          # the second click, mid-flight
                    await handle_card_response(
                        task_id=w["task"], option_id="approve", value="yes",
                        user_uid=w["member"])

            class _Controller:
                async def trigger(self):
                    return None

            with patch("app.task_controller.create_subtasks_from_planner", _fake_create),                  patch("app.task_controller.task_controller", _Controller()):
                res = await handle_card_response(
                    task_id=w["task"], option_id="approve", value="yes",
                    user_uid=w["member"])

            assert res.ok is True
            assert len(created) == 1, (
                f"a click arriving during subtask creation built another tree "
                f"({len(created)} total) — the state check is not the lock")
    _run(body())


def test_the_claim_comes_before_the_subtasks_in_the_source():
    """Ordering is the whole property: creating first and claiming second
    is the bug, and both orders pass a single-click test."""
    from app.services import card_responses

    src = inspect.getsource(card_responses.handle_card_response)
    claim = src.index("state = 'awaiting_approval' RETURNING id")
    create = src.index("create_subtasks_from_planner(task, result)")
    assert claim < create, (
        "subtasks are created before the transition is claimed, so two "
        "concurrent approvals both build a tree")


# ── a failed handler does not eat the event ──────────────────────────────────

def test_a_dedup_claim_is_released_when_handling_fails():
    """is_duplicate claims BEFORE the handler runs, which is what makes it
    atomic. The router has already answered 200, so nothing retries — a
    handler that throws used to lose the message permanently."""
    from app.channels import base

    async def body():
        async with world() as (conn, pool, w):
            assert await base.is_duplicate("slack", "evt-1") is False
            assert await base.is_duplicate("slack", "evt-1") is True
            await base.forget_event("slack", "evt-1")
            assert await base.is_duplicate("slack", "evt-1") is False, (
                "the claim was not released, so a redelivery is still dropped")
    _run(body())


def test_forget_event_survives_a_broken_database():
    """It runs in an except path; raising there would replace a handler
    failure with a different one."""
    from app.channels import base
    from unittest.mock import patch

    class _Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("no database")

    async def body():
        with patch("app.channels.base.get_pool", lambda: _Broken()):
            await base.forget_event("slack", "evt-x")   # must not raise
    _run(body())


def test_the_slack_handler_releases_its_claim_on_failure():
    from app.channels.slack.inbound import SlackInboundAdapter

    src = inspect.getsource(SlackInboundAdapter.handle_event)
    assert "forget_event" in src, (
        "a handler that raises keeps the dedup claim, so the event can never "
        "be redelivered and is lost")
