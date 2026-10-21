"""One spelling of a story's priority, whoever wrote it.

PRIORITY IS ABOUT TO CARRY WEIGHT. The taximeter scores Throughput half
on priority points -- low 1, medium 3, high 5, critical 8 -- so a story
stored as `'High '` or `'h igh'` is not a cosmetic blemish any more: it
falls through to the default and quietly costs somebody two points.

FIVE WRITERS, ONE RULE. The board, the internal API, GO's create and
update, and the Slack command each take a value from a human or a model
and store it as given. They normalise through here instead, so the rule
lives in one place and the database can be given a CHECK constraint that
none of them trips.

WHITESPACE ANYWHERE, NOT JUST THE ENDS. `'h igh'` is a real thing a
model emits, and `strip()` does not catch it. Case is folded, every
space removed, and anything still unrecognised becomes `medium` -- the
same fallback the readers already apply, now applied once at the write
instead of differently at each read.
"""

from __future__ import annotations

#: The only values a story's priority may hold.
VALID_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "critical")

#: What an unrecognised value becomes. Not `low`: a story somebody
#: bothered to file is more likely ordinary than negligible, and
#: silently demoting it would under-count the person who did it.
DEFAULT_PRIORITY = "medium"


def normalize_priority(value: object) -> str:
    """`' High '`, `'h igh'`, `'HIGH'` -> `'high'`; anything else -> `'medium'`.

    Total: every input has an answer, including `None` and a non-string,
    because a writer that got a surprise should still store something
    the readers and the CHECK constraint accept.
    """
    if value is None:
        return DEFAULT_PRIORITY
    folded = "".join(str(value).split()).lower()
    return folded if folded in VALID_PRIORITIES else DEFAULT_PRIORITY


#: The same rule in SQL, for the migration and the materialized view.
#:
#: KEPT BESIDE THE PYTHON ON PURPOSE. Two copies of a rule drift; two
#: copies in one file drift visibly, and `test_story_priority.py` runs
#: the same table of inputs through both and fails when they disagree.
PRIORITY_SQL_CASE = """
    CASE lower(replace(replace(replace(COALESCE({col}, ''), ' ', ''),
                               chr(9), ''), chr(10), ''))
        WHEN 'low'      THEN 'low'
        WHEN 'medium'   THEN 'medium'
        WHEN 'high'     THEN 'high'
        WHEN 'critical' THEN 'critical'
        ELSE 'medium'
    END
"""


def priority_sql(col: str = "priority") -> str:
    """The normalising CASE for `col`, as one line of SQL."""
    return PRIORITY_SQL_CASE.format(col=col)
