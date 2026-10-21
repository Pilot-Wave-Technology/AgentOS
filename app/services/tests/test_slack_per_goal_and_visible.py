"""Slack is per goal, switchable any time, and what happens in it is visible.

MOST OF THIS ALREADY WORKED, which is worth saying because the tests
would otherwise look like they are testing new code.
`goal_platform_integrations.enabled` has always been per goal, every
lookup joins on it, and flipping it stops or starts the relay at once.
Inbound Slack messages have always been inserted into `messages` with
a `platform` column, and Slack button actions have always resolved the
clicker to a real uid, so the ledger attributes the human and not
`system`.

FOUR THINGS DID NOT WORK, and they are what these tests are for:

  * Turning Slack on LATE created channels for the threads that
    existed at that moment and none of the ones already open, so a
    goal switched on after twenty stories kept twenty invisible
    conversations.
  * Turning it off left the channels live, holding their names.
  * `messages.platform` was written and read by nothing, so a message
    typed in Slack rendered exactly like one typed in a browser.
  * A Slack button's reply is ephemeral, so the panel showed a card
    changing with no record of who decided it, or where.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid

from app.services.tests.slack_fixtures import FakeSlack, fake_slack, world


def _run(coro):
    return asyncio.run(coro)


# ── the switch, which already worked ─────────────────────────────────────────

def test_a_goal_with_no_row_relays_nothing():
    """Off by default. Most goals never want Slack and must stay silent."""
    async def body():
        async with world(with_slack=False) as (conn, pool, w):
            from app.channels.base import get_binding_by_thread
            assert await get_binding_by_thread("slack", w["thread"],
                                               w["goal"]) is None
    _run(body())


def test_disabling_stops_the_relay_immediately():
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.base import get_binding_by_thread
            assert await get_binding_by_thread("slack", w["thread"], w["goal"])
            await conn.execute(
                "UPDATE goal_platform_integrations SET enabled=false "
                " WHERE goal_id=$1", w["goal"])
            assert await get_binding_by_thread("slack", w["thread"],
                                               w["goal"]) is None
    _run(body())


def test_disabling_keeps_the_binding_so_re_enabling_reuses_the_channel():
    """Archiving on disable would burn the name -- an archived channel
    keeps its name reserved for ever -- so every toggle would cost a
    fresh channel. Same policy deprovision_member already follows."""
    async def body():
        async with world() as (conn, pool, w):
            await conn.execute(
                "UPDATE goal_platform_integrations SET enabled=false "
                " WHERE goal_id=$1", w["goal"])
            assert await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings "
                " WHERE goal_id=$1", w["goal"]) is None

            await conn.execute(
                "UPDATE goal_platform_integrations SET enabled=true "
                " WHERE goal_id=$1", w["goal"])
            from app.channels.base import get_binding_by_thread
            again = await get_binding_by_thread("slack", w["thread"], w["goal"])
            assert again and again["external_channel_id"] == w["channel"], (
                "re-enabling made a new channel instead of reusing the one "
                "that was already there")
    _run(body())


# ── turning it on late ───────────────────────────────────────────────────────

def test_enabling_late_back_fills_the_threads_already_open():
    """THE GAP. Provisioning made the channels that existed at that
    moment; threads already open got nothing and stayed invisible."""
    async def body():
        async with world() as (conn, pool, w):
            from app.services.slack_backfill import backfill_goal

            # three live threads with no channel, as a goal switched on
            # after a few stories would have
            for n in range(3):
                tid = str(uuid.uuid4())
                sid = f"{w['story']}-bf{n}"
                await conn.execute(
                    "INSERT INTO stories (id, goal_id, title, created_by, "
                    " state, assigned_to) VALUES ($1,$2,$3,$4,'in_progress',$4)",
                    sid, w["goal"], f"backfilled {n}", w["member"])
                await conn.execute(
                    "INSERT INTO threads (id, goal_id, created_by, story_id) "
                    "VALUES ($1,$2,$3,$4)", tid, w["goal"], w["member"], sid)

            import app.services.slack_backfill as bf
            bf._GAP_SECONDS = 0          # the pacing is tested separately
            with fake_slack() as fake:
                out = await backfill_goal(pool, w["goal"])

            assert out["created"] == 3, f"back-filled {out}"
            assert len(fake.of("conversations_create")) == 3
            assert await conn.fetchval(
                "SELECT count(*) FROM platform_channel_bindings "
                " WHERE goal_id=$1 AND archived_at IS NULL", w["goal"]) == 4
    _run(body())


def test_back_filling_twice_creates_nothing_the_second_time():
    """Somebody will toggle it twice."""
    async def body():
        async with world() as (conn, pool, w):
            import app.services.slack_backfill as bf
            bf._GAP_SECONDS = 0
            # A thread with a LIVE STORY: the back-fill deliberately
            # ignores dormant story-less ones, so a test built on those
            # would back-fill nothing and prove nothing.
            sid = f"{w['story']}-twice"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state) "
                "VALUES ($1,$2,'twice',$3,'in_progress')", sid, w["goal"],
                w["member"])
            tid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, story_id) "
                "VALUES ($1,$2,$3,$4)", tid, w["goal"], w["member"], sid)
            with fake_slack():
                first = await bf.backfill_goal(pool, w["goal"])
            with fake_slack() as fake2:
                second = await bf.backfill_goal(pool, w["goal"])
            assert first["created"] == 1
            assert second["created"] == 0
            assert not fake2.of("conversations_create")
    _run(body())


def test_finished_stories_are_not_back_filled():
    """A channel for a story finished months ago would be created only to
    be archived, and would spend the rate limit the live ones need."""
    async def body():
        async with world() as (conn, pool, w):
            import app.services.slack_backfill as bf
            bf._GAP_SECONDS = 0
            done_story = f"{w['story']}-old"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state, "
                " assigned_to) VALUES ($1,$2,'old',$3,'done',$3)",
                done_story, w["goal"], w["member"])
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, story_id) "
                "VALUES ($1,$2,$3,$4)", str(uuid.uuid4()), w["goal"],
                w["member"], done_story)
            with fake_slack() as fake:
                out = await bf.backfill_goal(pool, w["goal"])
            assert out["created"] == 0
            assert not fake.of("conversations_create")
    _run(body())


def test_the_back_fill_is_paced():
    """conversations.create is Tier-2, ~20/min. Twenty threads at once is
    exactly the burst that finds that out."""
    from app.services import slack_backfill

    src = inspect.getsource(slack_backfill.backfill_goal)
    assert "sleep" in src, (
        "the back-fill creates channels as fast as it can and will be "
        "rate-limited on any goal large enough to need it")


def test_enabling_slack_triggers_the_back_fill():
    """The module working is not the same as anything calling it."""
    from app.routers import goals

    src = inspect.getsource(goals)
    enable = src[src.index("async def enable_channel"):] \
        if "async def enable_channel" in src else src
    assert "backfill_goal" in enable[:6000], (
        "turning Slack on for a goal does not back-fill the threads it "
        "already has")


def test_a_slack_failure_during_back_fill_does_not_lose_the_rest():
    async def body():
        async with world() as (conn, pool, w):
            import app.services.slack_backfill as bf
            bf._GAP_SECONDS = 0
            for n in range(2):
                sid = f"{w['store' if False else 'story']}-fail{n}"
                await conn.execute(
                    "INSERT INTO stories (id, goal_id, title, created_by, "
                    " state) VALUES ($1,$2,'f',$3,'in_progress')",
                    sid, w["goal"], w["member"])
                await conn.execute(
                    "INSERT INTO threads (id, goal_id, created_by, story_id) "
                    "VALUES ($1,$2,$3,$4)", str(uuid.uuid4()), w["goal"],
                    w["member"], sid)
            with fake_slack(FakeSlack(
                    fail_with={"conversations_create": "internal_error"})):
                out = await bf.backfill_goal(pool, w["goal"])
            assert out["failed"] == 2 and out["error"] == "", (
                "one failure ended the pass")
    _run(body())


# ── visible in the cloud ─────────────────────────────────────────────────────

def test_an_inbound_slack_message_carries_its_platform():
    async def body():
        async with world() as (conn, pool, w):
            msg_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO messages (id, thread_id, sender, role, content, "
                " token_count, platform) VALUES ($1,$2,$3,'human','hi',0,'slack')",
                msg_id, w["thread"], w["member"])
            assert await conn.fetchval(
                "SELECT platform FROM messages WHERE id=$1", msg_id) == "slack"
    _run(body())


def test_the_panel_queries_actually_select_platform():
    """It was written and read by NOTHING: not selected in either message
    query, not rendered in either template. So a Slack message looked
    exactly like a browser one."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    gt = (root / "app/routers/goal_thread.py").read_text(encoding="utf-8")
    assert "m.platform" in gt, "the goal thread query drops platform"

    chat = (root / "app/routers/chat.py").read_text(encoding="utf-8")
    assert "SELECT m.*" in chat, (
        "chat.py no longer selects every column, so platform may be gone")


