"""`ask` -- the executor answering a person, from its own head.

WHAT THE ANSWERER CANNOT REACH. It reconstructs from the record, the
tree and the journal. What the executor TRIED AND REJECTED, what it is
about to do, why it read an error the way it did: none of that is
written anywhere, so for "what are you doing and why" the model is the
only source and asking beats inferring.

WHY THE ANSWERER STAYS. A park ENDS the leg -- the next one rebuilds its
messages from the task, the brief and the standing steers -- so
PARKED_HOT looks live and is not. Five of the seven askable states have
no mind to ask. `ask` is for the one that does.

THE FOUR THINGS THIS MUST NOT DO, which are what the tests below hold:
break the model loop, stop a running job, let a question read as an
instruction, or strand a person when the leg turns out not to answer.
"""
from __future__ import annotations

import inspect

from kernel.answerer import deliver
from kernel.harness import intake
from kernel.harness.tools_worker import reply
from kernel.mailbox.schema import (LEG_LIVE_STATES, PROPOSAL_KINDS,
                                   route_question, validate_proposal)


# ── routing: the caller never chooses ─────────────────────────────────

def test_only_a_running_run_has_a_mind_to_ask():
    """A park ends the leg, so PARKED_HOT is not live however it reads."""
    assert route_question("RUNNING") == "ask"
    for state in ("PENDING", "ADMITTED", "DISPATCHING", "PARKED_HOT",
                  "PARKED_COLD", "VERIFYING", "DONE", ""):
        assert route_question(state) == "question", state
    assert LEG_LIVE_STATES == frozenset({"RUNNING"})


def test_the_assistant_routes_rather_than_picks():
    """Asking a person to choose is asking them to understand the leg
    model in order to ask 'what are you doing'."""
    from kernel.assistant.write import ask_executor

    src = inspect.getsource(ask_executor)
    assert "route_question" in src
    assert '"question"' not in src.split("route_question")[1][:400], (
        "the kind is hardcoded again after routing")


def test_the_console_routes_too():
    """It passes `kind` straight through from the caller, so without
    this it is the way round the router."""
    from kernel.console import server

    src = inspect.getsource(server)
    assert "route_question" in src


def test_both_kinds_still_need_a_question():
    for kind in ("question", "ask"):
        bad = validate_proposal("human-1", kind, {"question": "  "},
                                "k1", 1)
        assert bad is not None and bad.code == "E_EMPTY_QUESTION", kind
    assert "ask" in PROPOSAL_KINDS


# ── it must not read as an instruction ────────────────────────────────

def _message(**over):
    payload = {"question": "why in-memory storage?", "from": "human-1",
               "request_id": 77}
    payload.update(over)
    return intake._ask_message(payload)["content"]


def test_the_question_is_labelled_as_one():
    """Asked why it chose in-memory storage, an executor CHANGED THE
    STORAGE. That is the measured failure this framing exists to stop."""
    body = _message()
    assert "THIS IS NOT AN INSTRUCTION" in body
    assert "Do not start work" in body
    assert "do not revise" in body.lower()


def test_it_says_where_to_put_the_answer():
    """A model with nowhere to put an answer expresses itself by
    working."""
    body = _message()
    assert "reply_to_human" in body
    assert "request_id=77" in body


def test_it_points_at_the_steer_channel_for_a_real_change():
    assert "steer" in _message()


def test_ask_is_carried_by_the_loop_and_question_is_not():
    assert "ask" in intake.LOOP_KINDS
    assert "question" not in intake.LOOP_KINDS


# ── it must not stop a running job ────────────────────────────────────

def test_the_message_says_the_job_is_still_running():
    """The model has just been pulled out of a wait it did not choose to
    end. Left to infer, the plausible readings are 'the job failed' and
    'start over', and both are expensive."""
    body = _message()
    assert "STILL" in body and "RUNNING" in body
    assert "bash_wait" in body
    assert "Do not restart the job" in body


def test_the_reply_tool_touches_no_job():
    # THE FUNCTION, not the module: the module docstring names `jobcore`
    # precisely to say it does not touch it, and a test that cannot tell
    # a mention from a call forbids explaining yourself.
    src = inspect.getsource(reply.reply_to_human)
    for forbidden in ("jobcore", "bash_kill", "cancel(", "kill("):
        assert forbidden not in src, forbidden


def test_the_reply_says_so_again_on_the_way_out():
    src = inspect.getsource(reply.reply_to_human)
    assert "still running" in src


# ── it must not break the loop ────────────────────────────────────────

