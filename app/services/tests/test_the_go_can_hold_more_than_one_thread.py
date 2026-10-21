"""A goal's GO replies into the thread it was asked in.

WHY THIS IS THE GATE FOR AUTONOMOUS ERRANDS. An approved errand opens a
thread of its own on the GO, so a goal stops having exactly one GO
conversation. Everything under the GO was already built for that -- the
session tracks `run_id -> thread_id`, the gateway session key carries
the thread, `send_to_gateway` reads the thread off the message -- with
one exception:

`_make_go_callbacks` ACCEPTED `thread_id` AND IGNORED IT. Both callbacks
took the argument and then wrote the message row, the ledger event, the
unread bump, the broadcast, the Slack relay and the notification link
against the closure's `go_thread_id`. With one thread per goal that was
invisible. With two it puts every errand's reply in the main goal
thread: wrong conversation, wrong correlation id, and absent from the
thread the person is reading.

`app/routers/chat.py:262` is the assistant's equivalent and has always
used the argument -- it has been multi-thread since it was written.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from app.routers import goal_thread
from app.routers import goal_thread_subs as subs


@pytest.fixture(autouse=True)
def _empty_registry():
    """The registry is a module global. A test that leaves a subscriber
    behind makes the NEXT test's fan-out count wrong, which reads as a
    bug in the code rather than in the suite."""
    subs._subscribers.clear()
    yield
    subs._subscribers.clear()


def _callbacks_source() -> str:
    return inspect.getsource(goal_thread._make_go_callbacks)


def test_the_reply_is_written_to_the_thread_it_came_from():
    """The four durable writes -- message row, ledger payload,
    correlation id, conversation context -- all take the argument."""
    src = _callbacks_source()
    body = src.split("async def on_message", 1)[1].split(
        "async def on_notify", 1)[0]
    assert "msg_id, thread_id, go_agent_id, clean_content" in body, (
        "the message row is still addressed to the closure's thread")
    assert "correlation_id=thread_id" in body
    assert "context_conversation_id=thread_id" in body


def test_nothing_in_the_callbacks_still_reaches_for_the_closure():
    """The whole defect was a name in scope that looked right. Any
    remaining use of it is either the fallback or a missed site, and
    there is exactly one legitimate use."""
    src = _callbacks_source()
    uses = re.findall(r"\bgo_thread_id\b", src)
    # One in the signature, one in the docstring's explanation, and one
    # in `_thread`'s fallback. Anything more is a site that was missed.
    assert len(uses) <= 4, (
        f"go_thread_id is still read {len(uses)} times inside the "
        f"callbacks; each one sends a reply to the wrong conversation")
    assert "return thread_id or go_thread_id" in src


def test_the_fallback_survives():
    """A caller that passes nothing used to reach the main thread and
    must keep reaching it. Silence would be a worse regression than the
    bug being fixed."""
    src = _callbacks_source()
    assert "def _thread(" in src
    assert "thread_id = _thread(thread_id)" in src


def test_the_notification_links_to_the_right_thread():
    src = _callbacks_source()
    notify = src.split("async def on_notify", 1)[1]
    assert "thread_id=thread_id" in notify
    assert "thread_id=go_thread_id" not in notify


# ── the broadcast, which was the other goal-wide surface ──────────────

def test_a_broadcast_reaches_only_that_thread():
    sent: list[tuple[str, str]] = []

    async def a(msg):
        sent.append(("a", msg))

    async def b(msg):
        sent.append(("b", msg))

    subs.subscribe("KER12", "thread-main", a)
    subs.subscribe("KER12", "thread-errand", b)

    asyncio.run(subs.broadcast("KER12", "hello", "thread-main"))
    assert sent == [("a", "hello")], (
        "the errand thread's watcher was shown the main thread's message")


def test_naming_no_thread_still_reaches_the_whole_goal():
    """The old behaviour, kept on purpose for a caller that has no
    thread to name -- a widening, never a narrowing."""
    sent: list[str] = []

    async def a(msg):
        sent.append(msg)

    async def b(msg):
        sent.append(msg)

    subs.subscribe("MARL2", "t1", a)
    subs.subscribe("MARL2", "t2", b)

    asyncio.run(subs.broadcast("MARL2", "hello"))
    assert len(sent) == 2


def test_one_goal_does_not_receive_another_goals_traffic():
    sent: list[str] = []

    async def a(msg):
        sent.append(msg)

    subs.subscribe("CAL07", "t1", a)

    asyncio.run(subs.broadcast("MARL2", "hello", "t1"))
    assert sent == []


def test_unsubscribe_forgets_the_key_too():
    """A registry that keeps empty sets for every thread a goal ever had
    grows for the life of the process."""
    async def a(msg):
        pass

    off = subs.subscribe("KER12", "gone", a)
    off()
    assert ("KER12", "gone") not in subs._subscribers


# ── the callers that had to start naming a thread ─────────────────────

def _broadcast_calls(src: str) -> list[str]:
    """Every `_broadcast_to_goal_thread(...)` CALL, argument list intact.

    A REGEX WILL NOT DO THIS, and the first version of this test proved
    it by failing on two calls that were already correct: the arguments
    contain `json.dumps({...})`, so any character class you pick either
    stops at the first bracket or runs past the closing one. Balancing
    the parentheses is the only way to see where a call ends.
    """
    out: list[str] = []
    needle = "_broadcast_to_goal_thread("
    i = src.find(needle)
    while i != -1:
        if not src[:i].rstrip().endswith("async def") and                 not src[:i].rstrip().endswith("def"):
            j = i + len(needle)
            depth = 1
            while j < len(src) and depth:
                if src[j] in "([{":
                    depth += 1
                elif src[j] in ")]}":
                    depth -= 1
                j += 1
            out.append(src[i + len(needle):j - 1])
        i = src.find(needle, i + 1)
    return out


def _top_level_args(arglist: str) -> int:
    depth, count = 0, 1
    for ch in arglist:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count if arglist.strip() else 0


def test_every_broadcast_in_the_router_names_its_thread():
    """A call left goal-wide would put GO output in every open tab, and
    it would look correct on a goal that has only one thread -- which is
    every goal in the system until the first errand is approved."""
    src = inspect.getsource(goal_thread)
    calls = _broadcast_calls(src)
    assert calls, "the guard found no calls at all -- it has gone blind"
    bad = [c for c in calls if _top_level_args(c) < 3]
    assert not bad, (
        f"{len(bad)} broadcast(s) in goal_thread.py still fan out to the "
        f"whole goal: {bad}")


def test_the_external_callers_name_a_thread():
    """Slack and the channels relay both persist into a thread they
    already know, so neither has an excuse to broadcast goal-wide.

    REWRITTEN 2026-10-16. The original asked whether a substring
    appeared ANYWHERE in the file, joined by `or` across the whole
    text -- so one correct call anywhere excused every other call, and
    a new goal-wide broadcast added below it passed. It also broke on
    reformatting, because it matched `}), go_thread_id)` literally.
    It now counts the arguments of each call, the same way the
    goal_thread test above does, so it answers the question it claims
    to ask.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    for rel in ("app/channels/base.py",
                "app/channels/slack/inbound.py",
                "app/slack/inbound.py"):
        path = root / rel
        if not path.exists():
            continue          # app/slack/* is deleted in the Slack rework
        calls = _broadcast_calls(path.read_text(encoding="utf-8"))
        bad = [c for c in calls if _top_level_args(c) < 3]
        assert not bad, (
            f"{rel}: {len(bad)} broadcast(s) fan out to the whole goal "
            f"instead of naming a thread: {bad}")


