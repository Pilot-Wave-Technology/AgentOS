"""A channel shows what its thread shows, and keeps showing only that.

THE RULE HAS TWO MODES and Slack has to reproduce both, because a
channel that mirrors the wrong one either hides a conversation from
people entitled to it or shows a private one to the whole goal
(`app/routers/chat.py:426-437`):

    participants EMPTY -> every member of the goal
    participants SET   -> those uids plus created_by, nobody else

TWO TRAPS, both of which produce a plausible-looking wrong answer.

`thread_participants` is NOT the access list -- it is the unread
counter cache, and its own migration backfills it with every goal
member for GO threads. Reading it would put the whole goal into every
GO channel while looking entirely reasonable.

And membership DECAYS. Any human in a private channel can invite
another human and Slack cannot forbid it, so a rule enforced only at
creation is not enforced. The reconcile tests are the ones that matter
most here.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid

from app.channels.slack.membership import expected_uids, sync_channel_members
from app.services.tests.slack_fixtures import (FakeSlack, fake_slack,
                                                world)


def _run(coro):
    return asyncio.run(coro)


# ── who is entitled ──────────────────────────────────────────────────────────

def test_an_empty_participant_list_means_the_whole_goal():
    async def body():
        async with world() as (conn, pool, w):
            uids = await expected_uids(pool, w["thread"])
            assert w["member"] in uids
            assert w["outsider"] not in uids, (
                "somebody with no role on the goal is entitled to its thread")
    _run(body())


def test_a_set_participant_list_means_only_those_people():
    async def body():
        async with world(participants=["ghost"]) as (conn, pool, w):
            await conn.execute(
                "INSERT INTO goal_members (goal_id, uid, role, added_by) "
                "VALUES ($1,$2,'member',$2) ON CONFLICT DO NOTHING",
                w["goal"], w["outsider"])
            # outsider is now in the GOAL but not on the THREAD
            uids = await expected_uids(pool, w["thread"])
            assert w["outsider"] not in uids, (
                "a goal member who is not on a restricted thread can see it")
    _run(body())


def test_the_creator_is_always_entitled_to_a_restricted_thread():
    """They opened it. Dropping them is the obvious off-by-one."""
    async def body():
        async with world(participants=["someone-else"]) as (conn, pool, w):
            assert w["member"] in await expected_uids(pool, w["thread"]), (
                "the thread's creator lost access to their own thread")
    _run(body())


def test_leaving_the_goal_removes_you_even_if_you_are_named_on_the_thread():
    async def body():
        async with world(participants=[]) as (conn, pool, w):
            await conn.execute(
                "UPDATE threads SET participants = $1 WHERE id = $2",
                [w["member"], w["outsider"]], w["thread"])
            uids = await expected_uids(pool, w["thread"])
            assert w["outsider"] not in uids, (
                "goal membership must gate both modes")
    _run(body())


def test_a_thread_that_does_not_exist_entitles_nobody():
    async def body():
        async with world() as (conn, pool, w):
            assert await expected_uids(pool, str(uuid.uuid4())) == set()
    _run(body())


def test_the_unread_cache_is_not_consulted():
    """`thread_participants` is seeded with every goal member for GO
    threads. Using it would be a quiet, total leak."""
    import ast

    from app.channels.slack import membership

    # Docstrings AND comments are stripped: this module's docstring names
    # the trap on purpose, and a guard that cannot tell an explanation
    # from an instruction is a guard that forbids explaining things.
    tree = ast.parse(inspect.getsource(membership))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "thread_participants" not in code, (
        "visibility is being read from the unread-counter cache, which is "
        "backfilled with every goal member")


# ── and the channel is made to match ─────────────────────────────────────────

def test_an_entitled_person_is_invited():
    async def body():
        async with world() as (conn, pool, w):
            fake = FakeSlack()
            fake.members = []
            with fake_slack(fake):
                out = await sync_channel_members(pool, w["thread"])
            assert w["member"] in out["invited"]
            assert fake.of("conversations_invite")
    _run(body())


def test_somebody_who_should_not_be_there_is_removed():
    """THE DECAY CASE. A human invited another human; Slack allowed it."""
    async def body():
        async with world() as (conn, pool, w):
            fake = FakeSlack()
            fake.members = ["U_MEMBER", "U_STRANGER"]
            with fake_slack(fake):
                out = await sync_channel_members(pool, w["thread"])
            assert "U_STRANGER" in out["kicked"]
            assert "U_MEMBER" not in out["kicked"], (
                "it removed somebody who was entitled to be there")
    _run(body())


def test_a_removal_is_never_silent():
    async def body():
        async with world() as (conn, pool, w):
            fake = FakeSlack()
            fake.members = ["U_STRANGER"]
            with fake_slack(fake):
                await sync_channel_members(pool, w["thread"])
            posted = " ".join(c.get("text", "") for c in fake.of("chat_postMessage"))
            assert "Removed" in posted, (
                "somebody lost access with no notice in the channel")
    _run(body())


def test_invite_only_mode_removes_nobody():
    """The add-participant path knows somebody was ADDED; walking the
    channel to evict strangers is the reconciler's job, not the request's."""
    async def body():
        async with world() as (conn, pool, w):
            fake = FakeSlack()
            fake.members = ["U_STRANGER"]
            with fake_slack(fake):
                out = await sync_channel_members(pool, w["thread"], kick=False)
            assert out["kicked"] == []
            assert not fake.of("conversations_kick")
    _run(body())


def test_the_bot_is_never_kicked_from_its_own_channel():
    async def body():
        async with world() as (conn, pool, w):
            await conn.execute(
                """UPDATE platform_integrations
                      SET credentials_json = $1 WHERE platform='slack'""",
                '{"bot_token": "xoxb-fake", "workspace_id": "T_FAKE",'
                ' "bot_user_id": "U_BOT"}')
            fake = FakeSlack()
            fake.members = ["U_BOT", "U_MEMBER"]
            with fake_slack(fake):
                out = await sync_channel_members(pool, w["thread"])
            assert "U_BOT" not in out["kicked"], (
                "the bot removed itself; the channel is now unreachable")
    _run(body())


def test_an_unmapped_person_is_counted_rather_than_guessed_at():
    """Matching by email here would bypass platform_user_map's review
    state, which exists so a half-matched identity cannot be given access."""
    async def body():
        async with world() as (conn, pool, w):
            await conn.execute("DELETE FROM platform_user_map")
            fake = FakeSlack()
            fake.members = []
            with fake_slack(fake):
                out = await sync_channel_members(pool, w["thread"])
            assert out["invited"] == []
            assert out["skipped"] >= 1
    _run(body())


def test_a_slack_failure_reports_rather_than_raising():
    async def body():
        async with world() as (conn, pool, w):
            fake = FakeSlack(fail_with={"conversations_members": "internal_error"})
            with fake_slack(fake):
                out = await sync_channel_members(pool, w["thread"])
            assert out["error"]
    _run(body())


def test_a_goal_with_slack_off_syncs_nothing():
    async def body():
        async with world(with_slack=False) as (conn, pool, w):
            out = await sync_channel_members(pool, w["thread"])
            assert out == {"invited": [], "kicked": [], "skipped": 0, "error": ""}
    _run(body())


# ── the reconcile pass ───────────────────────────────────────────────────────

def test_the_reconcile_pass_walks_live_bindings_and_fixes_them():
    async def body():
        async with world() as (conn, pool, w):
            from app.services import slack_membership_reconcile as rec
            fake = FakeSlack()
            fake.members = ["U_STRANGER"]
            with fake_slack(fake):
                out = await rec.reconcile_once(pool)
            assert out["checked"] >= 1
            assert out["removed"] >= 1
    _run(body())


def test_the_reconcile_pass_skips_archived_and_disabled():
    async def body():
        async with world() as (conn, pool, w):
            from app.services import slack_membership_reconcile as rec
            await conn.execute(
                "UPDATE goal_platform_integrations SET enabled=false "
                " WHERE goal_id=$1", w["goal"])
            fake = FakeSlack()
            fake.members = ["U_STRANGER"]
            with fake_slack(fake):
                out = await rec.reconcile_once(pool)
            assert out["checked"] == 0, (
                "it reconciled a goal that has Slack switched off")
    _run(body())


def test_one_bad_channel_does_not_end_the_pass():
    """The next channel may be the one with a stranger in it."""
    async def body():
        async with world() as (conn, pool, w):
            from unittest.mock import patch

            from app.services import slack_membership_reconcile as rec

            calls = []

            async def _explode(pool_, thread_id, **kw):
                calls.append(thread_id)
                raise RuntimeError("boom")

            with patch("app.channels.slack.membership.sync_channel_members",
                       _explode):
                out = await rec.reconcile_once(pool)
            assert calls, "the pass never reached a channel"
            assert out["checked"] == 0
    _run(body())


def test_the_pass_retires_a_channel_that_should_never_have_existed():
    """Exclusion only applied to NEW threads, which left the old ones.

    The councils and the agent mesh were excluded from channel creation
    after three of their channels already existed on feature. Nothing
    else revisits a binding once it is made, so without this the rule
    is "no new ones" and the channels it exists to prevent stay up.
    """
    async def body():
        async with world() as (conn, pool, w):
            from app.services import slack_membership_reconcile as rec
            # Make the bound thread the goal's council thread.
            await conn.execute(
                "UPDATE goals SET council_thread_id=$2 WHERE id=$1",
                w["goal"], w["thread"])
            fake = FakeSlack()
            with fake_slack(fake):
                out = await rec.reconcile_once(pool)

            assert out["retired"] == 1, (
                "an excluded thread kept its channel; the exclusion only "
                "ever applied to threads created after it landed")
            assert out["checked"] == 0, (
                "it reconciled members of a channel it was retiring")
            archived = await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings "
                " WHERE thread_id=$1", w["thread"])
            assert archived is not None, "the binding is still live"
    _run(body())


def test_an_ordinary_channel_is_not_retired():
    """The predicate is shared with creation; a story thread must pass."""
    async def body():
        async with world() as (conn, pool, w):
            from app.services import slack_membership_reconcile as rec
            fake = FakeSlack()
            with fake_slack(fake):
                out = await rec.reconcile_once(pool)
            assert out["retired"] == 0, (
                "a normal thread was retired — the exclusion predicate is "
                "matching far too much")
            assert out["checked"] >= 1
    _run(body())


def test_a_finished_story_loses_its_channels():
    """One channel per thread means a goal collects one dead
    conversation per story for ever unless something retires them."""
    async def body():
        async with world() as (conn, pool, w):
            from app.services.slack_retire import sweep
            story = await conn.fetchval(
                "SELECT story_id FROM threads WHERE id=$1", w["thread"])
            if not story:
                story = "rt-" + w["thread"][:6]
                await conn.execute(
                    "INSERT INTO stories (id, goal_id, title, state) "
                    "VALUES ($1,$2,'t','done') "
                    "ON CONFLICT (id) DO UPDATE SET state='done'",
                    story, w["goal"])
                await conn.execute(
                    "UPDATE threads SET story_id=$2 WHERE id=$1",
                    w["thread"], story)
            else:
                await conn.execute(
                    "UPDATE stories SET state='done' WHERE id=$1", story)

            fake = FakeSlack()
            with fake_slack(fake):
                n = await sweep(pool)
            assert n == 1, "a finished story kept its Slack channel"
            assert await conn.fetchval(
                "SELECT archived_at FROM platform_channel_bindings "
                " WHERE thread_id=$1", w["thread"]) is not None
    _run(body())


def test_a_live_story_keeps_its_channel():
    """The sweep must not take the channel of work in progress."""
    async def body():
        async with world() as (conn, pool, w):
            from app.services.slack_retire import sweep
            story = await conn.fetchval(
                "SELECT story_id FROM threads WHERE id=$1", w["thread"])
            if story:
                await conn.execute(
                    "UPDATE stories SET state='in_progress' WHERE id=$1",
                    story)
            fake = FakeSlack()
            with fake_slack(fake):
                assert await sweep(pool) == 0
    _run(body())


def test_both_of_a_storys_threads_are_retired_not_just_the_active_one():
    """A story has the assistant's thread AND the GO's narration one.
    Retiring only `active_thread_id` leaves half the clutter."""
    import inspect as _i

    from app.services import slack_retire
    full = _i.getsource(slack_retire.retire_story)
    # PAST THE DOCSTRING. It explains the defect by name, so a naive
    # substring search over the whole function matches its own warning.
    src = full.split('"""', 2)[-1]
    assert "active_thread_id" not in src, (
        "retire_story keys off active_thread_id, so the GO's narration "
        "channel survives every finished story")
    assert "t.story_id = $1" in src


