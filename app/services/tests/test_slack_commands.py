"""The commands match what the GO and the assistant actually do.

TWO FAULTS, AND THE SECOND IS THE LARGER.

The commands that existed had drifted. `/story work` promised "your
personal assistant channel for a specific story" -- singular, one
channel per member, the story rotating through it -- which is the
model one-channel-per-thread replaces. The state vocabulary knew
seven states and the schema has nine, so a story entering
`awaiting_merge` announced itself with the raw column value.

And the whole execution layer had NO command. Executions, legs,
steps, parks, approvals: the place where work actually happens was
reachable from a browser and nowhere else. Nine capabilities the
panel exposes had no slash command, and the three that existed as
buttons were inert in production.

The tests that matter most here are the parametrised ones at the
bottom: a new command gets covered without anybody remembering to
write a test for it.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.channels.slack.commands.registry import COMMANDS, lookup
from app.services.tests.slack_fixtures import fake_slack, world


def _run(coro):
    return asyncio.run(coro)


# ── the execution layer is reachable at all ──────────────────────────────────

@pytest.mark.parametrize("name", ["run", "park", "errand"])
def test_the_execution_commands_exist(name):
    assert lookup(name) is not None, (
        f"/{name} is not registered, so that part of the execution layer is "
        "still reachable only from a browser")


def test_every_human_facing_kernel_endpoint_has_a_command_or_an_excuse():
    """THE COVERAGE RULE. Walks the kernel routers for human-facing routes
    and fails on one with nothing in Slack that reaches it."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3] / "app/routers"
    routes = set()
    for name in ("kernel_park.py", "kernel_control.py", "kernel_errands.py"):
        text = (root / name).read_text(encoding="utf-8")
        for m in re.finditer(r'@router\.post\(\s*[^"\']*["\']([^"\']+)["\']', text):
            routes.add(m.group(1).rstrip("/").rsplit("/", 1)[-1])

    # What each is reachable by, or why it deliberately is not.
    covered = {
        "decide": "/park and the kernel_park card buttons",
        "retry": "/run retry",
        "pause": "/goal pause and /run",
        "resume": "/goal resume",
        "cancel": "/run cancel",
        "more-work": "/run more",
    }
    missing = sorted(r for r in routes if r not in covered)
    assert not missing, (
        f"these kernel actions have no Slack route: {missing}. Add a command, "
        f"or add it to `covered` with the reason it is panel-only.")


# ── no command can 500 into Slack's generic error ────────────────────────────

@pytest.mark.parametrize("spec", COMMANDS, ids=lambda c: c.name)
def test_no_command_raises_on_garbage(spec):
    """Parametrised over the registry, so a NEW command is covered without
    anybody remembering. A handler that raises becomes a 500 and Slack
    shows its own `dispatch_failed`, which tells the user nothing."""
    from app.channels.slack.commands.registry import CommandContext

    async def body():
        async with world() as (conn, pool, w):
            with fake_slack():
                for args in ("", "   ", "!!!", "../../etc/passwd",
                             "approve", "x" * 500, "help"):
                    ctx = CommandContext(
                        args=args, user_id="U_NOBODY", channel_id="C_NOPE",
                        team_id="T_FAKE", response_url="",
                        command=spec.name)
                    out = await spec.dispatch(ctx)
                    assert isinstance(out, str), (
                        f"/{spec.name} {args!r} returned {type(out)}, not a "
                        "string Slack can show")
    _run(body())


@pytest.mark.parametrize("spec", COMMANDS, ids=lambda c: c.name)
def test_every_command_has_usage_text(spec):
    assert spec.summary.strip() and spec.usage.strip(), (
        f"/{spec.name} has no help text, so /help lists a command nobody can "
        "learn to use")


def test_the_dispatcher_cannot_raise():
    from app.channels.slack.commands import handle_slack_command

    src = inspect.getsource(handle_slack_command)
    assert "except Exception" in src


# ── what the channel already knows ───────────────────────────────────────────

def test_a_story_channel_resolves_its_own_story():
    """The dividend of one channel per thread: `/park answer yes` instead
    of `/park answer 4182 yes`."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.commands._context import resolve
            ctx = await resolve(w["channel"], w["team"], pool)
            assert ctx.goal_id == w["goal"]
            assert ctx.thread_id == w["thread"]
            assert ctx.story_id == w["story"], (
                "the channel does not know its story, so every command in it "
                "would demand an id the person can already see")
            assert ctx.is_story_channel
    _run(body())


def test_an_unknown_channel_resolves_to_nothing():
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.commands._context import resolve
            assert not (await resolve("C_UNKNOWN", w["team"], pool)).ok
    _run(body())


def test_an_explicit_id_beats_the_channel():
    """Somebody in one story's channel can still ask about another."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.commands._context import resolve, story_for
            other = f"{w['story']}-other"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state, "
                " assigned_to) VALUES ($1,$2,'other',$3,'in_progress',$3)",
                other, w["goal"], w["member"])
            ctx = await resolve(w["channel"], w["team"], pool)
            got, err = await story_for(ctx, other, pool)
            assert not err and got == other
    _run(body())


