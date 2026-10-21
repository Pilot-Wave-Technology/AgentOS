"""The GO drives autonomous errands. Everything else is the assistant's.

WHY A PREDICATE AND NOT A ROUTE. Adding `steer`, `ask_executor`,
`story_progress`, `story_status` and `cancel_story` to `GO_ROUTES` is
cheap precisely because nothing underneath them checks authority --
every one takes `actor_id` as an opaque string and performs no check at
all. That makes route membership a coarse yes-or-no PER TOOL, and it
cannot express "yes, but only for this run".

So the boundary is `go_may_drive`, called once in `_dispatch_go` for
every tool in `GO_PER_STORY`. The failure mode this file exists to catch
is a sixth tool added to that surface without being added to that list.

THE TWO CONDITIONS, and why neither alone is enough:

  A STORY is refused even though the GO planned it, because a story has
  an assistant whose entire job is that story -- two agents driving one
  run is how somebody gets told something was settled that was not.

  A HAND-STARTED ERRAND is refused even though it IS an errand, because
  the person who started it is sitting in the assistant thread it was
  started from. Nobody has left.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from kernel.assistant import api
from kernel.assistant.context import Refused
from kernel.go import drivable


# ── the two conditions ────────────────────────────────────────────────

class _Conn:
    """Answers the two queries `go_may_drive` makes, in order."""

    def __init__(self, kind, granted, raises=False):
        self._kind, self._granted, self._raises = kind, granted, raises

    async def fetchval(self, sql, *_a):
        if self._raises:
            raise RuntimeError("the database went away")
        if "work_kind" in sql:
            return self._kind
        return self._granted


def _drive(kind, granted, story="e1", raises=False):
    return asyncio.run(
        drivable.go_may_drive(_Conn(kind, granted, raises), "KER12", story))


def test_an_autonomous_errand_is_allowed():
    assert _drive("errand", 1) is None


def test_a_story_is_refused_and_told_where_it_belongs():
    r = _drive("story", 1)
    assert r is not None and r.code == "E_NOT_AN_ERRAND"
    assert "assistant" in r.message


def test_a_row_written_before_work_kind_existed_is_a_story():
    """NULL means story. `!= 'story'` would have admitted every row that
    predates the column, which is the reason the check is positive."""
    assert _drive(None, 1) is not None


def test_a_hand_started_errand_is_refused():
    """It is an errand, and that is not enough: nobody has left. The
    person who started it is in the assistant thread it came from."""
    r = _drive("errand", None)
    assert r is not None and r.code == "E_NOT_AUTONOMOUS"
    assert "by hand" in r.message


def test_a_story_that_is_not_this_goals_is_refused():
    assert _drive(None, 1).code == "E_NO_STORY"


def test_no_story_named_is_refused():
    assert _drive("errand", 1, story="").code == "E_NO_STORY"


def test_a_database_error_refuses_rather_than_allows():
    """The whole point is a boundary, and a boundary that opens when a
    query hiccups is not one."""
    r = _drive("errand", 1, raises=True)
    assert r is not None


# ── the grant predicate, which is deliberately not the pin ────────────

def test_the_grant_check_omits_the_plan_hash_pin():
    """`approved_plan_hash` is NULL until the first plan is approved, so
    including it would refuse the GO on the FIRST run -- the run it most
    needs to watch. The pin gates running unattended; this gates who may
    drive. Two questions about one grant."""
    src = inspect.getsource(drivable)
    assert "approved_plan_hash" not in drivable._HAS_GRANT
    assert "approved_by IS NOT NULL" in drivable._HAS_GRANT
    assert "revoked_at IS NULL" in drivable._HAS_GRANT
    assert "expires_at" in drivable._HAS_GRANT
    assert "pin" in src.lower(), "the omission is not explained"


def test_revoking_a_grant_hands_the_errand_back():
    """Double duty, and on purpose: `revoked_at` stops the errand running
    unattended AND returns it to the assistant."""
    assert _drive("errand", None).code == "E_NOT_AUTONOMOUS"


# ── the gate is actually wired ────────────────────────────────────────

def test_every_per_story_tool_goes_through_the_gate():
    src = inspect.getsource(api._dispatch_go)
    assert "if tool in GO_PER_STORY:" in src
    assert "go_may_drive" in src
    # BEFORE the tools, not after one of them.
    assert src.index("go_may_drive") < src.index('if tool == "story_status"')


def test_the_gated_list_holds_every_tool_that_takes_a_story():
    """THE FAILURE MODE THIS FILE EXISTS FOR: a sixth per-run tool added
    to the GO surface and not to `GO_PER_STORY`. Anything in the GO
    dispatcher whose args include `story` and which is not a goal-scoped
    read must be gated."""
    assert set(api.GO_PER_STORY) == {
        "story_status", "story_progress", "steer", "ask_executor",
        "cancel_story", "run_control", "decide_plan", "show_card"}


def test_amend_is_not_on_the_go_surface():
    """It withdraws an approval and writes a new contract version -- the
    one thing in this family that changes what a person agreed to."""
    assert "amend" not in api.GO_ROUTES


def test_the_two_surfaces_still_agree():
    """The startup assertion refuses to serve when they do not, so a
    mismatch is a dead deployment rather than a bad one."""
    from kernel.go.tools import TOOLS as GO_TOOLS
    assert set(api.GO_ROUTES) == set(GO_TOOLS)


def test_the_assistant_lost_nothing():
    """C adds to the GO's set and removes nothing from the assistant's.
    A story and a hand-started errand must stay drivable from the
    assistant exactly as before -- including ones the GO proposed."""
    for tool in ("story_status", "story_progress", "steer", "ask_executor",
                 "cancel_story", "amend", "start_story"):
        assert tool in api.ROUTES, tool


def test_cancel_keeps_its_own_human_check():
    """Two independent gates on the most destructive verb is right, not
    redundant: `go_may_drive` says which runs, `human_uid` says whose
    decision."""
    from kernel.assistant.write import cancel_story
    src = inspect.getsource(cancel_story)
    assert "human_uid" in src
    dispatch = inspect.getsource(api._dispatch_go)
    assert 'args.get("human_uid")' in dispatch


# ── the clarification cap ─────────────────────────────────────────────

def test_the_cap_is_a_circuit_breaker_not_a_budget():
    from kernel.go.answers import CAP
    assert CAP >= 10, (
        "three clarifications parked the errand on a person for something "
        "GO could answer in a sentence")
    assert CAP <= 25, "past here it stops tripping on a genuine loop"


def test_the_cap_still_escalates_rather_than_failing():
    from kernel.go import answers
    src = inspect.getsource(answers)
    assert "E_ANSWER_CAP" in src


def test_stories_were_already_out_of_reach_of_relay_answer():
    """C.3 in the plan asked to widen `may_answer` from errands GO
    PROPOSED to errands it owns. Reading it, the refusal already fires on
    `kind != 'errand'` and says nothing about who proposed it -- so there
    was nothing to widen, and the plan was wrong about the code."""
    from kernel.go.answers import may_answer
    src = inspect.getsource(may_answer)
    assert 'row["kind"] != "errand"' in src
    assert "proposed" not in src.split('"""')[2] if '"""' in src else True


# ── the gateway spec, which is the fourth declaration ─────────────────

def _go_spec_tail() -> str:
    # VIA `gateway_surface`, not by finding the file myself. A guard in
    # test_gateway_surface.py forbids a second implementation of the
    # surface comparison, and it caught this file doing it -- correctly,
    # even though what is read here is the PROSE rather than the
    # comparison. One place knows where those specs live.
    from app.services.gateway_surface import specs_dir
    d = specs_dir()
    if d is None or not (d / "kernel-go-tools.ts").exists():
        pytest.skip("the gateway checkout is not beside this one")
    body = (d / "kernel-go-tools.ts").read_text(encoding="utf-8")
    return body.split("DRIVING AN AUTONOMOUS ERRAND", 1)[1]


def test_the_gateway_spec_requires_a_story_on_every_per_run_tool():
    """Unlike the assistant's versions, where `story` is optional and
    inferred from the conversation. GO's errand threads name their errand
    in the turn context, and the kernel refuses a call with no story
    anyway -- it cannot check a permission against a run it has not been
    told about.

    COUNTED AGAINST `GO_PER_STORY`, never a literal: the first version
    said 5 and went red the moment C.4 added the two hold verbs -- a
    guard failing for exactly the change it should have been checking."""
    tail = _go_spec_tail()
    want = 'story: Type.String({ description: "The errand' + chr(39) + 's story id." })'
    assert tail.count(want) == len(api.GO_PER_STORY), tail.count(want)
    assert "Execution id" not in tail, (
        "a per-run GO tool copied the assistant's optional story argument")


def test_the_gateway_spec_says_errands_only():
    flat = " ".join(_go_spec_tail().split())
    assert flat.count("AUTONOMOUS ERRANDS ONLY") == len(api.GO_PER_STORY), (
        "a tool that does not say so invites the GO into a refusal, and a "
        "refusal the prompt walked it into reads as a platform bug")


# ── C.4: pause / resume / cancel, one tool ────────────────────────────
#
# WHAT THIS REPLACED AND WHY. A first draft shipped `pause_errand` and
# `resume_errand`, and `pause_errand` wrote a `story`-scoped row into
# `execution_pauses` -- while the Pause BUTTON on the thread submits a
# `hold` PROPOSAL. Two mechanisms for one word, so "why is it not
# paused" had two answers depending on who asked. And cancel was missing
# from the set entirely, sitting on `cancel_story`, which ends the STORY
# -- for a recurring errand the next run still comes.

class _RunConn:
    """Enough of a connection for the three actions."""

    def __init__(self, state="RUNNING", run_id=7, hold_qid=None):
        self.state, self.run_id, self.hold_qid = state, run_id, hold_qid
        self.proposals: list = []
        self.resolved: list = []

    async def fetchrow(self, sql, *a):
        if "FROM kernel.executions" in sql:
            return None if self.state is None else {"id": self.run_id,
                                                    "state": self.state}
        return None

    async def fetchval(self, sql, *a):
        return self.hold_qid


def _control(action, uid="alice", message="", actor_id="", **kw):
    from kernel.go import run_control as rc
    conn = _RunConn(**kw)
    sent: list = []
    resolved: list = []

    async def _fake_submit(c, eid, kind, payload, human_uid, actor_id=""):
        sent.append((kind, payload, human_uid, actor_id))
        return None

    async def _fake_resolve(c, qid, answer, answered_by="", verdict="",
                            human_uid=None, **_):
        resolved.append((qid, answer, answered_by, verdict, human_uid))

    import kernel.harness.park_resolve as pr
    old_submit, old_resolve = rc._submit, pr.resolve_request
    rc._submit, pr.resolve_request = _fake_submit, _fake_resolve
    try:
        got = asyncio.run(
            rc.run_control(conn, "KER12", "e1", action, uid, message,
                           actor_id=actor_id))
    finally:
        rc._submit, pr.resolve_request = old_submit, old_resolve
    return got, sent, resolved


def test_one_tool_covers_all_three():
    from kernel.go.run_control import ACTIONS
    assert set(ACTIONS) == {"pause", "resume", "cancel"}


def test_an_unknown_action_is_refused_with_the_list():
    got, _, _ = _control("stop")
    assert got.code == "E_UNKNOWN_ACTION"
    assert "pause" in got.message and "cancel" in got.message


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_every_action_needs_a_person(action):
    """All three stop or alter work somebody approved. GO relays a
    decision; it does not take one."""
    got, sent, _ = _control(action, uid="")
    assert got.code == "E_NO_HUMAN"
    assert not sent, "it acted before checking whose decision it was"


def test_nothing_to_control_when_no_run_is_live():
    got, _, _ = _control("pause", state=None)
    assert got.code == "E_NO_RUN"


# ── the mechanisms, which must be the buttons' own ────────────────────

def test_pause_submits_a_hold_proposal():
    """NOT `execution_pauses`. The button submits `hold`, the leg takes
    it at a step boundary, and `resume` answers it."""
    got, sent, _ = _control("pause", message="thinking")
    assert got["action"] == "pause"
    assert sent and sent[0][0] == "hold"
    assert sent[0][2] == "alice"


def test_pause_is_refused_unless_the_run_is_working():
    """A run parked on a plan is already waiting for a person; there is
    no leg to stop."""
    got, sent, _ = _control("pause", state="PARKED_HOT")
    assert got.code == "E_NOT_WORKING"
    assert not sent


def test_cancel_submits_a_cancel_proposal_from_any_live_state():
    for state in ("RUNNING", "PARKED_HOT", "PARKED_COLD", "VERIFYING"):
        got, sent, _ = _control("cancel", state=state)
        assert sent and sent[0][0] == "cancel", state


def test_cancel_says_it_ends_the_run_and_not_the_errand():
    """Somebody told "cancelled" will otherwise hear that the errand is
    over. It is not: the grant survives and it can be run again.

    The old wording said "a recurring errand's next run still comes",
    which stopped being true when recurrence was removed and was then
    doubly wrong -- there is no next run coming, and the errand is not
    finished either."""
    got, _, _ = _control("cancel")
    assert "ENDS THE RUN, NOT THE ERRAND" in got["detail"]
    assert "can be run again" in got["detail"]
    assert "recurring" not in got["detail"]


def test_cancel_is_refused_on_a_run_that_has_not_started():
    """RUN 345. A pod that cannot be placed sets `stall_reason` while the
    run is still DISPATCHING. The check-in reported a stall, GO asked the
    executor (which does not exist before the pod does), got no answer,
    and cancelled at eighty seconds -- inside the ninety the conductor
    documents as normal for Unschedulable.

    The reasoning is sound and will recur, so the refusal is in the
    code: the run IS stalled, it IS producing nothing, and cancelling IS
    what you do with a dead run. What GO cannot see is that the queue is
    not the work."""
    for state in ("PENDING", "ADMITTED", "DISPATCHING"):
        got, sent, _ = _control("cancel", state=state)
        assert got.code == "E_NOT_STARTED", state
        assert not sent, f"{state} still submitted a cancel"


def test_the_refusal_says_why_cancelling_would_not_help():
    """A refusal GO cannot act on is a refusal GO argues with. Name the
    two facts it is missing: nothing is running, and cancelling frees no
    capacity -- so waiting is not merely allowed, it is the only thing
    that shortens the wait."""
    got, _, _ = _control("cancel", state="DISPATCHING")
    assert "Cancelling frees no capacity" in got.message
    assert "Cancel button on the board" in got.message


def test_a_cancel_carries_the_caller_and_the_human_separately():
    """RUN 345's LEDGER SAID A PERSON DID IT. `actor_id` was
    `f"human:{human_uid}"`, so GO's own cancellation was indistinguishable
    from the goal owner pressing the button -- and the owner was not in
    the conversation.

    Two facts, carried as two: who acted, and whose decision they
    carried. (What `_submit` then WRITES is the next test -- this one
    replaces `_submit`, so it can only see what reaches it.)"""
    got, sent, _ = _control("cancel", actor_id="go:err16")
    kind, payload, human_uid, actor_id = sent[0]
    assert actor_id == "go:err16", "GO is invisible in its own act"
    assert human_uid == "alice"


def test_the_proposal_names_go_as_actor_and_the_human_in_the_payload():
    """The write itself, against the real `_submit`.

    `proposals.actor_id` is the field a reader trusts -- `intake` says of
    a neighbouring kind that it "cannot be spoofed by whatever wrote the
    instruction", which is exactly why stamping a human into it was
    wrong. GO goes in the actor; the person goes in the payload."""
    import asyncio

    from kernel.go import run_control as rc

    seen = {}

    async def _fake_proposal(conn, actor_id, kind, payload, **kw):
        seen.update(actor_id=actor_id, kind=kind, payload=payload)
        return 1

    import kernel.mailbox.api as mb
    old = mb.submit_proposal
    mb.submit_proposal = _fake_proposal
    try:
        asyncio.run(
            rc._submit(None, 345, "cancel", {"reason": "because"},
                       "rPeDJCGuzKeka9yyYARENy4rhT22", "go:err16"))
    finally:
        mb.submit_proposal = old

    assert seen["actor_id"] == "go:err16", (
        "the ledger says a person cancelled this; run 345 all over again")
    assert seen["payload"]["human_uid"] == "rPeDJCGuzKeka9yyYARENy4rhT22"
    assert seen["payload"]["reason"] == "because"


def test_a_person_pressing_the_button_is_still_recorded_as_themselves():
    """The fix must not erase the human path. No caller identity means
    the actor IS the person -- that is the board, and it was never the
    thing that was wrong."""
    import asyncio

    from kernel.go import run_control as rc

    seen = {}

    async def _fake_proposal(conn, actor_id, kind, payload, **kw):
        seen.update(actor_id=actor_id)
        return 1

    import kernel.mailbox.api as mb
    old = mb.submit_proposal
    mb.submit_proposal = _fake_proposal
    try:
        asyncio.run(
            rc._submit(None, 345, "cancel", {"reason": "x"}, "alice"))
    finally:
        mb.submit_proposal = old
    assert seen["actor_id"] == "human:alice"


def test_resume_answers_the_hold_question():
    got, _, resolved = _control("resume", hold_qid=99)
    assert resolved and resolved[0][0] == 99
    assert resolved[0][4] == "alice", "the person's uid is not carried"


def test_resume_is_refused_when_nothing_is_holding():
    """And points at the plan card, because that is the likely confusion
    -- a run parked on a PLAN is not a run on hold."""
    got, _, resolved = _control("resume", state="PARKED_HOT", hold_qid=None)
    assert got.code == "E_NOT_HOLDING"
    assert "approves that from its card" in got.message
    assert not resolved


def test_a_resume_message_goes_in_as_a_steer_BEFORE_the_unpark():
    """`take_proposals` drains the mailbox at the top of the step loop,
    before the first model call. Submitted after the un-park it would be
    a step late, which is the whole point of the ordering."""
    got, sent, resolved = _control("resume", hold_qid=99,
                                   message="skip the slow ones")
    assert sent and sent[0][0] == "steer"
    assert got["steered"] is True
    src = inspect.getsource(
        __import__("kernel.go.run_control", fromlist=["x"])._resume)
    assert src.index("steer") < src.index("resolve_request")


def test_a_bare_resume_does_not_tell_the_executor_it_was_approved():
    """`resolve_request` would otherwise have it read "WHAT THEY SAID:
    approved" about a pause in which nobody approved anything."""
    got, sent, resolved = _control("resume", hold_qid=99)
    assert not sent, "a bare resume submitted a steer"
    assert "not a decision about the work" in resolved[0][1]


