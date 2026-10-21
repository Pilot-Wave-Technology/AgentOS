"""Multi-repo first-execution fix (`research/MULTI_REPO_STORIES_PLAN.md` §12.5), against
a real database.

The bug that motivated this was a RACE: a second write repo's branch got cut a few
seconds after the first provisioning attempt had already given up on it. Reproducing
that race in a test would be flaky by construction — sometimes the timing lands,
sometimes it doesn't.

None of these tests try to. Instead they build, by hand, the exact database state the
race PRODUCES — a `ready` workspace with one write repo recorded and a second one
granted-and-branched-but-unrecorded — and assert the decision functions handle that
state correctly. The race is what makes the state possible in production; it is not
what makes the state testable.
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.operator_provisioning import is_write_repo_set_incomplete
from app.services.story_kickoff_state import story_has_had_kickoff
from app.services.tests.test_local_workspace_nodes_sql import _node, _story
from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        leaked = sum([
            await conn.fetchval("SELECT count(*) FROM stories WHERE id LIKE $1", f"{PREFIX}%"),
            await conn.fetchval("SELECT count(*) FROM messages WHERE thread_id LIKE $1", f"{PREFIX}%"),
        ])
        await conn.close()
        assert leaked == 0, f"{leaked} fixture rows survived rollback — dev DB dirtied"


async def _grant(conn, sid: str, owner: str, name: str, *, relation: str, is_write: bool,
                 status: str = "active", branch_name: str | None = None):
    rid = f"{sid}-{name}"
    await conn.execute(
        """INSERT INTO goal_resources (id, goal_id, kind, name, created_by, status,
                                       repo_owner, repo_name, branch_name)
           VALUES ($1,$2,'repo',$3,$4,$5,$6,$7,$8)""",
        rid, f"{sid}-goal", f"{name}-grant", f"{PREFIX}-user", status, owner, name, branch_name,
    )
    await conn.execute(
        "INSERT INTO story_resources (story_id, resource_id, relation) VALUES ($1,$2,$3)",
        sid, rid, relation,
    )


class _FlagOn:
    """Wraps the real settings object, forcing `multi_repo_enabled` on regardless of
    this environment's actual config — the decision logic must be tested independently
    of whether this particular dev stack happens to have the flag flipped."""
    def __init__(self, real):
        self._real = real
    def __getattr__(self, name):
        if name == "multi_repo_enabled":
            return True
        return getattr(self._real, name)


@contextlib.contextmanager
def _multi_repo_enabled():
    from unittest.mock import patch
    from app import config as config_module
    from app.services import operator_provisioning
    settings = config_module.get_settings()
    with patch.object(operator_provisioning, "get_settings", lambda: _FlagOn(settings)):
        yield


async def _post_kickoff_marker(conn, thread_id: str, sid: str):
    """`messages.thread_id` FKs to `threads`, which FKs to `goals` — both must exist
    first. `ON CONFLICT DO NOTHING` lets two calls for the same story share one goal."""
    await conn.execute(
        "INSERT INTO goals (id, name, created_by) VALUES ($1,$2,$3) "
        "ON CONFLICT (id) DO NOTHING",
        f"{sid}-goal", f"fixture {sid}", f"{PREFIX}-user",
    )
    await conn.execute(
        "INSERT INTO threads (id, goal_id, created_by, title, story_id) "
        "VALUES ($1,$2,$3,$4,$5)",
        thread_id, f"{sid}-goal", f"{PREFIX}-user", "kickoff fixture thread", sid,
    )
    await conn.execute(
        """INSERT INTO messages (id, thread_id, role, sender, content, created_at)
           VALUES (gen_random_uuid(), $1, 'agent', 'go', $2, NOW())""",
        thread_id, f"# Story Mode Active — {sid}\n\nkickoff.",
    )


# ── is_write_repo_set_incomplete ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_exact_race_state_reads_as_incomplete():
    """The state the race actually produces: two write grants with cut branches, only
    one recorded in story_workspace_nodes."""
    async with _conn() as conn:
        sid = f"{PREFIX}-race"
        await _story(conn, sid, execution_mode="operator")
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "repo-b", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _node(conn, sid, "acme", "repo-a", "feat/x", "repos/acme__repo-a")
        # repo-b: granted, branch cut, but NOT in story_workspace_nodes — the drop.

        with _multi_repo_enabled():
            assert await is_write_repo_set_incomplete(conn, f"{sid}-goal", sid) is True


@pytest.mark.asyncio
async def test_complete_repo_set_is_not_incomplete():
    async with _conn() as conn:
        sid = f"{PREFIX}-complete"
        await _story(conn, sid, execution_mode="operator")
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "repo-b", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _node(conn, sid, "acme", "repo-a", "feat/x", "repos/acme__repo-a")
        await _node(conn, sid, "acme", "repo-b", "feat/x", "repos/acme__repo-b")

        with _multi_repo_enabled():
            assert await is_write_repo_set_incomplete(conn, f"{sid}-goal", sid) is False


@pytest.mark.asyncio
async def test_a_write_repo_whose_branch_is_not_cut_yet_is_not_incomplete():
    """The FIRST provisioning attempt's own state, before the race even happens — repo B
    has no branch yet, so there is nothing to add and nothing to flag."""
    async with _conn() as conn:
        sid = f"{PREFIX}-notyet"
        await _story(conn, sid, execution_mode="operator")
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "repo-b", relation="produces",
                     is_write=True, branch_name=None)  # not cut yet
        await _node(conn, sid, "acme", "repo-a", "feat/x", "repos/acme__repo-a")

        with _multi_repo_enabled():
            assert await is_write_repo_set_incomplete(conn, f"{sid}-goal", sid) is False


@pytest.mark.asyncio
async def test_an_unrecorded_readonly_repo_is_not_incomplete():
    """A `needs` grant never gets a node — that is expected, not a drop."""
    async with _conn() as conn:
        sid = f"{PREFIX}-readonly"
        await _story(conn, sid, execution_mode="operator")
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "repo-ctx", relation="needs",
                     is_write=False, branch_name=None)
        await _node(conn, sid, "acme", "repo-a", "feat/x", "repos/acme__repo-a")

        with _multi_repo_enabled():
            assert await is_write_repo_set_incomplete(conn, f"{sid}-goal", sid) is False


class _FlagOff:
    """Wraps the real settings object, overriding only `multi_repo_enabled`."""
    def __init__(self, real):
        self._real = real
    def __getattr__(self, name):
        if name == "multi_repo_enabled":
            return False
        return getattr(self._real, name)


@pytest.mark.asyncio
async def test_flag_off_is_always_complete():
    """`multi_repo_enabled=False` must short-circuit to False without even querying —
    a single-repo story cannot reach this state, so this must never change its
    behaviour."""
    from unittest.mock import patch
    from app import config as config_module
    from app.services import operator_provisioning
    async with _conn() as conn:
        sid = f"{PREFIX}-flagoff"
        await _story(conn, sid, execution_mode="operator")
        # Even a deliberately "incomplete-looking" grant set must not matter.
        await _grant(conn, sid, "acme", "repo-a", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _grant(conn, sid, "acme", "repo-b", relation="produces",
                     is_write=True, branch_name="feat/x")
        await _node(conn, sid, "acme", "repo-a", "feat/x", "repos/acme__repo-a")

        settings = config_module.get_settings()
        with patch.object(operator_provisioning, "get_settings", lambda: _FlagOff(settings)):
            assert await is_write_repo_set_incomplete(conn, f"{sid}-goal", sid) is False


# ── story_has_had_kickoff ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kickoff_marker_present_is_detected_unwindowed():
    """No time window: a marker from any point in the past still counts — this is what
    distinguishes it from the existing 2-minute double-fire guard."""
    async with _conn() as conn:
        sid = f"{PREFIX}-kickoff-yes"
        thread = f"{PREFIX}-thread-yes"
        await _post_kickoff_marker(conn, thread, sid)
        assert await story_has_had_kickoff(conn, sid, thread) is True


@pytest.mark.asyncio
async def test_no_marker_means_never_sent():
    async with _conn() as conn:
        assert await story_has_had_kickoff(conn, "some-story", f"{PREFIX}-thread-none") is False


@pytest.mark.asyncio
async def test_empty_story_id_is_never_sent_without_a_query():
    async with _conn() as conn:
        assert await story_has_had_kickoff(conn, "") is False


@pytest.mark.asyncio
async def test_a_different_storys_marker_does_not_count():
    """The content match is per-story, not just per-thread — a shared/general thread
    with another story's marker must not read as this story's kickoff."""
    async with _conn() as conn:
        thread = f"{PREFIX}-thread-shared"
        await _post_kickoff_marker(conn, thread, "some-other-story")
        assert await story_has_had_kickoff(conn, f"{PREFIX}-this-story", thread) is False


@pytest.mark.asyncio
async def test_marker_in_another_thread_of_the_same_story_counts():
    """The regression this predicate exists for (live, 2026-08-25, story e97ea4fb).

    A story's conversation fragments across threads — `active_thread_id` drifts or
    clears and a fresh thread is minted, which is exactly why `threads.story_id` was
    added. Scoped to one thread, the guard answered "never kicked off" in every NEW
    thread, so `dispatch_story_kickoff_to_assistant` took its destructive re-clone
    branch once per assistant on a workspace that already held real work. Story-scoped,
    the kickoff is still visible from a thread that has no marker of its own.
    """
    async with _conn() as conn:
        sid = f"{PREFIX}-kickoff-fragmented"
        await _post_kickoff_marker(conn, f"{PREFIX}-thread-old", sid)
        assert await story_has_had_kickoff(conn, sid, f"{PREFIX}-thread-new") is True
