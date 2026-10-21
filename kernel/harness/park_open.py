"""Opening a park, and retiring one the run will never get an answer to.

SPLIT OUT OF `park.py` (plan 11.5c). This module owns the WRITE side of
the wait: the state transition into `PARKED_HOT`, the two best-effort
side-effects that hang off it, and the one path that closes an open
question without anybody answering it.

WHAT IT DELIBERATELY DOES NOT OWN. Reading the question back is
`park_read`; the human's reply and the un-park are `park_resolve`. That
line is not cosmetic — everything here runs while the executor still
holds the leg, and everything in `park_resolve` runs from the mailbox
worker minutes or hours later, in a different process.

THE ORDER IN `open_request` IS LOAD-BEARING and is stated again at each
site: ask, park, THEN batch and notify. Both tails are presentation, both
are wrapped, and a failure in either must never stop a run from asking.
"""

from __future__ import annotations

import json
import logging

from kernel.harness.park_kinds import _KIND

logger = logging.getLogger(__name__)

async def open_request(conn, execution_id: int, generation: int, kind: str,
                       payload: dict, content_hash: str | None = None):
    """Record what the run is waiting on, and park it.

    Returns the request id, or an `Invalid` when the ask is not
    well-formed — the caller hands that refusal to the model, which can
    read it and correct itself. A malformed ask must NOT park the run:
    parking is what costs a human their attention.

    The row is written by `kernel.mailbox.api.ask_question`, which is the
    only writer. E6i built a parallel `human_requests` table with its own
    insert and no evidence floor; that table is gone, its rows came
    across, and this function now owns exactly the one thing the mailbox
    does not — the state transition.

    A second open request on the same execution is refused by
    questions_one_open: the database declines the state rather than the
    caller remembering not to create it.
    """
    from kernel.harness.batching import assign_batch
    from kernel.mailbox.api import ask_question

    result = await ask_question(
        conn, execution_id, _KIND.get(kind, "clarification"),
        payload.get("question") or "plan approval", payload,
        generation=generation, content_hash=content_hash)
    if not isinstance(result, int):
        # An Invalid. The run keeps running: it was told what is missing.
        logger.info("execution %s asked badly (%s) and was not parked",
                    execution_id, result.code)
        return result

    if payload.get("checks"):
        # F11. On the row rather than buried in the payload. The original
        # reason was the dry-run pass, which found plans with unproven
        # checks by index instead of parsing every payload in the table;
        # E9 retired that pass (plan 5.6) and the column OUTLIVED it,
        # because two live readers still want it as a column and not as
        # JSON inside another column: `worker/adopt.py` adopts the checks
        # the moment the plan is approved, and `console/api.py` ships them
        # with the card so a person approves the plan and its definition
        # of done in one act. The write stays; only its old justification
        # went. Do not "clean this up" alongside `dryrun`.
        await conn.execute(
            "UPDATE questions SET checks = $2::jsonb WHERE id = $1",
            result, json.dumps(payload["checks"]))

    await conn.execute(
        # PARKED_HOT: the sandbox stays up for the life of the story, so a
        # resumed leg finds its workspace and caches intact.
        "UPDATE executions SET state = 'PARKED_HOT' WHERE id = $1",
        execution_id,
    )

    # H5: group with any sibling asked in the last ninety seconds, so a
    # person sees one card rather than three notifications.
    #
    # AFTER the park, and best-effort. This is a PRESENTATION concern: if
    # it fails the question is still asked, still answerable, and the
    # person simply sees two cards instead of one. Letting it raise here
    # would mean a grouping bug could stop a run from asking at all,
    # which trades a real capability for a cosmetic one.
    try:
        goal_id = await conn.fetchval(
            "SELECT goal_id FROM executions WHERE id = $1", execution_id)
        await assign_batch(conn, result, goal_id)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("question %s not batched (%s); it is still asked",
                       result, e)

    # I5g — TELL GO AN ERRAND HAS STOPPED.
    #
    # "GO watches over the errand" was a poll GO had no reason to run.
    # A card goes to whoever is driving; nothing went to the planner, so
    # an errand parked on a clarification GO could answer in a sentence
    # waited for a person to notice a thread they were not watching.
    #
    # ERRANDS ONLY. A story's park already has an assistant whose whole
    # job is that story; adding GO there would put two agents on one
    # question and change how story executions behave, which this stage
    # is explicitly not allowed to do.
    #
    # BEST-EFFORT, AFTER THE PARK, like the batching above and for the
    # same reason: a notification failing must never stop a run from
    # asking. The listener has a poll backstop, so a lost NOTIFY is a
    # late wake rather than none.
    try:
        await _notify_parked(conn, execution_id, result, kind)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("question %s: GO not notified (%s); the backstop "
                       "will catch it", result, e)

    logger.info("execution %s parked on %s request %s",
                execution_id, kind, result)
    return result