# ── and the duplicate mechanism is gone ───────────────────────────────

def test_there_is_no_second_way_to_pause_a_run():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    assert not (root / "kernel/go/hold.py").exists(), (
        "the execution_pauses pause is back; one word, two mechanisms")
    from kernel.conductor import pauses
    assert "'story'" not in pauses.SQL, (
        "the kill switch reads a story scope again, which nothing writes")


def test_the_kill_switch_is_back_to_two_scopes():
    from kernel.conductor import pauses
    import inspect as _i
    sig = _i.signature(pauses.frozen)
    assert list(sig.parameters) == ["conn", "goal_id"], (
        "frozen() still takes a story, so the duplicate pause is only "
        "half removed")


# ── carrying a person's decision on a parked plan ─────────────────────
#
# WHY THIS EXISTS AT ALL. An autonomous errand's FIRST run parks on its
# plan -- by design, and the approval card says so. But the person who
# approved the errand has gone away, which is the point of approving
# one, so the run sat there until they came back to a card they may
# never open. Observed on ERR01/5227676d: "I cannot approve the plan.
# Plan approval is strictly a human action" -- true of the tools GO
# held, and the errand stopped.
#
# THIS IS NOT AN AGENT APPROVING. `resolve_request`'s gate has never
# been "the caller must be human"; it is "an approval must carry a human
# uid", and `cancel_story` has relied on that since it was written.

