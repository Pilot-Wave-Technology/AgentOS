"""One channel per thread, and a binding that never changes thread.

THE BUG THIS FIXES WAS LIVE AND SILENT IN BOTH DIRECTIONS. The internal
model moved to one thread per story, but the binding held one row per
(goal, actor) with `thread_id` rotating under it -- and only the Slack
`/work` path repointed it. The web import and git-engine paths never
did. So from a member's SECOND story onward the outbound relay found no
binding and dropped the reply at DEBUG, while inbound messages routed
to the FIRST story's thread and landed in the wrong conversation.

Nothing raised. Nothing was logged above DEBUG. The only symptom was
that Slack went quiet.

The tests that matter here are the second-story ones: a model that only
ever has one thread per actor passes everything else in this file.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest

from app.channels.slack.naming import THREAD_KEY, channel_name, slugify
from app.services.tests.slack_fixtures import (FakeSlack, fake_slack,
                                                world)


def _run(coro):
    return asyncio.run(coro)


# ── the name is unique forever ───────────────────────────────────────────────

def test_the_name_is_keyed_on_the_thread_not_the_story():
    """An archived channel keeps its name reserved FOREVER, and
    `conversations.list` cannot even see archived channels -- so a
    re-imported story asking for its old name gets `name_taken` and a
    lookup that cannot explain it. A new thread means a new name."""
    a = channel_name(goal_id="GOAL1", thread_id="aaaaaaaa-1111", title="Fix login")
    b = channel_name(goal_id="GOAL1", thread_id="bbbbbbbb-2222", title="Fix login")
    assert a != b, "two threads of the same story share a channel name"


def test_the_same_title_in_two_goals_does_not_collide():
    a = channel_name(goal_id="AAA", thread_id="cccccccc", title="Fix login bug")
    b = channel_name(goal_id="BBB", thread_id="dddddddd", title="Fix login bug")
    assert a != b


def test_a_title_that_slugifies_to_nothing_still_gives_a_usable_name():
    """The old slugifier fell back to the literal "user", so twenty CJK or
    emoji titles all wanted one name."""
    for title in ("日本語のタイトル", "🎉🎉🎉", "", "   ", "---"):
        n = channel_name(goal_id="G1", thread_id="eeeeeeee", title=title)
        assert n and "user" not in n
        assert n.endswith("eeeeeeee"), f"{title!r} produced {n!r}"


def test_titles_that_truncate_together_still_differ():
    long_a = "Add retry to the publish step for the reconciler, part one"
    long_b = "Add retry to the publish step for the reconciler, part two"
    a = channel_name(goal_id="G1", thread_id="11111111", title=long_a)
    b = channel_name(goal_id="G1", thread_id="22222222", title=long_b)
    assert a != b


@pytest.mark.parametrize("title", [
    "x" * 500, "Ünïcôdé — em dash & ampersand", "a/b\\c:d*e?f", "🙂" * 50])
def test_every_name_is_legal_for_slack(title):
    n = channel_name(goal_id="GOAL1", thread_id=str(uuid.uuid4()),
                     title=title, env_suffix="feature")
    assert len(n) <= 80, f"{len(n)} chars: {n}"
    assert n == n.lower()
    assert all(c.isalnum() or c == "-" for c in n), n
    assert not n.startswith("-") and not n.endswith("-")


def test_the_thread_key_is_never_truncated_away():
    """It is the only part that makes the name unique, so a long title
    must eat the slug and never the key."""
    n = channel_name(goal_id="A" * 20, thread_id="abcdef0123456789",
                     title="t" * 300, env_suffix="feature")
    assert "abcdef01"[:THREAD_KEY] in n


def test_a_name_without_a_thread_is_refused():
    """Silently producing a name that is not unique is the whole failure."""
    with pytest.raises(ValueError):
        channel_name(goal_id="G1", thread_id="", title="whatever")


def test_the_environment_goes_at_the_front():
    """All environments share one workspace; a prefix groups them in
    Slack's channel list where a suffix scatters them."""
    n = channel_name(goal_id="GOAL1", thread_id="abcdefgh", title="x",
                     env_suffix="dev")
    assert n.startswith("dev-")


