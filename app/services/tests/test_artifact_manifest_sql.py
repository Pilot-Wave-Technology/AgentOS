"""Ec2b/Ec2c — `emit_artifact` and the manifest, against a REAL PostgreSQL.

WHAT ONLY SQL CAN ANSWER, and therefore what a mocked pool would let past:

  * the `role` CHECK actually refuses a sixth role, so a typo is a failed
    insert rather than a row nobody's filter matches
  * `UNIQUE (execution_id, artifact_id)` plus the `ON CONFLICT` upsert, so
    emitting one file twice in a run corrects the row instead of raising
  * the supersede chain is derived by (goal, role, name) across
    EXECUTIONS — a rerun is a different execution, so a query scoped to one
    would never chain and the test would pass against the wrong thing
  * `NULLIF($7, '')`, so "no predecessor" is NULL and the partial index
    over `supersedes IS NOT NULL` stays small

**How to run.** Start the local stack (`local-development/scripts/stack.ps1`).
No database reachable means SKIP, not fail.

**Safety.** One transaction, always rolled back, asserted rather than
assumed. Fixture ids are namespaced `zz-emit-test%`.

**Why `asyncio.run`.** `pytest-asyncio` is not installed, and an async test
without it ERRORS rather than skipping — which is how twelve of these once
sat dead behind a green gate.
"""
from __future__ import annotations

import asyncio
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-emit-test"


def _candidate_dsns() -> list[str]:
    """`DATABASE_URL` cannot be trusted alone: a sibling conftest sets a
    dummy at import, so a full-suite run would skip while a solo run
    passed."""
    seen, out = set(), []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


async def _connect_or_skip():
    attempts = []
    for dsn in _candidate_dsns():
        try:
            # `search_path = kernel, public` IS THE RUNTIME. Every kernel
            # pool sets it, and `progress.py` writes unqualified SQL
            # against it. A test connection on the default path exercises
            # different SQL than production runs.
            conn = await asyncio.wait_for(
                asyncpg.connect(dsn, server_settings={
                    "search_path": "kernel,public"}), timeout=5)
        except Exception as exc:                            # noqa: BLE001
            attempts.append(f"{type(exc).__name__}")
            continue
        ok = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'kernel' "
            "AND table_name IN ('executions','artifact_manifest')")
        if ok == 2:
            return conn
        await conn.close()
        attempts.append(f"only {ok}/2 kernel tables")
    pytest.skip("no local database with the kernel schema — start the local "
                f"stack. Tried: {'; '.join(attempts) or 'nothing'}")


async def _execution(conn, goal_id: str) -> int:
    return await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id) "
        "VALUES ('errand', 'ops', $1) RETURNING id", goal_id)


def _run(body):
    """One connection, one transaction, always rolled back."""
    async def _go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await body(conn)
        finally:
            await tx.rollback()
            left = await conn.fetchval(
                "SELECT count(*) FROM kernel.artifact_manifest m "
                "JOIN kernel.executions e ON e.id = m.execution_id "
                "WHERE e.goal_id LIKE $1", PREFIX + "%")
            await conn.close()
            assert left == 0, f"{left} fixture rows survived the rollback"
    asyncio.run(_go())


class _Envelope:
    def __init__(self, root):
        self.workspace_path = root


def _emit(conn, execution_id, root, **args):
    from kernel.harness.emit import emit_artifact
    return emit_artifact(conn, _Envelope(root), execution_id, args, "tc-1")


# ── the table's own rules ────────────────────────────────────────────────

def test_a_sixth_role_is_refused_by_the_database():
    """The CHECK, not just the tool. A row with role='summary' would be
    invisible to every filter and visible to nothing — a typo must fail at
    the insert."""
    async def body(conn):
        eid = await _execution(conn, f"{PREFIX}-roles")
        for role in ("report", "dataset", "method", "log", "figure"):
            await conn.execute(
                "INSERT INTO kernel.artifact_manifest "
                "(execution_id, artifact_id, role, name) "
                "VALUES ($1, $2, $3, 'n')", eid, f"sha-{role}", role)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO kernel.artifact_manifest "
                "(execution_id, artifact_id, role, name) "
                "VALUES ($1, 'sha-x', 'summary', 'n')", eid)
    _run(body)


def test_an_unnamed_artifact_is_refused():
    """A blank name makes a list unreadable, and the reader has no other
    handle on the row — the id is a hash."""
    async def body(conn):
        eid = await _execution(conn, f"{PREFIX}-name")
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO kernel.artifact_manifest "
                "(execution_id, artifact_id, role, name) "
                "VALUES ($1, 'sha-y', 'report', '   ')", eid)
    _run(body)


