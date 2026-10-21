"""Phase 3 · GO can say a piece of work is an errand, and can see one.

THE INTENT DIED AT THE TOOL BOUNDARY. `create_story` had no shape field
and `stories.story_kind` is CHECK-constrained to ('code','merge') — the
git engine's taxonomy, where `merge` is not a kind of work but the
engine's own story for landing a branch. So the only route to an errand
was: GO writes an ordinary story, a human accepts it, and THE ASSISTANT
DECIDES ON ITS OWN to pass `kind="errand"` to `start_story`. The planner's
judgement about the shape of the work never reached the thing that ran it.

`work_kind` is a second column because they answer different questions:
`story_kind` is what the ENGINE does with it, `work_kind` is what SHAPE
the work is. One column would mean the engine's dependency graph has to
have an opinion about work that produces no branch, and the first thing
that goes wrong is a merge story generated for something with nothing to
merge.

Real Postgres, one transaction, always rolled back.
"""
from __future__ import annotations

import asyncio
import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-workkind-test"


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
            "WHERE table_name='stories' AND column_name='work_kind'")
        if ok:
            return conn
        await conn.close()
    pytest.skip("no local database with the work_kind column")


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


async def _goal_and_story(conn, work_kind=None, with_resource=True):
    tag = os.urandom(4).hex()
    goal, story = f"{PREFIX}-g-{tag}", f"{PREFIX}-s-{tag}"
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,'t','tester')",
        goal)
    await conn.execute(
        "INSERT INTO stories (id, goal_id, title, state, created_by, work_kind)"
        " VALUES ($1,$2,'s','accepted','tester',$3)", story, goal, work_kind)
    if with_resource:
        rid = f"{PREFIX}-r-{tag}"
        await conn.execute(
            "INSERT INTO goal_resources (id, goal_id, kind, name, clone_url,"
            " created_by) VALUES ($1,$2,'repo','r',"
            "'https://github.com/o/n.git','tester')", rid, goal)
        await conn.execute(
            "INSERT INTO story_resources (story_id, resource_id, relation)"
            " VALUES ($1,$2,'produces')", story, rid)
    return goal, story


# ── the column ───────────────────────────────────────────────────────────

def test_the_database_refuses_a_shape_nobody_built():
    """A closed set, because this decides which PROFILE runs the work.
    An unknown value would be found at dispatch, after a row exists to
    clean up."""
    async def body(conn):
        goal, _ = await _goal_and_story(conn)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO stories (id, goal_id, title, state, "
                    "created_by, work_kind) VALUES "
                    "($1,$2,'x','proposed','tester','chore')",
                    f"{PREFIX}-bad", goal)
    _run(body)


def test_null_means_story_and_every_existing_row_is_one():
    """A default of 'story' would rewrite forty thousand rows to say what
    their absence already says."""
    async def body(conn):
        _, story = await _goal_and_story(conn, work_kind=None)
        got = await conn.fetchval(
            "SELECT work_kind FROM stories WHERE id = $1", story)
        assert got is None
    _run(body)


def test_the_engines_taxonomy_is_untouched():
    """`story_kind` still means code|merge. Overloading it would give the
    git engine an opinion about work with nothing to merge."""
    async def body(conn):
        _, story = await _goal_and_story(conn, work_kind="errand")
        row = await conn.fetchrow(
            "SELECT story_kind, work_kind FROM stories WHERE id = $1", story)
        assert row["story_kind"] == "code", "the engine's column moved"
        assert row["work_kind"] == "errand"
    _run(body)


# ── start_story reads it ─────────────────────────────────────────────────

def test_the_shape_go_chose_reaches_the_execution():
    """THE WHOLE POINT. Without this the planner's judgement stopped at
    the story row and the assistant guessed."""
    async def body(conn):
        from kernel.assistant.write import start_story

        goal, story = await _goal_and_story(conn, work_kind="errand",
                                            with_resource=False)
        got = await start_story(conn, goal, "tester", story,
                                "read the logs and say what failed",
                                f"key-{os.urandom(3).hex()}")
        assert not hasattr(got, "code"), getattr(got, "message", got)
        row = await conn.fetchrow(
            "SELECT kind, domain FROM kernel.executions WHERE story_id = $1",
            story)
        assert (row["kind"], row["domain"]) == ("errand", "ops")
    _run(body)


def test_an_ordinary_story_is_unchanged():
    """THE STORY PATH. A NULL work_kind must behave exactly as before —
    including the resource guard, which an errand skips."""
    async def body(conn):
        from kernel.assistant.write import start_story

        goal, story = await _goal_and_story(conn, work_kind=None)
        got = await start_story(conn, goal, "tester", story, "build it",
                                f"key-{os.urandom(3).hex()}")
        assert not hasattr(got, "code"), getattr(got, "message", got)
        row = await conn.fetchrow(
            "SELECT kind, domain FROM kernel.executions WHERE story_id = $1",
            story)
        assert (row["kind"], row["domain"]) == ("story", "coding")
    _run(body)


