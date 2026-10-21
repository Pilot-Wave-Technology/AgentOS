"""The executor answering a person, without stopping to do it.

WHY THIS EXISTS BESIDE THE ANSWERER. `kernel/answerer` reconstructs: it
reads the record, the tree and the journal, and infers what the executor
was doing. That is the right and only way to answer a story with no live
leg -- five of the seven askable states have none, and a park ends the
leg, so there is genuinely no mind there.

What it cannot reach is the half that was never written down. What the
executor TRIED AND REJECTED, what it is about to do next, why it read an
error the way it did: none of that is in the journal, and no amount of
reading the tree recovers it. For "what are you doing and why", the
model is the only source.

WHAT THIS IS NOT. It is not a park, not a claim, not a gate, and not an
effect. Nothing about the plan, the checks or the workspace moves
because a person asked a question -- the same rule `answerer/prompt`
states for its own two endings: "answering may narrow what the executor
is allowed to do and may never widen it". Here it does neither.

AND IT DOES NOT STOP A JOB. A question can arrive mid-`bash_wait`,
because `_wait_for_job` peeks the mailbox once a slice; the wait ends,
the JOB does not. Nothing in this file touches `jobcore`, and the
message the model reads says so twice.
"""

from __future__ import annotations

import logging

from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)

MAX_ANSWER = 8000


async def reply_to_human(conn, execution_id: int, tool_args: dict,
                         tool_call_id: str) -> ToolResult:
    """Record the executor's answer. Never parks, never fails the leg."""
    answer = str(tool_args.get("answer") or "").strip()
    if not answer:
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error="reply_to_human: `answer` is required. If you do not "
                  "know, say that -- it is a useful answer.",
            exit_code=1)

    request_id = tool_args.get("request_id")
    try:
        request_id = int(request_id) if request_id is not None else None
    except (TypeError, ValueError):
        request_id = None

    # THE QUESTION IT IS ANSWERING, found rather than trusted. The model
    # is given the id in the message and usually passes it back; when it
    # does not, the oldest unanswered `ask` on this run is the only
    # candidate, because `_claim`-style delivery hands them over one at
    # a time and a second cannot arrive before the first is answered.
    row = await conn.fetchrow(
        """
        SELECT p.id, p.actor_id, p.payload->>'question' AS question,
               p.payload->>'asked_by' AS asked_by
          FROM proposals p
         WHERE p.execution_id = $1 AND p.kind = 'ask'
           AND ($2::bigint IS NULL OR p.id = $2)
           AND NOT EXISTS (SELECT 1 FROM answers a
                            WHERE a.proposal_id = p.id)
         ORDER BY p.id
         LIMIT 1
        """,
        execution_id, request_id)

    if row is None:
        # NOT AN ERROR THE LEG SHOULD CARRY. A model that answers twice,
        # or answers after the asker gave up and the question was
        # written off, has done nothing wrong -- and failing the call
        # would invite it to retry or to go looking for the problem.
        return ToolResult(
            tool_call_id=tool_call_id,
            output="There is no question waiting for an answer on this "
                   "run, so nothing was recorded. If you already "
                   "answered one, that answer stands. Carry on with "
                   "your work.",
            exit_code=0)

    answer_id = await conn.fetchval(
        """
        INSERT INTO answers (execution_id, proposal_id, asked_by, question,
                             answer, looked_at, held, failed, hold_declined)
        VALUES ($1, $2, $3, $4, $5, '', false, false, false) RETURNING id
        """,
        execution_id, row["id"],
        row["asked_by"] or row["actor_id"] or "",
        row["question"] or "", answer[:MAX_ANSWER])

    logger.info("execution %s answered question %s from its own leg",
                execution_id, row["id"])
    return ToolResult(
        tool_call_id=tool_call_id,
        output=(f"Answered; the person has it (answer {answer_id}). "
                f"Nothing about your work changed. Carry on from exactly "
                f"where you were -- and if you were waiting on a "
                f"background job, it is still running: call bash_wait on "
                f"the same job_id again."),
        exit_code=0)
