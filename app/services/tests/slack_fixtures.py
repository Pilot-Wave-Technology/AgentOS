"""Shared scaffolding for the Slack tests. Not a test file.

NOTHING IN THE REPO IMPORTED `app.channels.slack` BEFORE THIS. Twenty
tests were about to each invent a fake Slack client and a set of DB
rows, so this is the one place both live.

THE SKIP IS THE DANGEROUS PART. A DB test that cannot connect skips,
and a skipped test reports as a pass. The suite has been green on a
file whose fixtures never ran. So `connect_or_skip` names every DSN it
tried and why each failed, and `REQUIRE_DB=1` turns the skip into a
hard failure for CI, where a Slack test that quietly did not run is
worth nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import asyncpg
import pytest

# A1: the local suite container is agentos/agentos, NOT postgres/postgres.
# Getting this wrong does not fail — it skips, silently.
DSNS = [
    os.environ.get("DATABASE_URL", ""),
    "postgresql://agentos:agentos@localhost:55432/agentos_test",
]

_TABLES = (
    "users", "goals", "goal_members", "stories", "tasks", "approvals",
    "threads", "messages", "platform_integrations",
    "goal_platform_integrations", "platform_channel_bindings",
    "platform_user_map",
)


class Pool:
    """An asyncpg connection wearing the pool interface the app expects."""

    def __init__(self, conn):
        self._c = conn

    async def fetchval(self, *a, **k):
        return await self._c.fetchval(*a, **k)

    async def fetchrow(self, *a, **k):
        return await self._c.fetchrow(*a, **k)

    async def fetch(self, *a, **k):
        return await self._c.fetch(*a, **k)

    async def execute(self, *a, **k):
        return await self._c.execute(*a, **k)

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self._c


async def connect_or_skip():
    attempts: list[str] = []
    for dsn in [d for d in DSNS if d]:
        where = dsn.rsplit("@", 1)[-1] or "<malformed>"
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                              # noqa: BLE001
            attempts.append(f"{where}: {type(exc).__name__}")
            continue
        found = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = ANY($1::text[])", list(_TABLES))
        if found == len(_TABLES):
            return conn
        await conn.close()
        attempts.append(f"{where}: {found}/{len(_TABLES)} tables")
    why = "no database with the Slack schema. Tried: " + "; ".join(attempts)
    if os.environ.get("REQUIRE_DB") == "1":
        pytest.fail(why)      # CI: a silent skip is a false green
    pytest.skip(why)


class FakeSlack:
    """Stands in for `WebClient`. Records calls; raises what you tell it to.

    `fail_with` maps a method name to a Slack error code, so a test can
    say "this post hits `ratelimited`" without constructing a response.
    """

    def __init__(self, fail_with: dict[str, str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_with = dict(fail_with or {})
        self._ts = 1000

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        code = self.fail_with.get(name)
        if code:
            from slack_sdk.errors import SlackApiError

            class _R(dict):
                status_code = 429 if code == "ratelimited" else 200
                headers = {"Retry-After": "1"} if code == "ratelimited" else {}

            raise SlackApiError(code, _R({"ok": False, "error": code}))

    def of(self, name: str) -> list[dict]:
        """Every kwargs dict passed to `name`, in order."""
        return [kw for n, kw in self.calls if n == name]

    # — the methods the module actually uses —
    def chat_postMessage(self, **kw):
        self._record("chat_postMessage", kw)
        self._ts += 1
        return {"ok": True, "ts": f"{self._ts}.000100",
                "channel": kw.get("channel", "C0")}

    def chat_update(self, **kw):
        self._record("chat_update", kw)
        return {"ok": True, "ts": kw.get("ts", "1.0")}

    def chat_postEphemeral(self, **kw):
        self._record("chat_postEphemeral", kw)
        return {"ok": True}

    def conversations_create(self, **kw):
        self._record("conversations_create", kw)
        return {"ok": True, "channel": {"id": f"C{len(self.calls):05d}",
                                        "name": kw.get("name", "")}}

    def conversations_join(self, **kw):
        self._record("conversations_join", kw)
        return {"ok": True}

    def conversations_invite(self, **kw):
        self._record("conversations_invite", kw)
        return {"ok": True}

    def conversations_kick(self, **kw):
        self._record("conversations_kick", kw)
        return {"ok": True}

    def conversations_members(self, **kw):
        self._record("conversations_members", kw)
        return {"ok": True, "members": getattr(self, "members", [])}

    def conversations_info(self, **kw):
        self._record("conversations_info", kw)
        return {"ok": True, "channel": {"id": kw.get("channel", "C0"),
                                        "name": "fake-channel"}}

    def conversations_list(self, **kw):
        self._record("conversations_list", kw)
        return {"ok": True, "channels": [], "response_metadata": {"next_cursor": ""}}

    def conversations_archive(self, **kw):
        self._record("conversations_archive", kw)
        return {"ok": True}

    def users_info(self, **kw):
        self._record("users_info", kw)
        return {"ok": True, "user": {"id": kw.get("user", "U0"),
                                     "profile": {"email": "someone@example.test"}}}

    def users_lookupByEmail(self, **kw):
        self._record("users_lookupByEmail", kw)
        return {"ok": True, "user": {"id": "U_LOOKED_UP"}}

    def views_open(self, **kw):
        self._record("views_open", kw)
        return {"ok": True}

    def files_upload_v2(self, **kw):
        self._record("files_upload_v2", kw)
        return {"ok": True}


@contextlib.asynccontextmanager
async def world(*, with_slack: bool = True, participants: list[str] | None = None):
    """A goal, a member, an outsider, a story, a thread — all rolled back.

    `with_slack` adds the integration rows and a binding, so a test can
    choose between "Slack is set up here" and "it is not", which are
    different code paths and both need covering.
    """
    conn = await connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        tag = uuid.uuid4().hex[:8]
        w = {
            "goal": f"SF{tag[:3].upper()}",
            "member": f"sf-{tag}-member",
            "outsider": f"sf-{tag}-outsider",
            "story": f"sf-{tag}-story",
            "task": f"sf-{tag}-task",
            "thread": str(uuid.uuid4()),
            "approval": str(uuid.uuid4()),
            "channel": f"C{tag.upper()}",
            "team": "T_FAKE",
        }
        for uid in (w["member"], w["outsider"]):
            # A4: system_role must be spelled out — its DEFAULT is not in
            # its own CHECK, so omitting it fails.
            await conn.execute(
                "INSERT INTO users (uid, email, display_name, system_role) "
                "VALUES ($1,$2,$3,'member') ON CONFLICT (uid) DO NOTHING",
                uid, f"{uid}@example.test", uid)
            # users.actor_id defaults to a fresh uuid but nothing creates the
            # `actors` row it points at, and platform_user_map has a FK to it.
            # In production `add_goal_member` does this; a fixture that skips
            # it fails only on the Slack tables, which is a confusing place to
            # find out.
            aid = await conn.fetchval(
                "SELECT actor_id FROM users WHERE uid = $1", uid)
            await conn.execute(
                "INSERT INTO actors (id, type, uid, display_name) "
                "VALUES ($1,'user',$2,$3) ON CONFLICT (id) DO NOTHING",
                aid, uid, uid)
        await conn.execute(
            "INSERT INTO goals (id, name, created_by, status) "
            "VALUES ($1,'slack fixture',$2,'active')", w["goal"], w["member"])
        await conn.execute(
            "INSERT INTO goal_members (goal_id, uid, role, added_by) "
            "VALUES ($1,$2,'member',$2)", w["goal"], w["member"])
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, created_by, state, "
            "assigned_to, active_thread_id) "
            "VALUES ($1,$2,'fixture story',$3,'in_progress',$3,$4)",
            w["story"], w["goal"], w["member"], w["thread"])
        # The thread CARRIES ITS STORY, as a real story thread does. Without
        # it the fixture modelled a thread belonging to no story, and every
        # test of "the channel knows what it is about" passed for the wrong
        # reason — or failed for one.
        await conn.execute(
            "INSERT INTO threads (id, goal_id, created_by, participants, story_id) "
            "VALUES ($1,$2,$3,$4,$5)",
            w["thread"], w["goal"], w["member"], participants or [], w["story"])

        await conn.execute(
            "INSERT INTO tasks (id, goal_id, title, created_by, state) "
            "VALUES ($1,$2,'fixture task',$3,'awaiting_approval')",
            w["task"], w["goal"], w["member"])
        await conn.execute(
            "INSERT INTO approvals (id, story_id, kind) VALUES ($1,$2,'diff')",
            w["approval"], w["story"])

        if with_slack:
            integ = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO platform_integrations "
                "(id, platform, status, credentials_kind, credentials_json, config) "
                "VALUES ($1,'slack','active','oauth',$2,$3)",
                integ,
                '{"bot_token": "xoxb-fake", "workspace_id": "T_FAKE"}',
                '{"client_id": "1.2", "client_secret": "s", "signing_secret": "s"}')
            # `goal_config` carries the goal channel id, which
            # `get_slack_config_for_goal` returns and the work-on-story
            # flow bails without. An empty config here made that whole
            # path return at its first guard, so a test of it passed by
            # never reaching it.
            await conn.execute(
                "INSERT INTO goal_platform_integrations "
                "(goal_id, integration_id, enabled, config) "
                "VALUES ($1,$2,true,$3::jsonb)",
                w["goal"], integ,
                '{"goal_channel_id": "C_GOAL_FAKE"}')
            actor = await conn.fetchval(
                "SELECT actor_id FROM users WHERE uid = $1", w["member"])
            await conn.execute(
                "INSERT INTO platform_channel_bindings "
                "(goal_id, integration_id, actor_id, external_channel_id, "
                " resource_kind, thread_id) "
                "VALUES ($1,$2,$3,$4,'thread',$5)",
                w["goal"], integ, actor, w["channel"], w["thread"])
            await conn.execute(
                "INSERT INTO platform_user_map "
                "(platform, external_user_id, actor_id, mapping_status) "
                "VALUES ('slack',$1,$2,'confirmed')", "U_MEMBER", actor)
            w["integration"] = integ
            w["actor"] = str(actor)

        pool = Pool(conn)
        # Provisioning refuses to run from a non-production environment with
        # no SLACK_COMMAND_SUFFIX (all environments share one workspace), and
        # the test process is neither. Give it one, so a test exercises the
        # real provisioning path rather than the guard.
        with _global_pool(pool), _suffix("test"):
            yield conn, pool, w
    finally:
        await tx.rollback()
        await conn.close()


@contextlib.contextmanager
def _global_pool(pool):
    """Point every module's `get_pool` at this connection.

    PATCHING `app.database.get_pool` IS NOT ENOUGH, and the way that
    fails is nasty. Modules like `app/channels/base.py` do a top-level
    `from app.database import get_pool`, which binds their own name --
    so the patch reaches them only if they have not been imported yet.
    A test therefore PASSES ALONE and fails in company, or the reverse,
    depending on what the rest of the suite imported first. That is the
    worst kind of test: one whose result depends on its neighbours.

    So: rebind the attribute on every already-imported `app.*` module
    that holds one, plus `app.database` for anything imported later.
    Self-maintaining, which matters because the later phases add modules
    and nobody will remember to extend a hardcoded list.
    """
    import sys
    from unittest.mock import patch

    def getter():
        return pool

    targets = [
        f"{name}.get_pool"
        for name, mod in list(sys.modules.items())
        if name.startswith("app.") and getattr(mod, "get_pool", None) is not None
    ]
    if "app.database.get_pool" not in targets:
        targets.append("app.database.get_pool")

    with contextlib.ExitStack() as stack:
        for t in targets:
            try:
                stack.enter_context(patch(t, getter))
            except (AttributeError, ModuleNotFoundError):
                continue
        yield


@contextlib.contextmanager
def _suffix(value: str):
    """A SLACK_COMMAND_SUFFIX for the duration, leaving other settings alone."""
    from unittest.mock import patch

    from app.config import get_settings

    # `model_copy` rather than walking dir(): touching a pydantic model's
    # attributes through dir() hits deprecated descriptors and buries the
    # real output under warnings.
    stub = get_settings().model_copy(update={"slack_command_suffix": value})
    with patch("app.config.get_settings", lambda: stub):
        yield


class AsyncSlack:
    """FakeSlack with awaitable methods, since every client is async now."""

    def __init__(self, fake: FakeSlack):
        self._f = fake

    def __getattr__(self, name):
        inner = getattr(self._f, name)

        async def call(**kw):
            return inner(**kw)
        return call


@contextlib.contextmanager
def fake_slack(fake: FakeSlack | None = None):
    """Make every module's Slack client the fake one.

    SAME TRAP AS `_global_pool`, in the other direction. `outbound.py`,
    `lifecycle.py` and the rest each do a module-level
    `from app.channels.slack.client import make_async_client`, so
    patching `client.make_async_client` reaches a module only if it has
    NOT been imported yet. A test therefore passed alone -- importing
    outbound for the first time while the patch was active -- and failed
    when a neighbour had imported it first. That is the worst kind of
    failure: it looks like the code, and it is the test.

    Rebinds every already-imported `app.*` module that holds the name,
    plus client.py for anything imported later.
    """
    import sys
    from unittest.mock import patch

    fake = fake or FakeSlack()
    wrapped = AsyncSlack(fake)

    def factory(*_a, **_k):
        return wrapped

    targets = [
        f"{name}.make_async_client"
        for name, mod in list(sys.modules.items())
        if name.startswith("app.") and getattr(mod, "make_async_client", None)
    ]
    if "app.channels.slack.client.make_async_client" not in targets:
        targets.append("app.channels.slack.client.make_async_client")

    with contextlib.ExitStack() as stack:
        for t in targets:
            try:
                stack.enter_context(patch(t, factory))
            except (AttributeError, ModuleNotFoundError):
                continue
        yield fake