def test_a_story_with_no_resources_is_still_refused():
    """The guard that stops a story dispatching into an empty workspace.
    An errand skips it legitimately; a story must not."""
    async def body(conn):
        from kernel.assistant.write import start_story

        goal, story = await _goal_and_story(conn, work_kind=None,
                                            with_resource=False)
        got = await start_story(conn, goal, "tester", story, "build it",
                                f"key-{os.urandom(3).hex()}")
        assert getattr(got, "code", "") == "E_NO_RESOURCES"
    _run(body)


def test_the_assistant_may_still_override_the_shape():
    """It has had the conversation GO has not. "This is smaller than GO
    thought" is a real correction."""
    async def body(conn):
        from kernel.assistant.write import start_story

        goal, story = await _goal_and_story(conn, work_kind=None,
                                            with_resource=False)
        got = await start_story(conn, goal, "tester", story, "check it",
                                f"key-{os.urandom(3).hex()}", kind="errand")
        assert not hasattr(got, "code"), getattr(got, "message", got)
        assert await conn.fetchval(
            "SELECT kind FROM kernel.executions WHERE story_id = $1",
            story) == "errand"
    _run(body)


# ── I3b · validated here, not at dispatch ────────────────────────────────

def test_a_shape_that_is_not_one_is_refused_at_the_call():
    """`goal_state` returns a field literally called `kind` whose values
    are code|merge — a different taxonomy under the same name. An
    assistant echoing the board wrote a PENDING row that died at dispatch,
    a long way from its cause."""
    async def body(conn):
        from kernel.assistant.write import start_story

        goal, story = await _goal_and_story(conn, with_resource=False)
        got = await start_story(conn, goal, "tester", story, "t",
                                f"key-{os.urandom(3).hex()}", kind="code")
        assert getattr(got, "code", "") == "E_UNKNOWN_KIND"
        said = getattr(got, "message", "")
        assert "git engine" in said, "it does not say why `code` is wrong"
        assert await conn.fetchval(
            "SELECT count(*) FROM kernel.executions WHERE story_id = $1",
            story) == 0, "a dead row was created anyway"
    _run(body)


def test_a_profile_nobody_built_is_refused_at_the_call():
    """The pair has to resolve, not just the kind. `errand` with an
    unbuilt domain is the same dead row by another route."""
    async def body(conn):
        from kernel.assistant.write import start_story

        goal, story = await _goal_and_story(conn, with_resource=False)
        got = await start_story(conn, goal, "tester", story, "t",
                                f"key-{os.urandom(3).hex()}",
                                kind="errand", domain="nope")
        assert getattr(got, "code", "") == "E_UNKNOWN_PROFILE"
        assert "errand/ops" in getattr(got, "message", ""), (
            "the refusal does not list what exists")
    _run(body)


# ── I3d/I3e · goal_state tells the truth ─────────────────────────────────

def test_goal_state_reports_the_work_shape_and_no_meaningless_landing():
    """An errand reported `landed: false` forever fed straight into GO's
    "never announce delivery until it lands" — so GO would sit on every
    finished errand's answer waiting for a merge that cannot happen."""
    async def body(conn):
        from kernel.go import goal_state

        goal, story = await _goal_and_story(conn, work_kind="errand",
                                            with_resource=False)
        await conn.execute(
            "UPDATE stories SET state='done', result_summary=$2 WHERE id=$1",
            story, "March was up 4%; report emitted.")

        board = await goal_state(conn, goal)
        row = next(r for r in board["stories"] if r["story"] == story)
        assert row["work_kind"] == "errand"
        assert row["landed"] is None, "an errand cannot land"
        assert "March was up 4%" in row["result"]
        assert board["done_not_landed"] == 0, (
            "a finished errand counts as an unclearable backlog")
    _run(body)


def test_a_story_still_reports_landed_and_counts():
    """THE STORY PATH, unchanged. This is the field GO acts on."""
    async def body(conn):
        from kernel.go import goal_state

        goal, story = await _goal_and_story(conn, work_kind=None)
        await conn.execute(
            "UPDATE stories SET state='done' WHERE id=$1", story)
        board = await goal_state(conn, goal)
        row = next(r for r in board["stories"] if r["story"] == story)
        assert row["work_kind"] == "story"
        assert row["landed"] is False, "a story's landing must stay a fact"
        assert board["done_not_landed"] == 1
    _run(body)


