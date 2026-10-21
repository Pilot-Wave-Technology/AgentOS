"""Ec4d — the grant is the boundary, so this is where scoping is real.

WHY THIS CARRIES THE WEIGHT. The sandbox joins the WORKER's network
namespace (`network_mode=container:<worker>`), so anything the worker can
reach on the network, model-authored bash can reach too — including a
minting sidecar and, on GCP, the metadata server at 169.254.169.254.
Verified on a live sandbox: general egress works, and the only reason
metadata is unreachable locally is that a laptop has no metadata server.

So network placement cannot be the boundary. The only thing standing
between a run and a cloud identity is this module answering "was it
granted".
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"

SA_A = "cf-invoker@project-a.iam.gserviceaccount.com"
SA_B = "deployer@project-b.iam.gserviceaccount.com"


async def _connect_or_skip():
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception:                                   # noqa: BLE001
            continue
    pytest.skip("no local database")


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


async def _story(conn):
    tag = os.urandom(4).hex()
    goal, story = f"zz-cr-{tag}", f"zzs{tag[:6]}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')", goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'t','in_progress','alice')", story, goal)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, state) "
        "VALUES ('story','coding',$1,$2,'RUNNING') RETURNING id", goal, story)
    return goal, story, eid


async def _grant(conn, goal, story, rid, kind, metadata, relation="needs"):
    await conn.execute(
        """INSERT INTO goal_resources (id, goal_id, kind, name, metadata,
                                       created_by, status)
           VALUES ($1,$2,$3,$1,$4::jsonb,'alice','active')""",
        rid, goal, kind, json.dumps(metadata))
    await conn.execute(
        "INSERT INTO story_resources (story_id, resource_id, relation) "
        "VALUES ($1,$2,$3)", story, rid, relation)


def test_no_grant_means_no_identity():
    """The ordinary case. Almost no run needs cloud reach, and a run
    without a grant gets nothing — silently, not as an error."""
    async def body(conn):
        from kernel.credentials.authorize import granted_identity

        _, _, eid = await _story(conn)
        assert await granted_identity(conn, eid) is None
    _run(body)


def test_an_environment_alone_mints_nothing():
    """A project context is not an identity. Holding it must not be
    enough, or "which project" and "may act there" collapse into one
    grant."""
    async def body(conn):
        from kernel.credentials.authorize import granted_identity

        goal, story, eid = await _story(conn)
        await _grant(conn, goal, story, "env_a", "gcp_environment",
                     {"target_project_id": "project-a"})
        assert await granted_identity(conn, eid) is None
    _run(body)


def test_a_granted_account_with_its_environment_resolves():
    async def body(conn):
        from kernel.credentials.authorize import granted_identity

        goal, story, eid = await _story(conn)
        await _grant(conn, goal, story, "env_a", "gcp_environment",
                     {"target_project_id": "project-a",
                      "target_region": "us-central1"})
        await _grant(conn, goal, story, "sa_a", "gcp_test_sa",
                     {"sa_email": SA_A})
        got = await granted_identity(conn, eid)
        assert got["account"] == SA_A
        assert got["scope"] == "project-a"
        assert got["region"] == "us-central1"
        assert got["resource_id"] == "sa_a"
    _run(body)


def test_two_identities_are_refused():
    """A run has one identity. With two, every command has two and the
    record cannot say which it used — the first question anybody asks
    when something goes wrong."""
    async def body(conn):
        from kernel.credentials.authorize import NotAuthorised, granted_identity

        goal, story, eid = await _story(conn)
        await _grant(conn, goal, story, "env_a", "gcp_environment",
                     {"target_project_id": "project-a"})
        await _grant(conn, goal, story, "sa_a", "gcp_test_sa",
                     {"sa_email": SA_A})
        await _grant(conn, goal, story, "sa_a2", "gcp_deploy_sa",
                     {"sa_email": "other@project-a.iam.gserviceaccount.com"})
        with pytest.raises(NotAuthorised) as e:
            await granted_identity(conn, eid)
        assert e.value.code == "E_TWO_IDENTITIES"
    _run(body)


def test_an_account_from_another_project_is_refused():
    """THE SCOPING TEST. A story holding project A's environment may not
    act as project B's account, or the environment bounds nothing and one
    grant reaches every project the platform can."""
    async def body(conn):
        from kernel.credentials.authorize import NotAuthorised, granted_identity

        goal, story, eid = await _story(conn)
        await _grant(conn, goal, story, "env_a", "gcp_environment",
                     {"target_project_id": "project-a"})
        await _grant(conn, goal, story, "sa_b", "gcp_deploy_sa",
                     {"sa_email": SA_B})
        with pytest.raises(NotAuthorised) as e:
            await granted_identity(conn, eid)
        assert e.value.code == "E_SCOPE_MISMATCH"
    _run(body)


def test_an_account_without_its_environment_is_refused():
    async def body(conn):
        from kernel.credentials.authorize import NotAuthorised, granted_identity

        goal, story, eid = await _story(conn)
        await _grant(conn, goal, story, "sa_a", "gcp_test_sa",
                     {"sa_email": SA_A})
        with pytest.raises(NotAuthorised) as e:
            await granted_identity(conn, eid)
        assert e.value.code == "E_NO_ENVIRONMENT"
    _run(body)


def test_a_malformed_account_is_refused_not_guessed():
    async def body(conn):
        from kernel.credentials.authorize import NotAuthorised, granted_identity

        goal, story, eid = await _story(conn)
        await _grant(conn, goal, story, "env_a", "gcp_environment",
                     {"target_project_id": "project-a"})
        await _grant(conn, goal, story, "sa_x", "gcp_test_sa",
                     {"sa_email": "somebody@gmail.com"})
        with pytest.raises(NotAuthorised) as e:
            await granted_identity(conn, eid)
        assert e.value.code == "E_BAD_ACCOUNT"
    _run(body)


def test_another_storys_grant_does_not_leak():
    """Grants are per story. A second story in the same goal holding the
    account must not give this execution an identity."""
    async def body(conn):
        from kernel.credentials.authorize import granted_identity

        goal, story, eid = await _story(conn)
        other = "zzother1"
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, state, created_by) "
            "VALUES ($1,$2,'t','in_progress','alice')", other, goal)
        await _grant(conn, goal, other, "env_a", "gcp_environment",
                     {"target_project_id": "project-a"})
        await _grant(conn, goal, other, "sa_a", "gcp_test_sa",
                     {"sa_email": SA_A})
        assert await granted_identity(conn, eid) is None
    _run(body)


def test_unreadable_metadata_fails_closed():
    """asyncpg hands jsonb back as TEXT and this codebase has been bitten
    four times. For a credential decision the safe direction is no
    identity, never a guess."""
    from kernel.credentials.authorize import _as_dict

    assert _as_dict("not json") == {}
    assert _as_dict(None) == {}
    assert _as_dict('["a"]') == {}
    assert _as_dict('{"a": 1}') == {"a": 1}
    assert _as_dict({"a": 1}) == {"a": 1}
