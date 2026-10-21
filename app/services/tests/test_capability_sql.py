"""Ec4a/Ec4c — grants decide reach, and the ceiling decides grants.

`story_resources` says a story HOLDS a resource with a relation — `reads`,
`produces`, `modifies`. Right for a repository; nearly useless for a cloud
account, where the resource is a project and the question is which of ten
thousand operations are permitted.

So a capability grant names VERBS, a SCOPE, and a CEILING. **The ceiling is
the load-bearing part.** Without it the only check is "is this verb in the
grant", and the grant is written per story by whoever set the story up. The
ceiling is registered once against the resource by whoever owns the
account, so a story cannot be given more than the account was ever
registered to allow — however the grant was written and whoever wrote it.
That is the difference between a grant and a wish.

WHAT ONLY REAL SQL ANSWERS, and therefore what a mocked pool lets past:

  * the `verbs <@ ceiling` CHECK, which is the one enforcement no code
    path can go around — a grant written by a migration, a console or a
    mistake is checked the same way
  * `cardinality(verbs) > 0`, so a row of nothing does not read as
    "granted" to every count and card
  * the live-grant query's three conditions together — revoked, expired,
    and story scope — which a mock would assert one at a time
  * that a grant goes stale when the REGISTRATION narrows underneath it,
    which the database CHECK cannot see because it compares against the
    ceiling copied onto the row

Real Postgres, one transaction, always rolled back.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from kernel.workspace import capability as cap

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-cap-test"


async def _connect_or_skip():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
        ok = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name IN ('capability_grants','goal_resources',"
            "'story_resources','executions')")
        if ok >= 4:
            return conn
        await conn.close()
    pytest.skip("no local database with the kernel schema — start the stack")


def _run(body):
    async def _go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(_go())


async def _fixture(conn, ceiling=("logs.*", "storage.list"),
                   uri="https://console.cloud.google.com/x"):
    """A goal, a story, a registered resource and an execution."""
    tag = os.urandom(4).hex()
    goal, story = f"{PREFIX}-g-{tag}", f"{PREFIX}-s-{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','tester')",
        goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'s','in_progress','tester')", story, goal)
    resource = f"{PREFIX}-r-{tag}"
    await conn.execute(
        "INSERT INTO goal_resources (id, goal_id, kind, name, uri, metadata,"
        " created_by) VALUES ($1,$2,'service','svc',$3,$4::jsonb,'tester')",
        resource, goal, uri,
        json.dumps({"capability_ceiling": list(ceiling)}))
    await conn.execute(
        "INSERT INTO story_resources (story_id, resource_id, relation) "
        "VALUES ($1,$2,'needs')", story, resource)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id) "
        "VALUES ('errand','ops',$1,$2) RETURNING id", goal, story)
    return {"goal": goal, "story": story, "resource": resource, "eid": eid}


async def _grant(conn, f, verbs, **kw):
    return await conn.fetchval(
        """
        INSERT INTO kernel.capability_grants
            (goal_id, story_id, resource_id, verbs, scope, ceiling,
             expires_at, revoked_at, granted_by)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'tester') RETURNING id
        """,
        f["goal"], kw.get("story", f["story"]), f["resource"],
        list(verbs), kw.get("scope", ""),
        list(kw.get("ceiling", ("logs.*", "storage.list"))),
        kw.get("expires_at"), kw.get("revoked_at"))


# ── the ceiling, as pure logic ───────────────────────────────────────────

@pytest.mark.parametrize("verb", ["logs.read", "storage.list",
                                  "run.services.get", "a.b.c.d"])
def test_a_well_formed_verb_is_accepted(verb):
    assert cap.normalise([verb]) == (verb,)


def test_case_is_normalised_rather_than_refused():
    """`Logs.Read` unambiguously means `logs.read`. Refusing it would be
    friction for no safety: the comparison, the card and the provider
    mapping all use the lowered form, so lowering on the way in is the
    same answer with one fewer way to get it wrong."""
    assert cap.normalise(["Logs.Read", "  STORAGE.list "]) == (
        "logs.read", "storage.list")


def test_duplicates_collapse_and_order_is_stable():
    """A card lists these. `logs.read, logs.read, storage.list` reads as
    a mistake even when it is harmless."""
    assert cap.normalise(["logs.read", "storage.list", "logs.read"]) == (
        "logs.read", "storage.list")


@pytest.mark.parametrize("bad", [
    "logs", "logs.", ".read", "logs..read", "logs read",
    "logs.*", "*", "a.b.c.d.e", "logs.read;drop",
])
def test_a_malformed_verb_is_refused(bad):
    """`logs.*` is refused IN A GRANT specifically: a wildcard's meaning
    changes when the provider adds an action, and nobody reviewed that."""
    with pytest.raises(cap.BadCapability):
        cap.normalise([bad])


def test_a_wildcard_is_allowed_in_a_ceiling_and_covers_its_family():
    assert cap.normalise_ceiling(["logs.*"]) == ("logs.*",)
    assert cap.covers(("logs.*",), "logs.read")
    assert cap.covers(("logs.*",), "logs.tail")
    assert not cap.covers(("logs.*",), "storage.list")
    assert not cap.covers(("logs.*",), "logsx.read")


def test_a_grant_of_nothing_is_not_a_grant():
    with pytest.raises(cap.BadCapability):
        cap.normalise([])


def test_the_refusal_names_the_excess_and_the_ceiling():
    """"refused" sends whoever wrote the grant to read a table. Naming
    both tells them either to narrow the grant or to widen the
    registration — which are different people's decisions."""
    said = cap.check(["storage.delete", "logs.read"],
                     ["logs.*", "storage.list"])
    assert "storage.delete" in said
    assert "logs.*" in said and "storage.list" in said
    assert "different decisions" in said