def test_two_executions_may_name_the_same_bytes():
    """Content addressing means a rerun with identical output produces the
    same id. Two runs naming one object is a fact to record, not a
    conflict — the uniqueness is per execution."""
    async def body(conn):
        a = await _execution(conn, f"{PREFIX}-dedup")
        b = await _execution(conn, f"{PREFIX}-dedup")
        for eid in (a, b):
            await conn.execute(
                "INSERT INTO kernel.artifact_manifest "
                "(execution_id, artifact_id, role, name) "
                "VALUES ($1, 'sha-same', 'report', 'r.md')", eid)
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO kernel.artifact_manifest "
                "(execution_id, artifact_id, role, name) "
                "VALUES ($1, 'sha-same', 'report', 'r.md')", a)
    _run(body)


# ── emit_artifact end to end ─────────────────────────────────────────────

def test_a_2kb_report_becomes_a_durable_artifact(tmp_path):
    """Ec2c's test. Before this, an artifact existed only because a tool
    output crossed 32 KB — so a 50 MB log was durable and the report was
    not, which is exactly backwards for an errand."""
    async def body(conn):
        # `newline=""` because Windows translates "\n" to "\r\n" on write,
        # and `bytes` is measured on what actually reached the store.
        body_text = "# March\n" + "x" * 2000
        (tmp_path / "march.md").write_text(body_text, encoding="utf-8",
                                           newline="")
        eid = await _execution(conn, f"{PREFIX}-emit")
        result = await _emit(conn, eid, str(tmp_path), path="march.md",
                             role="report", summary="March numbers, up 4%.")
        assert result.exit_code == 0, result.error

        row = await conn.fetchrow(
            "SELECT * FROM kernel.artifact_manifest WHERE execution_id = $1",
            eid)
        assert row["role"] == "report"
        assert row["name"] == "march.md"
        assert row["media_type"] == "text/markdown"
        assert row["summary"].startswith("March numbers")
        assert row["supersedes"] is None
        assert row["bytes"] == len(body_text.encode())
        # The id is the store's sha256, and the bytes are really there.
        from kernel.artifacts.store import get_store
        assert get_store().get(row["artifact_id"]).startswith(b"# March")
    _run(body)


def test_a_rerun_chains_by_supersedes_across_executions(tmp_path):
    """Ec2b's test, and the reason the lookup is scoped to the GOAL. A
    rerun is a different execution; a query scoped to one would never
    chain, and the reader would have two answers and no current one."""
    async def body(conn):
        goal = f"{PREFIX}-chain"
        first = await _execution(conn, goal)
        (tmp_path / "r.md").write_text("v1 content", encoding="utf-8")
        assert (await _emit(conn, first, str(tmp_path), path="r.md",
                            role="report", summary="first")).exit_code == 0

        second = await _execution(conn, goal)
        (tmp_path / "r.md").write_text("v2 content, different", encoding="utf-8")
        out = await _emit(conn, second, str(tmp_path), path="r.md",
                          role="report", summary="second")
        assert out.exit_code == 0
        assert "Supersedes" in out.output

        # BY id, NOT created_at. `now()` is the TRANSACTION's start time
        # in Postgres, and both rows are written inside this one — so
        # their `created_at` are identical to the microsecond and the
        # order between them is whatever the planner feels like. This
        # test was flaky for exactly that reason: green in isolation and
        # occasionally red in a full run, which reads as pollution.
        #
        # It is also the bug `emit.py` was fixed for in Ec2f, where the
        # same ORDER BY forked a supersede chain in production. Ordering
        # the assertion the way the code orders the lookup is the point.
        rows = await conn.fetch(
            "SELECT m.* FROM kernel.artifact_manifest m "
            "JOIN kernel.executions e ON e.id = m.execution_id "
            "WHERE e.goal_id = $1 ORDER BY m.id", goal)
        assert len(rows) == 2
        assert rows[0]["supersedes"] is None
        assert rows[1]["supersedes"] == rows[0]["artifact_id"]
        # The head of the chain is the one nothing supersedes.
        superseded = {r["supersedes"] for r in rows} - {None}
        head = [r for r in rows if r["artifact_id"] not in superseded]
        assert len(head) == 1 and head[0]["summary"] == "second"
    _run(body)