def test_production_has_no_prefix():
    n = channel_name(goal_id="GOAL1", thread_id="abcdefgh", title="x",
                     env_suffix="")
    assert n.startswith("goal1-")


def test_slugify_never_returns_something_illegal():
    for s in ("", "   ", "---", "日本語", "A B  C", "x" * 200):
        out = slugify(s, 20)
        assert out == out.lower()
        assert all(c.isalnum() or c == "-" for c in out)
        assert not out.startswith("-") and not out.endswith("-")


# ── nothing looks a channel up by name ───────────────────────────────────────

def test_nothing_ROUTES_to_a_channel_by_name():
    """`_find_channel_by_name` paged 2,000 channels and could not see
    archived ones at all. One channel per thread reaches that ceiling,
    and past it the failure is a misleading error. So where a message
    GOES is decided by the binding's channel id, never by a name.

    ONE EXEMPTION, and it is a different question. `_adopt_own_orphan`
    runs only after `conversations_create` returns `name_taken`, to ask
    "did this bot create the thing in the way?" -- which cannot be
    answered from a binding, because the orphan it is looking for is
    precisely one that has no binding. It is a recovery path, not a
    routing path: it runs once per failed create, and its answer becomes
    a binding rather than replacing one.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app/channels/slack"
    allowed = {"_adopt_own_orphan"}
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "_find_channel_by_name" in text:
            offenders.append(f"{path.name}: _find_channel_by_name is back")
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in allowed:
                continue
            if "conversations_list" in ast.dump(node):
                offenders.append(f"{path.name}:{node.name}")
    assert not offenders, (
        f"{offenders} look a channel up by name. The binding holds the "
        "channel id; the name is decoration and may be stale or renamed by "
        "hand. The only exemption is _adopt_own_orphan, which asks a "
        "different question and has no binding to ask it of.")


# ── one binding per thread, and it stays put ─────────────────────────────────

def test_two_threads_for_one_actor_get_two_bindings():
    """THE SECOND-STORY CASE. The whole bug in one assertion."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.thread_channels import ensure_channel_for_thread
            from unittest.mock import patch

            fake = FakeSlack()
            second = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by) VALUES ($1,$2,$3)",
                second, w["goal"], w["member"])

            # One target only: thread_channels imports the factory inside
            # the function, so the name it resolves is the one in client.py.
            # Patching the importing module's attribute would create
            # something nothing ever reads — and the test would pass anyway.
            with fake_slack(fake):
                ch = await ensure_channel_for_thread(
                    pool, goal_id=w["goal"], thread_id=second,
                    title="second story", actor_id=w["actor"])

            assert ch, "no channel was created for the second thread"
            n = await conn.fetchval(
                "SELECT count(*) FROM platform_channel_bindings "
                " WHERE goal_id=$1 AND archived_at IS NULL", w["goal"])
            assert n == 2, f"expected a binding per thread, found {n}"
    _run(body())


def test_the_relay_can_find_the_second_threads_binding():
    """The outbound lookup is the thing that was returning None."""
    async def body():
        async with world() as (conn, pool, w):
            from unittest.mock import patch

            from app.channels.base import get_binding_by_thread
            from app.channels.slack.thread_channels import ensure_channel_for_thread

            fake = FakeSlack()
            second = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by) VALUES ($1,$2,$3)",
                second, w["goal"], w["member"])
            with fake_slack(fake):
                await ensure_channel_for_thread(
                    pool, goal_id=w["goal"], thread_id=second, title="second",
                    actor_id=w["actor"])

            for t in (w["thread"], second):
                assert await get_binding_by_thread("slack", t, w["goal"]), (
                    f"thread {t[:8]} has no binding — its replies would be "
                    "dropped at DEBUG")
    _run(body())


def test_creating_the_same_threads_channel_twice_is_idempotent():
    async def body():
        async with world() as (conn, pool, w):
            from unittest.mock import patch

            from app.channels.slack.thread_channels import ensure_channel_for_thread
            fake = FakeSlack()
            with fake_slack(fake):
                a = await ensure_channel_for_thread(
                    pool, goal_id=w["goal"], thread_id=w["thread"], title="x")
                b = await ensure_channel_for_thread(
                    pool, goal_id=w["goal"], thread_id=w["thread"], title="x")
            assert a == b
            assert len(fake.of("conversations_create")) == 0, (
                "it created a channel for a thread that already had one")
    _run(body())


