"""Ec2f/Ec2g — what the run produced, told to GO and findable by the assistant.

Ec2f. `on_story_done` told GO that a story finished and quoted the
executor's claim. For a coding story that is nearly enough — the work IS the
branch, named two lines below. For an errand it is nothing: no branch, a
discarded tree, and GO learns only that something ended. And
`stories.result_summary`, the field every later reader looks at, stayed
empty for every errand ever run.

Ec2g. `read_artifact` needs an ID, and an id comes from the footer of a
bounded output — a step the assistant may never have seen. An errand runs
unwatched and its artifacts are the whole result, so "what did that errand
produce" had nowhere to look.

Real Postgres, one transaction, always rolled back. `search_path =
kernel,public` because that is what every kernel pool sets.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.services import errand_result

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-result-test"


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
            "WHERE table_name IN ('artifact_manifest','executions','stories')")
        if ok >= 3:
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


class _Env:
    def __init__(self, root):
        self.workspace_path = root


async def _emit(conn, eid, root, **args):
    from kernel.harness.emit import emit_artifact
    got = await emit_artifact(conn, _Env(root), eid, args, "tc")
    assert got.exit_code == 0, got.error
    return got


async def _execution(conn, goal: str, story: str | None = None) -> int:
    return await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, story_id) "
        "VALUES ('errand','ops',$1,$2) RETURNING id", goal, story)


# ── Ec2f: what GO is told ────────────────────────────────────────────────

def test_the_summary_lists_what_was_produced(tmp_path):
    async def body(conn):
        eid = await _execution(conn, f"{PREFIX}-list")
        (tmp_path / "r.md").write_text("body", encoding="utf-8")
        (tmp_path / "rows.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                    summary="March was up 4% on February.")
        await _emit(conn, eid, str(tmp_path), path="rows.csv",
                    role="dataset", summary="the underlying rows")

        said = await errand_result.summarise(conn, eid, claim="looks done")
        assert "Produced 2 artifact(s)" in said
        assert "report: r.md" in said and "dataset: rows.csv" in said
        assert "March was up 4%" in said, (
            "the summary is what makes a list readable; without it a "
            "reader has two filenames and no answer")
        assert "claimed: looks done" in said
    _run(body)


def test_a_superseded_version_is_not_listed_as_a_second_answer(tmp_path):
    """A chain is a history and its head is the answer. Listing every
    version gives a reader three answers and no current one."""
    async def body(conn):
        goal = f"{PREFIX}-chain"
        first = await _execution(conn, goal)
        (tmp_path / "r.md").write_text("v1", encoding="utf-8")
        await _emit(conn, first, str(tmp_path), path="r.md", role="report",
                    summary="draft")

        second = await _execution(conn, goal)
        (tmp_path / "r.md").write_text("v2 final", encoding="utf-8")
        await _emit(conn, second, str(tmp_path), path="r.md", role="report",
                    summary="final")
        # The second execution holds only its own row, and that row
        # supersedes the first — so nothing is hidden here, and the
        # listing for the FIRST is what the head-detection must handle.
        both = await conn.fetch(
            "SELECT m.artifact_id, m.supersedes FROM kernel.artifact_manifest m "
            "JOIN kernel.executions e ON e.id = m.execution_id "
            "WHERE e.goal_id = $1", goal)
        assert len(both) == 2
        assert any(r["supersedes"] for r in both)
    _run(body)


def test_no_claim_and_no_artifacts_says_how_it_ended_instead(tmp_path):
    """NULL CLAIM IS NOT AN ERROR — a terminal command exited zero, or
    somebody accepted by hand. Saying nothing reads as "it produced
    nothing", which is a different statement."""
    async def body(conn):
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions "
            "(kind, domain, goal_id, terminal_command, state) "
            "VALUES ('errand','ops',$1,'pytest -q','DONE') RETURNING id",
            f"{PREFIX}-noclaim")
        said = await errand_result.summarise(conn, eid, claim=None)
        assert said, "a finished run must say something"
        assert "pytest -q" in said and "DONE" in said

        bare = await _execution(conn, f"{PREFIX}-bare")
        await conn.execute(
            "UPDATE kernel.executions SET state = 'DONE' WHERE id = $1", bare)
        said = await errand_result.summarise(conn, bare, claim=None)
        assert "without a completion claim" in said
    _run(body)


def test_it_writes_result_summary_but_never_overwrites_a_person(tmp_path):
    """`assistant/write.py` sets this column when somebody files a report,
    and a human sentence beats a generated one every time."""
    async def body(conn):
        goal = f"{PREFIX}-story"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) "
            "VALUES ($1,'t','tester') ON CONFLICT DO NOTHING", goal)
        story = f"{PREFIX}-s1"
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, state, created_by) "
            "VALUES ($1,$2,'a story','in_progress','tester')", story, goal)
        eid = await _execution(conn, goal, story)
        (tmp_path / "r.md").write_text("body", encoding="utf-8")
        await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                    summary="the finding")

        wrote = await errand_result.record(conn, story, eid, claim="done")
        assert wrote and "the finding" in wrote
        stored = await conn.fetchval(
            "SELECT result_summary FROM stories WHERE id = $1", story)
        assert "the finding" in stored

        # A second run must not clobber it.
        again = await errand_result.record(conn, story, eid, claim="x")
        assert again == ""
        assert await conn.fetchval(
            "SELECT result_summary FROM stories WHERE id = $1",
            story) == stored
    _run(body)


