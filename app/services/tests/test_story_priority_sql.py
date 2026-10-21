"""Priority, as the database and the writers actually handle it.

THE CHECK CONSTRAINT IS THE POINT OF RISK. Normalising at five writers
is only safe if there is no SIXTH, and a CHECK turns a missed writer
from a quietly mis-scored story into a 500 at the moment somebody files
one. So the last test here does not exercise today's writers at all --
it looks for tomorrow's.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from app.services.story_priority import normalize_priority
from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

_APP = pathlib.Path(__file__).resolve().parents[2]


def _run(body):
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


async def _goal(conn) -> str:
    gid = "tp-" + "g1"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1, 'fixture', 'u1') "
        "ON CONFLICT (id) DO NOTHING", gid)
    return gid


def test_the_constraint_refuses_an_unrecognised_value():
    """What makes a missed writer loud instead of silent."""
    async def body(conn):
        gid = await _goal(conn)
        with pytest.raises(Exception) as got:
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state, priority) "
                "VALUES ('tp-bad', $1, 't', 'u1', 'proposed', 'blocker')", gid)
        assert "stories_priority_valid" in str(got.value) or "check" in str(got.value).lower()
    _run(body)


def test_every_valid_value_is_accepted():
    async def body(conn):
        gid = await _goal(conn)
        for i, p in enumerate(("low", "medium", "high", "critical")):
            await conn.execute(
                "INSERT INTO stories (id, goal_id, title, created_by, state, priority) "
                "VALUES ($1, $2, 't', 'u1', 'proposed', $3)", f"tp-ok{i}", gid, p)
        assert await conn.fetchval(
            "SELECT count(*) FROM stories WHERE id LIKE 'tp-ok%'") == 4
    _run(body)


def test_the_migration_rewrites_bad_rows_and_is_safe_to_run_twice():
    """Run against a copy of the table so the real constraint is not in
    the way -- the migration's job is to make rows legal BEFORE the
    constraint exists, and that is what is being checked."""
    async def body(conn):
        await conn.execute(
            "CREATE TEMP TABLE pr_fixture (id text, priority text) ON COMMIT DROP")
        bad = ["High", " high ", "h igh", "CRITICAL", "blocker", "", None]
        for i, v in enumerate(bad):
            await conn.execute(
                "INSERT INTO pr_fixture VALUES ($1, $2)", f"s{i}", v)

        rewrite = """
            UPDATE pr_fixture
               SET priority = CASE lower(replace(replace(replace(
                                     COALESCE(priority, ''), ' ', ''),
                                     chr(9), ''), chr(10), ''))
                                  WHEN 'low' THEN 'low'
                                  WHEN 'medium' THEN 'medium'
                                  WHEN 'high' THEN 'high'
                                  WHEN 'critical' THEN 'critical'
                                  ELSE 'medium' END
        """
        await conn.execute(rewrite)
        first = dict(await conn.fetch("SELECT id, priority FROM pr_fixture") and
                     {r["id"]: r["priority"] for r in
                      await conn.fetch("SELECT id, priority FROM pr_fixture")})
        await conn.execute(rewrite)                 # twice
        second = {r["id"]: r["priority"] for r in
                  await conn.fetch("SELECT id, priority FROM pr_fixture")}
        assert first == second, "the rewrite is not idempotent"

        for i, v in enumerate(bad):
            assert first[f"s{i}"] == normalize_priority(v), v
        assert set(first.values()) <= {"low", "medium", "high", "critical"}
    _run(body)


def test_no_row_in_this_database_is_out_of_set():
    """The migration's own 'done when'."""
    async def body(conn):
        rogue = await conn.fetchval(
            "SELECT count(*) FROM stories "
            " WHERE priority IS NOT NULL "
            "   AND priority NOT IN ('low','medium','high','critical')")
        assert rogue == 0, f"{rogue} stories hold an out-of-set priority"
    _run(body)


def test_no_writer_stores_a_priority_without_normalising_it():
    """THE SIXTH WRITER, which does not exist yet.

    The five that do are normalised and the four cases above prove the
    constraint. What this guards is the one added next month: with a
    CHECK in place it would not mis-score a story, it would 500 on
    somebody filing one, in production, which is a worse way to find
    out than a failing test.

    Looks for a priority reaching a write without passing through
    `normalize_priority` -- an INSERT naming the column, or a SET of it.
    Files that import the normaliser are trusted; the point is to notice
    a file that does not.
    """
    # BOUNDED, BECAUSE PYTHON HAS NO STATEMENT TERMINATOR. The first
    # version matched `[^;]*`, which inside a .py file runs to the end of
    # the file: it reported `goals.py` (which writes goals.priority) and
    # a council module (which writes an integer on another table), neither
    # of which touches a story. A guard that cries wolf gets deleted, so
    # this looks only at the text immediately following a write to
    # `stories`, and stops at the end of the SQL literal.
    SPAN = 600

    def writes_story_priority(src: str) -> bool:
        for m in re.finditer(r"(INSERT INTO\s+stories\b|UPDATE\s+stories\b)",
                             src, re.I):
            window = re.split(r'"""', src[m.start():m.start() + SPAN])[0]
            if re.search(r"\bpriority\b", window):
                return True
        return False

    offenders = []
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP.parent).as_posix()
        if "/tests/" in rel or rel.endswith("story_priority.py"):
            continue
        src = path.read_text(encoding="utf-8", errors="ignore")
        if not writes_story_priority(src):
            continue
        if "normalize_priority" in src:
            continue
        # A literal is already normalised by inspection -- the Slack
        # create writes 'medium' and has nothing to normalise.
        if re.search(r"'(low|medium|high|critical)'", src):
            continue
        offenders.append(rel)
    assert not offenders, (
        "these write stories.priority without normalize_priority: "
        + ", ".join(offenders))