def test_slack_off_for_the_goal_means_no_channel_and_no_error():
    """The per-goal switch. Most goals have Slack off and must stay silent."""
    async def body():
        async with world(with_slack=False) as (conn, pool, w):
            from app.channels.slack.thread_channels import ensure_channel_for_thread
            assert await ensure_channel_for_thread(
                pool, goal_id=w["goal"], thread_id=w["thread"], title="x") is None
    _run(body())


def test_a_disabled_goal_integration_also_means_no_channel():
    async def body():
        async with world() as (conn, pool, w):
            await conn.execute(
                "UPDATE goal_platform_integrations SET enabled=false "
                " WHERE goal_id=$1", w["goal"])
            from app.channels.slack.thread_channels import ensure_channel_for_thread
            new = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by) VALUES ($1,$2,$3)",
                new, w["goal"], w["member"])
            assert await ensure_channel_for_thread(
                pool, goal_id=w["goal"], thread_id=new, title="x") is None
    _run(body())


def test_the_database_refuses_two_live_bindings_for_one_thread():
    """Belt to the code's braces: the invariant is enforced by an index,
    so a path that forgets to check cannot create a duplicate."""
    import asyncpg

    async def body():
        async with world() as (conn, pool, w):
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await conn.execute(
                    """INSERT INTO platform_channel_bindings
                       (goal_id, integration_id, external_channel_id,
                        resource_kind, thread_id)
                       VALUES ($1,$2::uuid,'C_OTHER','channel',$3)""",
                    w["goal"], w["integration"], w["thread"])
    _run(body())


# ── archiving follows the thread ─────────────────────────────────────────────

def test_archiving_a_thread_archives_its_channel_and_binding():
    async def body():
        async with world() as (conn, pool, w):
            from unittest.mock import patch

            from app.channels.slack.thread_channels import archive_channel_for_thread
            fake = FakeSlack()
            with fake_slack(fake):
                assert await archive_channel_for_thread(pool, w["thread"]) is True
            assert fake.of("conversations_archive"), "Slack was never told"
            assert await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings "
                " WHERE thread_id=$1", w["thread"]) is not None
    _run(body())


def test_the_binding_is_stamped_even_if_slack_refuses():
    """A channel we cannot archive is untidy. A binding left live points
    the relay at a channel nobody reads, which is worse."""
    async def body():
        async with world() as (conn, pool, w):
            from unittest.mock import patch

            from app.channels.slack.thread_channels import archive_channel_for_thread
            fake = FakeSlack(fail_with={"conversations_archive": "internal_error"})
            with fake_slack(fake):
                await archive_channel_for_thread(pool, w["thread"])
            assert await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings "
                " WHERE thread_id=$1", w["thread"]) is not None
    _run(body())


def test_deleting_a_thread_goes_through_the_archive_path():
    from app.routers import chat

    src = inspect.getsource(chat.delete_thread)
    assert "archive_channel_for_thread" in src, (
        "deleting a thread leaves its Slack channel live forever — a goal "
        "that runs fifty stories would leave fifty open channels")


# ── the queries that used to rewrite every channel an actor owned ────────────