class _PlanConn:
    """`granted` and `cycles` exist because the GO may now decide alone.

    A self-decision is allowed only where a person has consented to this
    errand running unattended, and `self_decide.alone_ok` re-reads that
    consent at the moment of use. A fake that answered the grant query
    unconditionally would make every test here pass through a guard
    that does nothing -- so it is modelled, and `granted=False` is a
    real case with a test of its own.
    """

    def __init__(self, member=True, question=("OPEN", "authorization"),
                 granted=True, cycles=0):
        self.member, self.question = member, question
        self.granted, self.cycles = granted, cycles
        self.resolved = []

    async def fetchval(self, sql, *a):
        if "goal_members" in sql:
            return 1 if self.member else None
        if "errand_schedules" in sql:
            return 1 if self.granted else None
        if "count(*)" in sql and "questions" in sql:
            return self.cycles
        if "execution_id FROM questions" in sql:
            return 99
        return None

    async def fetchrow(self, sql, *a):
        if self.question is None:
            return None
        status, kind = self.question
        return {"id": 7, "kind": kind, "status": status, "story_id": "e1"}


def _decide(verdict="approve", uid="alice", said="go ahead", **kw):
    from kernel.go import decide_plan as dp
    conn = _PlanConn(**kw)
    seen = []

    async def _fake_resolve(c, qid, answer, answered_by="", declined=False,
                            verdict="", human_uid=None, **_):
        seen.append({"qid": qid, "answer": answer, "by": answered_by,
                     "declined": declined, "verdict": verdict,
                     "human_uid": human_uid})

    import kernel.harness.park_resolve as pr
    old = pr.resolve_request
    pr.resolve_request = _fake_resolve
    try:
        got = asyncio.run(dp.decide_plan(conn, "KER12", "e1", 7, verdict,
                                         uid, said))
    finally:
        pr.resolve_request = old
    return got, seen


