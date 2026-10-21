"""Ec4d — one grant shape, three clouds, and where they genuinely differ.

Every cloud here has the same two-part grant: a SCOPE (project, account,
subscription) and an IDENTITY within it (service account, role, managed
identity). A run is granted both and reaches exactly that scope. The
authorisation module, the lease seam, the pointer and the sweep are all
cloud-neutral; only minting and the file format are per cloud.

THREE PLACES THEY REALLY DIFFER, each asserted here rather than assumed:

  * WHETHER THE SCOPE IS DERIVABLE. A GCP service-account email carries
    its project and an AWS role ARN carries its account, so the pairing
    can be CHECKED and cannot be lied to. An Azure client id carries
    nothing, so it is taken from the registration -- and the caller is
    told which it got, via `scope_verified`.
  * WHAT COMES BACK. GCP one bearer token; AWS a triple, all three of
    which are needed; Azure a token with a lifetime it chooses.
  * WHAT THE TOOLING READS. ADC JSON, an INI, a token file -- all of
    them PATHS, which is what lets the pointer model hold everywhere.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"

GCP_SA = "cf-invoker@project-a.iam.gserviceaccount.com"
AWS_ROLE = "arn:aws:iam::123456789012:role/agentos-reader"
AZ_ID = "11111111-2222-3333-4444-555555555555"


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


async def _granted(conn, env_kind, env_meta, id_kind, id_meta):
    tag = os.urandom(4).hex()
    goal, story = f"zz-cl-{tag}", f"zzs{tag[:6]}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')", goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by) "
        "VALUES ($1,$2,'t','in_progress','alice')", story, goal)
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id, state) "
        "VALUES ('story','coding',$1,$2,'RUNNING') RETURNING id", goal, story)
    for rid, kind, meta in ((f"env_{tag}", env_kind, env_meta),
                            (f"id_{tag}", id_kind, id_meta)):
        await conn.execute(
            """INSERT INTO goal_resources (id, goal_id, kind, name, metadata,
                                           created_by, status)
               VALUES ($1,$2,$3,$1,$4::jsonb,'alice','active')""",
            rid, goal, kind, json.dumps(meta))
        await conn.execute(
            "INSERT INTO story_resources (story_id, resource_id, relation) "
            "VALUES ($1,$2,'needs')", story, rid)
    return eid


# ── the same grant shape resolves for all three ──────────────────────────

def test_each_cloud_resolves_through_the_same_function():
    async def body(conn):
        from kernel.credentials.authorize import granted_identity

        cases = [
            ("gcp", "gcp_environment", {"target_project_id": "project-a"},
             "gcp_test_sa", {"sa_email": GCP_SA}, GCP_SA, "project-a"),
            ("aws", "aws_account", {"account_id": "123456789012"},
             "aws_test_role", {"role_arn": AWS_ROLE}, AWS_ROLE, "123456789012"),
            ("azure", "azure_subscription", {"subscription_id": "sub-1"},
             "azure_test_identity", {"client_id": AZ_ID}, AZ_ID, "sub-1"),
        ]
        for cloud, ek, em, ik, im, account, scope in cases:
            eid = await _granted(conn, ek, em, ik, im)
            got = await granted_identity(conn, eid)
            assert got is not None, cloud
            assert got["cloud"] == cloud
            assert got["account"] == account
            assert got["scope"] == scope
    _run(body)


def test_only_gcp_and_aws_can_prove_the_pairing():
    """`scope_verified` is the honest answer to "was this checked or
    taken on trust". Azure client ids encode no subscription, so the
    binding is asserted at registration and cannot be derived; saying so
    beats a check that silently does nothing."""
    async def body(conn):
        from kernel.credentials.authorize import granted_identity

        gcp = await _granted(conn, "gcp_environment",
                             {"target_project_id": "project-a"},
                             "gcp_test_sa", {"sa_email": GCP_SA})
        aws = await _granted(conn, "aws_account", {"account_id": "123456789012"},
                             "aws_test_role", {"role_arn": AWS_ROLE})
        azu = await _granted(conn, "azure_subscription",
                             {"subscription_id": "sub-1"},
                             "azure_test_identity", {"client_id": AZ_ID})
        assert (await granted_identity(conn, gcp))["scope_verified"] is True
        assert (await granted_identity(conn, aws))["scope_verified"] is True
        assert (await granted_identity(conn, azu))["scope_verified"] is False
    _run(body)


def test_a_role_from_another_account_is_refused():
    """The AWS half of the scoping test — the same rule as GCP's, proved
    from the ARN rather than from an email."""
    async def body(conn):
        from kernel.credentials.authorize import NotAuthorised, granted_identity

        eid = await _granted(conn, "aws_account", {"account_id": "999999999999"},
                             "aws_test_role", {"role_arn": AWS_ROLE})
        with pytest.raises(NotAuthorised) as e:
            await granted_identity(conn, eid)
        assert e.value.code == "E_SCOPE_MISMATCH"
    _run(body)


def test_a_gcp_environment_cannot_scope_an_aws_role():
    """Only an environment of the SAME cloud can bound an identity. A GCP
    project does not bound an AWS role, and holding one must not be
    mistaken for holding the other."""
    async def body(conn):
        from kernel.credentials.authorize import NotAuthorised, granted_identity

        eid = await _granted(conn, "gcp_environment",
                             {"target_project_id": "project-a"},
                             "aws_test_role", {"role_arn": AWS_ROLE})
        with pytest.raises(NotAuthorised) as e:
            await granted_identity(conn, eid)
        assert e.value.code == "E_NO_ENVIRONMENT"
    _run(body)


def test_a_malformed_arn_is_refused():
    async def body(conn):
        from kernel.credentials.authorize import NotAuthorised, granted_identity

        eid = await _granted(conn, "aws_account", {"account_id": "123456789012"},
                             "aws_test_role", {"role_arn": "not-an-arn"})
        with pytest.raises(NotAuthorised) as e:
            await granted_identity(conn, eid)
        assert e.value.code == "E_BAD_ACCOUNT"
    _run(body)


# ── the file each cloud's tooling actually reads ─────────────────────────

def test_every_pointer_is_a_path_and_never_a_value():
    """THE RULE THE WHOLE MODEL RESTS ON. A value in the environment is
    in every child's environ, cannot be revoked mid-run, and has no lease
    id to point at. Each cloud has a file convention precisely so this
    works everywhere."""
    from kernel.credentials.provider import provider_for
    from kernel.workspace.credentials import holds_secret

    path = "/workspace/.agentos/credentials/exec-1-cloud.json"

    # The variable each cloud's OWN tooling reads. GCP is deliberately
    # not GOOGLE_APPLICATION_CREDENTIALS: pointing that at the lease
    # record is WORSE than leaving it unset, because google-auth finds
    # it, fails to parse it and raises -- so a library that would have
    # fallen back to the ambient identity reports a hard credential
    # error instead. gcloud reads a bare token from
    # CLOUDSDK_AUTH_ACCESS_TOKEN_FILE, and the provider derives that
    # path from the lease's. See kernel/credentials/gcp.py.
    expected = {"gcp": "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
                "aws": "AWS_SHARED_CREDENTIALS_FILE",
                "azure": "AZURE_ACCESS_TOKEN_FILE"}
    stem = path[:-len(".json")]
    for cloud, variable in expected.items():
        pointers = provider_for(cloud).sandbox_pointers(path)
        assert variable in pointers, cloud
        # A path, and one belonging to THIS lease -- the record itself
        # or a sibling derived from it. Asserting the exact string would
        # forbid the derivation; asserting the stem still forbids
        # pointing somewhere else entirely.
        assert str(pointers[variable]).startswith(stem), (cloud, pointers)
        for value in pointers.values():
            assert not holds_secret(str(value)), cloud


def test_aws_writes_an_ini_because_that_is_what_its_sdks_read():
    """GCP's ADC JSON shape would be unreadable to every AWS SDK. The
    lease is a file; its format is the provider's business."""
    from kernel.credentials.aws import AwsProvider

    minted = {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "s3cret",
              "session_token": "sess", "account": AWS_ROLE,
              "scope": "123456789012", "expires_at": "2026-01-01T00:00:00Z"}
    text = AwsProvider().lease_payload(minted, {"cloud": "aws"})
    assert isinstance(text, str), "an AWS lease is an INI, not JSON"
    for line in ("[default]", "aws_access_key_id = AKIAEXAMPLE",
                 "aws_secret_access_key = s3cret",
                 "aws_session_token = sess"):
        assert line in text
    # Provenance still travels, as comments, because a file found on disk
    # should answer "whose is this".
    assert AWS_ROLE in text


