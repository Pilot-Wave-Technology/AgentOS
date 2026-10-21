"""Phase 4 · a person can watch an errand.

Three of the four controls already worked: an assistant-started errand
rides a story row and a story thread, and the run-control lookups carry no
`kind` filter, so live activity, pause, resume and cancel found it. What
did not work was everything about seeing what it was DOING.

**The workspace panel said "No files in workspace yet." for the whole
life of every errand**, while the files sat one directory away. And five
of the tools an errand calls most had no entry in the ticker's vocabulary,
so a twenty-poll background job was twenty identical blank grey lines —
which reads as a stuck run rather than a watched one.

The story path is checked in both directions here: `I4c` touches four
tools that story-coding also holds, so those tests assert the change is
additive rather than a substitution.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.services.executor_activity import FAMILY, summarise

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-ui-test"


# ── I4b/I4c · the ticker can name what an errand does ────────────────────

def test_the_ticker_knows_every_tool_an_errand_can_call():
    """THE JOIN, and the reason five were missing: nothing connected the
    profile's surface to the vocabulary, so granting a tool in Ec1a left
    the ticker silent about it and nobody found out by reading either
    file."""
    from kernel.profiles.loader import load_all

    errand = load_all()[("errand", "ops")]
    unknown = sorted(t for t in errand.tools if t not in FAMILY)
    assert not unknown, (
        f"an errand can call {unknown} and the ticker renders them as "
        f"anonymous grey dots")


def test_the_ticker_knows_every_tool_a_story_can_call():
    """The same join for the other two profiles, since the vocabulary is
    shared and a story-coding tool missing from it is the same defect."""
    from kernel.profiles.loader import load_all

    for key, profile in load_all().items():
        unknown = sorted(t for t in profile.tools if t not in FAMILY)
        assert not unknown, f"{key}: {unknown}"


@pytest.mark.parametrize("tool,args,expected", [
    ("emit_artifact", {"role": "report", "name": "march.md"},
     "report: march.md"),
    ("emit_artifact", {"path": "out/rows.csv"}, "out/rows.csv"),
    ("bash_output", {"job_id": "job-a1", "since": 4096}, "job-a1 from 4096"),
    ("bash_output", {"job_id": "job-a1"}, "job-a1"),
    ("bash_background", {"command": "npm run build"}, "npm run build"),
    ("bash_kill", {"job_id": "job-a1"}, "stopping job-a1"),
    ("web_fetch", {"url": "https://api.example.com/v1"},
     "https://api.example.com/v1"),
])
def test_each_new_tool_says_what_it_is_doing(tool, args, expected):
    """A row that names the tool and nothing else is a row a person
    cannot read. Twenty polls of one job were twenty identical lines."""
    got = summarise(tool, json.dumps(args))
    assert got["detail"] == expected
    assert got["family"] != "other", f"{tool} has no colour"


def test_emitting_reads_as_keeping_the_work():
    """An errand has no `story_commit`, so `emit_artifact` IS its "the
    work is now kept" moment and should look like one."""
    assert FAMILY["emit_artifact"] == FAMILY["story_commit"] == "commit"


def test_the_story_ticker_gained_detail_and_lost_nothing():
    """I4c IS STORY-VISIBLE and that is the point. All four are on
    story-coding's surface, and before this they rendered slate with no
    detail. Additive, never a substitution — so every tool that had a
    family keeps the one it had."""
    for tool, family in (("read", "read"), ("write", "write"),
                         ("bash", "exec"), ("story_commit", "commit"),
                         ("register_plan", "plan"), ("ask_human", "ask"),
                         ("no_tool", "idle")):
        assert FAMILY[tool] == family, f"{tool} changed family"


def test_an_unknown_tool_is_still_named_and_never_described():
    """A default that guessed at the first argument is how a future
    tool's secret argument ends up on a screen."""
    got = summarise("nobody_declared_me", json.dumps({"token": "sk-secret"}))
    assert got["tool"] == "nobody_declared_me"
    assert got["detail"] == ""
    assert got["family"] == "other"


# ── I4a · the workspace panel finds the scratch tree ─────────────────────

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
            "WHERE table_schema='kernel' AND table_name='executions'")
        if ok:
            return conn
        await conn.close()
    pytest.skip("no local database with the kernel schema")


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


def test_an_errands_scratch_tree_is_browsable(tmp_path):
    """THE REGRESSION. `mounts_for` returns () for the errand shape — no
    `execution_mounts` rows and no `workspace_source` — so the explorer
    never looked, and the panel said "No files in workspace yet." for the
    entire run while the files sat one directory away."""
    async def body(conn):
        from kernel.workspace.explorer import mounts_of

        (tmp_path / "report.md").write_text("findings", encoding="utf-8")
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, "
            "workspace_path, state) VALUES ('errand','ops',$1,$2,'RUNNING') "
            "RETURNING id", f"{PREFIX}-ws", str(tmp_path))

        mounts = await mounts_of(conn, eid)
        assert mounts, "the errand's workspace is still invisible"
        assert len(mounts) == 1
        assert mounts[0]["kind"] == "scratch"
        assert mounts[0]["writable"] is True
        assert mounts[0]["root"] == str(tmp_path)
    _run(body)


def test_a_run_whose_directory_does_not_exist_gets_no_mount():
    """A synthetic mount pointing at nothing is worse than none — the
    panel would render an empty tree as though it had looked."""
    async def body(conn):
        from kernel.workspace.explorer import mounts_of

        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, "
            "workspace_path, state) VALUES ('errand','ops',$1,$2,'PENDING') "
            "RETURNING id", f"{PREFIX}-gone", "/no/such/path/exec-1")
        assert await mounts_of(conn, eid) == []
    _run(body)


def test_a_run_with_no_workspace_path_is_unchanged():
    async def body(conn):
        from kernel.workspace.explorer import mounts_of

        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, state) "
            "VALUES ('errand','ops',$1,'PENDING') RETURNING id",
            f"{PREFIX}-nopath")
        assert await mounts_of(conn, eid) == []
    _run(body)


def test_a_story_with_real_mounts_gets_those_and_no_scratch(tmp_path):
    """THE STORY PATH. The synthetic mount is reachable only when there
    are NO mounts and a path exists; a story always has at least one, so
    it can never see this branch."""
    async def body(conn):
        from kernel.workspace.explorer import mounts_of

        repo = tmp_path / "repos" / "o__n"
        repo.mkdir(parents=True)
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, "
            "workspace_path, workspace_source, state) "
            "VALUES ('story','coding',$1,$2,$3,'RUNNING') RETURNING id",
            f"{PREFIX}-story", str(tmp_path), "https://github.com/o/n.git")

        mounts = await mounts_of(conn, eid)
        assert mounts, "the legacy single-source branch stopped working"
        assert [m["kind"] for m in mounts] == ["repo"], (
            "a story picked up a scratch mount it should never see")
    _run(body)