def test_it_carries_all_three_verdicts():
    from kernel.go.decide_plan import VERDICTS
    assert set(VERDICTS) == {"approve", "revise", "decline"}


def test_an_approval_reaches_the_kernel_with_the_persons_uid():
    got, seen = _decide()
    assert got["verdict"] == "approve"
    assert seen and seen[0]["human_uid"] == "alice"
    assert seen[0]["by"] == "go", "the record must show GO relayed it"


def test_a_decline_is_sent_as_declined():
    """`resolve_request` spells it `declined`; the tool says `decline`
    because that is the word a person uses."""
    _, seen = _decide(verdict="decline", said="wrong approach")
    assert seen[0]["declined"] is True


def test_a_revise_is_not_a_decline():
    """Conflated, a person saying "nearly, change step 3" tells the
    model its whole approach was rejected."""
    _, seen = _decide(verdict="revise", said="use the API instead")
    assert seen[0]["declined"] is False
    assert seen[0]["verdict"] == "revise"


@pytest.mark.parametrize("verdict", ["approve", "revise", "decline"])
def test_the_go_may_now_decide_alone_on_an_autonomous_errand(verdict):
    """THE RULE CHANGED, DELIBERATELY, on 2026-09-24.

    It was ASK FIRST: `human_uid` required on all three verdicts, so the
    GO could only carry a decision. The argument was good and it held
    for everything except the shape the verb exists for -- an autonomous
    errand, where the person approved it precisely so they would not
    have to watch, and where waiting for them puts back the stall.

    WHAT REPLACES THE UID is not nothing, and the other tests in this
    file are the rest of it: the grant is re-read at the moment of use
    (so revoking takes the authority back), the run must be an
    autonomous errand (`go_may_drive`, at dispatch), and three plan
    cycles per run is the ceiling before a person must look.

    `said` is still required either way -- relaying it is their words,
    deciding it is the reasoning, and the executor reads it as the
    answer.
    """
    got, seen = _decide(verdict=verdict, uid="")
    assert not isinstance(got, Refused), got
    assert len(seen) == 1
    assert seen[0]["human_uid"] == "", "a self-decision must carry no uid"
    assert seen[0]["by"] == "go"
    assert got["decided_by"] == "go"


