"""Which stories are somebody's work, and which are the engine's plumbing.

MOVED HERE FROM `app/models/story.py`, WHICH NOW RE-EXPORTS IT. The
kernel may not import from `app/`, and the acceptance seam —
`kernel/harness/park_resolve.py` — has to answer this question to decide
whether a finished run owes a review. Copying the predicate into the
kernel would have reproduced exactly the drift its own docstring was
written about: two callers once tested different conditions, so a merge
story authored by anything but 'engine' was skipped by the council AND
sat visibly in the Done column for ever with nothing to explain it.

So there is one definition, in the layer both sides can import.
"""

from __future__ import annotations

from collections.abc import Mapping

# The three columns the predicate reads. Named so a caller fetching a
# row for this purpose cannot select the wrong ones — the version of
# this bug that mattered was a caller checking only `created_by`.
COLUMNS = ("created_by", "story_kind", "merge_group_id")


def is_engine_git_story(story: Mapping[str, object]) -> bool:
    """True if this story is a mechanical git operation the ENGINE authored
    (merge / diff / fix), not human-level goal work.

    This is the ONE definition of "engine git story", deliberately shared
    by the three places that must agree on it:

      * ``kernel.worker.finish`` — these are never reviewed, so a
        finished run owes no review and goes straight to DONE.
      * ``council.runs.enqueue_story_review`` — these were never
        council-reviewed either, for the same reason plus one of its
        own: a done->reviewed move would make the SHCA workspace
        reapable MID-GRIND, pulling the branch out from under the next
        diff/fix turn.
      * ``routers.stories`` board bucketing — they render under the
        'reviewed' column so it reads as the terminal column for every
        story.

    Accepts any mapping (asyncpg Record or dict) so callers can pass a DB
    row directly. Missing keys are treated as "not an engine story".

    NOTE WHAT IS NOT HERE: `work_kind`. An ERRAND is not an engine git
    story — it is human-level work with a different shape — and it is
    excluded from review on its own test, read from `executions.kind`
    rather than the story row "so it cannot disagree with what
    happened". Folding errands in here would make this predicate mean
    two things and hide the second.
    """
    return (
        story.get("created_by") == "engine"
        or story.get("story_kind") == "merge"
        or story.get("merge_group_id") is not None
    )
