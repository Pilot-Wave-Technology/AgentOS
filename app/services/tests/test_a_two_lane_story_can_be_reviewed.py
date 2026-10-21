"""The reviewer can see what a two-lane story changed, and what it is for.

STORY 40aec728 CAME BACK `code_not_reviewed` OVER CODE THAT WAS FINE.
Two independent platform faults, both of the same shape -- an assumption
that was true when it was written and stopped being true:

  1. `changed_files` keyed its trunk lookup by `repos/<owner>__<name>`.
     A story writing two lanes of one repository has its checkouts at
     `repos/<owner>__<name>@<lane>`, so every lookup missed, `trunk`
     came back empty, and the brief told the reviewer "this repository
     has no recorded trunk" for BOTH checkouts. The bare key could not
     have worked regardless: two lanes write it twice and the second
     overwrites the first, so one lane would have been diffed against
     the other's trunk.

  2. `goal_context` reads four tables to describe what the goal is for.
     The leg role held two of them. `permission denied for table goals`,
     and the section came back as "could not be read".
     `kernel_leg_role_settled_tree.sql` had left `public.goals` out on
     the stated grounds that "nothing on the leg's graph reads it" --
     true when written, false once the review brief moved into the pod.

Neither fault was in the story, and the verdict that reached the goal
read as a judgement about the code. That is the cost worth testing
against: a reviewer that cannot see the diff is right to refuse, so the
thing to protect is its ability to see one.
"""

from __future__ import annotations

import asyncio
import io
import os
import pathlib
import uuid

import asyncpg
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
OWNER, REPO = "Pilot-Wave-Technology", "agent_os_test_suites"


async def _connect_or_skip():
    tried = []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if not dsn:
            continue
        try:
            return await asyncio.wait_for(asyncpg.connect(
                dsn, server_settings={"search_path": "kernel,public"}),
                timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip(f"no local database. Tried: {'; '.join(tried)}")


class _Mount:
    def __init__(self, at):
        self.at, self.binding = at, "git"


# ── the trunk lookup ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_each_lane_finds_its_own_trunk(tmp_path):
    """THE BUG. Two nodes, two lanes, two checkouts -- and the map was
    keyed by the repository, so neither checkout resolved and both came
    back `readable=False`: "this repository has no recorded trunk"."""
    from kernel.harness.review_sources import changed_files

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        tag = uuid.uuid4().hex[:8]
        goal, story = f"R{tag}", f"s{tag}"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','a')",
            goal)
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, state, created_by) "
            "VALUES ($1,$2,'t','done','a')", story, goal)
        repo_id = f"gr_{tag}"
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name, trunk_branch) "
            "VALUES ($1,$2,$3,'main')", repo_id, OWNER, REPO)
        lanes = ("calculator", "calculator_frontend")
        for i, lane in enumerate(lanes):
            nid = f"gn_{tag}_{i}"
            await conn.execute(
                "INSERT INTO git_nodes (id, goal_id, git_repo_id, "
                "  branch_name, trunk_branch, state) "
                "VALUES ($1,$2,$3,$4,$5,'IN_PROGRESS')",
                nid, goal, repo_id, f"feat/{goal}-{story}-{lane}", lane)
            await conn.execute(
                "INSERT INTO git_node_stories (git_node_id, story_id) "
                "VALUES ($1,$2)", nid, story)

        # A WORKSPACE THAT LOOKS REAL ENOUGH. `changed_files` skips any
        # mount without a `.git` directory, so without these the rows
        # never exist and the test passes on an empty list -- which is
        # what the first draft of it did.
        base = f"repos/{OWNER}__{REPO}"
        mounts = []
        for lane in lanes:
            at = f"{base}@{lane}"
            (tmp_path / at / ".git").mkdir(parents=True)
            mounts.append(_Mount(at))

        rows = await changed_files(conn, story, str(tmp_path), mounts)
        assert len(rows) == 2, f"expected both checkouts, got {rows}"
        got = {r["at"]: r for r in rows}
        for lane in lanes:
            r = got[f"{base}@{lane}"]
            assert r["trunk"] == lane, (
                f"{lane} resolved trunk {r['trunk']!r} -- a lane diffed "
                f"against the wrong trunk is worse than no diff")
            assert r["readable"] is True, (
                f"{lane} reported no recorded trunk, which is the "
                f"`code_not_reviewed` this test exists for")
    finally:
        await tx.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_a_single_lane_story_is_unchanged(tmp_path):
    """The overwhelming majority. Its checkout is NOT suffixed, so it
    resolves through the bare key -- which must keep working."""
    from kernel.harness.review_sources import changed_files

    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        tag = uuid.uuid4().hex[:8]
        goal, story = f"R{tag}", f"s{tag}"
        await conn.execute(
            "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','a')",
            goal)
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, state, created_by) "
            "VALUES ($1,$2,'t','done','a')", story, goal)
        repo_id = f"gr_{tag}"
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name, trunk_branch) "
            "VALUES ($1,$2,$3,'main')", repo_id, OWNER, REPO)
        nid = f"gn_{tag}_0"
        await conn.execute(
            "INSERT INTO git_nodes (id, goal_id, git_repo_id, branch_name, "
            "  trunk_branch, state) VALUES ($1,$2,$3,$4,'main','IN_PROGRESS')",
            nid, goal, repo_id, f"feat/{goal}-{story}")
        await conn.execute(
            "INSERT INTO git_node_stories (git_node_id, story_id) "
            "VALUES ($1,$2)", nid, story)

        at = f"repos/{OWNER}__{REPO}"
        (tmp_path / at / ".git").mkdir(parents=True)
        rows = await changed_files(conn, story, str(tmp_path), [_Mount(at)])
        assert len(rows) == 1
        assert rows[0]["trunk"] == "main" and rows[0]["readable"] is True
    finally:
        await tx.rollback()
        await conn.close()


