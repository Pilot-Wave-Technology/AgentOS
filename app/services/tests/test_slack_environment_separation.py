"""Three environments, one Slack workspace, and nothing may blur them.

TWO FAILURES THIS PREVENTS, both of which were silent.

The first is scope drift. The OAuth consent URL, the manifest the admin
page generates, and the documented template each carried their own
hand-written scope list. Slack grants the INTERSECTION of the manifest
and the install request, so a scope in two of the three is simply not
granted -- and you find out later, as a `missing_scope` inside an
`except SlackApiError` that logs a warning and drops the message.
`files:write` was in none of them while two code paths called
`files_upload_v2`.

The second is channel sharing. Channel names are unique per WORKSPACE,
all three environments install into one, and the only thing keeping
them apart is the name prefix. When a name collided, the code found the
existing channel and JOINED it -- so dev bound its goal to production's
channel and both read and wrote the same conversation, at INFO level.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from app.channels.slack import scopes

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SLACK = _ROOT / "app/channels/slack"


# ── scopes are derived, not maintained in parallel ───────────────────────────

def test_every_slack_api_method_the_code_calls_has_a_declared_scope():
    """THE ONE THAT CATCHES THE NEXT MISSING SCOPE.

    Walks the module for real `client.<method>(` calls and fails if the
    method is not in METHOD_SCOPES. That is how `files:write` went
    missing: somebody added `files_upload_v2` and there was nowhere
    obvious that said a scope was now owed.
    """
    called: set[str] = set()
    for path in _SLACK.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            name = fn.attr
            # slack_sdk method names, distinguished from our own helpers
            if re.match(r"^(chat|conversations|users|files|views|auth|team)_\w+$", name):
                called.add(name)

    undeclared = sorted(called - set(scopes.METHOD_SCOPES))
    assert not undeclared, (
        f"these Slack API methods are called but declare no scope: {undeclared}. "
        "Add them to METHOD_SCOPES in app/channels/slack/scopes.py — an "
        "undeclared scope fails at runtime as missing_scope, inside an except "
        "branch that drops the message.")


def test_files_write_is_requested_because_we_upload_files():
    """The concrete gap. `files_upload_v2` is called in actions.py twice."""
    assert "files:write" in scopes.bot_scopes()


@pytest.mark.parametrize("unused", ["reactions:write", "mpim:read"])
def test_scopes_nothing_calls_are_not_requested(unused):
    """Asking for more than you use is how a scope review stops meaning
    anything. Both of these were requested everywhere and called nowhere."""
    assert unused not in scopes.bot_scopes()


def test_the_oauth_request_and_the_manifest_ask_for_the_same_thing():
    """They are generated from one list now; this fails if someone
    reintroduces a literal in either place."""
    from app.routers.admin import _slack_bot_scopes

    assert _slack_bot_scopes() == scopes.bot_scopes()
    assert sorted(scopes.scope_string().split(",")) == scopes.bot_scopes()


def test_no_hardcoded_scope_list_survives_anywhere():
    """A source guard, because the failure mode is a SECOND list rather
    than a wrong one."""
    offenders = []
    for path in list(_SLACK.rglob("*.py")) + [_ROOT / "app/routers/admin.py"]:
        if path.name == "scopes.py":
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'"(chat:write|channels:manage|users:read\.email)"', text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(_ROOT)}:{line}")
    assert not offenders, (
        f"scope literals outside scopes.py: {offenders}. Scopes are derived "
        "from METHOD_SCOPES so the consent URL and the manifest cannot drift.")


def test_the_events_we_subscribe_to_are_one_list_too():
    """The template omitted `message.im` while the generator included it."""
    from app.routers.admin import _slack_bot_events

    assert _slack_bot_events() == list(scopes.BOT_EVENTS)
    assert "message.im" in scopes.BOT_EVENTS


# ── a taken channel name is an error, never a join ───────────────────────────

def test_a_taken_channel_name_raises_instead_of_being_worked_around():
    from slack_sdk.errors import SlackApiError

    from app.channels.slack.lifecycle import (ChannelNameTaken,
                                              create_private_channel)

    class _Client:
        def __init__(self):
            self.joined = []

        def conversations_create(self, **kw):
            raise SlackApiError("name_taken", {"ok": False, "error": "name_taken"})

        def conversations_join(self, **kw):
            self.joined.append(kw)
            return {"ok": True}

        def conversations_list(self, **kw):
            return {"ok": True, "channels": [{"name": "goal1", "id": "C_THEIRS"}],
                    "response_metadata": {"next_cursor": ""}}

    import asyncio

    class _AsyncClient(_Client):
        async def conversations_create(self, **kw):
            return _Client.conversations_create(self, **kw)

        async def conversations_join(self, **kw):
            return _Client.conversations_join(self, **kw)

    c = _AsyncClient()
    with pytest.raises(ChannelNameTaken):
        asyncio.run(create_private_channel(c, "goal1"))
    assert not c.joined, (
        "it joined a channel it did not create — that is how two environments "
        "end up sharing one conversation")


def test_nothing_creates_a_public_channel_any_more():
    """A channel is visible only to the people in its thread, so a helper
    that makes public ones is a hazard sitting there waiting to be reused.
    `create_or_join_public_channel` was deleted once its last caller went."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app/channels/slack"
    offenders = []
    for path in root.rglob("*.py"):
        code = " ".join(line.split("#", 1)[0]
                        for line in path.read_text(encoding="utf-8").splitlines()
                        if not line.strip().startswith("#"))
        if "is_private=False" in code or "create_or_join_public_channel(" in code:
            offenders.append(path.name)
    assert not offenders, f"{offenders} can still create a public channel"


