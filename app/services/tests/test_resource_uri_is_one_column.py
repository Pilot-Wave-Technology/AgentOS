"""`goal_resources.uri` is the locator; `gcs_uri` is a copy of it.

WHAT THIS UNBLOCKS. `kernel/` is meant to be free of any one cloud, and
four of its SQL sites read a column called `gcs_uri` — the storage
provider's name, in the substrate-agnostic half of the system. They read
it as the last arm of a COALESCE after `uri`, so it never once changed
an answer: `app/routers/chat.py` inserts THE SAME VALUE into both
columns on upload.

Measured before removing it: 12 rows carry a real `gcs_uri`, and all 12
have `uri` equal to it. The other 123 rows are repos and artifacts,
where `uri` holds an `https://` or `artifact://` locator and `gcs_uri`
is the empty string — which is why a naive `uri IS DISTINCT FROM
gcs_uri` count says 121 and means nothing.

WHY A TEST AND NOT JUST A COMMENT. The removal is safe because of an
invariant nothing enforces: every writer populates `uri`. A future
writer that sets only `gcs_uri` would make the resource invisible to the
kernel — no mount, no egress host — and nothing would raise. The story
would simply not see the file it was given, which is indistinguishable
from the file not having been attached.

The column stays. Dropping it is a contract change for `app/`, and
expand/contract says the drop is a later deploy than the last read —
see scripts/check_expand_contract.py.
"""

from __future__ import annotations

import io
import pathlib

import pytest

asyncpg = pytest.importorskip("asyncpg")

import asyncio  # noqa: E402

import os  # noqa: E402

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
ROOT = pathlib.Path(__file__).resolve().parents[3]


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
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='goal_resources' AND column_name='gcs_uri'")
        if ok:
            return conn
        await conn.close()
    pytest.skip("no local database with goal_resources.gcs_uri")


def _run(body):
    async def go():
        conn = await _connect_or_skip()
        try:
            await body(conn)
        finally:
            await conn.close()
    asyncio.run(go())


def test_every_row_with_a_gcs_uri_has_the_same_uri():
    """The invariant the kernel's reads now depend on."""
    async def check(conn):
        bad = await conn.fetch(
            """SELECT id, uri, gcs_uri FROM goal_resources
                WHERE COALESCE(gcs_uri, '') <> ''
                  AND COALESCE(uri, '') IS DISTINCT FROM gcs_uri
                LIMIT 10""")
        assert not bad, (
            f"{len(bad)} row(s) carry a gcs_uri their uri does not match, "
            f"so the kernel — which now reads uri alone — would use a "
            f"different locator from app/: {[dict(r) for r in bad]}")
    _run(check)


def test_no_row_has_only_a_gcs_uri():
    """A resource the kernel cannot see is a file the story was given and
    never receives, with nothing raised anywhere."""
    async def check(conn):
        orphans = await conn.fetchval(
            """SELECT count(*) FROM goal_resources
                WHERE COALESCE(gcs_uri, '') <> ''
                  AND COALESCE(uri, '') = ''""")
        assert orphans == 0, (
            f"{orphans} resource(s) have a gcs_uri and no uri. The kernel "
            f"reads uri only, so these are invisible to mounts and to the "
            f"egress allowlist — silently.")
    _run(check)


def test_the_kernel_names_no_cloud_specific_column():
    """The point of the exercise. `kernel/` may not read `gcs_uri`.

    Comments are allowed to mention it — this one does, and so does the
    note left at each former call site. Only executable SQL counts, so
    the check looks for the column inside a string rather than anywhere
    in the file.
    """
    offenders = []
    for path in (ROOT / "kernel").rglob("*.py"):
        if "__pycache__" in str(path) or "tests" in path.parts:
            continue
        for n, line in enumerate(
                io.open(path, encoding="utf-8", errors="replace"), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "gcs_uri" not in line:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{n}: {stripped[:70]}")
    assert not offenders, (
        "kernel/ reads a storage-provider-specific column; the generic "
        "`uri` carries the same value:\n  " + "\n  ".join(offenders))
