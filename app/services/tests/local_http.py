"""HTTP test harness for the `/internal/local/*` routes — the surface the VS Code
extension talks to.

WHY OVER HTTP. Every authorization rule of the local lane lives in the routers
(`_enforce_local_scope`, `_require_human`, `_enforce_goal_membership`), and no test
reached them: services were called directly, so a route that forgot its guard would
pass every test. This drives the real routers with real tokens against a real
database, inside one rolled-back transaction per test.

    async with local_http() as h:
        goal, story = await h.seed_story(state="in_progress")
        r = await h.post("/get-state", {"story_id": story}, token=h.story_token(story, goal))
        assert r.status_code == 200

ONE CONNECTION, ONE LOOP. The app runs in-process through `httpx.ASGITransport`, so
requests execute on the test's own event loop and can share its single asyncpg
connection — which is what lets the whole test roll back.
"""
from __future__ import annotations

import contextlib
import os
import time
import uuid

import httpx
import jwt

from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip

SIGNING_KEY = "test-signing-key-not-a-secret"
SHARED_TOKEN = "test-shared-internal-token"


class PoolOnConn:
    """An asyncpg-pool-shaped wrapper around one connection (see module doc)."""

    def __init__(self, conn):
        self._conn = conn

    async def fetch(self, *a, **k):
        return await self._conn.fetch(*a, **k)

    async def fetchrow(self, *a, **k):
        return await self._conn.fetchrow(*a, **k)

    async def fetchval(self, *a, **k):
        return await self._conn.fetchval(*a, **k)

    async def execute(self, *a, **k):
        return await self._conn.execute(*a, **k)

    async def executemany(self, *a, **k):
        return await self._conn.executemany(*a, **k)

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _CM()


def _app():
    from fastapi import FastAPI
    import app.routers.local_execution  # noqa: F401  (registers routes)
    import app.routers.local_execution_run  # noqa: F401
    from app.routers.local_common import router
    a = FastAPI()
    a.include_router(router)
    return a


