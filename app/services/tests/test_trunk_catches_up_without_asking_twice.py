"""Trunk moved. The story catches itself up, and a person says yes.

THE PLATFORM WRITES THE PLAN; A PERSON APPROVES IT. Those are two
different things and the split is the whole design. Nobody should have
to AUTHOR three steps that are identical every time -- and nobody
should have their branch integrated without being asked, because the
canned steps run THE STORY'S EXISTING CHECKS and integration is exactly
when those can be too few.

An earlier version approved it as well as writing it, on the reasoning
that re-reading identical steps teaches rubber-stamping. That traded a
concrete risk for a speculative one. These pin the corrected bargain:
written for you, approved by you, revisable, and still scoped once
approved so a conflict cannot be edited through.
"""
from __future__ import annotations

import contextlib
import json
import uuid

import pytest

from app.services.tests.test_story_repo_scope_sql import PREFIX, _connect_or_skip


class _Borrowed:
    """The test's own connection, surviving the code closing it."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def close(self):
        return None


@contextlib.asynccontextmanager
async def _conn():
    conn = await _connect_or_skip()
    await conn.execute("SET search_path = kernel, public")
    tx = conn.transaction()
    await tx.start()
    import app.services.story_landing as _sl
    _orig = _sl._kernel_conn

    async def _borrowed():
        return _Borrowed(conn)
    _sl._kernel_conn = _borrowed
    try:
        yield conn
    finally:
        _sl._kernel_conn = _orig
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


async def _behind_story(conn, *, lanes=("main",)):
    """A story parked on its landing whose lane(s) trunk has moved past."""
    sid = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    gid = f"{sid}-goal"
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       gid, "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,state) "
        "VALUES ($1,$2,$3,'person','awaiting_merge')", sid, gid, "behind trunk")
    eid = await conn.fetchval(
        "INSERT INTO kernel.executions (goal_id, story_id, kind, domain, "
        "state, generation) VALUES ($1,$2,'story','code','PARKED_HOT',2) "
        "RETURNING id", gid, sid)
    for lane in lanes:
        rid = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_repos (id, owner, name, trunk_branch) "
            "VALUES ($1,$2,$3,$4)", rid, "acme", f"repo-{rid[:6]}", lane)
        nid = uuid.uuid4().hex
        await conn.execute(
            "INSERT INTO git_nodes (id, git_repo_id, goal_id, branch_name, "
            "state, trunk_branch, last_synced_trunk_sha, created_at) "
            "VALUES ($1,$2,$3,$4,'BRANCH_CREATED',$5,'old-sha',"
            "        now() - interval '1 day')",
            nid, rid, gid, f"feat/{sid}-{lane}", lane)
        await conn.execute(
            "INSERT INTO git_node_stories (git_node_id, story_id, seq) "
            "VALUES ($1,$2,1)", nid, sid)
        await conn.execute(
            "INSERT INTO git_repo_trunks (git_repo_id, trunk_branch, "
            "trunk_sha, trunk_sha_seen_at) VALUES ($1,$2,$3, now())",
            rid, lane, f"new-{lane}")
    return sid, eid


# ── the scope, which survives the change of who approves ─────────────

def test_one_approval_covers_resolving_a_conflict_and_nothing_more():
    """BMIF1 run 2 needed FOUR approvals to catch up one moved trunk --
    the canned plan, two revisions, then a conflict plan -- because the
    scope refused every write. Resolving a conflict now sits inside the
    one approval; anything beyond it (a shell, an environment change, a
    report) still does not."""
    from kernel.harness.integration_plan import TOOL_SCOPE
    for needed in ("sync_trunk", "run_check", "write", "edit",
                   "story_commit"):
        assert needed in TOOL_SCOPE
    for beyond in ("bash", "bash_background", "apply_patch", "web_fetch",
                   "env_install", "env_setup", "emit", "emit_artifact",
                   "publish_artifact"):
        assert beyond not in TOOL_SCOPE


def test_the_canned_plan_carries_the_checks_it_is_given():
    """Approving a plan WITH checks replaces the contract, so the sweep
    hands it the story's own checks plus each repo's suite. Built
    without any, it carries none rather than inventing some."""
    from kernel.harness.integration_plan import canned_plan
    assert canned_plan(["abc"])["checks"] == []
    given = [{"run": "pytest", "label": "guard"}]
    assert canned_plan(["abc"], checks=given)["checks"] == given


def test_no_step_asks_for_a_report():
    """The emit step produced "Emit a report artifact confirming trunk is
    synced" plans and an artifact goal check nobody needed."""
    from kernel.harness.integration_plan import STEPS
    text = " ".join(STEPS).lower()
    assert "`emit`" not in text
    assert "do not write a report" in text
    assert "resolve them in place" in text
    assert "full test suite" in text
    assert "with no tests passes" in text