# ── I3c · GO can read artifacts ──────────────────────────────────────────

def test_go_holds_list_artifacts_everywhere_it_must_be_declared():
    """Three declarations, and the list_artifacts incident was one of
    them missing. The startup assertion covers two; the template is the
    third and is what the gateway actually reads."""
    from kernel.assistant.api import GO_READS, GO_ROUTES
    from kernel.go import TOOLS

    assert "list_artifacts" in TOOLS
    assert "list_artifacts" in GO_READS, "a read, not a write"
    assert set(GO_ROUTES) == set(TOOLS), (
        f"GO's surface disagrees with itself: "
        f"{sorted(set(GO_ROUTES) ^ set(TOOLS))}")


def test_the_go_template_allows_it():
    async def body(conn):
        import json

        from kernel.go import TOOLS

        # `go-v2` IS THE KERNEL GO — `prompts.SYNCED_TEMPLATES` reconciles
        # its prompts from `kernel/go/` at startup and its allowlist is
        # this surface. `go` is a separate active template with its own,
        # smaller one; it denies eleven of these deliberately, and
        # asserting the kernel surface against it would be asserting that
        # two different agents are the same agent.
        allowed = await conn.fetchval(
            "SELECT tools_allow FROM templates WHERE id = 'go-v2'")
        if allowed is None:
            pytest.skip("no go-v2 template in this database")
        if isinstance(allowed, str):
            allowed = json.loads(allowed)
        assert "list_artifacts" in allowed, (
            "go-v2 forbids a tool the kernel serves GO")
        denied = sorted(set(TOOLS) - set(allowed))
        assert not denied, f"go-v2 denies: {denied}"

        # And the legacy row must NOT carry it: an allowlist entry with no
        # route is the inverse of the list_artifacts bug and just as
        # invisible — the model calls it and gets "unknown tool".
        legacy = await conn.fetchval(
            "SELECT tools_allow FROM templates WHERE id = 'go'")
        if legacy is not None:
            if isinstance(legacy, str):
                legacy = json.loads(legacy)
            assert "list_artifacts" not in legacy, (
                "the legacy GO declares a tool its surface does not serve")
    _run(body)


def test_go_reads_the_same_artifacts_the_assistant_does():
    """One implementation. Two would answer differently the first time
    one grew a field."""
    async def body(conn):
        from kernel.assistant.api import _dispatch_go

        goal, story = await _goal_and_story(conn, work_kind="errand",
                                            with_resource=False)
        eid = await conn.fetchval(
            "INSERT INTO kernel.executions (kind, domain, goal_id, story_id) "
            "VALUES ('errand','ops',$1,$2) RETURNING id", goal, story)
        await conn.execute(
            "INSERT INTO kernel.artifact_manifest "
            "(execution_id, artifact_id, role, name, summary, bytes) "
            "VALUES ($1,'sha-1','report','march.md','up 4%',900)", eid)

        got = await _dispatch_go(conn, "list_artifacts", goal, "go", {})
        assert got["count"] == 1
        assert got["artifacts"][0]["name"] == "march.md"
        assert "up 4%" in got["artifacts"][0]["summary"]
        assert all("text" not in a for a in got["artifacts"]), (
            "summaries only — GO does not read trees")
    _run(body)


# ── the shape survives the tool boundary ─────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("errand", "errand"), ("Errand", "errand"), ("  ERRAND  ", "errand"),
    ("story", None), ("", None), (None, None),
    # A typo must NOT become an errand. Downgrading a feature to
    # something with no branch and no Verifier, and then throwing the
    # workspace away with the work in it, is the expensive direction.
    ("errrand", None), ("chore", None), ("code", None), ("merge", None),
    (True, None), (7, None),
])
def test_only_the_word_errand_makes_an_errand(raw, expected):
    from app.services.work_shape import work_kind_of

    assert work_kind_of(raw) == expected


def test_the_route_stores_what_the_rule_returns():
    """The rule is extracted so it can be tested without HTTP; this is
    the join that keeps the extraction honest."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "app/routers/stories.py", encoding="utf-8").read()
    body = src[src.index("async def internal_create_story"):]
    assert 'work_kind_of(body.get("work_kind") or body.get("kind"))' in body
    assert "from app.services.work_shape import" in body
    # The column, not its position in the list. This pinned the exact
    # closing text `land_together, work_kind)`, so adding ANY later
    # column -- `short_title` did -- failed a guard that is about
    # `work_kind` being stored at all.
    insert = body[body.index("INSERT INTO stories"):]
    insert = insert[:insert.index(")")]
    assert "work_kind" in insert, (
        "the column is not in the INSERT")