def test_the_transition_does_not_wait_on_slack():
    """record_state writes a hash-chained ledger row; a workspace being
    unreachable must not fail the transition that already happened."""
    import inspect as _i

    from app.services import story_state_ledger
    src = _i.getsource(story_state_ledger.record_state)
    i = src.index("append_event")
    j = src.index("retire_story")
    assert i < j, "the Slack call happens before the ledger write"
    assert "create_task" in src[j - 400:j + 200], (
        "the retire call is awaited inline; Slack being down would fail "
        "a state transition")


def test_the_loop_is_registered_where_it_will_actually_run():
    """A loop registered only in main.py runs NOWHERE on an environment
    with a separate scheduler, which is what dev is."""
    from app.services.scheduler import registry

    src = inspect.getsource(registry)
    assert "slack_membership_reconcile" in src, (
        "the reconcile loop is not in the scheduler registry, so on any "
        "environment with AGENTOS_SCHEDULER_LOOPS=0 it never runs")


# ── nothing is created public ────────────────────────────────────────────────

def test_a_thread_channel_is_created_private():
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.thread_channels import ensure_channel_for_thread
            new = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by) VALUES ($1,$2,$3)",
                new, w["goal"], w["member"])
            fake = FakeSlack()
            with fake_slack(fake):
                await ensure_channel_for_thread(
                    pool, goal_id=w["goal"], thread_id=new, title="x")
            assert fake.of("conversations_create")[0]["is_private"] is True
    _run(body())


