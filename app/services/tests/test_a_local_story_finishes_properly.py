"""A locally-run story must finish the way a cloud one does.

It has no `kernel.executions` row -- the conductor is never involved -- so
none of the kernel's completion machinery fires. Three consequences, each
silent:

  * NOTHING TOOK IT PAST `done`. The council stands down for every `done`
    review while STORY_REVIEW_IN_POD is on, and there is no pod review to
    replace it, so `mark_story_reviewed` was never called. `done` leaves
    `active_thread_id` bound -- only `reviewed` and `cancelled` clear it --
    and keeps the workspace on the SHCA protect list for ever.

  * NOTHING COMPLAINED. The review-deadline backstop requires
    `EXISTS (kernel.executions)`, so a local story is invisible to it.
    Silence by accident, not by decision -- which is why there is a test
    here rather than a reliance on the sweep.

  * NOTHING SAID WHAT HAPPENED. `result_summary` was never written, so the
    GO's completion message rendered `Result: No result summary`.
"""

import pytest

from app.services.tests._source import code, source as src


# ── one writer, keyed on the story ───────────────────────────────────

def test_the_review_writer_can_be_keyed_on_a_story():
    """A local story has no execution, and the execution-keyed form could
    not express it."""
    body = src("kernel/worker/review_finish.py")
    assert "async def record_review_for_story(" in body


def test_the_execution_form_delegates_rather_than_duplicating():
    """`ONE FUNCTION WRITES THE REVIEW` is this module's own rule -- and a
    second writer for the local lane would break it exactly as the local
    thread resolver broke its."""
    body = code("kernel/worker/review_finish.py")
    fn = body[body.index("async def record_review(conn, execution_id"):]
    fn = fn[:fn.index("\ndef ")] if "\ndef " in fn else fn
    assert "record_review_for_story(" in fn, "record_review writes its own row again"
    assert "INSERT INTO story_reviews" not in fn, "there are two writers"


def test_a_story_keyed_call_ends_no_execution():
    """There is nothing running to end. Ending `None` would be a write
    against a row that does not exist."""
    body = code("kernel/worker/review_finish.py")
    fn = body[body.index("async def record_review_for_story("):]
    fn = fn[:fn.index("async def record_review(")]
    assert "if execution_id is not None:" in fn


# ── the fourth skipped_* verdict ─────────────────────────────────────

def test_the_local_exemption_has_its_own_verdict():
    """`skipped_local` rather than reusing `skipped_errand`: "nobody
    reviewed this because it ran on a laptop" is a different fact, and the
    point of recording a reason is that the two can be told apart."""
    from app.services.local_completion import SKIPPED_LOCAL
    assert SKIPPED_LOCAL == "skipped_local"


def test_reaching_reviewed_is_what_clears_the_thread():
    """This is why the exemption matters even though nobody reviewed
    anything: `done` alone leaves the thread bound and the workspace
    protected."""
    body = code("kernel/worker/review_finish.py")
    fn = body[body.index("async def record_review_for_story("):]
    assert "active_thread_id = NULL" in fn
    assert "state = 'reviewed'" in fn


def test_the_done_endpoint_finishes_the_story():
    body = code("app/routers/local_execution_run.py")
    i = body.index('@router.post("/story-done")')
    window = body[i:i + 2500]
    assert "finish_local_story" in window, (
        "the story reaches `done` and stops there")


def test_finishing_runs_after_the_transition_and_cannot_undo_it():
    """The story IS done either way. A failure to summarise it must not be
    reported to the person as a failure to finish it."""
    body = code("app/routers/local_execution_run.py")
    i = body.index('@router.post("/story-done")')
    window = body[i:i + 2500]
    assert window.index("apply_story_transition") < window.index("finish_local_story")


def test_each_half_of_finishing_survives_the_other_failing():
    """A failure to summarise must not stop the story reaching `reviewed`,
    and a failure to reach `reviewed` must not lose the summary."""
    body = src("app/services/local_completion.py")
    fn = body[body.index("async def finish_local_story("):]
    assert fn.count("except Exception") >= 2, (
        "one failure decides that neither happened")


# ── and the GO is told something ─────────────────────────────────────

def test_the_result_summary_never_clobbers_a_better_one():
    """The assistant's own report path writes the same column, so the guard
    is in the statement rather than a read-then-write."""
    body = code("app/services/local_completion.py")
    assert "COALESCE(btrim(result_summary), '') = ''" in body


@pytest.mark.parametrize("dead,live", [
    ("threads WHERE task_id = $1 LIMIT 1", "active_thread_id"),
    ("role = 'assistant'", "role IN ('assistant', 'agent')"),
])
def test_the_go_message_can_find_a_local_storys_conversation(dead, live):
    """Both lookups were dead for every local story, and neither errored:
    a story thread is keyed by `story_id` and local turns are `'agent'`."""
    body = code("app/services/go_events.py")
    i = body.index("Work report from assistant")
    window = body[max(0, i - 2000):i]
    assert live in window, f"the message still cannot find it ({live} missing)"
    assert window.count(dead) <= 1, (
        f"{dead!r} is still the primary lookup rather than a fallback")
