"""One spelling of a priority, in Python and in SQL, agreeing.

PRIORITY IS SCORED NOW -- Throughput is half priority points, low 1 to
critical 8 -- so an unrecognised spelling is not untidy, it is two
points taken off the person who did the work. Five writers took the
value from a human or a model and stored it as given.

THE RULE EXISTS TWICE and that is the risk being managed here: once in
`normalize_priority`, for the writers, and once as SQL, for the
migration and the materialized view. Two copies of a rule drift. So one
table of inputs runs through both, and the test fails the moment they
disagree -- which is the only reason the SQL copy is allowed to exist.
"""
from __future__ import annotations

from app.services.story_priority import (
    DEFAULT_PRIORITY,
    VALID_PRIORITIES,
    normalize_priority,
    priority_sql,
)

#: input -> what it must become. The odd ones are real: `'h igh'` is a
#: model emitting a space mid-word, `'Medium\n'` is a form field, and
#: `'blocker'`/`'urgent'`/`'info'` are words people reach for that this
#: system has never had.
CASES: list[tuple[object, str]] = [
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("critical", "critical"),
    ("High", "high"),
    (" high ", "high"),
    ("h igh", "high"),
    ("CRITICAL", "critical"),
    ("Medium\n", "medium"),
    ("\tLow", "low"),
    ("blocker", "medium"),
    ("info", "medium"),
    ("urgent", "medium"),
    ("", "medium"),
    (None, "medium"),
    ("p1", "medium"),
]


def test_every_input_has_the_expected_answer():
    for given, want in CASES:
        assert normalize_priority(given) == want, f"{given!r}"


def test_the_fallback_is_medium_and_is_itself_valid():
    """Not `low`. A story somebody bothered to file is more likely
    ordinary than negligible, and demoting it would under-count the
    person who did it."""
    assert DEFAULT_PRIORITY == "medium"
    assert DEFAULT_PRIORITY in VALID_PRIORITIES


def test_a_non_string_still_gets_an_answer():
    """Total by design: a writer that got a surprise must still store
    something the CHECK constraint accepts."""
    for odd in (3, 3.5, True, [], {}):
        assert normalize_priority(odd) in VALID_PRIORITIES


def test_normalising_twice_changes_nothing():
    for given, _ in CASES:
        once = normalize_priority(given)
        assert normalize_priority(once) == once


def test_the_sql_agrees_with_the_python_on_every_case():
    """The two copies of the rule, run against each other.

    Skips when no database is reachable -- the Python half above is
    still checked, and this half is about agreement, not about either
    one being right on its own.
    """
    import asyncio

    import pytest

    from app.services.tests.test_story_repo_scope_sql import _connect_or_skip

    async def go():
        conn = await _connect_or_skip()
        try:
            for given, want in CASES:
                got = await conn.fetchval(
                    f"SELECT {priority_sql('$1::text')}",
                    None if given is None else str(given))
                assert got == want, (
                    f"SQL says {got!r} for {given!r}, Python says "
                    f"{normalize_priority(given)!r}")
        finally:
            await conn.close()

    try:
        asyncio.run(go())
    except Exception as exc:                                # noqa: BLE001
        if "skip" in type(exc).__name__.lower():
            pytest.skip(str(exc))
        raise
