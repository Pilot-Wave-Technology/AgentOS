"""A thread in the sidebar should read as work, not as a hash.

WHAT IT LOOKED LIKE. `label = f"{story_id} — {title}"`, so every errand
row opened with eight characters of hex nobody recognises. A list of
conversations read as a list of ids, and people navigated by opening
things until they found the right one.

THE LABEL IS NOT AN IDENTIFIER, and that distinction is the one thing
these tests exist to keep. `stories.id` addresses a story in every tool,
log and ledger entry. `short_title` is written by a person or a model,
unique only within its goal, and never resolved back to a row. A unique
index makes it usable in a list; it does not make it a key.

THE FALLBACK IS THE MIGRATION STRATEGY. Every story that exists has no
short title, and `start_story` deliberately does not ask for one --
that would be a field for an assistant to get wrong rather than a
decision worth making. Deriving from the real title means nothing is
ever unlabelled and no backfill has to invent names for finished work.
"""

from __future__ import annotations

import io
import re

import pytest

from app.services.tests.shipped_tree import (IS_DISTRIBUTION, baseline_mentions,
                                             create_table, table_has_column)

from kernel.go.short_title import (BadTitle, MAX_WORDS, clash_message,
                                   derive, label_for, validate)


# ── the limit, and why it is refused rather than truncated ───────────

def test_six_words_is_the_limit():
    assert validate("one two three four five six") == "one two three four five six"


def test_seven_words_is_refused_with_a_suggestion():
    """Truncating silently would turn a label into a prefix, and the
    author would never know their name was cut."""
    with pytest.raises(BadTitle) as exc:
        validate("one two three four five six seven")
    msg = str(exc.value)
    assert "too many" in msg
    assert "one two three four five six" in msg, (
        "say what WOULD fit -- a refusal that only says no costs a round "
        "trip to guess at")


def test_an_empty_title_is_refused():
    for blank in ("", "   ", "\n", "***"):
        with pytest.raises(BadTitle):
            validate(blank)


def test_it_tidies_what_a_model_sends():
    """Markdown, quotes and runs of whitespace all arrive from a model
    and none of them belong in a sidebar row."""
    assert validate('  **Fix**   the "login"  redirect ') == "Fix the login redirect"
    assert validate("fix_the_login_redirect") == "fix the login redirect"


def test_the_word_limit_is_one_number():
    assert MAX_WORDS == 6


# ── the fallback chain ───────────────────────────────────────────────

def test_a_missing_label_is_derived_from_the_real_title():
    assert derive("Document the four arithmetic endpoints and their models") \
        == "Document the four arithmetic endpoints and"


def test_label_prefers_the_short_title():
    assert label_for("Fix login redirect", "Some much longer title", "ab12") \
        == "Fix login redirect"


def test_label_falls_back_before_it_reaches_the_id():
    """THE WHOLE POINT. An id in the sidebar is the thing being fixed,
    so it is the last resort and not the second choice."""
    assert label_for(None, "Investigate the intermittent checkout timeout",
                     "ab12") == "Investigate the intermittent checkout timeout"
    assert label_for("", "  ", "ab12") == "ab12"
    assert label_for("", "", "") == "untitled", "never blank"


# ── uniqueness belongs to the database ───────────────────────────────

def test_uniqueness_is_per_goal_and_case_insensitive():
    if IS_DISTRIBUTION:
        assert baseline_mentions(
            r"CREATE UNIQUE INDEX stories_short_title_per_goal ON public\.stories "
            r"USING btree \(goal_id, lower\(short_title\)\) "
            r"WHERE \(short_title IS NOT NULL\);"), (
            "per goal, lowercased, and partial -- or the second story "
            "without a label collides with the first")
        return
    sql = io.open("migrations/story_short_title.sql", encoding="utf-8").read()
    assert "ON stories (goal_id, lower(short_title))" in sql, (
        "per goal, and lowercased -- 'Fix Login' and 'fix login' are the "
        "same row to somebody scanning a list")
    assert "WHERE short_title IS NOT NULL" in sql, (
        "a partial index, or the second story without a label collides "
        "with the first")


def test_it_is_nullable_so_existing_rows_survive():
    if IS_DISTRIBUTION:
        line = re.search(r"^\s+short_title .*$", create_table("stories"),
                         re.MULTILINE)
        assert line, "stories has no short_title column"
        assert "NOT NULL" not in line.group(0), (
            "backfilling would mean inventing names for finished work")
        return
    sql = io.open("migrations/story_short_title.sql", encoding="utf-8").read()
    assert "ADD COLUMN IF NOT EXISTS short_title TEXT" in sql
    assert "NOT NULL" not in sql.split("CREATE UNIQUE INDEX")[0], (
        "backfilling would mean inventing names for finished work")


def test_the_clash_message_names_the_goal_and_asks_for_a_difference():
    msg = clash_message("fix login", "G1")
    assert "fix login" in msg and "G1" in msg
    assert "number" in msg, (
        "'fix login (2)' is not a label anybody can navigate by, which "
        "is the whole purpose")


def test_it_is_in_the_sequence():
    if IS_DISTRIBUTION:
        # Squashed: the migration having run is the column being there.
        assert table_has_column("stories", "short_title")
        return
    seq = io.open("migrations/SEQUENCE", encoding="utf-8").read()
    assert "story_short_title" in seq


# ── the two places a thread gets its name ────────────────────────────

def test_the_errand_thread_no_longer_leads_with_an_id():
    src = io.open("app/services/errand_grants.py", encoding="utf-8").read()
    assert 'f"{story_id} — {title}"' not in src, (
        "this is the id-prefixed name being removed")
    assert "label_for(" in src


def test_the_story_thread_uses_the_label_too():
    src = io.open("app/routers/stories.py", encoding="utf-8").read()
    assert 'f"Story: {title[:80]}"' not in src
    assert "label_for(short_title, title, story_id)" in src


def test_a_clash_on_create_story_is_refused_not_suffixed():
    src = io.open("app/routers/stories.py", encoding="utf-8").read()
    assert "UniqueViolationError" in src
    assert "clash_message" in src


def test_an_approved_errand_is_never_blocked_by_a_clash():
    """A person has already approved it and the story MUST be created.
    Refusing here would lose the approval over a label."""
    src = io.open("app/services/errand_grants.py", encoding="utf-8").read()
    i = src.index("except UniqueViolationError")
    after = src[i:i + 800]
    assert "INSERT INTO stories" in after, (
        "the retry must still create the story, without the label")


def test_the_prompt_teaches_the_rule():
    doc = " ".join(io.open("kernel/go/AGENTS.md", encoding="utf-8")
                   .read().split())
    assert "short_title" in doc
    assert "LABEL, not an identifier" in doc, (
        "the one misunderstanding worth preventing: nothing resolves a "
        "story by this")
