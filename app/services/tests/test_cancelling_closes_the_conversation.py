"""A cancelled story leaves no live conversation behind.

WHAT WAS LEFT OPEN. Cancelling stopped the run, bridged the dependents
and retired the branch, and left the story's thread in the goal's list
with an agent session behind it. For an errand that is the thread GO
drove the work on: it reads as live, the gateway will resume it, and
opening it gives somebody a conversation about work that no longer
exists.
"""
from __future__ import annotations

import asyncio
import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]


class _Conn:
    """Enough of a connection to watch what the teardown does."""

    def __init__(self, rows):
        self.rows = rows
        self.updates: list = []

    async def fetch(self, sql, *a):
        assert "FROM threads" in sql
        assert "status IN ('active', 'open')" in sql, (
            "it would close threads somebody already closed, and delete "
            "sessions twice")
        return self.rows

    async def execute(self, sql, *a):
        self.updates.append((sql, a))


def _run(coro):
    return asyncio.run(coro)


def _patch_gateway(monkeypatch, fail=False):
    seen: list = []

    class _GW:
        async def delete_session(self, key, delete_transcript=True):
            seen.append((key, delete_transcript))
            if fail:
                raise RuntimeError("gateway unreachable")
            return {"ok": True}

    import app.openclaw as oc
    monkeypatch.setattr(oc, "gateway_admin", _GW())
    return seen


def test_the_gateway_session_is_deleted(monkeypatch):
    """Closing the row alone hides the thread and leaves the session on
    the gateway holding a context window, resumable by anything that
    still knows the key."""
    from app.services.story_session_teardown import close_story_sessions

    seen = _patch_gateway(monkeypatch)
    conn = _Conn([{"id": "t1", "instance_id": "go-err16", "agent_id": "go"}])
    assert _run(close_story_sessions(conn, "ERR16", "438c0a0d")) == 1
    assert seen == [("agent:go-err16:err16-t1", True)]


def test_the_thread_is_closed_so_the_ui_stops_showing_it(monkeypatch):
    """`goals.py` lists threads whose status is active, open, empty or
    NULL. Deleting the session without closing the row leaves a thread
    in the sidebar that reopens empty."""
    from app.services.story_session_teardown import close_story_sessions

    _patch_gateway(monkeypatch)
    conn = _Conn([{"id": "t1", "instance_id": "i1", "agent_id": "a1"}])
    _run(close_story_sessions(conn, "ERR16", "438c0a0d"))
    sql, args = conn.updates[0]
    assert "UPDATE threads SET status = 'closed'" in sql
    assert args == ("t1",)


def test_the_key_is_built_from_the_instance_not_the_template(monkeypatch):
    """A key built from the template id deletes nothing and reports
    success. `agent_id` is only the fallback for rows written before
    `instance_id` existed."""
    from app.services.story_session_teardown import close_story_sessions

    seen = _patch_gateway(monkeypatch)
    conn = _Conn([{"id": "t1", "instance_id": "inst-9",
                   "agent_id": "template"}])
    _run(close_story_sessions(conn, "G1", "s1"))
    assert "inst-9" in seen[0][0] and "template" not in seen[0][0]


def test_a_dead_gateway_does_not_undo_the_cancellation(monkeypatch):
    """The story is already cancelled by the time this runs. Same
    discipline as `stop_the_run`: an unreachable gateway leaves an
    untidy thread, not a failed request."""
    from app.services.story_session_teardown import close_story_sessions

    _patch_gateway(monkeypatch, fail=True)
    conn = _Conn([{"id": "t1", "instance_id": "i1", "agent_id": "a1"}])
    assert _run(close_story_sessions(conn, "G1", "s1")) == 1
    assert conn.updates, "the thread stayed open because the gateway was down"


def test_a_thread_with_no_agent_is_still_closed(monkeypatch):
    """There is no session to delete, which is not a reason to leave the
    row open."""
    from app.services.story_session_teardown import close_story_sessions

    seen = _patch_gateway(monkeypatch)
    conn = _Conn([{"id": "t1", "instance_id": None, "agent_id": ""}])
    assert _run(close_story_sessions(conn, "G1", "s1")) == 1
    assert seen == []
    assert conn.updates


def test_cancelling_calls_it():
    """The unit above is only true if the cancellation reaches it. The
    node teardown had the same shape and the tool that should have
    called it had no caller for weeks."""
    src = io.open(ROOT / "app/routers/stories.py", encoding="utf-8").read()
    body = src[src.index("async def _cancel_story"):]
    assert "close_story_sessions(conn, pid, sid)" in body


def test_it_runs_for_every_story_not_only_the_torn_down_ones():
    """The session teardown must not end up inside a conditional.

    It used to sit after an errand-only node teardown, and the shape
    was pinned so the errand guard could not grow to wrap it: an
    ordinary story's thread has to close too, because a conversation
    carries no commits and there is no reason to leave it open.

    The node teardown is no longer errand-only -- every started story
    gets one now -- so the guard this watched is gone. What still
    matters is unchanged: `close_story_sessions` runs unconditionally,
    for every cancelled story.
    """
    src = io.open(ROOT / "app/routers/stories.py", encoding="utf-8").read()
    body = src[src.index("async def _cancel_story"):]
    call = body.index("close_story_sessions(conn, pid, sid)")

    # Walk back to the start of its line and check nothing indents it
    # deeper than the transaction body it belongs to.
    line_start = body.rfind(chr(10), 0, call) + 1
    indent = len(body[line_start:call]) - len(body[line_start:call].lstrip())
    assert indent <= 8, (
        "close_story_sessions is nested inside a conditional; a story "
        "that does not match it keeps a live thread for cancelled work")



def test_both_prompts_say_the_conversation_goes_away():
    """A person is told "cancelled" and their thread vanishes. If the
    agents do not know that happens, the first question about it gets an
    invented answer -- and for GO the thread closing is the thread it was
    driving the errand on."""
    go = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()
    assert "its thread is closed and its session deleted" in go
    assert "closes underneath you" in go

    a = io.open(ROOT / "kernel/assistant/AGENTS.md", encoding="utf-8").read()
    assert "its thread is closed with its session deleted" in a
