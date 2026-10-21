"""One behind branch gets one integration, not two.

WHAT WENT WRONG ON bc9679f4. Trunk moved under a story that was parked
on its merge card. `trunk_integration.sweep` offered that story an
integration plan -- correct, and the whole point of the feature. The
git engine's grind loop ALSO saw the branch was behind and cut its own
`ds_*` diff story, twice. Those rows are filtered off the board
(`is_engine_git_story`), so the only trace a person saw was two extra
assistant threads appearing beside their story, with no explanation.

The engine's diff story was right for the world it was written for: the
feature story was already `done`, its pod collected, and a fresh story
was the only thing left that could run the integration. That is no
longer true while a story holds its pod for the merge -- but it is
still true for a node whose story is `done`, cancelled, or an errand's.

So the gate is per node, not a blanket switch: does this node still
have a live story that will do it itself?
"""
from __future__ import annotations

import inspect
import os

import pytest

from app.services.tests.test_trunk_catches_up_without_asking_twice import (
    _PoolOnConn, _behind_story, _conn,
)


async def _node_of(conn, sid):
    return await conn.fetchval(
        "SELECT git_node_id FROM git_node_stories WHERE story_id = $1", sid)


@pytest.mark.asyncio
async def test_a_story_that_is_still_waiting_integrates_itself(monkeypatch):
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services.trunk_integration import node_self_integrates
    async with _conn() as conn:
        sid, _ = await _behind_story(conn)
        assert await node_self_integrates(_PoolOnConn(conn),
                                          await _node_of(conn, sid)), (
            "the story is parked on its merge card with a pod held open, "
            "which is exactly who should catch trunk up")


@pytest.mark.asyncio
async def test_a_finished_story_still_gets_the_engines_diff_story(monkeypatch):
    """The fallback that must not be lost. A node whose story reached
    `done` has no pod left; the engine's diff story is the only thing
    that can integrate it, flag or no flag."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services.trunk_integration import node_self_integrates
    async with _conn() as conn:
        sid, _ = await _behind_story(conn)
        await conn.execute("UPDATE stories SET state = 'done' WHERE id = $1", sid)
        assert not await node_self_integrates(_PoolOnConn(conn),
                                              await _node_of(conn, sid))


@pytest.mark.asyncio
async def test_an_engine_story_is_never_mistaken_for_the_owner(monkeypatch):
    """A node carrying only engine-cut stories has nobody holding a pod.
    Reading one as the owner would stop the engine cutting the next
    diff story and leave the branch behind forever."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services.trunk_integration import node_self_integrates
    async with _conn() as conn:
        sid, _ = await _behind_story(conn)
        await conn.execute(
            "UPDATE stories SET created_by = 'engine', state = 'in_progress' "
            " WHERE id = $1", sid)
        assert not await node_self_integrates(_PoolOnConn(conn),
                                              await _node_of(conn, sid))


@pytest.mark.asyncio
async def test_with_the_flag_off_nothing_changes(monkeypatch):
    monkeypatch.delenv("STORY_HOLDS_FOR_MERGE", raising=False)
    from app.services.trunk_integration import node_self_integrates
    async with _conn() as conn:
        sid, _ = await _behind_story(conn)
        assert not await node_self_integrates(_PoolOnConn(conn),
                                              await _node_of(conn, sid)), (
            "off, every path must write exactly what it wrote before")


def test_grind_asks_before_it_cuts():
    """Pinned against the source because the ordering is the fix: the
    gate has to come BEFORE `_ensure_diff_story`, and a later edit that
    moves the spawn above it would restore the double integration with
    every test above still green."""
    from app.services.git_graph import grind
    src = inspect.getsource(grind._grind_one)
    gate = src.index("node_self_integrates")
    spawn = src.index("_ensure_diff_story")
    assert gate < spawn


def test_the_two_halves_share_one_definition():
    """`sweep` decides a story integrates itself; `grind` decides the
    engine should not. Two separate predicates would eventually
    disagree, and the disagreement is either two integrations or none
    -- so they live in one module."""
    from app.services import trunk_integration
    assert hasattr(trunk_integration, "node_self_integrates")
    assert "awaiting_merge" in trunk_integration._SELF_INTEGRATES_SQL
    assert "awaiting_merge" in trunk_integration.BEHIND_SQL


@pytest.mark.asyncio
async def test_a_second_repo_on_the_same_story_is_covered():
    """Multi-repo: the story holds two nodes and is behind on both. The
    gate answers per node, so BOTH must read as self-integrating -- one
    of them falling through would cut a diff story for that lane alone,
    which is the stray thread again with half the work in it."""
    os.environ["STORY_HOLDS_FOR_MERGE"] = "1"
    try:
        from app.services.trunk_integration import node_self_integrates
        async with _conn() as conn:
            sid, _ = await _behind_story(conn, lanes=("api", "web"))
            nodes = [r["git_node_id"] for r in await conn.fetch(
                "SELECT git_node_id FROM git_node_stories WHERE story_id = $1",
                sid)]
            assert len(nodes) == 2
            pool = _PoolOnConn(conn)
            for n in nodes:
                assert await node_self_integrates(pool, n)
    finally:
        os.environ.pop("STORY_HOLDS_FOR_MERGE", None)