def test_a_resource_with_no_registered_ceiling_grants_nothing():
    """Fail closed. An unregistered account is one nobody has said what
    it may do, and defaulting to "anything" is the wrong direction."""
    said = cap.check(["logs.read"], [])
    assert "no registered capability ceiling" in said


# ── the database enforces it too ─────────────────────────────────────────

def test_the_database_refuses_a_grant_above_its_ceiling():
    """THE ONE ENFORCEMENT NO CODE PATH GOES AROUND. A grant written by a
    migration, a console, a future service or a mistake meets this."""
    async def body(conn):
        f = await _fixture(conn)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await _grant(conn, f, ["storage.delete"],
                         ceiling=["logs.*", "storage.list"])
    _run(body)


def test_the_database_refuses_a_grant_of_no_verbs():
    async def body(conn):
        f = await _fixture(conn)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await _grant(conn, f, [])
    _run(body)


def test_a_live_grant_is_readable_and_a_revoked_one_is_not():
    async def body(conn):
        f = await _fixture(conn)
        gid = await _grant(conn, f, ["logs.read"])
        got = await cap.granted_verbs(conn, f["eid"])
        assert got == {f["resource"]: ("logs.read",)}
        assert cap.permits(got, f["resource"], "logs.read")
        assert not cap.permits(got, f["resource"], "storage.list")

        await conn.execute(
            "UPDATE kernel.capability_grants SET revoked_at = now() "
            "WHERE id = $1", gid)
        assert await cap.granted_verbs(conn, f["eid"]) == {}
    _run(body)


def test_an_expired_grant_is_not_a_grant():
    """TTL as an absolute timestamp, not a duration: a duration has to be
    added to something, and granted-at and read-at differ by however long
    the row sat there."""
    async def body(conn):
        import datetime as dt

        f = await _fixture(conn)
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        await _grant(conn, f, ["logs.read"], expires_at=past)
        assert await cap.granted_verbs(conn, f["eid"]) == {}

        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        await _grant(conn, f, ["storage.list"], expires_at=future)
        assert await cap.granted_verbs(conn, f["eid"]) == {
            f["resource"]: ("storage.list",)}
    _run(body)


