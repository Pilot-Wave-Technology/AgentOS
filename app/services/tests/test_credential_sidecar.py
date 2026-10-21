"""Ec4d — the sidecar assumes its caller is hostile, because it is reachable.

THE HOLE IT CLOSES. Minting in-process needs the worker's namespace to
reach the metadata server, and the sandbox SHARES that namespace
(`network_mode=container:<worker>`, verified on a live sandbox). So an
in-process mint on GCP means model-authored bash can take the host
identity directly and impersonate anything IAM allows, regardless of
grants.

The sidecar mints in a namespace the sandbox is not in. It is still
REACHABLE from the sandbox — that is unavoidable — so two independent
checks stand in the way, and this file is about both:

  * a shared secret the sandbox cannot obtain (`_safe_env` is an
    allowlist and the secret is not on it);
  * authorisation against the grant, so a stolen secret buys only what
    the named execution already had.

Without the first, a sandbox could name ANOTHER execution and collect its
credential. Authorisation alone is not enough here, which is why both are
tested.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
SA = "cf-invoker@project-a.iam.gserviceaccount.com"
SECRET = "test-sidecar-secret-value"


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


async def _granted(conn, project="project-a", sa_email=SA):
    tag = os.urandom(4).hex()
    goal, story = f"zz-sc-{tag}", f"zzs{tag[:6]}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')", goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'t','in_progress','alice')", story, goal)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, state) "
        "VALUES ('story','coding',$1,$2,'RUNNING') RETURNING id", goal, story)
    for rid, kind, meta in (
            (f"env_{tag}", "gcp_environment", {"target_project_id": project}),
            (f"sa_{tag}", "gcp_test_sa", {"sa_email": sa_email})):
        await conn.execute(
            """INSERT INTO goal_resources (id, goal_id, kind, name, metadata,
                                           created_by, status)
               VALUES ($1,$2,$3,$1,$4::jsonb,'alice','active')""",
            rid, goal, kind, json.dumps(meta))
        await conn.execute(
            "INSERT INTO story_resources (story_id, resource_id, relation) "
            "VALUES ($1,$2,'needs')", story, rid)
    return eid


# ── authentication: the sandbox has nothing to present ───────────────────

def test_a_caller_without_the_secret_is_refused():
    from kernel.credentials.sidecar_server import SidecarRefused, check_auth

    os.environ["AGENTOS_CREDENTIAL_SIDECAR_TOKEN"] = SECRET
    try:
        for header in (None, "", "Bearer ", "Bearer wrong", "Basic x"):
            with pytest.raises(SidecarRefused) as e:
                check_auth(header)
            assert e.value.status == 401
        check_auth(f"Bearer {SECRET}")          # the worker's call
    finally:
        os.environ.pop("AGENTOS_CREDENTIAL_SIDECAR_TOKEN", None)


def test_it_will_not_run_without_a_secret_configured():
    """A sidecar with no secret is one the sandbox can call, and it runs
    in the one namespace that can mint."""
    from kernel.credentials.sidecar_server import SidecarRefused, check_auth

    was = os.environ.pop("AGENTOS_CREDENTIAL_SIDECAR_TOKEN", None)
    try:
        with pytest.raises(SidecarRefused) as e:
            check_auth("Bearer anything")
        assert e.value.status == 500
    finally:
        if was is not None:
            os.environ["AGENTOS_CREDENTIAL_SIDECAR_TOKEN"] = was


def test_the_worker_secret_is_not_on_the_sandbox_allowlist():
    """THE REASON AUTHENTICATION WORKS AT ALL. If the secret were
    forwarded to the sandbox, the sandbox could authenticate as the
    worker and name any execution."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/bridge/tools_core.py", encoding="utf-8").read()
    block = src[src.index("def _safe_env"):]
    block = block[:block.index("\n\n\n")] if "\n\n\n" in block else block
    assert "AGENTOS_CREDENTIAL_SIDECAR_TOKEN" not in block
    assert "AGENTOS_CREDENTIAL_SIDECAR_URL" not in block