def test_the_bare_key_is_not_last_write_wins():
    """Two lanes of one repository both want the bare key. Overwriting
    means one lane silently diffed against the other's trunk -- a WRONG
    diff, which is worse than the refusal the missing key produced."""
    src = io.open(ROOT / "kernel/harness/review_sources.py",
                  encoding="utf-8").read()
    assert "lanes.setdefault(base, trunk)" in src, (
        "the bare key is assigned rather than set-once, so the last node "
        "read decides what every unsuffixed checkout diffs against")


def test_the_rule_for_when_a_checkout_is_suffixed_is_not_restated():
    """Registering BOTH names means this file never decides WHEN a
    checkout carries its lane -- `story_clone_layout` owns that, and a
    second copy of it here would drift the first time either moved."""
    src = io.open(ROOT / "kernel/harness/review_sources.py",
                  encoding="utf-8").read()
    body = src[src.index("async def changed_files"):]
    body = body[:body.index("\nasync def ") + 1] if "\nasync def " in body[10:] else body
    assert "count" not in body.lower().split("lanes = {}")[0][-400:], (
        "changed_files appears to be counting lanes, which is the "
        "set-aware rule story_clone_layout already owns")


# ── the grants ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_leg_may_read_everything_goal_context_reads():
    """Four tables, one function. It held two, so the FIRST query
    raised and the section came back as "could not be read" -- and the
    verdict that reached the goal read as a judgement about the code."""
    conn = await _connect_or_skip()
    try:
        missing = []
        for table in ("goals", "goal_summary", "goal_decisions", "stories"):
            ok = await conn.fetchval(
                "SELECT has_table_privilege('agentos_leg', $1, 'SELECT')",
                f"public.{table}")
            if not ok:
                missing.append(table)
        assert not missing, (
            f"`goal_context` reads {missing} and the leg role cannot: the "
            f"review brief's goal section will fail with permission denied")
    except asyncpg.exceptions.UndefinedObjectError:
        pytest.skip("agentos_leg does not exist in this database")
    finally:
        await conn.close()


def test_the_grant_names_the_tables_the_code_reads():
    """The migration and the function have to agree. A grant list
    written from memory is how `goals` was left out of the settled tree
    in the first place."""
    from app.services.tests.shipped_tree import IS_DISTRIBUTION, shipped_privileges

    # The distribution ships the grant in the leg login's own migration
    # (0004), not as this file.
    sql = None if IS_DISTRIBUTION else io.open(
        ROOT / "migrations/kernel_leg_role_goal_context_grant.sql",
        encoding="utf-8").read()
    src = io.open(ROOT / "kernel/harness/review_sources.py",
                  encoding="utf-8").read()
    fn = src[src.index("async def goal_context"):]
    fn = fn[:fn.index("\nasync def ", 10)] if "\nasync def " in fn[10:] else fn
    for table in ("goals", "goal_summary"):
        assert f"FROM {table} " in fn, f"goal_context no longer reads {table}"
        if IS_DISTRIBUTION:
            assert "SELECT" in shipped_privileges(f"public.{table}", "agentos_leg"), (
                f"the leg role is not granted SELECT on {table}")
        else:
            assert f"public.{table}" in sql, f"the grant omits {table}"


def test_it_is_in_the_sequence():
    seq = io.open(ROOT / "migrations/SEQUENCE", encoding="utf-8").read()
    assert "kernel_leg_role_goal_context_grant" in seq