def test_an_unchanged_rerun_does_not_supersede_itself(tmp_path):
    """Identical content is the same sha256. A row pointing at its own id
    makes the chain a loop, and every reader that walks it hangs."""
    async def body(conn):
        goal = f"{PREFIX}-same"
        (tmp_path / "r.md").write_text("identical", encoding="utf-8")
        first = await _execution(conn, goal)
        await _emit(conn, first, str(tmp_path), path="r.md", role="report",
                    summary="one")
        second = await _execution(conn, goal)
        await _emit(conn, second, str(tmp_path), path="r.md", role="report",
                   summary="two")
        rows = await conn.fetch(
            "SELECT m.artifact_id, m.supersedes FROM kernel.artifact_manifest m "
            "JOIN kernel.executions e ON e.id = m.execution_id "
            "WHERE e.goal_id = $1", goal)
        assert len({r["artifact_id"] for r in rows}) == 1, "should dedup"
        for r in rows:
            assert r["supersedes"] != r["artifact_id"]
    _run(body)


def test_emitting_one_file_twice_in_a_run_corrects_the_row(tmp_path):
    """The upsert. A model that emits, notices a poor summary and emits
    again must not hit a unique violation — the second call is a
    correction, and the row is the same object either way."""
    async def body(conn):
        eid = await _execution(conn, f"{PREFIX}-twice")
        (tmp_path / "r.md").write_text("content", encoding="utf-8")
        await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                    summary="vague")
        out = await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                          summary="precise: revenue by region, Q1")
        assert out.exit_code == 0, out.error
        rows = await conn.fetch(
            "SELECT summary FROM kernel.artifact_manifest "
            "WHERE execution_id = $1", eid)
        assert len(rows) == 1
        assert rows[0]["summary"].startswith("precise")
    _run(body)


def test_a_different_name_is_a_different_artifact_not_a_supersede(tmp_path):
    """The chain is (goal, role, name). Two reports called different
    things are two answers to two questions, not a history."""
    async def body(conn):
        goal = f"{PREFIX}-names"
        eid = await _execution(conn, goal)
        for fname, text in (("a.md", "aaa"), ("b.md", "bbb")):
            (tmp_path / fname).write_text(text, encoding="utf-8")
            await _emit(conn, eid, str(tmp_path), path=fname, role="report",
                        summary=f"about {fname}")
        rows = await conn.fetch(
            "SELECT name, supersedes FROM kernel.artifact_manifest "
            "WHERE execution_id = $1 ORDER BY name", eid)
        assert [r["name"] for r in rows] == ["a.md", "b.md"]
        assert all(r["supersedes"] is None for r in rows)
    _run(body)


# ── I1f · story_progress shows what the run MEANT to keep ────────────────

def test_story_progress_shows_an_emitted_artifact(tmp_path):
    """THE Ec2 GAP. `story_progress`'s artifacts came only from
    `step_journal.result_ref` — the >32 KB ACCIDENT — so an errand that
    emitted a 2 KB report, which is the entire point of Ec2c, showed
    ZERO artifacts in the one tool the prompt names twice for exactly
    this question."""
    async def body(conn):
        from kernel.assistant.progress import _steps

        eid = await _execution(conn, f"{PREFIX}-progress")
        (tmp_path / "march.md").write_text("the answer", encoding="utf-8")
        await _emit(conn, eid, str(tmp_path), path="march.md", role="report",
                    summary="March was up 4%")

        got = await _steps(conn, eid)
        artifacts = got.get("artifacts") or []
        assert artifacts, "the deliverable is invisible"
        first = artifacts[0]
        assert first["emitted"] is True
        assert first["role"] == "report" and first["name"] == "march.md"
        assert "March was up 4%" in first["summary"], (
            "without the summary a reader has a hash and a filename")
    _run(body)


def test_a_superseded_version_is_not_shown_as_a_second_deliverable(tmp_path):
    """A chain is a history and its head is the answer."""
    async def body(conn):
        from kernel.assistant.progress import _steps

        eid = await _execution(conn, f"{PREFIX}-prog-chain")
        for text, summary in (("v1", "draft"), ("v2 longer", "final")):
            (tmp_path / "r.md").write_text(text, encoding="utf-8")
            await _emit(conn, eid, str(tmp_path), path="r.md", role="report",
                        summary=summary)
        artifacts = (await _steps(conn, eid)).get("artifacts") or []
        assert len(artifacts) == 1, [a["summary"] for a in artifacts]
        assert artifacts[0]["summary"] == "final"
    _run(body)


def test_a_run_with_no_emitted_artifacts_is_unchanged():
    """THE STORY PATH. A story emits nothing and its bounded outputs must
    still be listed exactly as before — this is additive or it is a
    regression."""
    async def body(conn):
        from kernel.assistant.progress import _steps

        eid = await _execution(conn, f"{PREFIX}-storyshape")
        got = await _steps(conn, eid)
        assert "artifacts" not in got, (
            "a run with nothing stored now claims to have artifacts")
    _run(body)
