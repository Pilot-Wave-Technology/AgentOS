"""Ec3b/Ec3c — the twelve-leg deadlock, ended.

WHAT IT WAS. Chain stage 3 refused an `irreversible` call with "use
ask_human with what you intend to do". It read the approval from
`InterceptorContext.approved_effects` — **a field nothing has ever
written**. So a person could be asked, could say yes, and the next leg
built the same context with the same empty tuple and refused the same call
identically. Twelve legs, then FAILED.

An approval that can be demanded and never granted is not a gate, it is a
wall. Ec3a made that wall much taller — `terraform apply`, `aws s3 rm` and
`gcloud … delete` became irreversible — so without this they would be
impossible rather than approvable. The two belong in one change, and the
test that matters is the round trip:

    refused -> parked -> a person approves -> the same call proceeds

Everything else here is what must NOT also become true.

Real Postgres, one transaction, always rolled back.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from kernel.harness.chain import InterceptorContext, ToolCall, run_chain
from kernel.harness.permission import KIND, signature

asyncpg = pytest.importorskip("asyncpg")

LOCAL_DEV_DSN = "postgresql://agentos:agentos@localhost:5433/agentos"
PREFIX = "zz-perm-test"

DESTRUCTIVE = {"command": "terraform apply -auto-approve"}
ORDINARY = {"command": "pytest -q"}


def _ctx(approved=(), **kw):
    base = dict(writable_scope=["/workspace"], allowed_tools=None,
                budget_remaining=1000, execution_id=1, depth=0,
                approved_calls=frozenset(approved))
    base.update(kw)
    return InterceptorContext(**base)


def _bash(args):
    return ToolCall(tool="bash", args=args, target=None)


# ── the chain half, which needs no database ──────────────────────────────

def test_the_call_is_refused_before_anybody_approves():
    got = run_chain(_bash(DESTRUCTIVE), _ctx())
    assert not got.proceed
    assert got.denial.code == "E_APPROVAL_REQUIRED"
    assert got.denial.stage == 3


def test_the_same_call_proceeds_once_its_signature_is_approved():
    """THE FIX. Nothing could put anything in the old field, so this
    assertion was unreachable."""
    approved = {signature("bash", DESTRUCTIVE)}
    assert run_chain(_bash(DESTRUCTIVE), _ctx(approved)).proceed


def test_approving_one_call_does_not_approve_a_different_one():
    """A DELIBERATE NARROWING of what the plan proposed. `approved_effects`
    was class-wide, so approving one `terraform apply` would also approve
    every `aws s3 rm` for the rest of the run — the card shows one thing
    and grants another."""
    approved = {signature("bash", DESTRUCTIVE)}
    other = {"command": "aws s3 rm s3://bucket --recursive"}
    assert not run_chain(_bash(other), _ctx(approved)).proceed


def test_an_ordinary_call_never_needed_approval_and_still_does_not():
    assert run_chain(_bash(ORDINARY), _ctx()).proceed


def test_the_field_that_nothing_wrote_is_gone():
    """Leaving it would leave the trap. A field read by a guard and
    written by nothing is the shape of this whole bug."""
    assert not hasattr(_ctx(), "approved_effects")
    assert isinstance(_ctx().approved_calls, frozenset)


def test_the_corrective_no_longer_sends_the_model_down_the_dead_end():
    """It used to say "use ask_human", which parked on a question whose
    answer nothing read. It must now describe what actually happens."""
    said = run_chain(_bash(DESTRUCTIVE), _ctx()).denial.corrective
    assert "ask_human" not in said
    assert "next leg" in said and "unchanged" in said


def test_a_child_still_cannot_be_approved_at_all():
    """SA1: a child has no leg to park on, and there is nobody to escalate
    to — the parent is mid-step waiting on this very call. Widening what
    counts as irreversible must not have opened that."""
    got = run_chain(_bash(DESTRUCTIVE), _ctx(depth=1))
    assert not got.proceed
    assert got.denial.code == "E_EFFECT_UNAPPROVED"
    # And an approval granted to the PARENT does not travel down.
    approved = {signature("bash", DESTRUCTIVE)}
    assert not run_chain(_bash(DESTRUCTIVE), _ctx(approved, depth=1)).proceed


def test_a_policy_cannot_approve_an_irreversible_call():
    """The ceiling. Ec3d lets a profile carry a policy; the policy reaches
    stage 3, and stage 3 must still refuse."""
    for policy in ({"approve_reversible": True},
                   {"approve_contained": True},
                   {"approve_irreversible": True}):
        assert not run_chain(_bash(DESTRUCTIVE),
                             _ctx(effect_policy=policy)).proceed


def test_a_policy_can_tighten_an_ordinary_call():
    """Ec3d's test: a profile can ask for a person on every shell command.
    That is the right posture for an agent type on a production account
    and the wrong one for story-coding, which is why it is per profile."""
    assert not run_chain(_bash(ORDINARY),
                         _ctx(effect_policy={"approve_reversible": True})
                         ).proceed


# ── the database half: the yes is remembered ─────────────────────────────

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
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'kernel' "
            "AND table_name IN ('executions','questions')")
        if ok == 2:
            return conn
        await conn.close()
    pytest.skip("no local database with the kernel schema — start the stack")


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


async def _execution(conn) -> int:
    return await conn.fetchval(
        "INSERT INTO kernel.executions (kind, domain, goal_id, state) "
        "VALUES ('errand','ops',$1,'RUNNING') RETURNING id",
        f"{PREFIX}-{os.urandom(4).hex()}")


async def _answer(conn, request_id: int, text: str):
    await conn.execute(
        "UPDATE questions SET status = 'ANSWERED', answer_text = $2, "
        "answered_at = now() WHERE id = $1", request_id, text)


def test_a_yes_is_remembered_across_the_leg_boundary():
    """THE ROUND TRIP, and the reason this phase exists."""
    async def body(conn):
        from kernel.harness.permission import approved_calls, ask

        eid = await _execution(conn)
        assert await approved_calls(conn, eid) == frozenset()
        # Before: refused.
        assert not run_chain(_bash(DESTRUCTIVE), _ctx()).proceed

        got = await ask(conn, eid, 0, "bash", DESTRUCTIVE, "irreversible",
                        "", "changes real infrastructure")
        assert isinstance(got, int), got
        # Parked and unanswered is still not permission.
        assert await approved_calls(conn, eid) == frozenset()

        await _answer(conn, got, "approve")
        allowed = await approved_calls(conn, eid)
        assert allowed == {signature("bash", DESTRUCTIVE)}
        # After: the SAME call proceeds.
        assert run_chain(_bash(DESTRUCTIVE), _ctx(allowed)).proceed
    _run(body)


def test_a_decline_is_a_decision_and_is_not_permission():
    async def body(conn):
        from kernel.harness.permission import approved_calls, ask

        eid = await _execution(conn)
        got = await ask(conn, eid, 0, "bash", DESTRUCTIVE, "irreversible")
        await _answer(conn, got, "decline — not on production")
        assert await approved_calls(conn, eid) == frozenset()
        assert not run_chain(_bash(DESTRUCTIVE), _ctx()).proceed
    _run(body)


def test_the_card_shows_the_concrete_call():
    """Ec3b's test. "an irreversible tool call needs approval" is
    unanswerable — a person cannot tell a typo from a production
    deletion."""
    async def body(conn):
        from kernel.harness.permission import ask

        eid = await _execution(conn)
        got = await ask(conn, eid, 0, "bash", DESTRUCTIVE, "irreversible",
                        target="prod", why="changes real infrastructure")
        row = await conn.fetchrow(
            "SELECT kind, question_text, payload::text AS payload "
            "FROM questions WHERE id = $1", got)
        assert row["kind"] == KIND, (
            "stored as its own kind, or approved_calls cannot find it and "
            "a plan approval reads as permission for a call")
        assert "terraform apply -auto-approve" in row["question_text"]
        assert "changes real infrastructure" in row["question_text"]
        payload = json.loads(row["payload"])
        assert payload["signature"] == signature("bash", DESTRUCTIVE)
        assert payload["effect_class"] == "irreversible"
        assert payload["tool"] == "bash"
        assert payload["target"] == "prod"
    _run(body)


def test_there_is_no_attempts_floor():
    """`ask_human` demands three distinct prior attempts. This is the
    opposite case — the executor is not stuck, it is holding a destructive
    call — and demanding evidence would teach it to try the thing three
    times first."""
    async def body(conn):
        from kernel.harness.permission import ask

        eid = await _execution(conn)
        got = await ask(conn, eid, 0, "bash", DESTRUCTIVE, "irreversible")
        assert isinstance(got, int), (
            f"a permission ask was refused for lack of attempts: {got}")
    _run(body)


def test_an_ask_with_no_signature_is_refused():
    """The signature is what the verdict attaches to. Without one the ask
    is approved and then applies to nothing — a person's yes silently
    granting nothing at all."""
    async def body(conn):
        from kernel.mailbox.api import ask_question

        eid = await _execution(conn)
        got = await ask_question(
            conn, eid, KIND, "may I?", {"tool": "bash"}, generation=0)
        assert not isinstance(got, int)
        assert "SIGNATURE" in getattr(got, "code", "").upper()
    _run(body)


def test_two_different_calls_get_two_cards():
    """`open_request` dedupes, and without a per-call hash the second
    destructive call would be refused as a repeat — parking the run on
    nothing."""
    async def body(conn):
        from kernel.harness.permission import ask

        eid = await _execution(conn)
        first = await ask(conn, eid, 0, "bash", DESTRUCTIVE, "irreversible")
        assert isinstance(first, int)
        await _answer(conn, first, "approve")

        other = {"command": "aws s3 rb s3://bucket"}
        second = await ask(conn, eid, 1, "bash", other, "irreversible")
        assert isinstance(second, int), second
        assert second != first
    _run(body)


def test_an_agent_cannot_approve_its_own_permission():
    """The sharpest case of §1.3. The question is "may I run terraform
    destroy", and an agent able to answer its own is no gate at all."""
    async def body(conn):
        from kernel.harness.park import ApprovalRefused, resolve_request
        from kernel.harness.permission import ask

        eid = await _execution(conn)
        got = await ask(conn, eid, 0, "bash", DESTRUCTIVE, "irreversible")
        with pytest.raises(ApprovalRefused):
            await resolve_request(conn, got, "approve", "agent:shca",
                                  human_uid="")
        # A decline relays from anybody: saying no is safe.
        await resolve_request(conn, got, "decline", "agent:shca",
                              declined=True, human_uid="")
    _run(body)