def test_a_self_decision_says_to_tell_somebody():
    """A plan approved with nobody watching is the one a person most
    needs to hear about, and a rule that lives only in a prompt is a
    rule followed most of the time."""
    got, _ = _decide(uid="")
    assert "goal thread" in got["detail"]
    assert "why" in got["detail"]


def test_relaying_a_persons_decision_is_unchanged():
    """The mode that existed must still work exactly as it did -- with
    the uid checked, and both names on the record."""
    got, seen = _decide(uid="alice")
    assert not isinstance(got, Refused), got
    assert seen[0]["human_uid"] == "alice"
    assert got["on_behalf_of"] == "alice"


def test_said_is_required_even_when_the_go_decides():
    """Deciding, it is the reasoning; the executor reads it as the
    answer, and on a revise it is the only thing saying what to
    change."""
    got, seen = _decide(uid="", said="")
    assert got.code == "E_NOTHING_SAID"
    assert not seen


def test_a_uid_nobody_can_check_is_refused():
    """Otherwise `human_uid` is a free string an agent fills in, which
    is the hole `resolve_request` warns about `answered_by` being."""
    got, seen = _decide(member=False)
    assert got.code == "E_NOT_AN_ADMIN"
    assert not seen


def test_a_plain_member_cannot_decide_a_plan():
    """Approving authorises an executor to act unattended. The check is
    `role = 'admin'`, not mere membership."""
    from kernel.go import decide_plan as dp
    import inspect as _i
    assert "role = 'admin'" in _i.getsource(dp._is_admin)


