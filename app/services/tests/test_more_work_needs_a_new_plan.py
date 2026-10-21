"""More work needs a new plan, and that is enforced rather than asked for.

THE HOLE. Nothing ever set `plan_versions.status = 'executed'`, and both the
diff gate and the done gate accepted `IN ('approved', 'executed')`. So ONE
approved plan satisfied every later diff for the life of the story: a person
could approve a plan once and every subsequent cycle would land against it,
with nobody having agreed to the new work. There was no cycle boundary at all.

Now a cycle CLOSES when its diff is approved -- `diff_review` spends the plan
-- and `submit_local_diff` requires a plan that is `approved` and not yet
spent. `request_more_work` is the door to the next one: it puts the story back
in `planning` and tells the assistant, so the refusal is never the first the
person hears of the rule.
"""
from __future__ import annotations

import contextlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest

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
        await conn.close()


async def _story(conn, sid: str, phase: str = "awaiting_next"):
    await conn.execute("INSERT INTO goals (id,name,created_by) VALUES ($1,$2,$3)",
                       f"{sid}-goal", "fixture", f"{PREFIX}-user")
    await conn.execute(
        "INSERT INTO stories (id,goal_id,title,created_by,execution_mode,current_phase)"
        " VALUES ($1,$2,$3,$4,'local',$5)",
        sid, f"{sid}-goal", "cycle fixture", f"{PREFIX}-user", phase)
    return f"{sid}-goal"


async def _plan(conn, sid: str, version: int, status: str):
    await conn.execute(
        """INSERT INTO plan_versions (id, story_id, version, plan_doc_path,
                                      plan_json, proposed_by, status)
           VALUES ($1,$2,$3,'agentos_plans/x/plan-01.md','{}'::jsonb,'local',$4)""",
        str(uuid.uuid4()), sid, version, status)


@contextlib.contextmanager
def _pool_is(module, conn):
    from app.services.tests.local_http import PoolOnConn
    orig = module.get_pool
    module.get_pool = lambda: PoolOnConn(conn)
    try:
        yield
    finally:
        module.get_pool = orig


@pytest.mark.asyncio
async def test_a_spent_plan_does_not_authorise_another_cycle():
    from app.services import local_diff

    async with _conn() as conn:
        sid = f"{PREFIX}-cycle-spent"
        await _story(conn, sid)
        await _plan(conn, sid, 1, "executed")
        with _pool_is(local_diff, conn):
            r = await local_diff.submit_local_diff(sid, "diff", "1 file", False, "sha")
    assert r["ok"] is False
    assert r["reason"] == "no_approved_plan"
    assert "already landed" in (r.get("error") or ""), (
        "the refusal must say WHY -- a bare 'no approved plan' on a story that "
        "visibly has one reads as a bug"
    )


@pytest.mark.asyncio
async def test_a_fresh_approved_plan_does():
    from app.services import local_diff

    async with _conn() as conn:
        sid = f"{PREFIX}-cycle-fresh"
        await _story(conn, sid)
        await _plan(conn, sid, 1, "executed")
        await _plan(conn, sid, 2, "approved")
        # Everything PAST the plan gate is stubbed: this is about the gate, and
        # the push/approval path has its own tests.
        with _pool_is(local_diff, conn),              patch.object(local_diff, "_repo_ref", AsyncMock(return_value=None)),              patch.object(local_diff, "post_diff_review",
                          AsyncMock(return_value={"ok": True})):
            r = await local_diff.submit_local_diff(sid, "diff", "1 file", False, "sha")
    assert r.get("reason") != "no_approved_plan", (
        "a newly approved plan does not authorise the next cycle, so the gate "
        "can never be satisfied again"
    )


@pytest.mark.asyncio
async def test_asking_for_more_work_reopens_planning():
    from app.services import local_plan_gate

    async with _conn() as conn:
        sid = f"{PREFIX}-cycle-more"
        goal = await _story(conn, sid)
        await _plan(conn, sid, 1, "executed")
        # `report_message` holds its own `get_pool`, in local_report.
        from app.services import local_more_work, local_report
        with _pool_is(local_more_work, conn), _pool_is(local_report, conn):
            r = await local_plan_gate.request_more_work(goal, sid, "add retries", "u1")
        assert r["ok"] is True
        phase = await conn.fetchval(
            "SELECT current_phase FROM stories WHERE id = $1", sid)
        assert phase == "planning", (
            "the board still says the last cycle is finished while work happens"
        )


@pytest.mark.asyncio
async def test_more_work_on_a_parked_plan_expands_it_rather_than_refusing():
    """SUPERSEDED BEHAVIOUR, replaced deliberately. This used to refuse
    with `plan_pending` -- "review that one rather than asking for
    another" -- which made the only way to adjust a plan a person was
    reading a REJECTION, throwing the plan away to move a sentence.

    Abhishek, 2026-10-14: a parked plan should be changeable "just by
    talking". So the request expands it: same cycle, same plan file, and
    the agent revises what is already registered."""
    from app.services import local_more_work, local_plan_gate, local_report

    async with _conn() as conn:
        sid = f"{PREFIX}-cycle-pending"
        goal = await _story(conn, sid, phase="awaiting_plan_approval")
        with _pool_is(local_more_work, conn), _pool_is(local_report, conn):
            r = await local_plan_gate.request_more_work(goal, sid, "more", "u1")
    assert r["ok"] is True
    assert r["mode"] == "expand"