# ── authorisation: the request names a run, never an account ─────────────

def test_an_execution_with_no_grant_gets_nothing():
    async def body(conn):
        from kernel.credentials.sidecar_server import (SidecarRefused,
                                                       handle_mint)

        gid = "zz-sc-none"
        await conn.execute(
            "INSERT INTO goals (id,name,created_by) VALUES ($1,'t','a')", gid)
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind,domain,goal_id,state) "
            "VALUES ('story','coding',$1,'RUNNING') RETURNING id", gid)
        with pytest.raises(SidecarRefused) as e:
            await handle_mint(conn, {"execution_id": eid})
        assert e.value.status == 403
    _run(body)


def test_a_caller_naming_a_different_account_is_refused():
    """The caller's opinion is checked and never used. A mismatch means
    the worker and the sidecar disagree about who this run is, and
    minting the grant's answer anyway would hide that."""
    async def body(conn):
        from kernel.credentials.sidecar_server import (SidecarRefused,
                                                       handle_mint)

        eid = await _granted(conn)
        with pytest.raises(SidecarRefused) as e:
            await handle_mint(conn, {
                "execution_id": eid,
                "sa_email": "someone-else@project-a.iam.gserviceaccount.com"})
        assert e.value.status == 409
    _run(body)


def test_a_cross_project_grant_is_refused_by_the_sidecar_too():
    """The scoping rule must hold at every place that can mint, not only
    in the worker. The sidecar is reachable from the sandbox; the worker
    is not the only possible caller."""
    async def body(conn):
        from kernel.credentials.sidecar_server import (SidecarRefused,
                                                       handle_mint)

        eid = await _granted(
            conn, project="project-a",
            sa_email="deployer@project-b.iam.gserviceaccount.com")
        with pytest.raises(SidecarRefused) as e:
            await handle_mint(conn, {"execution_id": eid})
        assert e.value.status == 403
        assert "E_SCOPE_MISMATCH" in e.value.message
    _run(body)


def test_a_bad_execution_id_is_refused_before_anything_else():
    async def body(conn):
        from kernel.credentials.sidecar_server import (SidecarRefused,
                                                       handle_mint)

        for bad in ({}, {"execution_id": 0}, {"execution_id": -1},
                    {"execution_id": "abc"}):
            with pytest.raises(SidecarRefused) as e:
                await handle_mint(conn, bad)
            assert e.value.status == 400
    _run(body)


def test_a_refusal_never_carries_a_token():
    from kernel.credentials.sidecar_server import SidecarRefused, error_body

    body = error_body(SidecarRefused(403, "E_SA_NOT_IN_ENVIRONMENT: nope"))
    parsed = json.loads(body)
    assert set(parsed) == {"refused"}


# ── the client half ──────────────────────────────────────────────────────

def test_the_client_refuses_to_call_an_unauthenticated_sidecar():
    """Rather than trying and hoping. An unauthenticated sidecar is one
    the sandbox can call."""
    from kernel.credentials.provider import MintFailed
    from kernel.credentials.sidecar import SidecarProvider

    async def go():
        os.environ.pop("AGENTOS_CREDENTIAL_SIDECAR_URL", None)
        os.environ.pop("AGENTOS_CREDENTIAL_SIDECAR_TOKEN", None)
        with pytest.raises(MintFailed) as e:
            await SidecarProvider().mint({"account": SA, "cloud": "gcp"}, 1)
        assert e.value.code == "E_NO_SIDECAR"

        os.environ["AGENTOS_CREDENTIAL_SIDECAR_URL"] = "http://127.0.0.1:1"
        try:
            with pytest.raises(MintFailed) as e2:
                await SidecarProvider().mint({"account": SA, "cloud": "gcp"}, 1)
            assert e2.value.code == "E_NO_SIDECAR_TOKEN"
        finally:
            os.environ.pop("AGENTOS_CREDENTIAL_SIDECAR_URL", None)

    asyncio.run(go())