def test_no_binding_update_is_keyed_on_the_actor_without_a_thread():
    """Five sites did `UPDATE ... WHERE goal_id AND actor_id` with no thread
    key and no LIMIT, so each rewrote every channel the member owned."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for rel in ("app/channels/slack/lifecycle.py", "app/routers/assistant.py",
                "app/channels/slack/provisioner.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"UPDATE platform_channel_bindings(.{0,420})",
                             text, re.S):
            stmt = m.group(1)
            stmt = stmt[:stmt.index('"""')] if '"""' in stmt else stmt
            if "actor_id" in stmt and "thread_id" not in stmt \
                    and "current_story_id" not in stmt:
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{rel}:{line}")
    assert not offenders, (
        f"actor-keyed binding updates with no thread key: {offenders}. Each "
        "rewrites every channel that member owns in the goal.")


def test_a_bindings_thread_is_never_changed():
    """A binding belongs to its thread for life. What can change is the
    agent behind it, after the assistant has been reaped and recreated."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    offenders = []
    for rel in ("app/channels/slack/lifecycle.py", "app/routers/assistant.py"):
        text = (root / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"UPDATE platform_channel_bindings\s+SET (.{0,300}?)WHERE",
                             text, re.S):
            # the SET clause only — `AND thread_id = $n` in a WHERE is the
            # correct shape and must not read as an offence
            if re.search(r"\bthread_id\s*=\s*\$", m.group(1)):
                offenders.append(f"{rel}:{text[:m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        f"these repoint a binding at a different thread: {offenders}")


# ── inbound routes by the binding, not by the goal ───────────────────────────

def test_go_routing_uses_the_bindings_thread():
    """N GO channels used to collapse into one thread, so a message sent
    in story B's channel appeared in the conversation about story A."""
    from app.channels.slack import inbound

    src = inspect.getsource(inbound._route_to_go)
    assert "binding" in src, "the GO route still ignores the binding"
    bind_at = src.index('binding or {}')
    goal_at = src.index("go_thread_id FROM goals")
    assert bind_at < goal_at, (
        "goals.go_thread_id is read before the binding's own thread")


# ── and the wiring, which is the part that was actually missing ─────────────

def test_opening_a_story_thread_creates_its_channel():
    """THE LIVE BUG, END TO END.

    Every test above calls `ensure_channel_for_thread` directly, so they
    prove the module works — and all of them stayed green when the call
    was removed from `ensure_story_thread`, which is precisely the defect
    being fixed. The bug was never that channel creation was broken; it
    was that two of the three paths that open a story thread never asked
    for one.

    `ensure_story_thread` is the single door every path goes through, so
    this is the assertion that the door calls it.
    """
    from unittest.mock import patch

    async def body():
        async with world() as (conn, pool, w):
            from app.services.story_threads import ensure_story_thread

            story2 = f"{w['story']}-two"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state, "
                " assigned_to) VALUES ($1,$2,'second story',$3,'in_progress',$3)",
                story2, w["goal"], w["member"])

            fake = FakeSlack()
            with fake_slack(fake):
                tid = await ensure_story_thread(
                    pool, w["member"], w["goal"], "agent-x", story2)

            assert tid, "no thread was opened"
            assert await conn.fetchval(
                "SELECT count(*) FROM platform_channel_bindings "
                " WHERE thread_id=$1 AND archived_at IS NULL", tid) == 1, (
                "a story thread was opened with no Slack channel — this is "
                "the bug: the relay will find no binding and drop every "
                "reply at DEBUG")
            assert fake.of("conversations_create"), "Slack was never asked"
            assert fake.of("conversations_create")[0]["is_private"] is True, (
                "the channel is public — it must be visible only to the "
                "people in its thread")
    _run(body())


def test_a_slack_failure_does_not_stop_the_thread_being_opened():
    """Slack is a mirror. A workspace that is down must not block work."""
    from unittest.mock import patch

    async def body():
        async with world() as (conn, pool, w):
            from app.services.story_threads import ensure_story_thread

            story2 = f"{w['story']}-three"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state, "
                " assigned_to) VALUES ($1,$2,'third',$3,'in_progress',$3)",
                story2, w["goal"], w["member"])

            fake = FakeSlack(fail_with={"conversations_create": "internal_error"})
            with fake_slack(fake):
                tid = await ensure_story_thread(
                    pool, w["member"], w["goal"], "agent-x", story2)
            assert tid, "a Slack failure prevented the thread from opening"
    _run(body())


def test_every_binding_records_the_name_slack_actually_gave_it():
    """The goal binding's INSERT predated the column, so it alone stored
    NULL — seen on feature, where every thread channel had a name and the
    goal channel did not. Nothing ROUTES by name, so this is not a
    correctness bug; it is the one column that lets a person trace a
    channel back to what created it, which is exactly what you want at
    the moment something has gone wrong."""
    import ast
    import inspect

    from app.channels.slack import provisioner

    src = inspect.getsource(provisioner)
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        sql = node.value
        if "INSERT INTO platform_channel_bindings" not in sql:
            continue
        assert "channel_name" in sql, (
            "a binding is inserted without the name Slack gave the channel:\n"
            + sql.strip()[:300])
