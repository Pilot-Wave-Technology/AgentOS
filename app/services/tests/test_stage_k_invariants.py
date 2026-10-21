"""Stage K's invariants — one assistant, many stories.

Four things this stage decided, each of which fails silently if it drifts:

  1. The gateway agent id folds off the THREAD for a shared home, and only
     for it. Two call sites compare these strings (`stories.shca_instance_id`
     is a staleness guard), so a disagreement re-provisions forever.
  2. The per-user ceiling is FOUR live threads, the story being started does
     not count against itself, and the operator's one-story rule is untouched.
  3. A docs-only workspace never tells anyone the repo is at `repo/`. That
     sentence, written into a workspace with no checkout, is what sends a
     model off to invent a tree.
  4. RESOURCES.md for a no-checkout workspace contains no clone instructions
     and no Git Setup block.

**How to run.** 1, 3 and 4 need nothing. 2 needs the local stack
(`local-development/scripts/stack.ps1`) and SKIPS without it. Every DB test
runs inside a transaction that is always rolled back.
"""
from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from app.services.assistant_instance import base_agent_candidates, compose
from app.services.thread_ceiling import MAX_ACTIVE_THREADS, active_threads

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-stagek-test"


# ── 1. Identity ──────────────────────────────────────────────────────────

def test_shared_home_drops_the_thread():
    assert compose("abhi-shca", "KER14", "t-1", shared_home=True) == "abhi-shca-KER14"
    assert compose("abhi-shca", "KER14", "t-1", shared_home=False) == \
        "abhi-shca-KER14-t-1"


def test_base_agent_candidates_covers_both_forms():
    """Both forms are live at once for as long as any operator instance exists."""
    assert "abhi-shca" in base_agent_candidates("abhi-shca-ker14-9f2c-aa11", "KER14")
    assert "abhi-shca" in base_agent_candidates("abhi-shca-ker14", "KER14")


def test_base_agent_candidates_never_asserts_a_single_wrong_answer():
    """GO's own bare agents.id is `go-{goal}` — the same shape as a shared home.

    The first version of this returned ONE answer and folded `go-ker14` to
    `go`, which resolves to nothing or to some other agent. Candidates are a
    list precisely because the string cannot tell those two apart.
    """
    cands = base_agent_candidates("go-ker14", "KER14")
    assert "go-ker14" not in cands          # the caller already has the raw id
    assert cands == ["go"]                  # offered, never asserted
    assert base_agent_candidates("standalone-agent", "KER14") == []


# ── 3. The docs-only workspace says where the code is NOT ────────────────

class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetchrow(self, sql, *args):
        return self._rows.get("story" if "FROM stories" in sql else "goal")


def test_docs_identity_never_points_at_a_clone():
    from app.services.story_docs_workspace import build_identity_md

    pool = _FakePool({
        "story": {"title": "Add the ASIN extractor",
                  "description": "Pull ASINs out of the feed.",
                  "acceptance_criteria": ""},
        "goal": {"name": "KER14"},
    })
    md = asyncio.run(build_identity_md(pool, "KER14", "abc123"))

    assert "`repo/`" not in md and "./repo" not in md
    assert "cd repo" not in md
    # It must say where the code IS, not merely omit where it isn't.
    assert "workspace" in md.lower()
    assert "abc123" in md


# ── 4. RESOURCES.md for a workspace with no checkout ─────────────────────

def test_no_checkout_rules_have_no_clone_instructions():
    from app.resource_provisioning import _rules_section

    rules = _rules_section(has_repo=True, has_documents=True, repo_precloned=True,
                           clones=None, no_checkout=True)
    lowered = rules.lower()
    assert "git clone" in lowered  # the prohibition mentions it
    assert "clone each repo into" not in lowered
    assert "already checked out at" not in lowered
    assert "cd repo" not in lowered
    # The documents rules still apply — they are the point of this workspace.
    assert "documents are already on disk" in lowered


# ── 2. The ceiling (needs a database) ────────────────────────────────────