def test_a_short_id_is_resolved_by_prefix():
    """Eight characters is what the product shows people, so it is what
    they will type."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.commands._context import resolve, story_for
            ctx = await resolve(w["channel"], w["team"], pool)
            got, err = await story_for(ctx, w["story"][:8], pool)
            assert not err and got == w["story"]
    _run(body())


def test_a_goal_channel_asks_which_story():
    """It is about no single story, so there is nothing to infer."""
    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.commands._context import (ChannelContext,
                                                              story_for)
            ctx = ChannelContext(goal_id=w["goal"])
            got, err = await story_for(ctx, "", pool)
            assert not got and "Which story" in err
    _run(body())


# ── authorization, on every new command ──────────────────────────────────────

@pytest.mark.parametrize("module", ["run", "park", "errand"])
def test_the_new_commands_check_goal_membership(module):
    """Same rule as the buttons. The channel is the only hint of which goal
    a command is about, and a channel id is something anyone can type."""
    import importlib

    mod = importlib.import_module(f"app.channels.slack.commands.{module}")
    src = inspect.getsource(mod.handle)
    assert "get_goal_role" in src, (
        f"/{module} acts without checking the caller is in the goal")


@pytest.mark.parametrize("module", ["run", "park", "errand"])
def test_an_unmapped_slack_user_is_refused_before_anything_happens(module):
    import importlib

    async def body():
        async with world() as (conn, pool, w):
            mod = importlib.import_module(f"app.channels.slack.commands.{module}")
            with fake_slack():
                out = await mod.handle("", w["channel"], w["team"],
                                       slack_user_id="U_NOT_MAPPED")
            assert "recognise" in out.lower() or "recognize" in out.lower()
    _run(body())


# ── the renamed command ──────────────────────────────────────────────────────

def test_there_is_exactly_one_route_for_starting_a_story():
    """Four spellings of one route is three more ways to document it and
    three more to get wrong. `start` is the route; the old names answer
    with a pointer so nobody mid-flight is left guessing, and that is
    all they do."""
    from app.channels.slack.commands import story

    src = inspect.getsource(story.handle)
    assert src.count("commands.work import handle") == 1, (
        "more than one subcommand dispatches the start flow")
    seg = src[src.index('first_word in ("open", "import", "work")'):]
    assert "work import handle" not in seg[:400], (
        "the old names still run the flow instead of pointing at `start`")


# ── the state vocabulary ─────────────────────────────────────────────────────

def test_every_state_a_story_can_reach_has_a_label():
    """A missing label falls back to the raw column value, so a story
    entering `awaiting_merge` announced itself as "→ awaiting_merge".
    The fallback is what hid it."""
    from app.channels.slack.lifecycle import _STATE_LABELS
    from app.services.story_state import _NON_TERMINAL_STATES

    terminal = ("done", "reviewed", "cancelled", "rejected")
    for state in tuple(_NON_TERMINAL_STATES) + terminal + ("proposed",):
        assert state in _STATE_LABELS, (
            f"{state!r} has no Slack label and would be shown raw")


def test_aliases_is_gone_rather_than_declared_and_unused():
    from app.channels.slack.commands.registry import SlackCommand

    assert "aliases" not in SlackCommand.__dataclass_fields__, (
        "`aliases` is declared but nothing reads it — a field that looks "
        "like a feature and is not")


# ── accepting and importing a story, from Slack ─────────────────────────────

def test_the_old_names_point_at_start_rather_than_duplicating_it():
    """They briefly all ran the flow. One route, several spellings, is
    three more things to document and three more to get wrong."""
    from app.channels.slack.commands import story

    src = inspect.getsource(story.handle)
    assert 'first_word in ("open", "import", "work")' in src
    assert "start <id>" in src, "the pointer does not name the one route"


def test_start_is_no_longer_a_bare_state_transition():
    """As a transition it set accepted → in_progress and nothing else: no
    workspace, no assistant, no kickoff. Somebody typing `/story start`
    got a state change and no work, which is the most misleading thing a
    command can do."""
    from app.channels.slack.commands.story import _TRANSITION_ACTIONS

    assert "start" not in _TRANSITION_ACTIONS
    assert "accept" in _TRANSITION_ACTIONS, (
        "claiming a story without starting it must still be possible")


def test_starting_a_proposed_story_accepts_and_imports_it_in_one_go():
    """THE ASK. A proposed story should go to working from Slack without
    a trip to the panel in between."""
    from unittest.mock import patch

    seen = {}

    async def _provision(user_uid, goal_id, story_id="", engine_initiated=False):
        seen["story_id"] = story_id
        seen["user_uid"] = user_uid
        return ("t-1", "agent-1", False)

    async def body():
        async with world() as (conn, pool, w):
            proposed = f"{w['story']}-proposed"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state) "
                "VALUES ($1,$2,'a proposed story',$3,'proposed')",
                proposed, w["goal"], w["member"])
            # A story with no granted resource is refused, correctly: there
            # is nothing for the workspace to contain. Found by this test
            # failing on that guard rather than on the thing under test.
            rid = await conn.fetchval(
                "INSERT INTO goal_resources (id, goal_id, kind, name, "
                " created_by, status) VALUES (gen_random_uuid(),$1,'repo',"
                " 'fixture-repo',$2,'active') RETURNING id",
                w["goal"], w["member"])
            await conn.execute(
                "INSERT INTO story_resources (story_id, resource_id, relation) "
                "VALUES ($1,$2,'needs')", proposed, rid)

            from app.channels.slack.inbound import _handle_work_on_story
            with patch("app.routers.assistant."
                       "provision_assistant_thread_for_story", _provision), \
                 fake_slack():
                await _handle_work_on_story(
                    team_id=w["team"], goal_id=w["goal"], binding={},
                    slack_user="U_MEMBER", user_uid=w["member"],
                    text="a proposed story", pool=pool, response_url="")

            assert seen.get("story_id") == proposed, (
                f"the proposed story was not imported: {seen}")
            assert await conn.fetchval(
                "SELECT assigned_to FROM stories WHERE id=$1",
                proposed) == w["member"], "it was not assigned to the caller"
    _run(body())


def test_the_import_path_moves_a_proposed_story_into_work():
    """`provision_assistant_thread_for_story` is where proposed →
    in_progress happens, which is what makes one command enough."""
    from app.routers import assistant

    src = inspect.getsource(assistant.provision_assistant_thread_for_story)
    assert "'proposed', 'accepted'" in src or '"proposed", "accepted"' in src, (
        "importing no longer accepts a proposed story, so `/story start` "
        "would need an accept first")


def test_accept_on_its_own_still_claims_without_starting():
    from app.channels.slack.commands.story import _TRANSITION_ACTIONS

    assert "accept" in _TRANSITION_ACTIONS


# ── /task is gone with the task DAG ─────────────────────────────────────────

def test_the_task_command_is_gone():
    """It steered a task DAG that is no longer the execution model, so it
    offered actions against a thing that is not there."""
    assert lookup("task") is None
    assert "task" not in [c.name for c in COMMANDS]


def test_nothing_still_imports_the_task_command_module():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app"
    assert not (root / "channels/slack/commands/task.py").exists()
    offenders = [p.name for p in root.rglob("*.py")
                 if "tests" not in p.parts
                 and "commands.task" in p.read_text(encoding="utf-8",
                                                    errors="replace")]
    assert not offenders, f"{offenders} still import the removed /task command"


def test_a_story_whose_branch_is_not_cut_yet_says_so():
    """GOING STRAIGHT FROM PROPOSED TO IN_PROGRESS IS NOT THE SAME AS
    BEING READY TO WORK.

    `provision_assistant_thread_for_story` sets in_progress, and the
    kickoff then DEFERS if the git engine has not cut the workstream
    branch — correctly, because the branch-ready listener resumes it.
    But that deferral used to be a WARNING in a log and nothing else,
    while the person had just been told their channel was ready. They
    would type into a channel whose assistant had never been started,
    conclude it was broken, and start the story again.
    """
    from unittest.mock import patch

    said = []

    async def _provision(user_uid, goal_id, story_id="", engine_initiated=False):
        return ("t-1", "agent-1", False)

    async def _deferred(*a, **k):
        return False                      # waiting on the git engine

    async def _relay(goal_id, thread_id, content, *a, **k):
        said.append(content)

    async def body():
        async with world() as (conn, pool, w):
            sid = f"{w['story']}-nobranch"
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state, "
                " workspace_state) VALUES ($1,$2,'no branch yet',$3,"
                " 'in_progress','none')", sid, w["goal"], w["member"])
            rid = await conn.fetchval(
                "INSERT INTO goal_resources (id, goal_id, kind, name, "
                " created_by, status) VALUES (gen_random_uuid(),$1,'repo',"
                " 'r',$2,'active') RETURNING id", w["goal"], w["member"])
            await conn.execute(
                "INSERT INTO story_resources (story_id, resource_id, relation) "
                "VALUES ($1,$2,'needs')", sid, rid)

            from app.channels.slack.inbound import _handle_work_on_story
            with patch("app.routers.assistant."
                       "provision_assistant_thread_for_story", _provision), \
                 patch("app.routers.assistant."
                       "dispatch_story_kickoff_to_assistant", _deferred), \
                 patch("app.channels.base.relay_to_thread_channel", _relay), \
                 fake_slack():
                await _handle_work_on_story(
                    team_id=w["team"], goal_id=w["goal"], binding={},
                    slack_user="U_MEMBER", user_uid=w["member"],
                    text="no branch yet", pool=pool, response_url="")

            assert said, (
                "the kickoff deferred and the person was told nothing — they "
                "are now waiting on an assistant that was never started")
            assert "workspace" in said[0].lower() or "branch" in said[0].lower()
    _run(body())


# ── starting a story from wherever you happen to be ─────────────────────────

@pytest.mark.parametrize("where", ["goal", "go_story", "assistant_story"])
def test_a_story_can_be_started_from_any_bound_channel(where):
    """`/story start` used to resolve its goal with a lookup that required
    `pcb.actor_id IS NULL`. That is true of a goal channel and of a GO
    story channel, and NOT of an assistant's own story channel -- so
    starting a story from the place you are most likely to be standing
    answered "I couldn't figure out which goal this channel is linked to"
    and told you to go elsewhere.
    """
    import uuid as _uuid

    async def body():
        async with world() as (conn, pool, w):
            integ = w["integration"]
            if where == "goal":
                channel = "C_GOALCHAN"
                actor = None
            elif where == "go_story":
                channel = "C_GOSTORY"
                actor = None                     # GO channels carry no actor
            else:
                channel = w["channel"]           # has an actor
                actor = None                     # already inserted

            if actor is None and where != "assistant_story":
                tid = str(_uuid.uuid4())
                await conn.execute(
                    "INSERT INTO threads (id, goal_id, created_by) "
                    "VALUES ($1,$2,$3)", tid, w["goal"], w["member"])
                await conn.execute(
                    "INSERT INTO platform_channel_bindings "
                    "(goal_id, integration_id, actor_id, external_channel_id,"
                    " resource_kind, thread_id) "
                    "VALUES ($1,$2::uuid,NULL,$3,'channel',$4)",
                    w["goal"], integ, channel, tid)

            from app.channels.slack.commands._context import resolve
            ctx = await resolve(channel, w["team"], pool)
            assert ctx.ok and ctx.goal_id == w["goal"], (
                f"a {where} channel does not resolve to its goal, so "
                "/story start would refuse to run there")
    _run(body())


def test_the_start_flow_uses_the_binding_resolver():
    """Behavioural above; structural here, because the old resolver is
    still imported in that module for its other fields."""
    from app.channels.slack.commands import work

    src = inspect.getsource(work.handle)
    assert "_context import resolve" in src, (
        "/story start is back on a resolver that cannot see an assistant's "
        "own story channel")


def test_cancel_goes_through_the_one_implementation():
    """`_cancel_story` is what the board calls: it carries the invariant
    that a started code story is never cancelled, bridges the dependents
    so nothing waits on a story that will never arrive, re-derives the
    topology and writes the ledger. The easy version -- flipping state to
    'cancelled' -- would silently strand every dependent."""
    from app.channels.slack.commands import story

    src = inspect.getsource(story)
    assert "_cancel_story" in src, "Slack cancels stories its own way"
    seg = src[src.index("async def _cancel_from_slack"):]
    assert "UPDATE stories" not in seg, (
        "the Slack cancel writes the state itself instead of calling the "
        "shared path")
    assert "get_goal_role" in seg, "cancel does not check membership"


def test_cancelling_a_started_code_story_is_refused_with_its_reason():
    """The refusal explains what to do instead, so it must reach the
    person rather than being flattened to "couldn't"."""
    from unittest.mock import patch

    from fastapi.responses import JSONResponse

    async def _refuse(pid, sid, reason, actor_uid):
        return JSONResponse({"ok": False,
                             "error": "Cannot cancel 'X' — it is already started"},
                            status_code=400)

    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.commands.story import _cancel_from_slack
            with patch("app.routers.stories._cancel_story", _refuse), \
                 patch("app.channels.slack.commands.story.resolve_user",
                       lambda *a, **k: _ret(w["member"])):
                out = await _cancel_from_slack(
                    w["goal"], w["story"][:8], "", "U_MEMBER", "tok", pool)
            assert "already started" in out, out
    _run(body())


async def _ret(v):
    return v


def test_a_successful_cancel_says_what_it_cancelled():
    from unittest.mock import patch

    from fastapi.responses import JSONResponse

    async def _ok(pid, sid, reason, actor_uid):
        return JSONResponse({"ok": True})

    async def body():
        async with world() as (conn, pool, w):
            from app.channels.slack.commands.story import _cancel_from_slack
            with patch("app.routers.stories._cancel_story", _ok), \
                 patch("app.channels.slack.commands.story.resolve_user",
                       lambda *a, **k: _ret(w["member"])):
                out = await _cancel_from_slack(
                    w["goal"], w["story"][:8], "no longer needed",
                    "U_MEMBER", "tok", pool)
            assert "Cancelled" in out and "no longer needed" in out
    _run(body())


# ── the right person, with a reason when it is not them ────────────────────

def test_an_unassigned_story_may_be_claimed_by_any_member():
    """Picking up unclaimed work is the normal case and must stay easy."""
    from app.channels.slack.commands._entitled import may_start

    async def body():
        async with world() as (conn, pool, w):
            ok, why = await may_start(
                pool, w["member"], {"id": "s1", "title": "t", "assigned_to": ""})
            assert ok and why == ""
    _run(body())


def test_starting_somebody_elses_story_is_refused_and_names_them():
    """Starting it would reassign it out from under them. "You don't have
    access" would be a lie — they do have access to the goal — and gives
    them nothing to act on."""
    from app.channels.slack.commands._entitled import may_start

    async def body():
        async with world() as (conn, pool, w):
            ok, why = await may_start(
                pool, w["member"],
                {"id": "s1", "title": "Build it", "assigned_to": w["outsider"]})
            assert not ok
            assert w["outsider"] in why or "outsider" in why
            assert "reassign" in why.lower(), (
                "the refusal does not say what to do next")
    _run(body())


def test_your_own_story_starts_without_complaint():
    from app.channels.slack.commands._entitled import may_start

    async def body():
        async with world() as (conn, pool, w):
            ok, _ = await may_start(
                pool, w["member"],
                {"id": "s1", "title": "t", "assigned_to": w["member"]})
            assert ok
    _run(body())


def test_cancelling_somebody_elses_story_is_refused():
    """Narrower than starting, because it is destructive."""
    from app.channels.slack.commands._entitled import may_cancel

    async def body():
        async with world() as (conn, pool, w):
            ok, why = await may_cancel(
                pool, w["outsider"], w["goal"],
                {"id": "s1", "title": "Build it", "assigned_to": w["member"]})
            assert not ok and "theirs to cancel" in why
    _run(body())


def test_a_goal_admin_may_cancel_somebody_elses_story():
    """Somebody has to be able to, when a person has left or is away."""
    from app.channels.slack.commands._entitled import may_cancel

    async def body():
        async with world() as (conn, pool, w):
            await conn.execute(
                "INSERT INTO goal_members (goal_id, uid, role, added_by) "
                "VALUES ($1,$2,'admin',$2) "
                "ON CONFLICT (goal_id, uid) DO UPDATE SET role='admin'",
                w["goal"], w["outsider"])
            ok, _ = await may_cancel(
                pool, w["outsider"], w["goal"],
                {"id": "s1", "title": "t", "assigned_to": w["member"]})
            assert ok, "a goal admin cannot cancel, so nobody can clean up"
    _run(body())


def test_both_commands_actually_consult_the_guard():
    """The guard working is not the same as anything calling it — and for
    `start` the refusal used to happen deep in the backgrounded flow, as
    an ephemeral posted into the GOAL channel, so somebody running it
    from a story channel saw "Working on it…" and never the reason."""
    from app.channels.slack.commands import story, work

    assert "may_start" in inspect.getsource(work.handle), (
        "/story start does not check who holds the story before starting it")
    assert "may_cancel" in inspect.getsource(story), (
        "/story cancel does not check who holds the story")


def test_the_start_refusal_is_the_commands_own_answer():
    """It must come back in the slash reply, not land in another channel."""
    from app.channels.slack.commands import work

    src = inspect.getsource(work.handle)
    guard_at = src.index("may_start")
    bg_at = src.index("asyncio.create_task")
    assert guard_at < bg_at, (
        "the entitlement check runs after the work is backgrounded, so its "
        "refusal cannot be the reply")
