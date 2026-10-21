"""An answer GO asked for must reach GO, not only the browser.

RUN 308. GO called `ask_executor` at ~15:40 and the executor answered at
15:41:00. GO never referred to the answer again, and could not have: the
`kernel_answer` card is published through `notify_new_message`, whose
own docstring says it reaches "browser subscribers ... WITHOUT waking
the agent / burning a turn".

That is correct for a card a person reads. It is wrong for the reply to
a question GO asked -- and GO's prompt now sends it to `ask_executor`
FIRST on every scheduled check-in, so this is the path that matters
most. An ask whose answer never arrives is worse than no ask: it spends
a turn and a round trip, and leaves GO reasoning from the journal it was
just told not to trust.
"""

from __future__ import annotations

import io
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def _row(**kw):
    base = {"id": 7, "execution_id": 308, "story_id": "s1", "goal_id": "G",
            "asked_by": "go:err08", "question": "How is the plan going?",
            "answer": "Step 3 of 5, the slow suite is running.",
            "thread_id": "t1"}
    base.update(kw)
    return base


# -- it is wired in at all ---------------------------------------------

def test_the_wake_is_produced_where_the_chat_queue_is():
    """THE FIRST CUT HUNG THIS OFF THE CARD POSTER and it could never
    have worked. Card posters run in the scheduler's loop, and the
    scheduler has no `CHAT_QUEUE_ENABLED`:

        17:24:10 go_wakes: chat queue disabled in this process;
                 errand_answer a:26 for goal ERR09 was not delivered

    `enqueue_system_wake` returns None there, `_deliver` releases its
    claim, and the wake is gone -- no row, no exception. A card poster
    writes a row to `messages`; a wake producer enqueues a TURN. The
    errand watch was moved out of that same loop for that same reason
    earlier the same day."""
    src = _read("app/services/go_wakes.py")
    assert "from app.services.errand_answer_wake import (" in src
    assert "await _answer_wake(pool)" in src


def test_the_card_poster_does_not_wake_anything():
    src = _read("app/services/kernel_answer_cards.py")
    assert "wake_go" not in src, (
        "the wake is back in the scheduler's loop, where it is dropped")
    assert "notify_new_message" in src, (
        "the card itself must still be published to browsers")


def test_one_bad_answer_does_not_stop_the_sweep():
    src = _read("app/services/errand_answer_wake.py")
    fn = src[src.index("async def sweep("):]
    fn = fn[:fn.index("async def wake_go(")]
    assert "except Exception" in fn


def test_the_sweep_only_looks_at_recent_answers():
    """Without a bound the first tick after a deploy wakes GO once for
    every answer in the table, and an hour-old answer is not news."""
    src = _read("app/services/errand_answer_wake.py")
    assert "RECENT_S" in src
    assert "card_posted_at > now() -" in src


def test_there_is_no_second_delivered_column():
    """`go_wake_log` is unique on (goal_id, kind, ref) and `_deliver`
    claims through it, so a repeat pass is a no-op and a pass that
    failed to enqueue released its claim and retries. A "waked" column
    beside it would be a second source of truth that can disagree."""
    src = _read("app/services/errand_answer_wake.py")
    assert "waked" not in src and "wake_sent" not in src


# -- what it carries ---------------------------------------------------

def test_the_answer_itself_rides_in_the_wake():
    """A wake saying "the executor answered" costs a turn and then buys
    a tool call to find out what it said -- more expensive than the wake
    it replaces. Same rule the parked wake had to learn about the plan."""
    from app.services.errand_answer_wake import _body

    out = _body(_row())
    assert "Step 3 of 5" in out
    assert "How is the plan going?" in out


def test_it_says_what_to_do_with_the_answer():
    from app.services.errand_answer_wake import _body

    out = _body(_row())
    for word in ("steer", "run_control", "escalate"):
        assert word in out, word


def test_it_asks_go_to_report_even_when_all_is_well():
    """"Nothing to say" is what made the check-ins invisible on run 308.
    Somebody is waiting to hear the errand is being watched."""
    from app.services.errand_answer_wake import _body

    assert "One line is enough" in _body(_row())


def test_a_huge_answer_cannot_dominate_the_context():
    from app.services.errand_answer_wake import MAX_ANSWER, _body

    out = _body(_row(answer="x" * (MAX_ANSWER * 3)))
    assert len(out) < MAX_ANSWER * 2


# -- scoped ------------------------------------------------------------

def test_only_gos_own_question_wakes_it():
    """A person's question answered on a story is not GO's business,
    and waking it to watch somebody else's exchange is the waste the
    whole wake design exists to avoid."""
    src = _read("app/services/errand_answer_wake.py")
    fn = src[src.index("async def wake_go("):]
    assert 'row["asked_by"]' in fn
    assert "startswith(\"go\")" in fn


def test_only_an_autonomous_errand_thread():
    src = _read("app/services/errand_answer_wake.py")
    fn = src[src.index("async def wake_go("):]
    assert "is_autonomous_errand_thread" in fn
    assert fn.index("is_autonomous_errand_thread(conn, thread_id)") <         fn.index("_deliver(")


def test_it_dedupes_on_the_answer():
    """A replayed sweep must not wake GO twice about one reply."""
    src = _read("app/services/errand_answer_wake.py")
    assert '"errand_answer"' in src
    assert 'f"a:{row[\'id\']}"' in src


def test_a_failed_wake_does_not_lose_the_card():
    """The card is already posted and the answer is in the thread.
    Failing to wake GO must not roll that back or stop the sweep."""
    src = _read("app/services/errand_answer_wake.py")
    fn = src[src.index("async def wake_go("):]
    assert "except Exception" in fn and "return False" in fn


def test_the_prompt_knows_this_wake_exists():
    """A NEW WAKE KIND HAS TO BE IN THE TABLE. GO's prompt lists every
    wake and what to do with it, and its own doctrine is that a wake it
    has never heard of is one it reasons about from first principles
    every single time -- at a full context read, per occurrence.

    I added `[EXECUTOR ANSWERED]` and did not add the row. Caught by
    reading the prompt for an unrelated question, which is not a
    process."""
    md = io.open(ROOT / "kernel/go/AGENTS.md", encoding="utf-8").read()
    assert "| `[EXECUTOR REPLIED]` |" in md
    row = md[md.index("| `[EXECUTOR REPLIED]` |"):].splitlines()[0]
    # The row says what the wake IS and the one thing to do with it;
    # the full procedure is in "Answering a wake", which is the point
    # of having that section rather than repeating it per row.
    assert "steer" in row
    assert "## Answering a wake" in md
    assert "Every errand wake gets a reply" in md


def test_the_wake_header_matches_the_row():
    """The table is looked up by the literal header the wake carries. A
    row for a string nothing emits is worse than no row."""
    from app.services.errand_answer_wake import _body

    assert _body(_row()).startswith("[EXECUTOR REPLIED]")