# ── a non-production environment must declare its suffix ─────────────────────

def _with_settings(suffix: str, env: str):
    """A class body does NOT close over the enclosing function, so
    `app_env = app_env` inside one is a NameError, not a parameter read.
    Built with type() to keep the names distinct and the trap visible."""
    from unittest.mock import patch

    settings = type("_S", (), {"slack_command_suffix": suffix, "app_env": env})()
    return patch("app.config.get_settings", lambda: settings)


def test_a_non_production_environment_without_a_suffix_refuses_to_provision():
    from app.channels.slack.lifecycle import require_env_separation

    with _with_settings("", "development"):
        with pytest.raises(RuntimeError, match="SLACK_COMMAND_SUFFIX"):
            require_env_separation()


def test_production_may_run_unsuffixed_because_it_owns_the_clean_names():
    from app.channels.slack.lifecycle import require_env_separation

    with _with_settings("", "production"):
        require_env_separation()          # must not raise


def test_any_environment_with_a_suffix_is_fine():
    from app.channels.slack.lifecycle import require_env_separation

    for env in ("development", "production"):
        with _with_settings("feature", env):
            require_env_separation()


def test_channel_creation_is_actually_behind_the_guard():
    """The guard is worth nothing if the provisioning paths do not call it.

    THE LIST FOLLOWS THE CODE. `ensure_user_goal_channel` was named here
    until the private (member x goal) tier was removed on 2026-10-19;
    `ensure_channel_for_thread` took over creating channels and is named
    in its place. Deleting the entry without adding its replacement
    would have left this test passing while the path it exists to cover
    went unguarded.
    """
    from app.channels.slack import lifecycle, thread_channels

    paths = ((lifecycle, "setup_goal_channels"),
             (thread_channels, "ensure_channel_for_thread"))
    for module, fn_name in paths:
        src = inspect.getsource(getattr(module, fn_name))
        assert "require_env_separation()" in src, (
            f"{module.__name__}.{fn_name} creates channels without checking "
            "the environment separation guard")


# ── the suffix stripper has no hardcoded environment list ────────────────────

def test_suffix_stripping_does_not_depend_on_a_list_of_env_names():
    """It was ("prod", "dev", "feature") — which never contained "next",
    and would have been wrong again for whatever comes after it."""
    from app.channels.slack import commands

    src = inspect.getsource(commands.handle_slack_command)
    # Comments are allowed to name the old list — explaining why it is gone
    # is the point. Only executable lines are checked.
    code = " ".join(
        line.split("#", 1)[0] for line in src.splitlines()
        if not line.strip().startswith("#"))
    for env in ("prod", "dev", "feature", "next"):
        assert f'"{env}"' not in code and f"'{env}'" not in code, (
            f"the suffix fallback still hardcodes {env!r}; strip any trailing "
            "-<token> whose stem is a real command instead")


# ── the legacy stack is gone, and stays gone ─────────────────────────────────