def test_both_message_templates_render_it():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app/templates"
    for rel in ("chat/_message.html", "goal_thread/_message.html"):
        body = (root / rel).read_text(encoding="utf-8")
        assert "msg.platform" in body, f"{rel} does not show where a message came from"


# ── a decision made in Slack leaves a trace ─────────────────────────────────

def test_a_slack_decision_is_written_into_the_thread():
    """The button's reply is ephemeral — visible to the presser, in Slack,
    never persisted. Without this the panel shows a card changing and no
    record of who decided it."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.audit import record_decision
            msg_id = await record_decision(
                pool, thread_id=w["thread"], user_uid=w["member"],
                action="Approved", detail="looks right")
            assert msg_id
            row = await conn.fetchrow(
                "SELECT content, platform, role FROM messages WHERE id=$1",
                msg_id)
            assert "Approved" in row["content"]
            assert "from Slack" in row["content"]
            assert row["platform"] == "slack"
            assert row["role"] == "agent", (
                "a human role would make this look like something the "
                "person typed")
    _run(body())


def test_the_trace_carries_no_card_so_it_is_not_posted_back_to_slack():
    """A card would re-enter the fan-out and echo into the very channel
    it describes."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.audit import record_decision
            with fake_slack() as fake:
                await record_decision(pool, thread_id=w["thread"],
                                      user_uid=w["member"], action="Approved")
            assert await conn.fetchval(
                "SELECT count(*) FROM messages WHERE thread_id=$1 "
                "  AND card_data IS NOT NULL", w["thread"]) == 0
            assert not fake.of("chat_postMessage"), (
                "the audit line was posted back into Slack")
    _run(body())