def test_any_admin_will_do():
    """Waiting for one named person is how an autonomous errand ends up
    parked over a weekend -- the failure this verb exists to remove."""
    from kernel.go import decide_plan as dp
    import inspect as _i
    src = _i.getsource(dp._is_admin)
    assert "approved_by" not in src, (
        "it is checking the errand's approver, not any admin")


def test_it_refuses_to_relay_nothing():
    """`said` is the answer the executor reads -- on a revise it is the
    only thing telling it what to change."""
    got, seen = _decide(said="   ")
    assert got.code == "E_NOTHING_SAID"
    assert not seen


def test_a_question_from_another_story_is_refused():
    got, seen = _decide(question=None)
    assert got.code == "E_NO_QUESTION"
    assert not seen


def test_an_already_settled_question_says_so():
    """Very likely the person you were just talking to clicked the
    card."""
    got, seen = _decide(question=("ANSWERED", "authorization"))
    assert got.code == "E_ALREADY_SETTLED"
    assert not seen


def test_the_kernels_own_refusal_is_the_last_word():
    """This function is wider than the kernel's gate in places, and the
    gate must stay final rather than being second-guessed here."""
    src = inspect.getsource(
        __import__("kernel.go.decide_plan", fromlist=["x"]).decide_plan)
    assert "except ApprovalRefused" in src
    assert "E_APPROVAL_REFUSED" in src


