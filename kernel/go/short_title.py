"""The six-word label a story is known by in the UI.

WHY IT EXISTS. A thread row in the sidebar showed a story id. Nobody
recognises `4641396e` as the endpoint documentation they asked for an
hour ago, so a list of conversations reads as a list of hashes and
people navigate by opening things until they find the right one.

IT IS A LABEL AND NOT AN IDENTIFIER. `stories.id` addresses a story
everywhere — tools, engine, ledger, logs. This is never resolved back to
a row, and nothing may start doing so. A human-authored name two people
could plausibly pick is not a key, whatever the unique index says.

SIX WORDS, AND THE LIMIT IS THE POINT. The row it lives in is narrow;
anything longer is truncated by the browser, which turns a label into a
prefix. Refusing at seven is kinder than rendering "investigate the
intermittent checkout timeout that appea…".

DERIVED WHEN ABSENT, never blank. Every story predating this has none,
and so does anything an assistant creates — `start_story` does not ask
for one, deliberately, because that is a field for an assistant to get
wrong rather than a decision worth making. `derive` takes the first six
words of the real title, so those threads are readable without a
backfill inventing names for finished work.
"""

from __future__ import annotations

import re

MAX_WORDS = 6

# Collapse anything that is not a word character or an inner hyphen.
# Markdown, quotes and trailing punctuation all reach this from a model.
_TIDY = re.compile(r"[^\w\s-]+")
_SPACE = re.compile(r"\s+")


def clean(raw: str) -> str:
    """Normalise a candidate label. Does not judge its length."""
    text = _TIDY.sub(" ", (raw or "").replace("_", " "))
    return _SPACE.sub(" ", text).strip()


def words(raw: str) -> list[str]:
    return [w for w in clean(raw).split(" ") if w]


def derive(title: str) -> str:
    """A label from a full title, for anything that did not supply one.

    NEVER RETURNS EMPTY for a non-empty title: an empty label would put
    the caller straight back to showing an id, which is the thing this
    exists to stop.
    """
    got = words(title)[:MAX_WORDS]
    return " ".join(got)


class BadTitle(ValueError):
    """The label cannot be used, and why, in words a model can act on."""


def validate(raw: str) -> str:
    """The cleaned label, or `BadTitle` saying what to change.

    UNIQUENESS IS NOT CHECKED HERE. It belongs to the database, which is
    the only place it can be decided without a race: two stories created
    in the same second would both pass a read-then-write check. The
    caller catches the unique violation and turns it into a refusal
    naming the clash.
    """
    got = words(raw)
    if not got:
        raise BadTitle(
            "a short title is required: up to six words naming this work, "
            "the way a person would refer to it in conversation. It is the "
            "label on the thread in the sidebar, where the alternative is "
            "a story id nobody recognises.")
    if len(got) > MAX_WORDS:
        raise BadTitle(
            f"{len(got)} words is too many; {MAX_WORDS} at most. The thread "
            f"row is narrow and the browser truncates the rest, which turns "
            f"a label into a prefix. Yours: {' '.join(got)!r} — try "
            f"{' '.join(got[:MAX_WORDS])!r}.")
    return " ".join(got)


def clash_message(label: str, goal_id: str) -> str:
    """What to say when the goal already has one by this name."""
    return (
        f"{label!r} is already the short title of another story in "
        f"{goal_id}. They have to differ or the sidebar shows two "
        f"identical rows, which is the one thing this label exists to "
        f"prevent. Name what makes THIS one different — the component, "
        f"the symptom, the repository — rather than adding a number.")


def label_for(short: str | None, title: str | None,
              story_id: str = "") -> str:
    """What the UI shows for this story's thread. Never an id alone.

    THE FALLBACK CHAIN IS THE MIGRATION STRATEGY. Every story predating
    `short_title` has none, and so does anything an assistant creates.
    Deriving from the real title means those threads read correctly
    without backfilling names onto finished work.

    The id is the last resort and appears only when a story has no title
    at all, which should not happen -- but a blank row in the sidebar is
    worse than a hex id, because at least the id can be looked up.
    """
    if (short or "").strip():
        return (short or "").strip()
    got = derive(title or "")
    return got or (story_id or "").strip() or "untitled"