def test_a_string_lease_round_trips_through_the_seam():
    """`write_lease` had to learn about non-JSON leases for AWS. Checked
    end to end rather than by reading the code, because the failure would
    be a credentials file no SDK can parse."""
    import tempfile

    from kernel.workspace.credentials import lease_path, write_lease

    with tempfile.TemporaryDirectory() as ws:
        ini = "[default]\naws_access_key_id = AKIAX\n"
        write_lease(ws, "exec-1-cloud", ini)
        with open(lease_path(ws, "exec-1-cloud"), encoding="utf-8") as fh:
            assert fh.read() == ini


def test_the_stub_is_shaped_like_a_real_credential():
    """A stub whose lease looked different would exercise a different
    path than the one that matters."""
    from kernel.credentials.stub import StubProvider
    from kernel.workspace.credentials import holds_secret

    async def go():
        minted = await StubProvider().mint(
            {"account": GCP_SA, "scope": "project-a"}, 1)
        payload = StubProvider().lease_payload(minted, {"cloud": "gcp"})
        assert payload["type"] == "authorized_user"
        assert holds_secret(payload["access_token"])

    asyncio.run(go())


def test_the_registry_knows_every_kind_it_claims():
    """`ALL_IDENTITY_KINDS` drives the database query. A kind in CLOUDS
    but missing from the aggregate would never be looked up, and the
    grant would silently do nothing."""
    from kernel.credentials.clouds import (ALL_ENVIRONMENT_KINDS,
                                           ALL_IDENTITY_KINDS, CLOUDS,
                                           cloud_of_kind)

    for cloud in CLOUDS.values():
        assert cloud.environment_kind in ALL_ENVIRONMENT_KINDS
        assert cloud_of_kind(cloud.environment_kind) is cloud
        for kind in cloud.identity_kinds:
            assert kind in ALL_IDENTITY_KINDS
            assert cloud_of_kind(kind) is cloud
    assert cloud_of_kind("not_a_kind") is None
