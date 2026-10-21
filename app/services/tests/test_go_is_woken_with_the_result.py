"""GO is woken with the result, not a moment before it.

OBSERVED ON STORY 81ee97ea. The goal orchestrator, asked whether it had the
story result, answered: "The system completion notification listed: Result:
No result summary" -- and then found the summary itself by reading
`goal_state` separately. The record had it. The notification did not.

THE ORDER WAS THE BUG. `apply_story_transition(..., "done", ...)` is what
wakes GO, and the wake carries `stories.result_summary` as it reads at that
instant. `finish_local_story` wrote the summary AFTERWARDS, so the wake
always carried an empty one -- every local story, every time. A pod story
does not have this problem because the kernel's completion machinery writes
the summary as part of finishing.

Nothing in composing it needs the story to be `done`: it reads the approved
plan and the diff approval, both of which exist before the transition.
"""
from __future__ import annotations

import pathlib


SRC = (pathlib.Path(__file__).resolve().parents[3]
       / "app" / "routers" / "local_execution_run.py").read_text(encoding="utf-8")


def _done_handler() -> str:
    start = SRC.index('@router.post("/story-done")')
    return SRC[start:SRC.index('@router.post("/done-check")')]


def test_the_summary_is_written_before_the_wake():
    body = _done_handler()
    summary_at = body.index("record_result(")
    wake_at = body.index("apply_story_transition(")
    assert summary_at < wake_at, (
        "GO is woken before the result summary exists, so the completion "
        "notification says 'No result summary' for every local story"
    )


def test_composing_the_summary_cannot_stop_the_story_finishing():
    # A story that cannot describe itself is still finished.
    body = _done_handler()
    chunk = body[body.index("record_result("):body.index("apply_story_transition(")]
    assert "except" in chunk, (
        "a failure to summarise now blocks the done transition"
    )


def test_the_review_exemption_still_runs_after_the_transition():
    # `mark_reviewed_as_local` cannot run before the story is done, so
    # `finish_local_story` must stay on the far side of the transition.
    body = _done_handler()
    assert (body.index("finish_local_story(get_pool()")
            > body.index("apply_story_transition(")), (
        "the review exemption moved ahead of the done transition"
    )


def test_record_result_only_fills_an_empty_field():
    # It is now called TWICE -- once before the wake and once inside
    # `finish_local_story`. The second must not overwrite the first, and
    # neither must ever clobber a summary a person wrote themselves.
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "services" / "local_completion.py").read_text(encoding="utf-8")
    fn = src[src.index("async def record_result("):][:1800]
    assert "COALESCE(btrim(result_summary), '') = ''" in fn, (
        "record_result does not guard on the field being empty, so calling it "
        "twice -- or after a person wrote their own -- overwrites it"
    )