def test_the_goal_channel_is_created_private_too():
    """It was public, so anyone in the workspace could read a goal's
    conversation — including people with no role on it."""
    from app.channels.slack import lifecycle

    src = inspect.getsource(lifecycle.setup_goal_channels)
    assert "create_private_channel" in src, (
        "the goal channel is still created public, and Slack has no "
        "public-API way to convert one afterwards")


def test_the_orchestrator_half_path_is_gone():
    """Its provisioning had been commented out for some time while its
    readers stayed live — a feature in the code that did nothing."""
    from app.channels.slack import lifecycle

    src = inspect.getsource(lifecycle.setup_goal_channels)
    assert "orch_slug" not in src


# ── what never gets a channel ──────────────────────────────────────────────

def test_the_councils_and_the_agent_mesh_get_no_slack_channel():
    """They are read-only machine logs — councils reporting findings,
    agents coordinating with each other. Nobody answers them in Slack,
    so a channel each is three more per goal that exist to be muted."""
    import uuid

    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.thread_channels import (
                ensure_channel_for_thread, is_slack_excluded)

            named = {}
            for col in ("orchestrator_thread_id", "council_thread_id",
                        "security_thread_id"):
                tid = str(uuid.uuid4())
                await conn.execute(
                    "INSERT INTO threads (id, goal_id, created_by) "
                    "VALUES ($1,$2,$3)", tid, w["goal"], w["member"])
                await conn.execute(
                    f"UPDATE goals SET {col} = $1 WHERE id = $2",
                    tid, w["goal"])
                named[col] = tid

            fake = FakeSlack()
            for col, tid in named.items():
                assert await is_slack_excluded(pool, w["goal"], tid), col
                with fake_slack(fake):
                    got = await ensure_channel_for_thread(
                        pool, goal_id=w["goal"], thread_id=tid, title=col)
                assert got is None, f"{col} was given a channel"
            assert not fake.of("conversations_create")
    _run(body())


