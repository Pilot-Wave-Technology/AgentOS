"""A story has two threads now. Cards must land in the person's one.

THE REGRESSION THIS EXISTS TO CATCH, in the shape it actually happened.

Phase 7 of the Slack rework gave every story a SECOND thread -- the
Goal Orchestrator's, opened by `ensure_go_story_thread` about half a
second after the assistant's. Six card posters resolved their target as
"the newest thread for this story":

    ORDER BY th.created_at DESC LIMIT 1

which had been unambiguous for as long as a story had one thread, and
silently stopped being so. Measured on feature, story 3a1095ea: both
`kernel_park` approval cards sat in the GO's narration thread, and the
assistant thread the person was actually reading had none. Nothing
errored. Nothing logged. The cards were in the database, correct, and
in a channel nobody opens.

WHY THIS TEST IS AGAINST A REAL DATABASE. Every other test of these
posters reads the source, and the source looked right both before and
after the break -- the defect lived in the ORDERING OF TWO ROWS, which
no fake and no substring can see. So this one puts two real threads
half a second apart in real Postgres and asks the real SQL which one it
picks. It fails against the old resolver.

`asyncio.run`, not `@pytest.mark.asyncio`: pytest-asyncio is not
installed here, and an async test without it ERRORS rather than
skipping. Every test rolls back.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import pathlib
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from app.services.story_card_thread import newest_human_thread  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[3]
LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"


def _dsns() -> list[str]:
    seen, out = set(), []
    for dsn in (os.environ.get("INTERLOCK_TEST_DSN"),
                os.environ.get("DATABASE_URL"), LOCAL_DEV_DSN):
        if dsn and dsn not in seen:
            seen.add(dsn)
            out.append(dsn)
    return out


async def _connect_or_skip():
    tried = []
    for dsn in _dsns():
        try:
            return await asyncio.wait_for(asyncpg.connect(
                dsn, server_settings={"search_path": "kernel,public"}),
                timeout=5)
        except Exception as exc:                              # noqa: BLE001
            tried.append(f"{dsn.rsplit('@', 1)[-1]}: {type(exc).__name__}")
    pytest.skip("no local database — start local-development/scripts/"
                f"stack.ps1. Tried: {'; '.join(tried)}")


async def _two_threads(conn, story: str, goal: str):
    """The assistant's thread, then the GO's. In that order, as in life.

    `created_at` is written explicitly rather than slept for: the live
    gap is sub-second, and a test that depended on wall-clock ordering
    would be the flake rather than the guard.
    """
    human, go = str(uuid.uuid4()), str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO threads (id, goal_id, agent_id, story_id, created_by, "
        "title, participants, status, created_at) VALUES "
        "($1,$2,$3,$4,$5,$6,'{}'::text[],'active', now() - interval '1 s')",
        human, goal, "someone-assistant-0001", story, "uid-1", "the story")
    await conn.execute(
        "INSERT INTO threads (id, goal_id, agent_id, story_id, created_by, "
        "title, participants, status, created_at) VALUES "
        "($1,$2,$3,$4,$5,$6,'{}'::text[],'active', now())",
        go, goal, f"go-{goal}", story, "system", "GO: the story")
    return human, go


async def _a_goal(conn) -> str:
    goal = await conn.fetchval("SELECT id FROM goals LIMIT 1")
    if not goal:
        pytest.skip("no goal rows to hang a thread off")
    return goal


def test_the_resolver_skips_the_gos_narration_thread():
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            goal = await _a_goal(conn)
            story = f"ct-{uuid.uuid4().hex[:6]}"
            human, go_thread = await _two_threads(conn, story, goal)

            got = await conn.fetchval(
                f"SELECT {newest_human_thread('$1')}", story)
            assert got == human, (
                "the card would go to the GO's narration thread; the "
                "person reading the assistant thread sees nothing")
            assert got != go_thread
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def test_the_old_resolver_is_what_was_broken():
    """Proof the test above would have caught it, not a tautology.

    Without this, a future simplification could quietly restore the old
    query and the assertion above might still pass for some unrelated
    reason. This pins the actual failure.
    """
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            goal = await _a_goal(conn)
            story = f"ct-{uuid.uuid4().hex[:6]}"
            human, go_thread = await _two_threads(conn, story, goal)

            old = await conn.fetchval(
                "SELECT (SELECT th.id FROM threads th WHERE th.story_id = $1 "
                "AND th.left_goal_at IS NULL "
                "ORDER BY th.created_at DESC LIMIT 1)", story)
            assert old == go_thread, (
                "the old resolver no longer picks the GO thread, so the "
                "test above is no longer pinning the regression")
            assert old != human
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def test_a_story_with_only_a_human_thread_still_resolves():
    """The ordinary case must not have become a NULL."""
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            goal = await _a_goal(conn)
            story = f"ct-{uuid.uuid4().hex[:6]}"
            tid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, agent_id, story_id, "
                "created_by, title, participants, status) VALUES "
                "($1,$2,$3,$4,$5,$6,'{}'::text[],'active')",
                tid, goal, "someone-assistant-0001", story, "uid-1", "t")
            got = await conn.fetchval(
                f"SELECT {newest_human_thread('$1')}", story)
            assert got == tid
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


# ── and that the sixth copy cannot come back ──────────────────────────

CARD_POSTERS = (
    "app/services/kernel_park_cards.py",
    "app/services/kernel_answer_cards.py",
    "app/services/kernel_node_cards.py",
    "app/services/errand_answer_wake.py",
    "app/services/go_events.py",
)


def test_no_poster_spells_the_resolver_out_for_itself():
    """Six hand-written copies is how five of them get fixed.

    The source check is deliberately paired with the behavioural ones
    above: those prove the resolver is right, this proves nothing has
    gone back to not using it.
    """
    offenders = []
    for rel in CARD_POSTERS:
        src = (_ROOT / rel).read_text(encoding="utf-8")
        body = "".join(src.split())
        if "FROMthreadsthWHEREth.story_id" in body or (
                "FROMthreadsWHEREstory_id=$1ORDERBY" in body):
            offenders.append(rel)
    assert not offenders, (
        f"these spell the story-thread resolver out instead of importing "
        f"newest_human_thread, so a fix to it will miss them: {offenders}")


def test_the_resolver_deprioritises_go_threads_by_the_shared_prefix():
    """`go-` is how the whole tree recognises a GO-owned thread —
    the goal's orchestrator and an errand thread alike."""
    from app.services.story_card_thread import GO_AGENT_PREFIX
    assert GO_AGENT_PREFIX in newest_human_thread("x")
    assert "ORDER BY" in newest_human_thread("x")