def test_both_new_verbs_are_gated_like_the_rest():
    assert "decide_plan" in api.GO_PER_STORY
    assert "show_card" in api.GO_PER_STORY


def test_a_revoked_grant_takes_the_authority_back():
    """The grant is the consent being spent, and it is re-read at the
    moment of use for exactly this: revoking it must stop the GO
    deciding alone immediately, not at the next run."""
    got, seen = _decide(uid="", granted=False)
    assert got.code == "E_MUST_ASK_A_PERSON"
    assert "no live grant" in str(got)
    assert not seen


def test_three_cycles_alone_and_then_a_person_looks():
    """An executor and a GO can re-plan at each other indefinitely,
    each approval reasonable in isolation, burning a leg a cycle with
    nobody watching. The ceiling is crude on purpose."""
    from kernel.go.self_decide import CAP

    ok, seen = _decide(uid="", cycles=CAP - 1)
    assert not isinstance(ok, Refused), ok
    assert seen

    got, seen = _decide(uid="", cycles=CAP)
    assert got.code == "E_MUST_ASK_A_PERSON"
    assert not seen, "the run must stay parked where it was"


def test_the_ceiling_does_not_apply_when_a_person_decided():
    """A person answering is not a loop, however many times they do it."""
    from kernel.go.self_decide import CAP

    got, seen = _decide(uid="alice", cycles=CAP + 5)
    assert not isinstance(got, Refused), got
    assert seen[0]["human_uid"] == "alice"
