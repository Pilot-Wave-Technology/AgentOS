"""C3 — an artifact is granted, never registered again.

WHAT HAPPENED ON KER25. Asked to make three errand reports available to
the fix story, GO called `register_resource` three times and created
`report_float_boundaries`, `report_coverage_audit` and
`report_api_fuzzing` — duplicate rows pointing at the SAME
`artifact://` URIs as the `art_<hash>` rows D2 had already created. It
granted its own duplicates; the real rows sat unused beside them.

Its prompt said, in as many words, that artifacts join the catalogue by
themselves and should be granted with `needs`. The live prompt carried
that paragraph — all four phrases verified in `template_files`. GO did
it anyway.

So the fix is not more words. Two shape changes:

  * `list_artifacts` now names the `resource` id for each artifact, so
    the grantable id is in the data GO is already looking at rather
    than something it must infer from a rule it read earlier;
  * `register_resource` REFUSES an `artifact://` source and names the
    row that already exists.

A prompt is advice and a signature is not — which is the lesson
`grant_resource`'s required `why` already encodes.
"""
from __future__ import annotations

import asyncio
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
ART = "artifact://" + "a" * 64


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


async def _goal(conn):
    gid = "zz-c3-" + os.urandom(4).hex()
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','alice')", gid)
    return gid


def test_registering_an_artifact_is_refused_and_names_the_real_row():
    async def body(conn):
        from kernel.go.tools import register_resource

        gid = await _goal(conn)
        r = await register_resource(conn, gid, "my_copy", ART, kind="doc")
        assert getattr(r, "code", None) == "E_ALREADY_CATALOGUED", r
        # It must name the id to grant, or the refusal leaves GO stuck.
        assert "art_" + "a" * 12 in r.message
        assert "needs" in r.message
    _run(body)


def test_an_ordinary_document_still_registers():
    """The refusal must be scoped to artifacts. Documents are the one
    kind this tool exists to create."""
    async def body(conn):
        from kernel.go.tools import register_resource

        gid = await _goal(conn)
        out = await register_resource(conn, gid, "zz_spec",
                                      "gs://bucket/spec.md", kind="doc")
        assert not hasattr(out, "code"), out
        assert out["kind"] == "document"
    _run(body)


def test_list_artifacts_names_the_grantable_resource_id():
    """The id GO needs, in the data it is already reading."""
    from kernel.assistant.artifacts import _row
    from kernel.harness.artifact_resource import resource_id_for

    class R(dict):
        def __getitem__(self, k):
            return self.get(k)

    row = _row(R(artifact_id="b" * 64, role="report", name="r.md",
                 summary="s", bytes=10, media_type="text/markdown",
                 story_id="s1", execution_id=1, created_at=None,
                 supersedes=None))
    assert row["resource"] == resource_id_for("b" * 64)
    assert row["resource"].startswith("art_")