def _candidate_dsns() -> list[str]:
    seen, out = set(), []
    for dsn in (os.environ.get("STAGEK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


async def _connect_or_skip():
    """Resolved at TEST time — a sibling conftest sets a dummy DATABASE_URL at
    import, so an import-time probe would skip silently in a full-suite run."""
    attempts: list[str] = []
    for dsn in _candidate_dsns():
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=5)
        except Exception as exc:                  # noqa: BLE001
            attempts.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
            continue
        tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name IN ('stories','goals','threads')")
        if tables == 3:
            return conn
        await conn.close()
        attempts.append(f"{dsn.rsplit('@', 1)[-1]}: only {tables}/3 tables")
    pytest.skip("no local database — start the local stack. Tried: "
                + "; ".join(attempts))


@contextlib.asynccontextmanager
async def _rolled_back():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        leaked = await conn.fetchval(
            "SELECT count(*) FROM stories WHERE id LIKE $1", f"{PREFIX}%")
        await conn.close()
        assert leaked == 0, "fixture rows survived the rollback — dev DB dirtied"


async def _seed(conn, n_live: int) -> None:
    await conn.execute("INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
                       f"{PREFIX}-goal", "stage k fixture", f"{PREFIX}-user")
    for i in range(n_live):
        sid = f"{PREFIX}-s{i}"
        await conn.execute(
            "INSERT INTO stories (id, goal_id, title, state, created_by, assigned_to)"
            " VALUES ($1,$2,$3,'in_progress',$4,$4)",
            sid, f"{PREFIX}-goal", f"story {i}", f"{PREFIX}-user")
        await conn.execute(
            "INSERT INTO threads (id, goal_id, agent_id, story_id, created_by, title)"
            " VALUES ($1,$2,$3,$4,$5,$6)",
            f"{PREFIX}-t{i}", f"{PREFIX}-goal", f"{PREFIX}-shca", sid,
            f"{PREFIX}-user", sid)


def test_ceiling_is_four():
    """The number the person who pays for it chose. Not derived — asserted, so
    a change to it is a change someone made on purpose.

    Asserts the DEFAULT specifically. The ceiling is overridable
    (ASSISTANT_MAX_ACTIVE_THREADS) so a test run can prove the refusal at 2
    instead of running four stories to prove one branch — and this is what
    stops a lowered test value from quietly becoming the shipped one.
    """
    from app.services.thread_ceiling import (DEFAULT_MAX_ACTIVE_THREADS,
                                             max_active_threads)
    assert MAX_ACTIVE_THREADS == 4
    assert DEFAULT_MAX_ACTIVE_THREADS == 4

    import os
    was = os.environ.pop("ASSISTANT_MAX_ACTIVE_THREADS", None)
    try:
        assert max_active_threads() == 4
        os.environ["ASSISTANT_MAX_ACTIVE_THREADS"] = "2"
        assert max_active_threads() == 2
        # Nonsense falls back rather than refusing everything or letting
        # everything through — both of those are worse than the default.
        os.environ["ASSISTANT_MAX_ACTIVE_THREADS"] = "banana"
        assert max_active_threads() == 4
        os.environ["ASSISTANT_MAX_ACTIVE_THREADS"] = "0"
        assert max_active_threads() == 4
    finally:
        os.environ.pop("ASSISTANT_MAX_ACTIVE_THREADS", None)
        if was is not None:
            os.environ["ASSISTANT_MAX_ACTIVE_THREADS"] = was


def test_active_threads_counts_live_and_excludes_the_caller():
    async def run():
        async with _rolled_back() as conn:
            await _seed(conn, 4)
            live = await active_threads(conn, f"{PREFIX}-goal", f"{PREFIX}-user")
            assert len(live) == 4, [r["story_id"] for r in live]

            # The story being started never conflicts with itself.
            minus_self = await active_threads(
                conn, f"{PREFIX}-goal", f"{PREFIX}-user",
                excluding_story=f"{PREFIX}-s0")
            assert len(minus_self) == 3

            # A finished story frees its slot.
            await conn.execute("UPDATE stories SET state='done' WHERE id=$1",
                               f"{PREFIX}-s1")
            assert len(await active_threads(
                conn, f"{PREFIX}-goal", f"{PREFIX}-user")) == 3
    asyncio.run(run())


def test_a_refused_import_creates_no_thread():
    """A refusal must leave nothing behind, and Stage K is what put that at risk.

    The three refusals in `provision_assistant_thread_for_story` (kill switch,
    local checkout, ceiling) sat AFTER the thread resolution, and that was safe
    only while resolving a thread had no side effect — the operator has ONE
    thread per (user, goal), so the resolver found the existing row and created
    nothing. Stage K made the kernel assistant's thread PER STORY, and
    `_ensure_story_thread` INSERTs.

    Observed on KER15: the ceiling refused story 9a015cb0 and a fully-formed,
    story-bound thread (0537d960) was created one log line EARLIER. It showed
    up in the sidebar, and clicking it opened a gateway session on a story the
    system had just refused to start.

    Pinned as source order rather than behaviour because the behaviour needs a
    gateway, an agent instance and a live assistant to reproduce — and the thing
    that broke was purely where a line sits relative to another.

    Read off the FILE rather than an import: `app.routers.assistant` pulls in
    `firebase_admin`, which is not installed everywhere this suite runs, and a
    gate that skips on the machines that need it is not a gate.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[3].joinpath(
        "app", "routers", "assistant.py").read_text(encoding="utf-8")

    # The CALL, not the name: the function's own comments discuss both, and
    # matching prose made the first version of this test fail on the fixed code.
    body = src[src.index("async def provision_assistant_thread_for_story"):]
    guard = body.index("await _refuse_if_story_cannot_start(")
    creates = body.index("await _ensure_story_thread(")
    assert guard < creates, (
        "the refusals must run BEFORE the per-story thread is created — "
        "a refused import that leaves a thread behind is the KER15 leak")

    # And the guard must still cover all three reasons, or hoisting them
    # would have quietly dropped one.
    decl = src.index("async def _refuse_if_story_cannot_start")
    guard_src = src[decl:src.index("async def provision_assistant_thread_for_story")]
    for reason in ("dispatch_block", "checked_out_to_uid", "refusal_for"):
        assert reason in guard_src, f"{reason} refusal went missing"


def test_a_board_started_story_counts_even_with_no_thread():
    """The bug the original guard's comment warned about, re-introduced.

    The board's Start button never binds a thread — only an import does.
    A thread-scoped count therefore sees board-started work as zero and the
    ceiling stops existing for anyone who uses the board.
    """
    async def run():
        async with _rolled_back() as conn:
            g = f"{PREFIX}-goal"
            await conn.execute(
                "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3)",
                g, "board fixture", f"{PREFIX}-user")

            async def story(sid, state, assignee, with_thread):
                await conn.execute(
                    "INSERT INTO stories (id, goal_id, title, state, "
                    " created_by, assigned_to) VALUES ($1,$2,$3,$4,$5,$6)",
                    sid, g, sid, state, f"{PREFIX}-user", assignee)
                if with_thread:
                    await conn.execute(
                        "INSERT INTO threads (id, goal_id, agent_id, story_id,"
                        " created_by, title) VALUES ($1,$2,$3,$4,$5,$6)",
                        "t-" + sid, g, f"{PREFIX}-shca", sid,
                        f"{PREFIX}-user", sid)

            # Started from the BOARD: assigned, in flight, no thread.
            await story(f"{PREFIX}-board", "in_progress", f"{PREFIX}-user", False)
            # IMPORTED: has a thread.
            await story(f"{PREFIX}-import", "in_progress", f"{PREFIX}-user", True)
            # Not in flight — must NOT occupy a slot, or a goal is over its
            # ceiling the moment GO finishes planning.
            await story(f"{PREFIX}-proposed", "proposed", f"{PREFIX}-user", False)
            await story(f"{PREFIX}-accepted", "accepted", f"{PREFIX}-user", False)
            await story(f"{PREFIX}-done", "done", f"{PREFIX}-user", True)
            # Somebody else's, and this user has no thread on it.
            await story(f"{PREFIX}-theirs", "in_progress", "someone-else", False)

            live = await active_threads(conn, g, f"{PREFIX}-user")
            got = {r["story_id"] for r in live}
            assert got == {f"{PREFIX}-board", f"{PREFIX}-import"}, sorted(got)
    asyncio.run(run())