def test_every_poster_actually_imports_it():
    for rel in CARD_POSTERS:
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "newest_human_thread" in src, rel


def test_the_fragment_is_a_single_parenthesised_subquery():
    """It is spliced into a COALESCE; unbalanced parens break five
    queries at once and only at runtime."""
    frag = newest_human_thread("e.story_id")
    assert frag.startswith("(") and frag.rstrip().endswith(")")
    assert frag.count("(") == frag.count(")")
    # A trailing `--` comment would swallow whatever follows the splice.
    assert not frag.rstrip().endswith(("--", "-- "))
    assert inspect.isfunction(newest_human_thread)


def test_every_spliced_query_actually_parses():
    """A shared fragment is spliced into queries with different FROMs.

    This caught a real one: `PR_THREAD_SQL` joins git_nodes and stories
    and has no execution in scope, so the `e.story_id` the other four
    use is not a column that exists there. Python is happy to build the
    string; only Postgres knows it is nonsense, and only when the card
    poster next runs -- which is to say, in front of a user.

    PREPARE is the cheapest way to ask the real planner. It validates
    names and types without executing anything.
    """
    import importlib
    import re as _re

    mods = ("app.services.kernel_park_cards", "app.services.kernel_answer_cards",
            "app.services.kernel_node_cards", "app.services.errand_answer_wake")

    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            checked, bad = 0, []
            for mn in mods:
                m = importlib.import_module(mn)
                for name in dir(m):
                    v = getattr(m, name)
                    if not isinstance(v, str) or "threads th" not in v:
                        continue
                    if not _re.search(r"\bSELECT\b", v):
                        continue
                    checked += 1
                    try:
                        await conn.execute("PREPARE _p AS " + v)
                        await conn.execute("DEALLOCATE _p")
                    except Exception as exc:               # noqa: BLE001
                        bad.append(f"{mn}.{name}: "
                                   f"{str(exc).splitlines()[0]}")
            assert checked >= 7, (
                f"only {checked} queries found; the splice points have "
                f"moved and this guard is no longer watching them")
            assert not bad, "\n".join(bad)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def test_an_errands_go_thread_is_still_found():
    """THE OTHER WAY THIS GOES SILENT.

    An autonomous errand's thread is GO-owned — `errand_grants.
    _open_story_thread` puts the goal's `go_agent_id` on it — and it is
    the only thread such a story has, because nobody holds an assistant
    conversation about an errand they did not ask for. A resolver that
    EXCLUDED `go-` threads rather than deprioritising them would return
    NULL here and post the errand's cards nowhere: the same silent
    disappearance this module exists to prevent, from the other side.
    """
    async def go():
        conn = await _connect_or_skip()
        tx = conn.transaction()
        await tx.start()
        try:
            goal = await _a_goal(conn)
            story = f"ct-{uuid.uuid4().hex[:6]}"
            tid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO threads (id, goal_id, agent_id, story_id, "
                "created_by, title, participants, status) VALUES "
                "($1,$2,$3,$4,$5,$6,'{}'::text[],'active')",
                tid, goal, f"go-{goal}", story, "uid-1", "an errand")
            got = await conn.fetchval(
                f"SELECT {newest_human_thread('$1')}", story)
            assert got == tid, (
                "the errand's only thread was skipped; its cards would "
                "go nowhere at all")
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def test_an_ordinary_story_no_longer_gets_a_go_thread():
    """REMOVED BY DECISION, not by accident: a second thread per story
    meant a second Slack channel per story. The GO's goal thread is
    where its commentary about an ordinary story belongs.

    The errand case is deliberately NOT covered by this — that one
    still opens its own thread, in `errand_grants`.
    """
    src = (_ROOT / "app/services/story_threads.py").read_text(
        encoding="utf-8")
    assert "ensure_go_story_thread" not in src, (
        "importing a story opens a GO thread again, so every story is "
        "two threads and two Slack channels")
    assert not (_ROOT / "app/services/go_story_threads.py").exists()