def test_the_plan_names_every_lane_it_is_catching_up():
    """A story writing two repos can be behind on both, and they land as
    a group. A plan naming one would leave the other silently
    un-integrated, and the person approving could not tell."""
    from kernel.harness.integration_plan import canned_plan
    p = canned_plan(["aaaaaaaaaaaa", "bbbbbbbbbbbb"], ["api", "web"])
    assert "`api`" in p["question"] and "`web`" in p["question"]
    assert p["lanes"] == ["api", "web"]


def test_the_rationale_no_longer_claims_the_platform_approves_it():
    from kernel.harness.integration_plan import RATIONALE
    assert "YOU approve" in RATIONALE
    assert "revise" in RATIONALE.lower(), (
        "nothing tells the person they may add checks, which is the "
        "main thing the approval buys beyond consent")


def test_an_ordinary_plan_scopes_nothing():
    from kernel.harness.integration_plan import scope_of
    assert scope_of({"steps": ["do the thing"]}) is None
    assert scope_of({}) is None
    assert scope_of({"tool_scope": ["run_check"]}) == frozenset({"run_check"})


# ── the gate ─────────────────────────────────────────────────────────

def _executor(scope):
    from kernel.harness.executor import BridgeToolExecutor

    class _P:
        requires_plan = True

        def tool_names(self):
            return []
    return BridgeToolExecutor(profile=_P(), plan_approved=True,
                              plan_scope=scope, execution_id=1)


@pytest.mark.asyncio
async def test_a_scoped_plan_refuses_a_tool_it_did_not_name():
    """Tested with `bash` rather than `write`: the chain refuses a write
    earlier on writable scope, so a write would pass for the wrong
    reason and keep passing if this gate were deleted."""
    ex = _executor(frozenset({"sync_trunk", "run_check", "emit"}))
    res = await ex._call_inner("bash", {"command": "git merge main"}, "t1")
    assert res.error and "E_PLAN_NOT_APPROVED" in res.error
    assert "register_plan" in res.error


@pytest.mark.asyncio
async def test_a_scoped_plan_allows_what_it_named():
    ex = _executor(frozenset({"sync_trunk", "run_check", "emit"}))
    res = await ex._call_inner("sync_trunk", {}, "t2")
    assert not (res.error or "").startswith("E_PLAN_NOT_APPROVED")


@pytest.mark.asyncio
async def test_an_unscoped_plan_is_unaffected():
    ex = _executor(None)
    res = await ex._call_inner("write", {"path": "a.py"}, "t3")
    assert not (res.error or "").startswith("E_PLAN_NOT_APPROVED")


# ── offering it ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_plan_is_offered_for_approval_not_answered(monkeypatch):
    """THE CORRECTION. The platform writes it; a person approves it."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        _, eid = await _behind_story(conn)
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 1

        q = await conn.fetchrow(
            "SELECT status, verdict, payload FROM kernel.questions "
            " WHERE execution_id = $1 AND kind = 'authorization'", eid)
        assert q["status"] == "OPEN", (
            "the integration plan was answered by the platform — nobody "
            "is being asked whether these checks are enough")
        assert q["verdict"] is None
        pl = q["payload"]
        pl = json.loads(pl) if isinstance(pl, str) else pl
        from kernel.harness.integration_plan import TOOL_SCOPE
        assert pl["tool_scope"] == sorted(TOOL_SCOPE)
        assert pl["platform_canned"] is True


@pytest.mark.asyncio
async def test_the_run_parks_on_it_rather_than_being_woken(monkeypatch):
    """`open_request` parks the run; approving is what un-parks it,
    through the ordinary path. Nothing here wakes it by hand."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        _, eid = await _behind_story(conn)
        await trunk_integration.sweep(_PoolOnConn(conn))
        assert await conn.fetchval(
            "SELECT state FROM kernel.executions WHERE id = $1", eid
        ) == "PARKED_HOT"


