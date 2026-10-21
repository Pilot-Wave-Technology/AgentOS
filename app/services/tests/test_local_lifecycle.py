"""Local stories must emit task-level lifecycle events to Slack/Notion.

Before this, no `local_*` module called `relay_lifecycle_event` at all, so a local
story showed up on platform channels as a state change and eventually a diff card,
with no per-phase progress in between.

These tests pin the payload SHAPE (channel handlers key off `old_state`/`new_state`),
the done → `task.completed` vs blocked/failed → `task.state_changed` split, and — most
importantly — that a relay failure never propagates into the caller's already-committed
transition, while still being logged at WARNING. That last part is not incidental: F10
was a broken relay payload swallowed at `debug` level, which is why it survived in
production unnoticed.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.services import local_lifecycle as ll

_TASK = {
    "id": "t-1",
    "title": "implement the thing",
    "story_id": "s-1",
    "goal_id": "g-1",
}


async def _drain():
    """Let the fire-and-forget `asyncio.create_task` relay actually run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_claim_relays_ready_to_in_progress():
    relay = AsyncMock()
    with patch("app.channels.base.relay_lifecycle_event", relay):
        ll.relay_phase_claimed("g-1", _TASK)
        await _drain()

    relay.assert_awaited_once()
    goal_id, event, payload = relay.await_args.args
    assert goal_id == "g-1"
    assert event == "task.state_changed"
    assert payload == {
        "task_id": "t-1",
        "task_title": "implement the thing",
        "story_id": "s-1",
        "old_state": "ready",
        "new_state": "in_progress",
    }


@pytest.mark.asyncio
async def test_done_relays_task_completed_with_summary():
    relay = AsyncMock()
    with patch("app.channels.base.relay_lifecycle_event", relay):
        ll.relay_phase_reported("g-1", _TASK, "done", "shipped it")
        await _drain()

    _, event, payload = relay.await_args.args
    assert event == "task.completed"
    assert payload["new_state"] == "done"
    assert payload["result_summary"] == "shipped it"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "failed"])
async def test_non_done_relays_state_changed_without_summary(status):
    """A blocked/failed phase is a state change, not a completion — and its tail is a
    reason, not a result, so it must not be presented as one."""
    relay = AsyncMock()
    with patch("app.channels.base.relay_lifecycle_event", relay):
        ll.relay_phase_reported("g-1", _TASK, status, "could not compile")
        await _drain()

    _, event, payload = relay.await_args.args
    assert event == "task.state_changed"
    assert payload["new_state"] == status
    assert payload["result_summary"] == ""


@pytest.mark.asyncio
async def test_unknown_status_relays_nothing():
    relay = AsyncMock()
    with patch("app.channels.base.relay_lifecycle_event", relay):
        ll.relay_phase_reported("g-1", _TASK, "banana", "")
        await _drain()
    relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_goal_id_relays_nothing():
    """Channel dispatch is goal-scoped; relaying without a goal would be undeliverable."""
    relay = AsyncMock()
    with patch("app.channels.base.relay_lifecycle_event", relay):
        ll.relay_phase_claimed("", _TASK)
        ll.relay_phase_reported("", _TASK, "done", "x")
        await _drain()
    relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_relay_failure_is_swallowed_but_logged_at_warning(caplog):
    """The caller's transition is already committed, so a relay failure must not
    propagate — but it must be greppable, unlike the debug-level swallow that let F10
    hide in production."""
    boom = AsyncMock(side_effect=RuntimeError("slack is down"))
    with patch("app.channels.base.relay_lifecycle_event", boom):
        with caplog.at_level(logging.WARNING, logger="app.services.local_lifecycle"):
            ll.relay_phase_claimed("g-1", _TASK)
            await _drain()   # must not raise

    assert any(
        r.levelno == logging.WARNING and "relay of task.state_changed FAILED" in r.message
        for r in caplog.records
    ), f"expected a WARNING naming the failed relay, got: {[r.message for r in caplog.records]}"