async def _notify_parked(conn, execution_id: int, question_id: int,
                         kind: str) -> None:
    """NOTIFY `go_execution_parked`, for an errand and nothing else."""
    row = await conn.fetchrow(
        "SELECT goal_id, kind FROM executions WHERE id = $1", execution_id)
    if row is None or row["kind"] != "errand":
        return
    await conn.execute(
        "SELECT pg_notify('go_execution_parked', $1)",
        json.dumps({"goal": row["goal_id"], "execution": execution_id,
                    "question": question_id,
                    "kind": _KIND.get(kind, "clarification")}))


async def apply_hold(conn, execution_id: int, generation: int,
                     hold: dict) -> int | None:
    """Stop the leg on a person's decision, and park until they make it.

    THE RUN IS NOT BROKEN AND IT IS NOT OVER. This is the same park a
    plan approval takes — PARKED_HOT, sandbox up, journal intact — so
    the resume path, the cancel edge and the card sweeper are the ones
    that already exist. The only new thing is who opened the question:
    the executor did not, and the message it gets on resume says so.

    WHY IT PARKS RATHER THAN JUST RE-GATING. Withdrawing the plan
    approval would have blocked the executor's writes and left it
    RUNNING — burning legs discovering it was blocked, while the console
    said the story was working and the person who asked the question saw
    no reason it should stop. A stop a person can see, and undo, is
    worth more than a quiet one.

    Returns the question id, or None when the park was refused — which
    the caller must treat as "the run carries on", because it does.
    """
    payload = {"from": (hold.get("from") or "").strip(),
               "question": (hold.get("decision") or "").strip(),
               "decision": (hold.get("decision") or "").strip(),
               "answer": (hold.get("answer") or "").strip(),
               "asked": (hold.get("question") or "").strip(),
               "options": (hold.get("options") or "").strip(),
               "why_now": (hold.get("why_now") or "").strip()}
    # A CONTENT HASH THAT IS UNIQUE PER HOLD. With none, `open_request`
    # falls back to matching question TEXT against answered questions — and
    # every pause says the same thing, so the second was refused as a repeat
    # of the first. The proposal id is stable for one hold and different for
    # the next, which is exactly the identity a pause has.
    got = await open_request(conn, execution_id, generation, "direction",
                             payload,
                             content_hash=f"hold:{hold.get('proposal_id') or ''}"
                             if hold.get("proposal_id") else None)
    if not isinstance(got, int):
        # A REFUSED PARK IS NOT A SILENT ONE. The commonest cause is
        # `questions_one_open` — the run is already parked on something
        # else, so a person is already being asked and the hold has
        # nothing to add.
        logger.info("execution %s not held (%s); the run carries on",
                    execution_id, getattr(got, "code", got))
        return None

    if hold.get("answer_id"):
        await conn.execute(
            "UPDATE answers SET question_id = $2, held = TRUE "
            "WHERE id = $1", int(hold["answer_id"]), got)
    logger.info("execution %s HELD on question %s: %s", execution_id, got,
                payload["decision"][:160])
    return got


async def close_open_questions(conn, execution_id: int, reason: str) -> int:
    """Retire whatever this run was asking, because it is over.

    A CANCELLED RUN LEFT ITS CARD UP. Cancellation moved the execution
    and nothing else, so a plan approval opened by a run that no longer
    exists stayed OPEN -- and OPEN is what the inbox, the notifier and
    the control panel all read to decide what needs a person. Two of
    them were still sitting in KER07's thread asking someone to approve
    work that had been cancelled hours earlier, and there was no way for
    that person to make them go away: approving a dead run is not a
    thing, and nothing else closes a question.

    Declined rather than answered, because that is what happened -- the
    question will not be acted on. Returns how many were retired.
    """
    closed = await conn.fetch(
        """
        UPDATE questions
        SET status = 'ANSWERED', declined = TRUE, verdict = 'declined',
            answer_text = $2, answered_by = 'kernel', answered_at = now()
        WHERE execution_id = $1 AND status = 'OPEN'
        RETURNING id
        """,
        execution_id, f"The run ended before this was answered: {reason}"
                      if reason else "The run ended before this was answered.")
    if closed:
        logger.info("execution %s: retired %s open question(s) on cancel",
                    execution_id, len(closed))
    return len(closed)

