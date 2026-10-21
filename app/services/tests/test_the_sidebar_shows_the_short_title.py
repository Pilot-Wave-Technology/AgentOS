"""Story and errand rows in the sidebar read as words, not as a hex id.

WHAT WAS WRONG, and it survived the feature that was supposed to fix it.
The six-word short title shipped with `label_for`, and it was wired into
the two places that WRITE `threads.title`. The sidebar does not read
`threads.title` for story or errand rows: `assistant_story_threads` and
`go_errand_threads` join the story, and both row builders rendered a
hardcoded

    <span class="sidebar-label truncate"># {story_id}</span>

with the real title only in the hover tooltip. So every row still read
`# 2dd043d4` however good the label stored against it was -- reported
from the live UI on 2026-09-24, on an errand whose `short_title` was
correctly set and whose thread title was correctly written.

WHY THE FIX IS AT READ TIME. A stored label would need a backfill for
every existing row and a writer on every creation path -- four of them,
two of which already disagreed. Deriving the label where the row is
rendered corrects the whole history at once and leaves exactly one
place to be right.

THE ID DOES NOT DISAPPEAR. It stays in the tooltip and it still
addresses the story in every tool, log and message -- `short_title.py`
is explicit that the label is not an identifier, and a sidebar that
made the id unfindable would be the opposite mistake.
"""

from __future__ import annotations

import io
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC = io.open(ROOT / "app/services/sidebar_threads.py", encoding="utf-8").read()


def _code(text: str) -> str:
    no_doc = re.sub(r'""".*?"""', "", text, flags=re.S)
    return "\n".join(line for line in no_doc.splitlines()
                     if not line.lstrip().startswith("#"))


def _row(**kw) -> dict:
    base = {"id": "t1", "story_id": "2dd043d4", "state": "done",
            "title": "Research candidate calculator operators",
            "short_title": None}
    base.update(kw)
    return base


# ── the label itself ─────────────────────────────────────────────────

def test_the_short_title_wins():
    from app.services.sidebar_threads import sidebar_label

    assert sidebar_label(_row(short_title="Add power operator")) == (
        "Add power operator")


def test_a_story_without_one_falls_back_to_its_real_title():
    """Nothing backfills names onto finished work, so the overwhelming
    majority of rows take this path. Six words of the real title is a
    readable label; an id is not."""
    from app.services.sidebar_threads import sidebar_label

    assert sidebar_label(_row()) == "Research candidate calculator operators"


def test_the_id_is_the_last_resort_and_not_the_default():
    """THE BUG, stated as an assertion. The id was not a fallback here --
    it was the only thing ever rendered."""
    from app.services.sidebar_threads import sidebar_label

    assert sidebar_label(_row(title="", short_title="")) == "2dd043d4"
    assert sidebar_label(_row()) != "2dd043d4"


def test_a_long_title_is_cut_to_six_words():
    from app.services.sidebar_threads import sidebar_label
    from kernel.go.short_title import MAX_WORDS

    label = sidebar_label(_row(
        title="one two three four five six seven eight nine"))
    assert len(label.split()) == MAX_WORDS


# ── both rows, not one ───────────────────────────────────────────────

def test_neither_row_builder_renders_a_bare_id():
    """Two builders, one for the assistant's story rows and one for the
    GO's errand rows. Fixing one and not the other is how half a sidebar
    stays unreadable -- and they are 130 lines apart."""
    code = _code(SRC)
    assert 'sidebar-label truncate"># {_html.escape(story_id)}' not in code, (
        "a row builder still renders the story id as its label")
    assert code.count("sidebar_label(row)") == 2, (
        "both the story row and the errand row must use the helper")


def test_both_queries_select_the_short_title():
    """The label cannot prefer a column the query never fetched, and a
    missing key reads as None -- which falls back silently and looks
    exactly like a story that has no short title."""
    assert SRC.count("s.short_title") == 2, (
        "assistant_story_threads and go_errand_threads must both select it")


def test_the_id_is_still_reachable_on_the_row():
    """`short_title.py` is explicit that this is a label and not an
    identifier. Dropping the id from the row entirely would make the
    thing every tool is keyed by unfindable from the UI."""
    code = _code(SRC)
    assert code.count('f"{story_id} — {title} ({state_word})"') == 2, (
        "the tooltip must still carry the story id")


# ── and the writer agrees with the reader ────────────────────────────

def test_the_story_thread_writer_uses_the_label_too():
    """`threads.title` is read by other surfaces (the conversations list,
    the chat header). It was written as `f"{story_id} — {title}"`, which
    put eight hex characters in front of the readable part on a row the
    browser truncates.

    THE WRITER MOVED. It was `app/routers/assistant.py::_ensure_story_thread`
    and is now `app/services/story_threads.py`, because the local-execution
    lane needs the same resolver and a service cannot import a router. The
    invariant is unchanged; only where it is enforced. Asserted on the
    service, and the router is checked to be delegating rather than keeping
    a second writer.
    """
    src = io.open(ROOT / "app/services/story_threads.py", encoding="utf-8").read()
    code = _code(src)
    assert 'f"{story_id} — {story_title}"' not in code
    assert "label_for(" in code

    router = _code(io.open(ROOT / "app/routers/assistant.py", encoding="utf-8").read())
    assert "INSERT INTO threads" not in router, (
        "the router writes threads again — there must be ONE writer, or the "
        "two lanes can disagree about the title the way they disagreed about "
        "the key")