@pytest.mark.asyncio
async def test_the_open_landing_is_retired_first(monkeypatch):
    """`questions_one_open` is UNIQUE per execution on status='OPEN', so
    the plan cannot be inserted while the merge question is open.
    Retiring it is not tidying afterwards -- it is what makes the
    registration possible at all."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        _, eid = await _behind_story(conn)
        lid = await conn.fetchval(
            "INSERT INTO kernel.questions (execution_id, generation, kind, "
            "question_text, payload) VALUES ($1,2,'landing','waiting',"
            "'{}'::jsonb) RETURNING id", eid)
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 1
        assert await conn.fetchval(
            "SELECT status FROM kernel.questions WHERE id = $1", lid
        ) == "ANSWERED"


@pytest.mark.asyncio
async def test_one_plan_for_a_story_behind_on_two_lanes(monkeypatch):
    """THE MULTI-REPO FIX. Per node this produced two rows, and the
    second was refused by `questions_one_open` -- leaving that lane
    silently un-integrated."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        _, eid = await _behind_story(conn, lanes=("api", "web"))
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM kernel.questions WHERE execution_id = $1 "
            "AND kind = 'authorization'", eid) == 1
        pl = await conn.fetchval(
            "SELECT payload::text FROM kernel.questions "
            " WHERE execution_id = $1 AND kind = 'authorization'", eid)
        assert "api" in pl and "web" in pl, (
            "the plan names one lane of two — the other would be left "
            "behind with nobody told")
        targets = json.loads(pl)["targets"]
        assert sorted(t["lane"] for t in targets) == ["api", "web"]
        assert {t["sha"] for t in targets} == {"new-api", "new-web"}, (
            "each lane must carry ITS sha, not one from a sorted list")


@pytest.mark.asyncio
async def test_sweeping_twice_offers_one_plan(monkeypatch):
    """The sweep runs every poll. Re-offering would retire the landing
    park again each time, and one of those retirements raced a person's
    merge on a4098b67 — which then found no park, opened no closeout,
    and finished the story with no review."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        _, eid = await _behind_story(conn)
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 1
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM kernel.questions WHERE execution_id = $1 "
            "AND kind = 'authorization'", eid) == 1


# ── and when it must NOT fire ────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_story_still_being_written_is_left_alone(monkeypatch):
    """THE SHARPEST EDGE. `plan_scope` reads the newest approved plan,
    so an integration plan handed to a working executor re-scopes it
    mid-edit and locks it out of its own write tools with no way back.
    And there is no hurry: a branch still being written is not going
    anywhere until it is pushed."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        sid, eid = await _behind_story(conn)
        await conn.execute(
            "UPDATE stories SET state = 'in_progress' WHERE id = $1", sid)
        await conn.execute(
            "UPDATE kernel.executions SET state = 'RUNNING' WHERE id = $1",
            eid)
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 0