def test_answering_is_not_a_park_and_not_an_effect():
    """`ask` may narrow nothing and widen nothing. A question that could
    authorise work would be a way past the plan gate."""
    src = inspect.getsource(reply.reply_to_human)
    for forbidden in ("ParkRequested", "UPDATE executions", "contract"):
        assert forbidden not in src, forbidden


def test_an_answer_with_nothing_waiting_is_not_an_error():
    """A model that answers twice, or after the asker gave up, has done
    nothing wrong -- and failing the call invites a retry or a hunt for
    the problem."""
    src = inspect.getsource(reply.reply_to_human)
    tail = src.split("if row is None:")[1].split("answer_id")[0]
    assert "exit_code=0" in tail
    assert "error=" not in tail


def test_an_empty_answer_is_refused_with_the_way_out():
    src = inspect.getsource(reply.reply_to_human)
    assert "`answer` is required" in src
    assert "If you do not" in src


# ── and it must not strand the asker ──────────────────────────────────

def test_an_unanswered_ask_becomes_a_question():
    """The risk of preferring `ask` is entirely in the cases where there
    turns out to be no executor: the run parked between the routing and
    the boundary, the leg died, or the model never called the tool. All
    three look identical from outside."""
    src = inspect.getsource(deliver._fallback)
    assert "kind = 'question'" in src
    assert "status = 'PENDING'" in src
    assert "'PENDING', 'ACCEPTED'" in src, (
        "an ask the leg consumed and never answered is not covered")


def test_the_fallback_runs_before_the_write_off():
    """Otherwise a handed-back question ages on towards being written
    off with an apology instead of being answered."""
    src = inspect.getsource(deliver.deliver_questions)
    assert src.index("_fallback(") < src.index("_reap(")


def test_the_grace_is_not_a_race():
    """A live leg reaches a step boundary within one wait slice even
    mid-bash_wait, so this is 'the leg is not going to answer'."""
    assert 1 <= deliver.ASK_GRACE_MINUTES < deliver.STALE_MINUTES


def test_the_fallback_is_bounded():
    assert "LIMIT 20" in inspect.getsource(deliver._fallback)


# ── and the leg must be able to WRITE the answer ──────────────────────

def test_the_leg_may_insert_an_answer():
    """FOUND LIVE ON RUN 264, at the moment it first mattered:

        InsufficientPrivilegeError: permission denied for table answers

    Every answer before the `ask` channel was written by the answerer,
    which runs in the conductor as `agentos_worker`. A leg only ever
    READ them, so the role file granted SELECT and UPDATE and no
    INSERT -- exactly right for what a leg could do at the time.

    The executor answered correctly; the answer had nowhere to land.
    Same shape as the conductor's missing `jobs: create`: a scope-down
    enumerated the callers that existed, and a later feature became
    one."""
    import pathlib

    from app.services.tests.shipped_tree import (IS_DISTRIBUTION,
                                                 shipped_privileges)

    if IS_DISTRIBUTION:
        table = shipped_privileges("kernel.answers", "agentos_leg")
        assert "INSERT" in table
        # The sequence too, or the insert fails one line further on.
        assert "USAGE" in shipped_privileges("kernel.answers_id_seq",
                                             "agentos_leg")
        # NOT delete: an answer given is a record.
        assert not table & {"DELETE", "ALL", "ALL PRIVILEGES"}
        return

    root = pathlib.Path(__file__).resolve().parents[3]
    grant = (root / "migrations"
             / "kernel_leg_role_answers_insert.sql").read_text(
                 encoding="utf-8")
    assert "GRANT INSERT ON kernel.answers TO agentos_leg" in grant
    # The sequence too -- a grant on the table alone fails the insert
    # with "permission denied for sequence", one line further on.
    assert "SEQUENCES IN SCHEMA kernel" in grant
    # NOT delete: an answer given is a record, and a leg that could
    # erase one could erase the evidence of what it told somebody.
    #
    # THE GRANT LINES, NOT THE FILE. The comment above them says "No
    # DELETE" in order to explain itself, and a check that cannot tell
    # a statement from the prose around it forbids the explanation --
    # which is the third time today that shape has bitten.
    statements = [ln for ln in grant.splitlines()
                  if "GRANT" in ln.upper()]
    assert statements
    assert not any("DELETE" in ln.upper() for ln in statements)

    sequence = (root / "migrations" / "SEQUENCE").read_text(
        encoding="utf-8").split()
    assert "kernel_leg_role_answers_insert" in sequence, (
        "the migration exists and will never run")