def test_there_is_no_second_slack_implementation():
    """app/slack/ was a complete divergent copy: its own naming scheme, its
    own user-mapping table, and a dispatcher that knew only `card_*` — so
    every newer button posted by this stack arrived somewhere that could not
    parse it."""
    assert not (_ROOT / "app/slack").exists(), (
        "app/slack/ is back. One Slack implementation, in app/channels/slack/.")

    main = (_ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "from app.slack import" not in main


# ── the manifest is the registry, not a copy of it ──────────────────────────

def test_the_manifest_lists_exactly_the_registered_commands():
    """THE THIRD TIME THIS PATTERN HAS BITTEN.

    The scope list drifted, the bot_events list drifted, and the slash
    commands drifted too -- found on feature, in the manifest the live
    deployment was serving: it still advertised `/task`, removed with the
    task DAG, and knew nothing of /run, /park or /errand.

    A manifest is the ONLY thing that registers a command with Slack. A
    command missing from it does not exist however well it is
    implemented, and one listed that no longer exists answers "is not a
    recognised command" to anyone who tries it.
    """
    from app.channels.slack.commands.registry import COMMANDS
    from app.routers.admin import _slack_slash_commands

    rows = _slack_slash_commands("https://x/commands", lambda n: f"/{n}-test")
    assert [r["command"] for r in rows] == [f"/{c.name}-test" for c in COMMANDS]
    assert "/task-test" not in [r["command"] for r in rows]


def test_every_manifest_command_points_at_the_commands_endpoint():
    from app.routers.admin import _slack_slash_commands

    for r in _slack_slash_commands("https://x/channels/slack/commands",
                                   lambda n: f"/{n}"):
        assert r["url"].endswith("/channels/slack/commands")


def test_the_usage_hints_are_argument_shapes_not_prose():
    """They were derived from the first line of `usage`, which is a header
    like "/goal [help]" -- so half the commands advertised "[help]" as
    their entire argument list and the rest got a sentence fragment."""
    from app.channels.slack.commands.registry import COMMANDS
    from app.routers.admin import _slack_slash_commands

    hinted = {r["command"]: r.get("usage_hint", "")
              for r in _slack_slash_commands("u", lambda n: n)}
    for spec in COMMANDS:
        if spec.name == "help":
            continue                      # takes no arguments
        hint = hinted[spec.name]
        assert hint, f"/{spec.name} offers no usage hint in the composer"
        assert hint != "[help]", f"/{spec.name}'s hint is the header line"
        assert "—" not in hint and "--" not in hint, (
            f"/{spec.name}'s hint is prose, not an argument shape: {hint!r}")


def test_a_new_command_cannot_be_added_without_a_hint():
    """Cheap, and it is the thing somebody will forget."""
    from app.channels.slack.commands.registry import COMMANDS

    missing = [c.name for c in COMMANDS if c.name != "help" and not c.hint]
    assert not missing, f"these commands have no Slack usage hint: {missing}"


# ── our own orphan, which the guard must not strand ────────────────────────

class _Orphan:
    """A workspace where `name` exists already, created by `creator`."""

    def __init__(self, name, creator, me="U_BOT", archived=False,
                 private=True):
        self._name, self._creator, self._me = name, creator, me
        self._archived, self._private = archived, private
        self.joined = []

    async def conversations_create(self, **kw):
        from slack_sdk.errors import SlackApiError
        raise SlackApiError("name_taken", {"ok": False, "error": "name_taken"})

    async def auth_test(self, **kw):
        return {"ok": True, "user_id": self._me}

    async def conversations_list(self, **kw):
        if self._archived and kw.get("exclude_archived"):
            chans = []
        else:
            chans = [{"name": self._name, "id": "C_EXISTING",
                      "creator": self._creator,
                      "is_private": self._private}]
        return {"ok": True, "channels": chans,
                "response_metadata": {"next_cursor": ""}}

    async def conversations_join(self, **kw):
        # RECORDED BEFORE IT RAISES, which matters: the first version
        # appended only on success, so "did it try to join?" and "did the
        # join work?" were the same assertion -- and the test stayed
        # green when the code went back to attempting a join on a private
        # channel.
        #
        # THE REAL API REFUSES THIS FOR A PRIVATE CHANNEL. An earlier
        # stub accepted it, so the test passed while production failed
        # with `method_not_supported_for_channel_type`: a fake kinder
        # than the thing it stands in for.
        self.joined.append(kw)
        if self._private:
            from slack_sdk.errors import SlackApiError
            raise SlackApiError("nope", {
                "ok": False,
                "error": "method_not_supported_for_channel_type"})
        return {"ok": True}


def test_a_channel_this_bot_created_is_adopted_rather_than_refused():
    """THE RECOVERY CASE, and it is common rather than rare: a run that
    creates the channel and then fails before writing the binding leaves
    exactly this. Seen on feature within an hour of the guard shipping.

    Refusing it for ever is worse than the old join-anything behaviour,
    because there is no way back — archiving does not release the name,
    so recovery means a human renaming the channel by hand and nothing
    tells them that."""
    import asyncio

    from app.channels.slack.lifecycle import create_private_channel

    c = _Orphan("caly4-feature", creator="U_BOT")
    assert asyncio.run(create_private_channel(c, "caly4-feature")) == "C_EXISTING"
    assert not c.joined, (
        "it tried to join a private channel — conversations.join is "
        "public-only, and the bot is already a member of one it created")


def test_somebody_elses_channel_of_the_same_name_is_still_refused():
    """The whole point of the guard: binding one environment's goal to
    another's channel is how two deployments end up sharing one
    conversation."""
    import asyncio

    import pytest as _pytest

    from app.channels.slack.lifecycle import (ChannelNameTaken,
                                              create_private_channel)

    c = _Orphan("caly4-feature", creator="U_SOMEBODY_ELSE")
    with _pytest.raises(ChannelNameTaken):
        asyncio.run(create_private_channel(c, "caly4-feature"))
    assert not c.joined, "it joined a channel it did not create"


def test_an_archived_orphan_is_refused_with_advice_that_works():
    """An archived channel cannot be joined and its name is gone for
    good, so the only recovery is a rename — which the message has to
    say, because "archive it" is the obvious wrong guess."""
    import asyncio

    import pytest as _pytest

    from app.channels.slack.lifecycle import (ChannelNameTaken,
                                              create_private_channel)

    c = _Orphan("caly4-feature", creator="U_BOT", archived=True)
    with _pytest.raises(ChannelNameTaken) as e:
        asyncio.run(create_private_channel(c, "caly4-feature"))
    assert "Rename" in str(e.value)
    assert "archiv" in str(e.value).lower(), (
        "the message does not warn that archiving will not help")