@pytest.mark.asyncio
async def test_a_stale_trunk_observation_is_not_evidence(monkeypatch):
    """`trunk_sha` is a DETECTION CACHE, not the live tip. On BMIA1
    `bmi_backend` was cached from 8 Oct while its real tip -- and the
    branch's own base -- was newer. Trunk can only have moved SINCE the
    node was cut."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        await _behind_story(conn)
        await conn.execute(
            "UPDATE git_repo_trunks SET trunk_sha_seen_at = "
            " now() - interval '4 days'")
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 0


@pytest.mark.asyncio
async def test_a_caught_up_branch_is_left_alone(monkeypatch):
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        await _behind_story(conn)
        await conn.execute(
            "UPDATE git_nodes SET last_synced_trunk_sha = 'new-main'")
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 0


@pytest.mark.asyncio
async def test_an_errands_branch_is_never_caught_up(monkeypatch):
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services import trunk_integration
    async with _conn() as conn:
        sid, _ = await _behind_story(conn)
        await conn.execute(
            "UPDATE stories SET work_kind = 'errand' WHERE id = $1", sid)
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 0


@pytest.mark.asyncio
async def test_the_flag_off_changes_nothing():
    from app.services import trunk_integration
    async with _conn() as conn:
        await _behind_story(conn)
        assert await trunk_integration.sweep(_PoolOnConn(conn)) == 0


def test_the_sync_record_moved_off_the_pod():
    """BOTH OF THESE USED TO PIN THE POD WRITING IT, and the pod never
    managed to: `agentos_leg_dev` has no write on `git_nodes`, so every
    `sync_trunk` logged `permission denied` inside a best-effort
    try/except and the column stayed NULL. The tests passed throughout
    -- they read the source, not the outcome.

    The owner is now the engine, which has GitHub's own compare to go
    on. The rest of that argument, and the reason a GRANT would have
    been the worse fix, is in
    `test_one_integration_not_two.py`.
    """
    import inspect

    from kernel.harness import executor_tools
    assert "_record_synced" not in inspect.getsource(executor_tools)


def test_a_push_advances_the_lane_it_was_actually_for():
    """ONE REPO, SEVERAL LANES, ONE last-write-wins FIELD.

    The push handler read `repo["trunk_branch"]` — a single value on a
    repo that may carry five lanes. So a push to `bmi_backend` went and
    re-read `bmi_frontend`'s head and advanced THAT, and bmi_backend's
    observed sha was never refreshed by any push, ever. Found on BMIA1
    after three days stale: nothing could notice trunk moving under a
    story on that lane, so the integration sweep could never fire for
    it.

    `reconcile` states the rule this missed: trunk_branch is carried on
    the NODE, not repo-canonical.
    """
    import inspect

    from app.services.git_graph import detect
    src = inspect.getsource(detect._handle_event)
    i = src.index('event == "push"')
    arm = src[i:i + 1400]
    assert "_pushed_lane" in arm, (
        "the push handler still advances the repo's default lane "
        "regardless of which branch was pushed")
    assert "legacy=(pushed == trunk_branch)" in arm, (
        "a non-default lane would clobber the legacy coarse "
        "git_repos.trunk_sha with a sibling lane's head")

    helper = inspect.getsource(detect._pushed_lane)
    assert "refs/heads/" in helper
    assert "FROM git_nodes" in helper, (
        "a lane is recognised from git_repo_trunks, so a lane never yet "
        "observed — the one most needing a first observation — would "
        "not count")


def test_the_legacy_coarse_field_is_left_alone_for_a_sibling_lane():
    import inspect

    from app.services.git_graph import detect
    src = inspect.getsource(detect._advance_trunk)
    assert "if not legacy:" in src
    assert "UPDATE git_repos SET trunk_sha" in src


def test_the_integration_plan_gets_a_card():
    """IT IS WAITING FOR A PERSON, SO THEY HAVE TO SEE IT.

    A suppression here was correct while the platform ANSWERED this
    plan -- a card for a decision already taken is noise. The design
    then changed so a person approves it, and the suppression stayed:
    the plan sat OPEN with `card_posted_at` stamped and no card ever
    delivered. Seen live on bc9679f4 q238 -- the run parked waiting for
    an approval nobody could see.

    The same shape as every other guard that bit today: right for the
    case it was written for, left in place after that case stopped
    existing.
    """
    import inspect

    from app.services import kernel_park_cards
    src = inspect.getsource(kernel_park_cards._post)
    assert "platform_canned" not in src, (
        "the card poster still drops the platform's plan — which now "
        "needs a person's approval, so dropping it parks the run on a "
        "question nobody can answer")