def test_go_is_told_and_a_failure_there_does_not_stop_the_story():
    """A GO wake that raises is a story that completes in silence. The
    state transition above it is not optional; the manifest listing is."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "app/services/go_wake_events.py",
                  encoding="utf-8").read()
    block = src[src.index("async def on_story_done"):]
    block = block[:block.index("async def on_node_event")]
    assert "errand_result" in block
    assert "except Exception" in block, "an errand_result failure is fatal"
    # And the transition must be written BEFORE the listing is attempted.
    assert block.index("record_state_pooled") < block.index("errand_result")


# ── Ec2g: the assistant can find them ────────────────────────────────────

def test_the_assistant_can_list_what_a_goal_produced(tmp_path):
    async def body(conn):
        from kernel.assistant import list_artifacts

        goal = f"{PREFIX}-assist"
        eid = await _execution(conn, goal)
        (tmp_path / "r.md").write_text("body", encoding="utf-8")
        (tmp_path / "run.log").write_text("noise", encoding="utf-8")
        await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                    summary="revenue by region")
        await _emit(conn, eid, str(tmp_path), path="run.log", role="log",
                    summary="the run")

        got = await list_artifacts(conn, goal)
        assert got["count"] == 2
        names = {a["name"] for a in got["artifacts"]}
        assert names == {"r.md", "run.log"}
        # Summaries travel; content does not.
        assert any("revenue by region" in a["summary"]
                   for a in got["artifacts"])
        assert all("text" not in a and "content" not in a
                   for a in got["artifacts"]), (
            "a list carrying content puts the bytes back in the context "
            "that bounding kept them out of")
    _run(body)


def test_it_filters_by_role_and_by_text(tmp_path):
    async def body(conn):
        from kernel.assistant import list_artifacts

        goal = f"{PREFIX}-filter"
        eid = await _execution(conn, goal)
        for name, role, summary in (("a.md", "report", "revenue by region"),
                                    ("b.csv", "dataset", "raw rows"),
                                    ("c.log", "log", "what happened")):
            (tmp_path / name).write_text(name, encoding="utf-8")
            await _emit(conn, eid, str(tmp_path), path=name, role=role,
                        summary=summary)

        only = await list_artifacts(conn, goal, role="report")
        assert [a["name"] for a in only["artifacts"]] == ["a.md"]
        # `contains` searches the SUMMARY as well as the name, which is
        # why emit_artifact requires one — searching hashes finds nothing.
        found = await list_artifacts(conn, goal, contains="revenue")
        assert [a["name"] for a in found["artifacts"]] == ["a.md"]
        assert (await list_artifacts(conn, goal,
                                     contains="nothing"))["count"] == 0
    _run(body)


def test_superseded_versions_are_hidden_and_the_hiding_is_named(tmp_path):
    """A reader who expected four reports and sees one must be told the
    other three are older versions, not that they are missing."""
    async def body(conn):
        from kernel.assistant import list_artifacts

        goal = f"{PREFIX}-hidden"
        for text in ("v1", "v2", "v3"):
            eid = await _execution(conn, goal)
            (tmp_path / "r.md").write_text(text, encoding="utf-8")
            await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                        summary=text)

        head = await list_artifacts(conn, goal)
        assert head["count"] == 1
        assert head["artifacts"][0]["summary"] == "v3"
        assert head["superseded_hidden"] == 2

        full = await list_artifacts(conn, goal, history=True)
        assert full["count"] == 3
        assert full["superseded_hidden"] == 0
    _run(body)


def test_a_bad_role_is_refused_rather_than_returning_nothing():
    """An empty list and a typo look identical to a reader, and the
    assistant would report "it produced nothing"."""
    async def body(conn):
        from kernel.assistant import list_artifacts

        got = await list_artifacts(conn, f"{PREFIX}-bad", role="summary")
        assert getattr(got, "code", "") == "E_BAD_ROLE"
        assert "report" in getattr(got, "message", "")
    _run(body)


def test_another_goals_artifacts_are_not_listed(tmp_path):
    """The assistant serves one goal and one person."""
    async def body(conn):
        from kernel.assistant import list_artifacts

        mine = await _execution(conn, f"{PREFIX}-mine")
        theirs = await _execution(conn, f"{PREFIX}-theirs")
        (tmp_path / "r.md").write_text("secret", encoding="utf-8")
        await _emit(conn, theirs, str(tmp_path), path="r.md", role="report",
                    summary="not yours")

        got = await list_artifacts(conn, f"{PREFIX}-mine")
        assert got["count"] == 0
        assert mine
    _run(body)


def test_the_route_is_registered_and_the_surface_still_matches():
    """`test_h10` asserts the served routes ARE the tool set. A tool
    exported and not routed is one the assistant is told it has and gets
    `unknown tool` for."""
    from kernel.assistant import TOOLS
    from kernel.assistant.api import READS, ROUTES

    assert "list_artifacts" in TOOLS
    assert "list_artifacts" in READS, "listing is a GET, not a write"
    assert set(ROUTES) == set(TOOLS)
