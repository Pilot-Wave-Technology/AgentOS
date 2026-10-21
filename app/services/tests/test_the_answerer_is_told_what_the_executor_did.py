"""A question is answered beside the run, so the record IS the answer.

WHY NOT JUST SEND THE QUESTION TO THE EXECUTOR. `answerer/deliver.py`
gives the reason and it is a good one: "a question landing in that
position is read as one more thing to do". A steer is an instruction and
belongs in the executor's message list; a question is not and does not.
It also means a question can be asked in every state, including the ones
where no leg is running at all.

BUT THAT REASONING WAS DOING TOO MUCH WORK. It justifies not
interrupting the run. It does not justify handing the answerer a journal
with the contents removed, and that is what had happened: the record
rendered `r["tool"]` alone, so a whole diagnostic session reached the
answerer as `bash, bash, bash, read, bash`.

Run 262 is the measurement. Asked "is the executor going through each
test file individually to diagnose the slow tests?", the answerer could
not have said -- while the same question was answerable in one query
against the `args` column the record did not select.
"""
from __future__ import annotations

import inspect
import json

from kernel.answerer import deliver, record


def _rows(*pairs):
    return [{"tool": t, "args": json.dumps(a) if a is not None else None,
             "model_text": None} for t, a in pairs]


def test_a_move_names_what_it_acted_on():
    """A question about WHICH files cannot be answered by a list that
    names none of them."""
    got = record._moves(_rows(
        ("bash", {"command": "cd repos/x && pytest tests/runner -ra"}),
        ("read", {"path": "tests/world_model/test_sigma.py", "limit": 100}),
        ("bash_wait", {"job_id": "job-313e02fd"})))
    assert got[0].startswith("bash cd repos/x && pytest tests/runner")
    assert got[1] == "read tests/world_model/test_sigma.py"
    assert got[2] == "bash_wait job-313e02fd"


def test_a_step_with_nothing_identifying_still_renders():
    assert record._moves(_rows(("no_tool", {}))) == ["no_tool"]


def test_malformed_args_do_not_break_the_record():
    """Journalled args come from the MODEL. `brief.py` carries the same
    lesson: a non-string travelled into the brief once and lost it on
    the leg whose brief mattered."""
    rows = [{"tool": "grep", "args": "not json", "model_text": None},
            {"tool": "read", "args": None, "model_text": None},
            {"tool": "write", "args": json.dumps({"path": 12345}),
             "model_text": None}]
    assert record._moves(rows) == ["grep", "read", "write"]


def test_a_long_command_is_bounded():
    got = record._moves(_rows(("bash", {"command": "x" * 900})))
    assert len(got[0]) < 200, got[0]


def test_the_record_reads_what_the_executor_reads():
    """The module's own docstring promises exactly this, and selected
    two of the four columns `harness/brief.py` uses."""
    src = inspect.getsource(record.gather)
    assert "args::text" in src, (
        "the answerer is back to a list of bare tool names")
    assert "model_text" in src, (
        "what the model SAID is the part a question about intent needs")


def test_what_the_model_said_reaches_the_answer():
    src = inspect.getsource(record.gather)
    assert "What it said while doing it" in src


def test_the_workspace_host_travels_with_the_path():
    """The answerer runs in the conductor and the tree is in the
    execution's pod, so the path alone is an invitation to read the
    wrong disk -- which is what it did, on every pod execution."""
    src = inspect.getsource(record.gather)
    assert "workspace_host" in src


# ── and asking must never stall the conductor ─────────────────────────

def test_the_drain_claims_only_what_it_can_answer():
    """THE BOUND WAS APPLIED AFTER THE CLAIM, AND THAT LOST QUESTIONS.

    `_claim` marked EVERY pending question ACCEPTED; the caller then
    spawned only as many as it had pool connections for and "deferred"
    the rest with the comment "the claim stays pending". It did not --
    it had already been accepted. So the fifth question of a burst was
    taken, dropped, and written off by `_reap` fifteen minutes later
    with "I never finished looking into this".
    """
    src = inspect.getsource(deliver.deliver_questions)
    claim_at = src.index("_claim(conn, goal_id")
    room_at = src.index("room =")
    assert room_at < claim_at, (
        "the concurrency bound is computed after the claim again, so "
        "questions over the bound are accepted and never answered")
    assert "_claim(conn, goal_id, room)" in src


def test_an_unclaimed_question_is_still_pending():
    """Which is what makes 'it waits a tick' true rather than a comment."""
    assert "LIMIT $3" in inspect.getsource(deliver._claim)
    assert "SKIP LOCKED" in inspect.getsource(deliver._claim), (
        "two conductors racing must take disjoint sets, not block")


def test_an_inline_answer_cannot_hold_the_tick_for_every_asker():
    """With no pool the answer runs IN the tick, and the tick is serial.
    One per pass caps the wait at one model call rather than one per
    person who happened to ask."""
    assert deliver.INLINE_PER_TICK == 1
    src = inspect.getsource(deliver.deliver_questions)
    assert "INLINE_PER_TICK" in src


def test_the_answer_itself_is_wall_clocked():
    """A model that never returns must not become a connection held for
    ever. `answer_question` is the only thing between the pool and the
    provider."""
    from kernel.answerer import agent

    assert agent.WALL_CLOCK_S <= 600
    assert "asyncio.wait_for(" in inspect.getsource(agent.answer_question)


def test_a_dead_answerer_does_not_strand_the_asker():
    assert deliver.STALE_MINUTES >= 1
    assert "LIMIT 20" in inspect.getsource(deliver._reap), (
        "the reap is unbounded, so a backlog becomes a long tick")


def test_answering_never_runs_in_the_executor_s_leg():
    """The question channel is deliberately outside LOOP_KINDS: a
    question in the executor's message list reads as an instruction, and
    a run that had to be alive to be asked could not be asked in
    PARKED or VERIFYING."""
    from kernel.harness.intake import LOOP_KINDS

    assert "question" not in LOOP_KINDS
    assert "steer" in LOOP_KINDS
    assert "RUNNING" in deliver.ASKABLE and "VERIFYING" in deliver.ASKABLE
