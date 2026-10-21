"""Check-ins stop while a person is needed, and start again after.

THE STOPPING IS DELIBERATE. A run parked on a kind only a human may
settle -- `acceptance`, `direction`, `escalation`, `permission` -- gives
GO nothing to do, and waking it every window to watch somebody else's
decision is the waste the whole wake design exists to avoid.

THE RESTARTING IS THE HALF THAT COULD SILENTLY NOT WORK, which is why it
has its own test. Nothing resets a flag when the answer comes: the gate
is elapsed time since this run's last check-in, so a run that has been
parked for an hour is overdue the moment it is RUNNING again and fires
on the next sweep. A design that needed a flag cleared would be one more
thing to get wrong on a path nobody watches.

AND THE PERSON IS TOLD, because the quiet is otherwise total: the park
wake reaches GO, GO says "this needs a person" on a thread, and the
premise of an unattended errand is that nobody is reading that thread.
GO's own `ask_executor` can park a run on a `direction`, so the agent
can strand its own errand and then correctly fall silent about it.
"""

from __future__ import annotations

import asyncio
import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]

from app.services.go_errand_watch import HUMAN_ONLY, digest


class _Pool:
    def __init__(self, questions):
        self.questions = questions

    async def fetch(self, sql, *a):
        if "questions" in sql:
            # the real query filters these out in SQL
            return [q for q in self.questions if q["kind"] not in HUMAN_ONLY]
        return []

    async def fetchval(self, *a, **k):
        return "Abhishek"


def _digest(state, questions=(), quiet=900):
    row = {"id": 1, "goal_id": "E", "story_id": "s", "title": "t",
           "state": state, "stalled_at": None, "stall_card_note": None,
           "stalled_s": None}
    return asyncio.run(digest(_Pool(list(questions)), row, 900, quiet))


# -- stops --------------------------------------------------------------

def test_a_person_only_park_produces_no_wake():
    for kind in ("acceptance", "direction", "escalation", "permission"):
        out = _digest("PARKED_HOT",
                      [{"id": 9, "kind": kind, "question_text": "?"}])
        assert out == "", f"{kind} woke GO about somebody else's decision"


def test_a_park_go_can_settle_still_wakes_it():
    """`authorization` on an autonomous errand is GO's, so silence there
    would strand the run for the opposite reason."""
    out = _digest("PARKED_HOT",
                  [{"id": 9, "kind": "authorization",
                    "question_text": "plan approval"}])
    assert out.startswith("[ERRAND WAITING ON YOU]")


# -- and starts again ---------------------------------------------------

def test_the_check_in_resumes_the_moment_it_is_running_again():
    """No flag to clear. Overdue by elapsed time, so the first sweep
    after the answer lands fires."""
    out = _digest("RUNNING", quiet=900)
    assert out.startswith("[ERRAND CHECK-IN]")
    assert "nothing has been reported" in out


def test_a_short_park_does_not_earn_an_immediate_extra_check_in():
    """The cadence is preserved across the park rather than restarted
    by it -- a run that parked and unparked inside one window is not
    owed a wake it would not otherwise have had.

    ASSERTED ON THE SWEEP, not on `digest`. The gate is the sweep's:
    `digest` describes the run and the sweep decides whether it is due.
    My first version of this tested `digest` with a small `quiet` and
    failed, because `digest` had never been the thing that skips."""
    src = io.open(ROOT / "app/services/go_errand_watch.py",
                  encoding="utf-8").read()
    fn = src[src.index("async def sweep(pool)"):]
    assert "errand_checkin.is_due(quiet, row[\"age_s\"], window)" in fn
    assert "continue" in fn[fn.index("errand_checkin.is_due("):][:120]


def test_nothing_in_the_gate_is_stateful():
    """The proof that resumption cannot rot: the only thing consulted
    is the time of the last wake."""
    src = io.open(ROOT / "app/services/go_errand_watch.py",
                  encoding="utf-8").read()
    # The comparison lives in `errand_checkin.is_due` now, because a run
    # with NO check-in history needed a second clock -- its age -- and
    # the old inline gate silently treated "never checked in" as "due
    # now". That is what cancelled run 345 twenty-eight seconds into a
    # six-hundred-second window. Still stateless: two timestamps, no
    # stored cursor.
    assert "errand_checkin.is_due(" in src
    due = io.open(ROOT / "app/services/errand_checkin.py",
                  encoding="utf-8").read()
    assert "quiet if quiet is not None else (age_s or 0)) >= window" in due
    for flag in ("paused", "suspended", "resume_at", "check_ins_off"):
        assert flag not in src, (
            f"a stateful {flag} would need clearing, on a path nobody "
            f"watches")


# -- the person is told --------------------------------------------------

def test_the_park_notifies_the_approver():
    src = io.open(ROOT / "app/services/go_wake_events.py",
                  encoding="utf-8").read()
    assert "from app.services.errand_human_notice import notify" in src
    assert "person_only" in src


def test_only_for_the_kinds_go_cannot_settle():
    """An `authorization` GO owns must NOT pull a person in -- that is
    the whole autonomy feature, undone by a notification."""
    src = io.open(ROOT / "app/services/go_wake_events.py",
                  encoding="utf-8").read()
    block = src[src.index("person_only = ("):]
    block = block[:block.index("return await _deliver(")]
    assert "HUMAN_ANSWERED_KINDS" in block
    assert 'row["go_driven"]' in block


def test_the_notice_goes_to_a_person_and_does_not_wake_go_again():
    """GO has already said it on the thread. A second turn repeating
    itself moves nothing."""
    src = io.open(ROOT / "app/services/errand_human_notice.py",
                  encoding="utf-8").read()
    assert "push_notification" in src
    # Code only: the docstring explains why `_deliver` is NOT used, and
    # a bare substring check reads its own explanation as a violation.
    code = " ".join(l for l in src.splitlines()
                    if not l.lstrip().startswith("#"))
    code = code[code.index("_CLAIM ="):]
    assert "enqueue_system_wake" not in code
    assert "_deliver(" not in code


def test_the_notice_is_sent_once_per_question():
    src = io.open(ROOT / "app/services/errand_human_notice.py",
                  encoding="utf-8").read()
    assert "ON CONFLICT (goal_id, kind, ref) DO NOTHING" in src


def test_a_failed_notice_cannot_lose_the_park_wake():
    """The wake is what at least gets it onto a thread."""
    src = io.open(ROOT / "app/services/errand_human_notice.py",
                  encoding="utf-8").read()
    assert "except Exception" in src and "return False" in src