# ── who may say a branch is caught up ─────────────────────────────────

def test_the_pod_does_not_write_the_git_graph():
    """`sync_trunk` recorded the sync itself and was refused by
    Postgres on EVERY call -- `agentos_leg_dev` has no write on
    `git_nodes`, so the column it was meant to maintain stayed NULL
    while a try/except logged the failure and moved on.

    Pinned as an absence because the tempting fix is a GRANT, and a
    grant is the dangerous one: see the next test."""
    import inspect

    from kernel.harness import executor_tools
    src = inspect.getsource(executor_tools)
    assert "_record_synced" not in src
    assert "UPDATE git_nodes" not in src


def test_the_engine_writes_it_from_githubs_own_answer():
    """`behind_by == 0` from a live compare, not a pod's say-so. The
    column decides whether a story is OFFERED integration, so an
    executor that could write it could declare itself caught up
    without merging and land work that never took trunk in."""
    import inspect

    from app.services.git_graph import grind
    src = inspect.getsource(grind._grind_one)
    assert "record_caught_up" in src
    at = src.index("record_caught_up")
    assert 'cmp["behind_by"] == 0' in src[:at], (
        "recorded without first checking the branch is actually level")


@pytest.mark.asyncio
async def test_recording_it_stops_the_sweep_re_offering(monkeypatch):
    """The end-to-end point of the column: a story that has taken
    trunk in is no longer behind, so no second plan is offered."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services.trunk_integration import BEHIND_SQL, record_caught_up
    async with _conn() as conn:
        sid, _ = await _behind_story(conn, lanes=("main",))
        pool = _PoolOnConn(conn)
        mine = [r for r in await conn.fetch(BEHIND_SQL) if r["story_id"] == sid]
        assert mine, "fixture is not behind to begin with"

        node = await conn.fetchval(
            "SELECT git_node_id FROM git_node_stories WHERE story_id = $1", sid)
        await record_caught_up(pool, node, "main", "new-main")

        mine = [r for r in await conn.fetch(BEHIND_SQL) if r["story_id"] == sid]
        assert not mine, "still reads as behind after taking the whole lane"


@pytest.mark.asyncio
async def test_it_will_not_mark_a_lane_it_was_not_given(monkeypatch):
    """Multi-repo: the recorder is told WHICH lane is level. A node on
    another lane must not be marked by it, or one lane going quiet
    would silence integration for the other."""
    monkeypatch.setenv("STORY_HOLDS_FOR_MERGE", "1")
    from app.services.trunk_integration import record_caught_up
    async with _conn() as conn:
        sid, _ = await _behind_story(conn, lanes=("api", "web"))
        pool = _PoolOnConn(conn)
        rows = await conn.fetch(
            "SELECT gn.id, gn.trunk_branch FROM git_node_stories gns "
            "JOIN git_nodes gn ON gn.id = gns.git_node_id "
            "WHERE gns.story_id = $1 ORDER BY gn.trunk_branch", sid)
        api, web = rows[0], rows[1]
        await record_caught_up(pool, api["id"], "api", "new-api")

        assert await conn.fetchval(
            "SELECT last_synced_trunk_sha FROM git_nodes WHERE id = $1",
            api["id"]) == "new-api"
        assert await conn.fetchval(
            "SELECT last_synced_trunk_sha FROM git_nodes WHERE id = $1",
            web["id"]) == "old-sha", "the other lane was marked too"


# ── and then the run actually runs again ─────────────────────────────

def test_every_arm_that_submits_a_proposal_also_un_parks():
    """THE PROPOSAL IS ONLY READ BY A LEG, and a parked run takes no
    leg. `take_proposals` drains the mailbox at the top of the step
    loop, so an arm that submits a `replan` or a `cancel` and leaves
    the run PARKED has thrown the person's answer away: the question
    reads ANSWERED, the card is gone, and nothing happens, for ever.

    Seen on run 100 -- "ask for more work" on the merge card. The
    un-park was written as the last thing in the function and INDENTED
    INTO the `else` arm, so the three newest arms (`landing`,
    `closeout`, both `pr_closed`) never reached it. Every one of their
    comments says "the un-park at the bottom of this function"; the
    belief was right and the indentation was not.

    Pinned structurally, because an indentation bug is invisible to a
    test that only calls one arm.
    """
    import inspect
    import textwrap

    from kernel.harness import park_resolve
    src = textwrap.dedent(inspect.getsource(park_resolve.resolve_request))
    lines = src.split("\n")

    unpark = [i for i, ln in enumerate(lines)
              if "SET state = 'RUNNING', generation = generation + 1" in ln]
    assert len(unpark) == 1, "expected exactly one un-park"
    at = unpark[0]

    # Walk back to the `await conn.execute(` that owns it and measure
    # the indent of the statement, not of the string literal.
    stmt = next(i for i in range(at, -1, -1)
                if lines[i].lstrip().startswith("await conn.execute("))
    indent = len(lines[stmt]) - len(lines[stmt].lstrip())

    # Every arm of the kind chain sits at one depth; the un-park must
    # be at that same depth or shallower, never nested inside one arm.
    arms = [ln for ln in lines
            if ln.lstrip().startswith(("if q_kind ==", "elif q_kind =="))]
    assert arms, "the kind chain moved; this test needs rewriting"
    arm_indent = min(len(a) - len(a.lstrip()) for a in arms)
    assert indent <= arm_indent, (
        f"the un-park is nested inside one arm (indent {indent} vs chain "
        f"{arm_indent}) -- the arms that only submit a proposal will "
        f"never wake")


# ── the gates that never fired in production ─────────────────────────

def test_the_pod_arms_both_plan_gates():
    """NEITHER GATE HAS EVER FIRED IN A POD, and both failed open.

    `leg_runner` is the pod's leg entry point. It builds the executor
    before it has a connection, so it passed neither `plan_approved`
    nor `plan_scope` -- and the defaults are permissive by design:
    `_plan_approved is False` is never true when it is None, and the
    scope check is `is not None`. The worker path
    (`worker/executor_pass.py`) passes real values, so every test and
    every console run enforced them, and the thing that actually runs
    stories did not.

    Observed on run 100: the platform's integration plan, scoped to
    sync_trunk / run_check / emit, and the leg called `bash`,
    `story_commit` and `propose_completion` with no refusal logged.
    """
    import inspect

    from kernel.harness import leg_runner
    src = inspect.getsource(leg_runner)
    assert "plan_is_approved" in src and "plan_scope" in src, (
        "the pod builds an executor with both plan gates disarmed")
    assert "_plan_approved = await plan_is_approved" in src
    assert "_plan_scope = await plan_scope" in src


def test_the_gates_are_armed_before_the_leg_runs():
    """After `run_leg` they would arm nothing -- the leg is over."""
    import inspect

    from kernel.harness import leg_runner
    src = inspect.getsource(leg_runner)
    assert (src.index("_plan_scope = await plan_scope")
            < src.index("result = await run_leg("))


def test_an_unknown_approval_is_not_a_withdrawn_one():
    """`None` means nobody could say, and it must stay distinct from
    `False`. Collapsing them bricks every run the loader cannot answer
    for, which is the opposite failure and a worse one."""
    import inspect

    from kernel.harness import executor
    src = inspect.getsource(executor.BridgeToolExecutor._call_inner)
    assert "self._plan_approved is False" in src, (
        "the plan gate now treats an unknown approval as withdrawn")
    assert "self._plan_scope is not None" in src


def test_the_pod_gets_every_per_leg_fact_the_worker_does():
    """THE WHOLE CLASS, not the two instances that were noticed.

    `worker/executor_pass.py` and `harness/leg_runner.py` both build a
    `BridgeToolExecutor`. Tests and the console use the first; a pod
    uses the second. Five arguments were passed by one and not the
    other, and each one silently disabled something:

        plan_approved   work before anybody approved a plan
        plan_scope      the integration scope, never once enforced
        allowed_hosts   egress grants, unenforced -- FAILS OPEN
        approved_calls  a granted permission never seen -- fails closed
        mounts_env      a check cannot find `pytest` (execution 219)

    Pinned as a set difference so the NEXT argument someone adds to
    the worker path cannot quietly skip the pod.
    """
    import inspect
    import re

    from kernel.harness import leg_runner
    from kernel.worker import executor_pass

    def _kwargs(src, call):
        at = src.index(call)
        depth, out, i = 0, [], at + len(call) - 1
        for i in range(at + len(call) - 1, len(src)):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
        return set(re.findall(r"(\w+)=", src[at:i]))

    worker = _kwargs(inspect.getsource(executor_pass), "BridgeToolExecutor(")
    pod_src = inspect.getsource(leg_runner)
    pod = _kwargs(pod_src, "BridgeToolExecutor(")
    # The pod sets four of them AFTER construction, because the
    # executor must exist before the pool does.
    pod |= {m[1:] for m in re.findall(r"executor\.(_\w+) =", pod_src)}

    # THE THREE THAT DIFFER LEGITIMATELY, each for a stated reason.
    # `envelope` and `execution_id` the pod passes under its own names
    # from `args`. `bridge_host` it deliberately leaves at the default:
    # the worker dispatches to a sandbox in ANOTHER pod and must
    # resolve its address, while this code is already inside that pod
    # and the shim is local.
    expected = {"envelope", "execution_id", "bridge_host"}
    assert "bridge_host" not in pod, (
        "the pod now sets bridge_host — if that is deliberate, this "
        "exemption needs rewriting rather than widening")

    missing = worker - pod - expected
    assert not missing, (
        f"the pod builds an executor without {sorted(missing)} — "
        f"whatever those gate is off in production and on in every test")
