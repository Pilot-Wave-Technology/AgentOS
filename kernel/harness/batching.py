"""H5 — several questions, one interruption.

WHAT THE WINDOW IS FOR, and it is not throughput. Three runs in a goal
hitting a decision within a minute of each other produce three separate
notifications, and a person answers them one at a time with a context
switch between each. The questions are usually related — the same
ambiguity in the same codebase reaching three stories — and answering
them together is both faster and BETTER, because the answers stay
consistent with one another.

A BATCH IS A VIEW, NOT A QUEUE. Each question is still its own row, still
answerable on its own, and still parks exactly one execution. `batch_id`
groups them for presentation; nothing about the run depends on it. That
matters because the alternative — holding questions back until a batch
fills — would mean a run waits on an unrelated run's timing, and a single
question at 3am would wait ninety seconds for company that never comes.

So: questions are recorded IMMEDIATELY and grouped RETROSPECTIVELY. The
window says "these arrived close enough together to show as one card",
never "wait and see".

SCOPE IS THE GOAL, not the execution. The one-open-request index already
means an execution can have at most one question outstanding, so batching
within an execution would be batching a set of size one. What a person
actually faces is every open question across a goal.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# How close together two questions must arrive to share a card. Ninety
# seconds: long enough that a person answering one has not yet moved on,
# short enough that an unrelated question an hour later is its own
# interruption rather than an item appended to a card already read.
DEFAULT_WINDOW_S = 90.0


async def assign_batch(conn, question_id: int, goal_id: str | None,
                       window_s: float = DEFAULT_WINDOW_S) -> str | None:
    """Group this question with any recent unanswered sibling.

    Returns the batch id it joined or created, or None when there is no
    goal to scope by — a question with no goal is its own interruption
    and grouping it with strangers would be worse than not grouping.

    RETROSPECTIVE. The row already exists and its execution is already
    parked before this runs. If this function fails outright the question
    is still asked and still answerable; the person simply sees two cards
    instead of one, which is the correct direction for a presentation
    concern to fail in.
    """
    if not goal_id:
        return None

    existing = await conn.fetchval(
        """
        SELECT q.batch_id
          FROM questions q
          JOIN executions e ON e.id = q.execution_id
         WHERE e.goal_id = $1
           AND q.id <> $2
           AND q.status = 'OPEN'
           AND q.batch_id IS NOT NULL
           AND q.created_at > now() - ($3 || ' seconds')::interval
         ORDER BY q.created_at DESC
         LIMIT 1
        """,
        goal_id, question_id, str(int(window_s)))

    batch = existing or f"batch-{question_id}"
    await conn.execute(
        "UPDATE questions SET batch_id = $2 WHERE id = $1",
        question_id, batch)
    if existing:
        logger.info("question %s joined batch %s (within %.0fs)",
                    question_id, batch, window_s)
    return batch


async def card(conn, batch_id: str) -> dict:
    """Everything a person needs to answer one card.

    Returns the open questions in the batch, oldest first, each with the
    execution it parks. ANSWERED ONES ARE EXCLUDED: a card re-opened
    after two of three were answered must show the one still waiting, not
    re-present work already done.
    """
    rows = await conn.fetch(
        """
        SELECT q.id, q.execution_id, q.kind, q.question_text,
               q.blocking_on, q.reversible, q.created_at
          FROM questions q
         WHERE q.batch_id = $1 AND q.status = 'OPEN'
         ORDER BY q.created_at
        """,
        batch_id)
    return {"batch_id": batch_id, "count": len(rows),
            "questions": [dict(r) for r in rows]}


async def open_cards(conn, goal_id: str) -> list:
    """Open questions in a goal, as cards rather than as a flat list.

    One row per batch, so a caller rendering a person's inbox shows one
    entry for three related questions instead of three entries that will
    be answered in one sitting anyway.
    """
    rows = await conn.fetch(
        """
        SELECT COALESCE(q.batch_id, 'q-' || q.id::text) AS batch_id,
               count(*) AS n, min(q.created_at) AS opened_at
          FROM questions q
          JOIN executions e ON e.id = q.execution_id
         WHERE e.goal_id = $1 AND q.status = 'OPEN'
         GROUP BY 1
         ORDER BY min(q.created_at)
        """,
        goal_id)
    return [dict(r) for r in rows]
