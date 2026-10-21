"""The VS Code lane holds the story open until the merge, the same way.

SAME LIFECYCLE, DIFFERENT THING HELD. There is no pod here, no leg and
no envelope — `local_registry` is explicit that the two lanes share a
key and nothing else. What persists instead is the CHECKOUT LOCK, so
"hold the workspace until it lands" means "do not hand this story to
somebody else, and do not call it finished".

WHY THE EXIT NEEDED NOTHING NEW. `_sweep_cold` already finishes any
story sitting at `awaiting_merge` whose nodes have all landed and which
has no open landing park. A local story has no park, because it never
had a run — so it matches that query exactly as a pod story whose run
was stood down does. Two different arrivals, one right answer.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    await conn.execute("SET search_path = kernel, public")
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


class _PoolOnConn:
    def __init__(self, conn):
        self._conn = conn

    async def fetch(self, *a):
        return await self._conn.fetch(*a)

    async def fetchval(self, *a):
        return await self._conn.fetchval(*a)

    async def execute(self, *a):
        return await self._conn.execute(*a)

    def acquire(self):
        conn = self._conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _CM()


async def _local_story(conn, *, state="in_progress", node="BRANCH_CREATED",
                       held=True, stale_hours=0):
    sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    gid, rid = f"{sid}-goal", uuid.uuid4().hex
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state,"
        "execution_mode,checked_out_to_uid,checkout_session_id,"
        "checkout_expires_at,updated_at) "
        "VALUES ($1,$2,$3,'person',$4,'local',$5,$6,"
        "        now() - interval '1 hour', now() - ($7 || ' hours')::interval)",
        sid, gid, "local story", state,
        f"{PREFIX}-user" if held else None,
        "sess-1" if held else None, str(int(stale_hours)))
    if node is not None:
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name) VALUES ($1,$2,$3)",
            rid, "acme", f"repo-{rid[:6]}")
        nid = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, "
            "state) VALUES ($1,$2,$3,$4,$5)",
            nid, rid, gid, f"feat/{sid}", node)
        await conn.execute(
            "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
            "VALUES ($1,$2,1)", nid, sid)
        return sid, gid, nid
    return sid, gid, None


# ── what "has unlanded work" means ───────────────────────────────────

@pytest.mark.asyncio
async def test_a_story_with_an_open_branch_has_unlanded_work():
    from app.services.story_landing import has_unlanded_work
    async with _conn() as conn:
        sid, _, _ = await _local_story(conn)
        assert await has_unlanded_work(conn, sid) is True


@pytest.mark.asyncio
async def test_a_story_with_no_branch_at_all_does_not():
    """A docs story, or one whose work was abandoned. Nothing to land, so
    `done` is not a lie and it must finish normally."""
    from app.services.story_landing import has_unlanded_work
    async with _conn() as conn:
        sid, _, _ = await _local_story(conn, node=None)
        assert await has_unlanded_work(conn, sid) is False


@pytest.mark.asyncio
async def test_a_story_whose_branch_already_merged_does_not():
    from app.services.story_landing import has_unlanded_work
    async with _conn() as conn:
        sid, _, _ = await _local_story(conn, node="MERGED")
        assert await has_unlanded_work(conn, sid) is False


@pytest.mark.asyncio
async def test_an_errand_has_a_branch_and_still_has_nothing_to_land():
    """THE TRAP, AND IT IS NOT OBVIOUS. `reconcile` cuts a workstream for
    an errand like any other story — "the branch is still cut, that is
    D1" — and only declines to create the MERGE story, because its work
    does not land. So the node test alone says yes for an errand, and
    marking one done would park it waiting for a merge the platform
    guarantees will never happen.

    Caught by `test_an_errands_lifecycle_is_untouched`, which is exactly
    what that guard was written for.
    """
    from app.services.story_landing import has_unlanded_work
    async with _conn() as conn:
        sid, _, _ = await _local_story(conn)
        await conn.execute(
            "UPDATE stories SET work_kind = 'errand' WHERE id = $1", sid)
        assert await has_unlanded_work(conn, sid) is False


# ── the checkout survives the wait ───────────────────────────────────

@pytest.mark.asyncio
async def test_closing_the_editor_does_not_hand_the_story_away():
    """THE BUG THIS PHASE WAS FOR. The expiry reaper frees a lock whose
    heartbeat stopped, on the reading that it was abandoned. That is
    right while somebody is working and wrong the moment the story is
    waiting on a PR: the wait is days, the person is not at their desk,
    and closing the editor would hand their story to somebody else."""
    from app.services.local_checkout import reap_local_checkouts
    async with _conn() as conn:
        sid, _, _ = await _local_story(conn, state="awaiting_merge")
        import app.services.local_checkout as lc
        orig, lc.get_pool = lc.get_pool, lambda: _PoolOnConn(conn)
        try:
            await reap_local_checkouts()
        finally:
            lc.get_pool = orig
        assert await conn.fetchval(
            "SELECT checked_out_to_uid FROM stories WHERE id = $1", sid
        ) is not None, (
            "the checkout was reclaimed mid-merge-wait — somebody else can "
            "now claim a story that is still theirs")


@pytest.mark.asyncio
async def test_a_working_story_with_a_dead_heartbeat_is_still_reclaimed():
    """The carve-out is for the merge wait only. An abandoned checkout on
    a story somebody is supposedly working on is still freed."""
    from app.services.local_checkout import reap_local_checkouts
    async with _conn() as conn:
        sid, _, _ = await _local_story(conn, state="in_progress")
        import app.services.local_checkout as lc
        orig, lc.get_pool = lc.get_pool, lambda: _PoolOnConn(conn)
        try:
            await reap_local_checkouts()
        finally:
            lc.get_pool = orig
        assert await conn.fetchval(
            "SELECT checked_out_to_uid FROM stories WHERE id = $1", sid
        ) is None


@pytest.mark.asyncio
async def test_a_checkout_nobody_came_back_to_is_freed_by_the_deadline():
    """The other half of the bargain. The lock is not freed when the
    editor closes, and it IS freed when the merge never comes."""
    from app.services import awaiting_merge_janitor as j
    async with _conn() as conn:
        sid, _, _ = await _local_story(
            conn, state="awaiting_merge", stale_hours=j.STALE_AFTER_HOURS + 5)
        freed = await j._release_local(_PoolOnConn(conn))
        assert freed == 1
        row = await conn.fetchrow(
            "SELECT state, checked_out_to_uid FROM stories WHERE id = $1", sid)
        assert row["checked_out_to_uid"] is None
        assert row["state"] == "awaiting_merge", (
            "the story was moved as well as released — the branch really "
            "is pushed and its PR really is open, so that is still true")


@pytest.mark.asyncio
async def test_a_recent_wait_is_left_alone():
    from app.services import awaiting_merge_janitor as j
    async with _conn() as conn:
        await _local_story(conn, state="awaiting_merge", stale_hours=1)
        assert await j._release_local(_PoolOnConn(conn)) == 0


# ── and the merge finishes it, with no run to do it ──────────────────

@pytest.mark.asyncio
async def test_the_merge_OFFERS_CLOSEOUT_rather_than_finishing(monkeypatch):
    """CHANGED 2026-10-14, and the change is the point.

    `_sweep_cold` used to take a landed local story straight to `done`,
    which is right for the pod lane -- its run is gone, its workspace
    with it, there is nobody left to ask. It is wrong here. A local
    story's person is sitting in the editor the work was done in, and
    `done` on a local story runs `finish_local_story`: the result
    summary, the review exemption, the GO's completion message. Finishing
    it from the sweep skips every one of those and takes the click away
    from the one person able to make it.

    So the merge moves the PHASE and leaves the state alone."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import story_landing
    async with _conn() as conn:
        sid, _, nid = await _local_story(conn, state="awaiting_merge")
        await conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_merge' WHERE id = $1",
            sid)
        await conn.execute(
            "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid)
        assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 1
        row = await conn.fetchrow(
            "SELECT state, current_phase FROM stories WHERE id = $1", sid)
        assert row["state"] == "awaiting_merge", (
            "the sweep finished a local story the person had not closed out")
        assert row["current_phase"] == "awaiting_closeout"


@pytest.mark.asyncio
async def test_the_closeout_offer_does_not_fire_twice(monkeypatch):
    """The sweep is a poll. A second tick must not re-announce a merge,
    nor drag a story backwards that has since moved on to more work."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import story_landing
    async with _conn() as conn:
        sid, _, nid = await _local_story(conn, state="awaiting_merge")
        await conn.execute(
            "UPDATE stories SET current_phase = 'awaiting_merge' WHERE id = $1",
            sid)
        await conn.execute(
            "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid)
        assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 1
        assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 0


@pytest.mark.asyncio
async def test_a_POD_story_still_finishes_from_the_sweep(monkeypatch):
    """RULE ZERO. The pod lane has nobody left to ask, so `done` from the
    sweep is the only honest move there and must not have changed."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import story_landing
    async with _conn() as conn:
        sid, _, nid = await _local_story(conn, state="awaiting_merge")
        await conn.execute(
            "UPDATE stories SET execution_mode = 'operator' WHERE id = $1", sid)
        await conn.execute(
            "UPDATE git_nodes SET state = 'MERGED' WHERE id = $1", nid)
        assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 1
        assert await conn.fetchval(
            "SELECT state FROM stories WHERE id = $1", sid) == "done"


@pytest.mark.asyncio
async def test_an_unmerged_local_story_is_not_finished(monkeypatch):
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import story_landing
    async with _conn() as conn:
        sid, _, _ = await _local_story(conn, state="awaiting_merge")
        assert await story_landing.sweep_landed(_PoolOnConn(conn)) == 0
        assert await conn.fetchval(
            "SELECT state FROM stories WHERE id = $1", sid) == "awaiting_merge"


def test_marking_done_waits_when_the_work_has_not_landed():
    """The entry point for the whole local lane, pinned at source.

    `POST /goals/{pid}/stories/{sid}/mark-done` is behind auth and the
    full app, so this reads the branch rather than driving it. What it
    is protecting is specific: the awaiting_merge path must NOT do the
    terminal tidying, and each omission is load-bearing —

      `completed_at`      would date the story to before it landed
      `active_thread_id`  would retire the conversation the person
                          still needs to ask for more work in
      `is_active_focus`   same, one indirection along
    """
    import inspect

    from app.routers import assistant
    src = inspect.getsource(assistant)
    i = src.index("DONE MEANS LANDED HERE TOO")
    j = src.index("_write_task_controller_announcement", i)
    arm = src[i:j]

    assert "has_unlanded_work" in arm, (
        "mark-done no longer asks whether the work landed; it would "
        "write `done` on a story whose PR is still open")
    assert "holds_for_merge_enabled" in arm, "the cutover switch is gone"
    assert "'awaiting_merge'" in arm

    waiting = arm[arm.index("state = 'awaiting_merge'"):]
    for terminal in ("completed_at", "active_thread_id", "is_active_focus"):
        assert terminal not in waiting, (
            f"the awaiting_merge path still writes {terminal}, which is "
            f"terminal tidying for a story that has not finished")


def test_the_local_prompt_stops_saying_marking_done_finishes_it():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    body = (root / "profiles-local" / "story-coding" / "prompt.md").read_text(
        encoding="utf-8")
    assert "awaiting_merge" in body
    assert "is not the end of the story" in body
    assert "no `sync_trunk` tool here" in body, (
        "the local prompt does not say trunk integration is ordinary work "
        "here — an agent told to call a tool that does not exist in this "
        "lane will try to")


def test_the_flag_reaches_the_pod_as_well_as_the_panel():
    """HALF ON IS THE FAILURE TO PICTURE, and it is invisible.

    `finish_execution` is where all four "finished" paths converge and
    `owes_a_landing` is the first question it asks — and one of those
    paths, `loop.py`'s autonomous branch, runs INSIDE the executor pod.
    A pod that cannot see the flag finishes those runs straight to DONE
    while the control panel is moving every other story to
    `awaiting_merge`: the story reaches `done` with its PR still open,
    the workspace is reclaimed under it, and the board says the work
    landed.

    Pods get a deliberate allowlist rather than the conductor's whole
    environment, so a new flag does not travel by default. The review
    flag learned this the same way; its own comment in that file is the
    record of it.
    """
    from kernel.conductor.sandbox_spec import POD_ENV_ALLOWLIST
    assert "STORY_HOLDS_FOR_MERGE" in POD_ENV_ALLOWLIST, (
        "the pod cannot see the flag, so autonomous runs will finish to "
        "DONE with their PR still open while the panel waits for a merge")