def test_recording_a_decision_never_raises():
    """It runs after the decision is committed, so failing here must not
    look like the decision failed."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.audit import record_decision
            assert await record_decision(pool, thread_id="", user_uid="x",
                                         action="Approved") is None
    _run(body())


def test_the_go_button_handlers_record_what_they_did():
    from app.channels.slack import go_actions

    for fn in (go_actions.handle_park, go_actions.handle_errand):
        src = inspect.getsource(fn)
        assert "record_decision" in src, (
            f"{fn.__name__} decides something and leaves no trace in the "
            "thread — the ledger has it, but that is not what a person reads")


def test_a_slack_action_is_attributed_to_the_human_not_to_system():
    """Already true, pinned so a refactor cannot regress it: the handlers
    pass a real uid into the services, which is what keeps the ledger
    from saying `system` did it."""
    from app.channels.slack import go_actions

    for fn, kw in ((go_actions.handle_park, "human_uid=user_uid"),
                   (go_actions.handle_errand, "user_uid")):
        assert kw in inspect.getsource(fn), (
            f"{fn.__name__} no longer passes the acting human through")


def test_the_back_fill_ignores_dormant_legacy_threads():
    """THE BLAST-RADIUS CHECK.

    The first version took every live thread in the goal, which sounded
    right and was not: the old model gave each member one persistent
    (user, goal) thread and accumulated ad-hoc ones beside it, all still
    there and all dormant. Measured on feature before anybody ran it,
    one goal would have created 129 channels, none of them story
    threads — 129 Tier-2 creates and 129 names reserved for ever,
    because Slack never releases an archived channel's name.
    """
    import uuid as _uuid

    async def body():
        async with world() as (conn, pool, w):
            from app.services.slack_backfill import live_threads

            # a dormant thread with no story, as the old model left behind
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by) VALUES ($1,$2,$3)",
                str(_uuid.uuid4()), w["goal"], w["member"])
            # a thread for a FINISHED story
            done = f"{w['story']}-fin"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state) "
                "VALUES ($1,$2,'finished',$3,'done')", done, w["goal"], w["member"])
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, story_id) "
                "VALUES ($1,$2,$3,$4)", str(_uuid.uuid4()), w["goal"],
                w["member"], done)
            # and one that SHOULD be back-filled
            live = f"{w['story']}-live"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state) "
                "VALUES ($1,$2,'live',$3,'in_progress')", live, w["goal"],
                w["member"])
            wanted = str(_uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, story_id) "
                "VALUES ($1,$2,$3,$4)", wanted, w["goal"], w["member"], live)

            got = {t["id"] for t in await live_threads(pool, w["goal"])}
            assert wanted in got, "a live story's thread was not back-filled"
            assert len(got) == 1, (
                f"the back-fill picked up {len(got)} threads; dormant and "
                "finished ones must be left alone")
    _run(body())


def test_the_goals_own_named_threads_are_back_filled():
    """The GO thread and its siblings are real conversations with no
    story, so the story filter must not exclude them."""
    async def body():
        async with world() as (conn, pool, w):
            from app.services.slack_backfill import live_threads
            go_thread = await conn.fetchval(
                "SELECT go_thread_id FROM goals WHERE id=$1", w["goal"])
            if not go_thread:
                import uuid as _uuid
                go_thread = str(_uuid.uuid4())
                await conn.execute(
                    "INSERT INTO threads (id, goal_id, created_by) "
                    "VALUES ($1,$2,$3)", go_thread, w["goal"], w["member"])
                await conn.execute(
                    "UPDATE goals SET go_thread_id=$1 WHERE id=$2",
                    go_thread, w["goal"])
            got = {t["id"] for t in await live_threads(pool, w["goal"])}
            assert go_thread in got, (
                "the goal's own GO thread would never get a channel")
    _run(body())


def test_a_deleted_goal_is_never_back_filled():
    """Goal delete is soft and does not cascade to threads, so a deleted
    goal keeps every thread it ever had. On feature that is 145 of 147
    goals, the largest holding 129 threads — so the one thing that could
    make the back-fill catastrophic is a goal nobody uses any more."""
    import uuid as _uuid

    async def body():
        async with world() as (conn, pool, w):
            from app.services.slack_backfill import live_threads

            sid = f"{w['story']}-gone"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state) "
                "VALUES ($1,$2,'x',$3,'in_progress')", sid, w["goal"],
                w["member"])
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, story_id) "
                "VALUES ($1,$2,$3,$4)", str(_uuid.uuid4()), w["goal"],
                w["member"], sid)
            assert await live_threads(pool, w["goal"]), "nothing to test with"

            await conn.execute(
                "UPDATE goals SET status='deleted' WHERE id=$1", w["goal"])
            assert await live_threads(pool, w["goal"]) == [], (
                "a deleted goal would still be given Slack channels")
    _run(body())