# ── one agent, many sessions: what that buys and what it costs ────────

def test_the_session_key_carries_the_thread():
    """This is the whole reason an errand thread has its own context
    window, its own compaction and its own token accounting. The gateway
    stores a SessionEntry per key -- `compactionCount` is a field on it
    -- so two threads of one agent compact independently and neither
    inherits the other's history."""
    from app.openclaw import make_goal_session_key
    a = make_goal_session_key("go-ker12", "KER12", "thread-main")
    b = make_goal_session_key("go-ker12", "KER12", "thread-errand")
    assert a != b
    assert a.endswith("-thread-main") and b.endswith("-thread-errand")


def test_the_model_is_the_agents_and_not_the_threads():
    """An errand thread must run the model the GO runs. It does by
    construction -- there is one gateway agent and the context carries
    `agents.model` -- and the point of this guard is that nobody later
    adds a per-thread model and quietly gives errands a cheaper one."""
    import inspect
    from app.routers import goal_thread
    src = inspect.getsource(goal_thread._get_go_context)
    assert 'agent.get("model"' in src
    assert "thread" not in src.split('"model"')[1][:120], (
        "the model is being resolved per thread")


def test_one_cp_session_multiplexes_them():
    """The control panel keeps ONE _AgentSession per goal and routes by
    `_runs: run_id -> thread_id`. That is not the same as one gateway
    session, and conflating them is how somebody concludes the threads
    share a context window."""
    import inspect
    from app import openclaw
    src = inspect.getsource(openclaw.SessionManager.get_or_create)
    assert "key = (pid, agent_id)" in src
    assert 'agent_type == "SHCA"' in src, (
        "the GO is on the shared-session branch; if that changed, the "
        "comment about one session per goal is no longer true")