def test_the_GO_thread_itself_is_NOT_excluded():
    """The other half. The GO thread is the conversation with the Goal
    Orchestrator and one of the main reasons for having Slack at all —
    excluding it along with its three siblings would be an easy and
    very quiet mistake."""
    import uuid

    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.thread_channels import is_slack_excluded

            go = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by) VALUES ($1,$2,$3)",
                go, w["goal"], w["member"])
            await conn.execute(
                "UPDATE goals SET go_thread_id = $1 WHERE id = $2", go, w["goal"])
            assert await is_slack_excluded(pool, w["goal"], go) is False
    _run(body())


def test_the_back_fill_does_not_offer_them_either():
    import uuid

    async def body():
        async with world() as (conn, pool, w):
            from app.services.slack_backfill import live_threads

            tid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by) VALUES ($1,$2,$3)",
                tid, w["goal"], w["member"])
            await conn.execute(
                "UPDATE goals SET council_thread_id = $1 WHERE id = $2",
                tid, w["goal"])
            assert tid not in {t["id"] for t in await live_threads(pool, w["goal"])}
    _run(body())


# ── and a new channel is populated at once, not in fifteen minutes ─────────

def test_a_new_channel_has_its_people_in_it_immediately():
    """Creating the channel and leaving it empty was the whole of
    `ensure_channel_for_thread` until now: the bot sat alone in a private
    channel and only the reconcile pass would ever add anybody, up to
    fifteen minutes later. A story imported to its assignee produced a
    channel that assignee could not even see, because Slack does not
    show a non-member a private channel."""
    import uuid

    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.thread_channels import ensure_channel_for_thread

            tid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, created_by, participants) "
                "VALUES ($1,$2,$3,$4)", tid, w["goal"], w["member"],
                [w["member"]])
            fake = FakeSlack()
            fake.members = []
            with fake_slack(fake):
                assert await ensure_channel_for_thread(
                    pool, goal_id=w["goal"], thread_id=tid, title="x")
            invited = [c.get("users") for c in fake.of("conversations_invite")]
            assert invited, (
                "the channel was created empty — the person it is for cannot "
                "see it until the reconcile pass runs")
            assert "U_MEMBER" in invited
    _run(body())