class LocalHttp:
    def __init__(self, conn, client):
        self.conn = conn
        self.client = client

    # ── tokens ────────────────────────────────────────────────────────
    def human(self, uid: str) -> str:
        """The extension's ext-session token for a person."""
        from app.internal_auth import mint_ext_session_token
        return mint_ext_session_token(uid)

    def story_token(self, story_id: str, goal_id: str, aiid: str = "aiid-test") -> str:
        """The story-scoped human-local token the session/agent carries."""
        from app.internal_auth import mint_human_local_token
        return mint_human_local_token("assistant-test", goal_id, story_id, agent_instance_id=aiid)

    def expired(self, story_id: str, goal_id: str) -> str:
        now = int(time.time())
        return jwt.encode({"typ": "human_local", "sub": "assistant-test", "goal_id": goal_id,
                           "story_id": story_id, "scopes": [], "aiid": "aiid-test",
                           "iat": now - 7200, "exp": now - 3600}, SIGNING_KEY, algorithm="HS256")

    shared = SHARED_TOKEN

    # ── requests ──────────────────────────────────────────────────────
    async def post(self, path: str, body: dict, token: str | None = None, caps: str = ""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if caps:
            headers["X-AgentOS-Client-Capabilities"] = caps
        return await self.client.post(f"/internal/local{path}", json=body, headers=headers)

    async def get(self, path: str, token: str | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self.client.get(f"/internal/local{path}", headers=headers)

    # ── seeding ───────────────────────────────────────────────────────
    async def ensure_user(self, uid: str) -> None:
        await self.conn.execute(
            "INSERT INTO users (uid, email, system_role) VALUES ($1, $2, 'member') "
            "ON CONFLICT (uid) DO NOTHING",
            uid, f"{uid}@fixture.test")

    async def seed_goal(self, members=(("person", "admin"),)) -> str:
        gid = f"{PREFIX}-g{uuid.uuid4().hex[:8]}"
        await self.conn.execute("INSERT INTO goals (id, name, created_by) VALUES ($1, $2, $3)",
                                gid, "fixture goal", members[0][0] if members else "person")
        for uid, role in members:
            await self.ensure_user(uid)
            await self.conn.execute(
                "INSERT INTO goal_members (goal_id, uid, role, added_by) VALUES ($1, $2, $3, $1)",
                gid, uid, role)
        return gid

    async def seed_story(self, *, goal_id: str | None = None, state: str = "in_progress",
                         mode: str = "local", assigned_to: str = "person",
                         phase: str | None = None, work_kind: str | None = None,
                         held_by: str | None = "person") -> tuple[str, str]:
        gid = goal_id or await self.seed_goal()
        sid = f"{PREFIX}-s{uuid.uuid4().hex[:8]}"
        await self.conn.execute(
            "INSERT INTO stories (id, goal_id, title, created_by, state, execution_mode, "
            " assigned_to, current_phase, work_kind, checked_out_to_uid, checkout_session_id, "
            " checkout_expires_at) "
            "VALUES ($1,$2,'fixture story','person',$3,$4,$5,$6,$7,$8,$9, now() + interval '5 minutes')",
            sid, gid, state, mode, assigned_to, phase, work_kind, held_by,
            "sess-1" if held_by else None)
        return gid, sid

    async def seed_node(self, story_id: str, goal_id: str, *, state: str = "BRANCH_CREATED",
                        owner: str = "acme", name: str | None = None, trunk: str = "main",
                        pr: int | None = None) -> str:
        rid = uuid.uuid4().hex
        name = name or f"repo-{rid[:6]}"
        await self.conn.execute(
            "INSERT INTO git_repos (id, owner, name, trunk_branch) VALUES ($1,$2,$3,$4)",
            rid, owner, name, trunk)
        nid = uuid.uuid4().hex
        await self.conn.execute(
            "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, state, trunk_branch, pr_number) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            nid, rid, goal_id, f"feat/{story_id}", state, trunk, pr)
        await self.conn.execute(
            "INSERT INTO git_node_stories (git_node_id, story_id, seq) VALUES ($1,$2,1)", nid, story_id)
        return nid

    # ── reading back ──────────────────────────────────────────────────
    async def story(self, story_id: str):
        return await self.conn.fetchrow("SELECT * FROM stories WHERE id = $1", story_id)

    async def ledger(self, goal_id: str, event_type: str | None = None):
        """Ledger events for a goal, oldest first, as (event_type, actor_type, payload)."""
        import json
        rows = await self.conn.fetch(
            "SELECT event_type, actor_type, payload FROM ledger_events "
            "WHERE context_goal_id = $1 AND ($2::text IS NULL OR event_type = $2) "
            "ORDER BY sequence_number", goal_id, event_type)
        return [(r["event_type"], r["actor_type"],
                 r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]))
                for r in rows]


@contextlib.asynccontextmanager
async def local_http(env: dict | None = None):
    """A LocalHttp bound to one rolled-back transaction. `env` adds/overrides
    environment variables for the duration (feature flags)."""
    import app.database as database

    conn = await _connect_or_skip()
    await conn.execute("SET search_path = kernel, public")
    tx = conn.transaction()
    await tx.start()
    saved_env = {k: os.environ.get(k) for k in
                 ("AGENT_TOKEN_SIGNING_KEY", "AGENT_SCOPED_TOKENS_ENABLED", "OPENCLAW_AUTH_TOKEN",
                  *(env or {}))}
    os.environ.update({"AGENT_TOKEN_SIGNING_KEY": SIGNING_KEY, "AGENT_SCOPED_TOKENS_ENABLED": "true",
                       "OPENCLAW_AUTH_TOKEN": SHARED_TOKEN, **(env or {})})
    # `get_settings` is lru-cached: a test earlier in the session may already have
    # built Settings without these values, and then the env above is never read.
    from app.config import get_settings
    get_settings.cache_clear()
    saved_pool = database._pool
    database._pool = PoolOnConn(conn)
    try:
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://cp.test") as client:
            yield LocalHttp(conn, client)
    finally:
        database._pool = saved_pool
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()
        await tx.rollback()
        await conn.close()
