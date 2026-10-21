"""Which runnable execution gets the next leg slot. C3.

WHAT WAS TRUE BEFORE. `claimable` ended:

    ORDER BY e.id

and `worker_tick` then took `rows[:max_concurrent]`. No offset, no
rotation, no memory of who ran last. So with more runnable executions
than leg slots, the lowest id re-won the slot it had just freed, on every
pass, for ever — and the high-id rows were re-read, re-sorted and
re-discarded four seconds later, indefinitely. Not "slow": never. A goal
whose bucket admits four stories and a loop with one slot ran story one
to completion while stories two, three and four sat RUNNING and idle,
which from outside is three stories that "never start".

THE KEY IS ALREADY IN THE DATABASE. `_legs_taken` counts DISTINCT
generations out of `step_journal`, so the journal already records
per-execution activity — and `step_journal.ts` is `timestamptz NOT NULL
DEFAULT now()`. `max(ts)` per execution IS "when this run last did
something". No column was added and no migration was written: a new
`last_advanced_at` would have to be maintained by every path that
advances a run (the leg, the verdict, the un-park, the answer delivery),
and the first one somebody forgets re-creates this bug with a column
that claims otherwise.

NULLS FIRST, so a run that has never journalled a step goes ahead of one
that has. A newly dispatched execution has an empty journal, and the
alternative — treating "no activity" as "infinitely recent" — would
starve exactly the runs that have never run at all.

WHY THE ORDER IS DECIDED HERE AND NOT IN `ORDER BY`. `claimable` has no
LIMIT: it returns every runnable row and the caller truncates. So sorting
in the database buys nothing a `sorted()` does not, and costs the one
rule that decides WHO RUNS its only reachable test — the starvation
shipped precisely because `ORDER BY e.id` is unreviewable except by
reading it, and no test could ask it a question without a live Postgres.
The query still returns `ORDER BY e.id` so the input to the sort is
itself deterministic, whatever plan the planner picks.

DETERMINISM IS NOT DECORATION. `admit.py` says it in as many words about
its own FIFO — "created_at is tx-start time — identical within a batch.
id breaks ties deterministically (S7). Do not remove ', id'." The same
argument holds here and is stronger: two replicas ordering one row set
differently would hand the same slot to different executions and undo
the rotation this exists to create. Ties break on `id`, always.
"""

from __future__ import annotations

import datetime as _dt

# The floor for "has never advanced". A constant of the same type as the
# column so the sort key never compares a datetime with None — which is
# a TypeError, raised inside the worker sweep, taking every story with it.
NEVER = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

# The fairness key, as SQL. A correlated subselect rather than a join and
# a GROUP BY: `claimable` returns one row per RUNNING execution — tens,
# not thousands — and `step_journal_seq_unique (execution_id, seq)` makes
# each lookup an index scan of one run's own rows.
LAST_ADVANCED_SQL = ("(SELECT max(j.ts) FROM step_journal j "
                     "WHERE j.execution_id = e.id)")

# The column the subselect lands in, and the key this module sorts on.
# Named once so the query and the sort cannot drift apart.
LAST_ADVANCED = "last_advanced_at"


def _key(row: dict):
    """(never-advanced first, then oldest activity, then id)."""
    when = row.get(LAST_ADVANCED)
    return (when is not None, when or NEVER, row["id"])


def least_recently_advanced(rows: list[dict]) -> list[dict]:
    """The runnable rows, in the order the next slot should be offered.

    Total and deterministic: `id` is unique, so no two rows compare equal
    and the result does not depend on the order they arrived in.
    """
    return sorted(rows, key=_key)


def forget_the_key(rows: list[dict]) -> list[dict]:
    """Drop the fairness column once it has done its job.

    The row goes on to `make_executor`, the sandbox spec and the
    executor's context, and every one of those treats it as a bag of
    plain values. A `datetime` that only scheduling cares about has no
    business travelling that far — this is the same reason the bridge
    port and the connection are put on the row HERE and not in the query.
    """
    for row in rows:
        row.pop(LAST_ADVANCED, None)
    return rows