@pytest.mark.asyncio
async def test_more_work_is_refused_on_a_closed_story():
    from app.services import local_more_work, local_plan_gate

    async with _conn() as conn:
        sid = f"{PREFIX}-cycle-closed"
        goal = await _story(conn, sid)
        await conn.execute("UPDATE stories SET state = 'done' WHERE id = $1", sid)
        with _pool_is(local_more_work, conn):
            r = await local_plan_gate.request_more_work(goal, sid, "more", "u1")
    assert r["ok"] is False
    assert r["reason"] == "story_closed"


def test_a_landed_diff_does_not_spend_the_plan():
    """Superseded my own first answer, which spent the plan on diff approval.

    That conflated two events. A diff landing means SOME AGREED WORK is on the
    branch -- which happens mid-plan whenever a person lands a safe subset --
    while spending the plan should mean the agreed intent is finished with.
    Spending it there blocked the REST of a plan the person had already
    approved: the next submit refused with "this is new work" for work they
    agreed to three steps earlier.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[3]
           / "app" / "services" / "diff_review.py").read_text(encoding="utf-8")
    i = src.index("current_phase='awaiting_next'")
    after = src[i:i + 1800]
    code = "\n".join(l for l in after.split("\n") if not l.strip().startswith("#"))
    assert "status = 'executed'" not in code, (
        "a landed diff spends the plan again, which blocks the rest of an "
        "already-approved plan"
    )


def _function_body(src: str, fn: str) -> str:
    """A top-level function's source, from its `def` to the next one."""
    import re
    start = src.index(f"def {fn}(")
    rest = src[start:]
    nxt = re.search(r"\n(?:async def |def |@)", rest)
    return rest[:nxt.start()] if nxt else rest


def test_the_plan_is_spent_where_the_cycle_ENDS():
    """THE DOORS MOVED, 2026-10-14, and that is the flow revision.

    A cycle used to end when more work was ASKED FOR, which is exactly
    what made every request a new plan and left no way to add one line to
    a plan already being worked. A cycle now ends when its intent is
    DELIVERED -- the submit, which pushes and opens the pull request --
    or when the story is finished with. `request_more_work` spends
    nothing; it reads whether the cycle is already spent and decides
    `expand` or `new` from that."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3] / "app" / "services"
    for name, fn in (("local_diff.py", "_park_for_review"),
                     ("local_completion.py", "finish_local_story")):
        src = (root / name).read_text(encoding="utf-8")
        # TO THE END OF THE FUNCTION, not a character count. This used to
        # take the first 2500 characters, which made the pin a budget on
        # how much could be written above the spend rather than a claim
        # about the function: adding the errand branch to
        # `request_more_work` pushed the line past the window and failed a
        # test about something the change did not touch.
        body = _function_body(src, fn)
        # THE SPEND IS A NAMED CALL NOW (`local_cycles.spend_cycle`), so the
        # pin asks for the call rather than the SQL. One UPDATE in one
        # module, and the event argument says which door closed the cycle.
        assert "spend_cycle(" in body, (
            f"{fn} does not spend the plan, so the next cycle can be submitted "
            "against an intent nobody re-agreed to"
        )

    # AND THE DOOR THAT WAS CLOSED STAYS CLOSED. Spending on request is
    # what the revision removed; putting it back would silently restore
    # "every request is a new plan".
    mw = (root / "local_more_work.py").read_text(encoding="utf-8")
    assert "spend_cycle(" not in mw, (
        "asking for more work spends the cycle again, so no request can "
        "ever expand the plan it is asking about")


@pytest.mark.asyncio
async def test_landing_part_of_a_plan_can_be_continued():
    """The mid-plan case, which the first two fixes still left broken.

    Approving a diff sets `awaiting_next`, whose hint tells the session the
    story is not done and to WAIT. Right when the plan is finished; wrong when
    a person deliberately landed a safe subset. The plan is unspent and the
    diff gate would take the next submission -- but the agent has been told to
    stop, and the only button out spent the plan.
    """
    from app.services import local_plan_gate, local_report

    async with _conn() as conn:
        sid = f"{PREFIX}-cycle-continue"
        goal = await _story(conn, sid)                 # phase: awaiting_next
        await _plan(conn, sid, 1, "approved")          # unspent: work remains
        with _pool_is(local_plan_gate, conn), _pool_is(local_report, conn):
            r = await local_plan_gate.continue_this_plan(goal, sid, "u1")
        assert r["ok"] is True
        phase, status = await conn.fetchrow(
            "SELECT s.current_phase, p.status FROM stories s "
            "  JOIN plan_versions p ON p.story_id = s.id WHERE s.id = $1", sid)
        assert phase == "executing", (
            "the session is still told to wait, with an approved plan it has "
            "not finished"
        )
        assert status == "approved", "continuing spent the plan it is continuing"


@pytest.mark.asyncio
async def test_continuing_is_refused_when_there_is_no_plan_left():
    from app.services import local_plan_gate

    async with _conn() as conn:
        sid = f"{PREFIX}-cycle-nocont"
        goal = await _story(conn, sid)
        await _plan(conn, sid, 1, "executed")
        with _pool_is(local_plan_gate, conn):
            r = await local_plan_gate.continue_this_plan(goal, sid, "u1")
    assert r["ok"] is False
    assert r["reason"] == "no_approved_plan", (
        "moving to `executing` with no approved plan just defers the refusal "
        "to the next submit"
    )