def test_another_storys_grant_does_not_reach_this_execution():
    async def body(conn):
        f = await _fixture(conn)
        await _grant(conn, f, ["logs.read"], story=f"{PREFIX}-elsewhere")
        assert await cap.granted_verbs(conn, f["eid"]) == {}
    _run(body)


def test_a_goal_level_grant_covers_every_story_in_it():
    """`story_id IS NULL` is the wider form, and it has to work — a
    long-lived read-only observability grant is a goal fact."""
    async def body(conn):
        f = await _fixture(conn)
        await _grant(conn, f, ["logs.read"], story=None)
        assert await cap.granted_verbs(conn, f["eid"]) == {
            f["resource"]: ("logs.read",)}
    _run(body)


def test_a_grant_goes_stale_when_the_registration_narrows_under_it():
    """THE CHECK THE DATABASE CANNOT DO. Its CHECK compares against the
    ceiling COPIED onto the row, which is exactly the value that goes
    stale. Re-checking at read time is what makes narrowing a
    registration take effect on grants already written."""
    async def body(conn):
        f = await _fixture(conn, ceiling=("logs.*", "storage.list"))
        await _grant(conn, f, ["logs.read", "storage.list"])
        assert await cap.granted_verbs(conn, f["eid"]) == {
            f["resource"]: ("logs.read", "storage.list")}

        # The account's owner narrows what it is registered to allow.
        await conn.execute(
            "UPDATE goal_resources SET metadata = $2::jsonb WHERE id = $1",
            f["resource"], json.dumps({"capability_ceiling": ["logs.*"]}))
        assert await cap.granted_verbs(conn, f["eid"]) == {
            f["resource"]: ("logs.read",)}, "storage.list is still live"
    _run(body)


# ── Ec4a: the allowlist comes from the grants ────────────────────────────

def test_reach_follows_the_storys_own_grants():
    from kernel.workspace.egress import BASE_HOSTS, allowed_hosts, matches

    async def body(conn):
        f = await _fixture(conn, uri="https://api.internal.example/v1")
        hosts = await allowed_hosts(conn, f["eid"])
        assert "api.internal.example" in hosts
        assert matches("api.internal.example", hosts)
        # And nothing else.
        assert not matches("smtp.sendgrid.net", hosts)
        for base in BASE_HOSTS:
            assert base in hosts
    _run(body)


def test_a_run_with_no_grants_reaches_only_the_registries():
    from kernel.workspace.egress import BASE_HOSTS, allowed_hosts

    async def body(conn):
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id) "
            "VALUES ('errand','ops',$1) RETURNING id", f"{PREFIX}-bare")
        assert await allowed_hosts(conn, eid) == frozenset(BASE_HOSTS)
    _run(body)


def test_a_resource_may_name_extra_hosts_it_needs():
    """A cloud resource's verb set cannot imply its endpoints — the same
    `logs.read` is a different host on every provider — so the
    registration says."""
    from kernel.workspace.egress import allowed_hosts

    async def body(conn):
        tag = os.urandom(4).hex()
        goal, story = f"{PREFIX}-hg-{tag}", f"{PREFIX}-hs-{tag}"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) "
            "VALUES ($1,'t','tester')", goal)
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, state, created_by) "
            "VALUES ($1,$2,'s','in_progress','tester')", story, goal)
        rid = f"{PREFIX}-hr-{tag}"
        await conn.execute(
            "INSERT INTO goal_resources (id, goal_id, kind, name, metadata,"
            " created_by) VALUES ($1,$2,'service','svc',$3::jsonb,'tester')",
            rid, goal, json.dumps({"hosts": ["logging.googleapis.com",
                                             "storage.googleapis.com"]}))
        await conn.execute(
            "INSERT INTO story_resources (story_id, resource_id, relation) "
            "VALUES ($1,$2,'needs')", story, rid)
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, story_id)"
            " VALUES ('errand','ops',$1,$2) RETURNING id", goal, story)

        hosts = await allowed_hosts(conn, eid)
        assert "logging.googleapis.com" in hosts
        assert "storage.googleapis.com" in hosts
    _run(body)
