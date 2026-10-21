"""Relay a local story's TASK-level lifecycle to platform channels (Slack, Notion…).

Local execution previously emitted no task-level lifecycle events at all: no
`local_*` module called `relay_lifecycle_event`. Story-level changes did relay (the
local path uses the shared `story_transitions.apply_story_transition`), and the
diff-review card did too, so on Slack/Notion a local story appeared to change state
and eventually produce a diff — with **no per-phase progress in between**. That is
at odds with platform integrations being full operational surfaces rather than
notification feeds.

**Why this lives in a local-only module rather than the shared phase machinery.**
The obvious place would be `sentinel_parser.handle_phase_task_response`, which both
execution modes go through. But it currently relays nothing for *either* mode, so
adding a relay there would start emitting new events for gateway phase tasks too —
a behaviour change well beyond "make local stories visible", against a production
system. Keeping it here is additive and leaves the gateway byte-identical. The
broader question (should the shared phase path relay for both modes?) is recorded in
`research/LOCAL_EXEC_MULTIREPO_INTEGRATION.md` rather than answered by a side effect.

**Payload shapes mirror the gateway's** (`task_controller`: `task.created` at the
planner-subtask site, `task.state_changed` on IA dispatch, `task.completed` in
`handle_task_completion`) so downstream channel handlers need no new cases.

**Failures log at WARNING, deliberately.** The gateway's equivalent call sites use
`_log.debug`, and that is exactly how F10 hid in production for months: a `NameError`
in the relay payload was swallowed and logged below the production level, so every
planner subtask silently failed to reach Notion/Slack with no visible trace. A
dropped relay is a real (if non-fatal) loss of user-visible state and should be
greppable.
"""
from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)

# `report_phase` status → the lifecycle event a channel handler should see. Only
# 'done' is a completion; blocked/failed are state changes the human must notice.
_TERMINAL_EVENT = {
    "done": "task.completed",
    "blocked": "task.state_changed",
    "failed": "task.state_changed",
}


async def _relay(goal_id: str, event: str, payload: dict) -> None:
    """Fire one lifecycle event. Never raises — a relay failure must not fail the
    caller's transition, which has already been committed."""
    try:
        from app.channels.base import relay_lifecycle_event  # noqa: PLC0415
        await relay_lifecycle_event(goal_id, event, payload)
        _log.info(
            "local_lifecycle: relayed %s goal=%s task=%s", event, goal_id,
            payload.get("task_id"),
        )
    except Exception as exc:  # noqa: BLE001 — see module docstring on WARNING
        _log.warning(
            "local_lifecycle: relay of %s FAILED goal=%s task=%s: %s — platform "
            "channels will not show this transition",
            event, goal_id, payload.get("task_id"), exc,
        )


def relay_phase_claimed(goal_id: str, task: dict) -> None:
    """A local runner just claimed a ready phase task (ready → in_progress).

    Fire-and-forget: the dispatch response must not wait on an outbound HTTP call to
    Slack/Notion. Mirrors the gateway's `task.state_changed` dispatch payload.
    """
    if not goal_id:
        _log.debug("local_lifecycle: no goal_id for task=%s — skipping claim relay",
                   task.get("id"))
        return
    payload = {
        "task_id": task.get("id"),
        "task_title": task.get("title") or "",
        "story_id": str(task.get("story_id") or ""),
        "old_state": "ready",
        "new_state": "in_progress",
    }
    asyncio.create_task(_relay(goal_id, "task.state_changed", payload))


def relay_phase_reported(goal_id: str, task: dict, status: str, summary: str) -> None:
    """A local runner reported a phase terminal (done / blocked / failed).

    `status` is the reported status, not the task's post-transition state: downstream
    transitions are CAS-guarded and may legitimately no-op on an already-terminal
    task, so the report is what carries the human-meaningful information.
    """
    if not goal_id:
        _log.debug("local_lifecycle: no goal_id for task=%s — skipping report relay",
                   task.get("id"))
        return
    event = _TERMINAL_EVENT.get(status)
    if not event:
        _log.debug("local_lifecycle: status %r has no lifecycle event — skipping", status)
        return
    payload = {
        "task_id": task.get("id"),
        "task_title": task.get("title") or "",
        "story_id": str(task.get("story_id") or ""),
        "old_state": "in_progress",
        "new_state": "done" if status == "done" else status,
        "result_summary": (summary or "")[:500] if status == "done" else "",
    }
    asyncio.create_task(_relay(goal_id, event, payload))
