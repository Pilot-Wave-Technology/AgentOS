"""Draining `question` proposals: claim fast, answer beside the run.

WHY NOT AT A STEP BOUNDARY LIKE A STEER. Because answering is not an
instruction and must never arrive as one. A steer is injected into the
executor's own message list, which is right for a steer and exactly
wrong here: a question landing in that position is read as one more
thing to do. So nothing about a question reaches the executor's prompt.
It is answered beside the run, from the record and a throwaway copy of
the tree, and the executor learns of it only if the answer becomes a
hold or the person turns it into a steer.

WHICH IS ALSO WHY IT WORKS IN EVERY STATE. The answerer never touches
the sandbox, so a story that is RUNNING, PARKED or VERIFYING is asked
the same way, and a running leg is never interrupted to answer.

CLAIMED HERE, ANSWERED ELSEWHERE. An answer takes a model call and
minutes, so running one inside the pass would freeze every other story
behind it. The pass claims — one indexed UPDATE — and hands the work to
a task with a connection of its own, TAKEN FROM THE POOL. A dedicated
connection is right; a private one is not, and the difference is the
whole of `acquire` below.
"""

from __future__ import annotations

import asyncio
import logging

from kernel.answerer.one import answer_one, record_answer

logger = logging.getLogger(__name__)

# A finished run cannot be asked about this way: no tree to look at, and
# no work a hold could stop. The assistant answers those from the record.
ASKABLE = ("PENDING", "ADMITTED", "DISPATCHING", "RUNNING",
           "PARKED_HOT", "PARKED_COLD", "VERIFYING")

# How long a claimed question may stay unanswered before it is written
# off. Comfortably past the answerer's own wall clock, so this only ever
# fires on a process that died mid-answer.
STALE_MINUTES = 15

# How many questions may be answered at once in ONE conductor. Each holds
# a pooled connection (POOL_MAX is 16 in the conductor, which is the only
# process this pass runs in — since D2-prep `loopbridge` sizes the
# execution pod's pool at 1-2 instead) across a model call, so this is a
# connection budget. Kept small: an answer is never urgent enough to cost
# the tick its database.
ANSWER_CONCURRENCY = 4

# How many questions may be answered INLINE in one pass, which is the
# degrade taken when no pool was given. An inline answer happens in the
# tick and the tick is serial, so this is the number of model calls
# every other story may be made to wait behind. One.
INLINE_PER_TICK = 1

# How long an `ask` may sit with the executor before the answerer takes
# it over. A live leg reaches a step boundary within one wait slice (20s)
# even mid-`bash_wait`, so three minutes is not a race -- it is "the leg
# is not going to answer this".
ASK_GRACE_MINUTES = 3

# Fire-and-forget tasks are garbage collected if nothing holds them, and
# a collected task is a question that silently never gets answered.
_LIVE: set = set()


async def _claim(conn, goal_id: str | None, limit: int) -> list[dict]:
    """Take up to `limit` waiting questions, atomically. Returns them.

    ONE STATEMENT, so two workers cannot both pay for the same answer.
    Marked ACCEPTED rather than given a status of its own: the proposal
    HAS been accepted at this point — what follows is the acting on it,
    and the `answers` row is the record of that.

    `limit` EXISTS BECAUSE CLAIMING IS NOT FREE, and the version without
    it lost questions. This took EVERY pending question and the caller
    then spawned only as many as it had connections for, deferring the
    rest with the comment "the claim stays pending and the next tick
    picks it up". It did not stay pending — this statement had already
    marked it ACCEPTED. So the fifth question of a burst was ACCEPTED,
    never answered, and written off by `_reap` fifteen minutes later
    with "I never finished looking into this".

    Taking only what can be answered makes the deferral real: an
    unclaimed question is still PENDING, and the next tick claims it for
    nothing more than the wait.

    `FOR UPDATE ... SKIP LOCKED` keeps the atomicity the single
    statement had. Two conductors racing take disjoint sets rather than
    one blocking on the other.
    """
    rows = await conn.fetch(
        """
        UPDATE proposals p
           SET status = 'ACCEPTED'
          FROM executions e
         WHERE e.id = p.execution_id
           AND p.id IN (
               SELECT q.id
                 FROM proposals q
                 JOIN executions x ON x.id = q.execution_id
                WHERE q.status = 'PENDING' AND q.kind = 'question'
                  AND x.state = ANY($1::text[])
                  AND ($2::text IS NULL OR x.goal_id = $2)
                ORDER BY q.id
                LIMIT $3
                FOR UPDATE OF q SKIP LOCKED
           )
        RETURNING p.id AS proposal_id, p.actor_id, p.execution_id,
                  p.payload->>'question' AS question,
                  p.payload->>'asked_by' AS asked_by,
                  e.state
        """,
        list(ASKABLE), goal_id, limit)
    return [dict(r) for r in rows]


async def _fallback(conn, goal_id: str | None) -> int:
    """Hand an unanswered `ask` to the answerer. Returns how many.

    THE FALLBACK IS WHAT MAKES `ask` SAFE TO PREFER. Routing a question
    to the executor is better when there is an executor; the whole risk
    is the cases where there turns out not to be one, and they are
    ordinary rather than exotic:

      - the run parked or finished between the routing decision and the
        next step boundary, so nothing ever read the message;
      - the leg died, and the next leg rebuilds its messages from the
        record -- an injected question is not in the record;
      - the model read it and did not call `reply_to_human`.

    All three look the same from outside: a question with no answer. So
    this converts it back to a `question`, which is exactly what it
    would have been had the run not been RUNNING when it was asked, and
    `_claim` picks it up on the same tick. The person waits three
    minutes and gets an answer, rather than waiting fifteen and getting
    an apology.

    BOTH STATUSES, because both fail the same way. PENDING means no leg
    ever took it; ACCEPTED with no answer row means one took it and said
    nothing.
    """
    done = await conn.fetchval(
        """
        WITH stale AS (
            SELECT p.id FROM proposals p
             WHERE p.kind = 'ask'
               AND p.status IN ('PENDING', 'ACCEPTED')
               AND p.created_at < now() - ($1 || ' minutes')::interval
               AND NOT EXISTS (SELECT 1 FROM answers a
                                WHERE a.proposal_id = p.id)
               AND ($2::text IS NULL OR EXISTS (
                        SELECT 1 FROM executions e
                         WHERE e.id = p.execution_id AND e.goal_id = $2))
             ORDER BY p.id LIMIT 20
        )
        UPDATE proposals SET kind = 'question', status = 'PENDING'
         WHERE id IN (SELECT id FROM stale)
        RETURNING 1
        """,
        str(ASK_GRACE_MINUTES), goal_id)
    return int(done or 0)


async def _reap(conn, goal_id: str | None) -> list[dict]:
    """Write off questions whose answerer died before it answered.

    Without this a process killed mid-answer leaves a proposal marked
    ACCEPTED, no answer row, and a person waiting on a card that will
    never come — the exact failure the `answers` table exists to
    prevent, reintroduced by the crash path.
    """
    rows = await conn.fetch(
        """
        SELECT p.id AS proposal_id, p.actor_id, p.execution_id,
               p.payload->>'question' AS question,
               p.payload->>'asked_by' AS asked_by, e.state
        FROM proposals p
        JOIN executions e ON e.id = p.execution_id
        WHERE p.kind = 'question' AND p.status = 'ACCEPTED'
          AND p.created_at < now() - ($1 || ' minutes')::interval
          AND ($2::text IS NULL OR e.goal_id = $2)
          AND NOT EXISTS (SELECT 1 FROM answers a
                          WHERE a.proposal_id = p.id)
        ORDER BY p.id LIMIT 20
        """,
        str(STALE_MINUTES), goal_id)
    out = []
    for r in rows:
        claim = dict(r)
        aid = await record_answer(
            conn, claim["execution_id"], claim["proposal_id"], claim,
            {"text": "I never finished looking into this — whatever was "
                     "answering it stopped before it could reply. The work "
                     "was not touched or paused. Ask again if it still "
                     "matters.",
             "looked_at": "", "failed": True}, False)
        logger.warning("execution %s: question %s written off after %s "
                       "minutes", claim["execution_id"],
                       claim["proposal_id"], STALE_MINUTES)
        out.append({"execution_id": claim["execution_id"],
                    "action": "answer_abandoned",
                    "detail": f"answer {aid}; nothing replied in "
                              f"{STALE_MINUTES} minutes"})
    return out


async def _detached(acquire, claim: dict) -> None:
    """Answer on a connection of our own, so the worker pass is free.

    OF OUR OWN, FROM THE POOL. The pass goes on using its connection
    while this runs and asyncpg refuses concurrent use of one — "another
    operation is in progress" — so answering beside the run genuinely
    needs a connection to itself. What it does not need is one nobody
    counted. This used to call `asyncpg.connect`, so every question in
    flight sat on top of `loopbridge`'s pool, outside POOL_MAX and
    outside anything that reports on it; at twenty concurrent stories
    that does not surface as a refusal, it makes every other query in
    the process wait, which reads as "the database got slow".
    """
    async with acquire() as conn:
        try:
            await answer_one(conn, claim)
        except Exception as e:                                # noqa: BLE001
            logger.warning("answering %s failed outright: %s",
                           claim["proposal_id"], e)


async def deliver_questions(conn, goal_id: str | None = None,
                            background: bool = True,
                            acquire=None) -> list[dict]:
    """Claim pending `question` proposals and get them answered.

    `acquire` is `pool.acquire` — a zero-argument callable giving an
    async context manager over a connection — and it is what buys the
    answering-beside-the-run. Same name and same shape as
    `adjudicate_pass` and `worker_tick` take, because that is how a pool
    reaches anything here: threaded down from `worker/service.py`
    through `worker_pass`, never reached for through a module-level
    singleton.

    WITHOUT IT, ANSWERS GO INLINE — the identical degrade those two
    passes already take from a caller with no pool to give. It costs the
    pass one model call and says so in the log. The alternative, opening
    a connection of our own, is the bypass `acquire` exists to remove,
    and it is worse than being slow because nothing anywhere reports it.

    `background=False` answers inline too, which is what the console and
    the tests want: a deterministic return.
    """
    # BEFORE THE REAP, so a question the executor did not answer becomes
    # one the answerer will, on this tick, instead of ageing on towards
    # being written off.
    moved = await _fallback(conn, goal_id)
    if moved:
        logger.info("%d unanswered ask(s) handed to the answerer", moved)

    actions = await _reap(conn, goal_id)

    # BOUNDED BEFORE ANYTHING IS TAKEN, and that order is the fix.
    #
    # Each detached answer holds a pooled connection for a model call --
    # up to 300s. A burst of twenty questions once claimed twenty of the
    # sixteen connections in the pool and the tick that spawned them
    # could not get one back, so the bound was added. But it was applied
    # AFTER `_claim`, which had already marked every one ACCEPTED, so the
    # ones over the bound were not deferred at all: they were taken,
    # dropped, and written off by `_reap` a quarter of an hour later.
    #
    # INLINE IS BOUNDED HARDER, AND SEPARATELY. With no pool the answer
    # runs in the tick itself, and this pass is serial -- so every other
    # story waits behind it for as long as the model takes, up to
    # `WALL_CLOCK_S`. One per tick caps that at one model call instead of
    # one per person who happened to ask. The tick still makes progress;
    # the queue drains a question at a time.
    detached = background and acquire is not None
    room = (max(0, ANSWER_CONCURRENCY - len(_LIVE)) if detached
            else INLINE_PER_TICK)
    if room <= 0:
        actions.append({"execution_id": 0, "action": "answer_deferred",
                        "detail": f"{len(_LIVE)} answer(s) already in "
                                  f"flight; questions wait a tick and stay "
                                  f"PENDING"})
        return actions

    claims = await _claim(conn, goal_id, room)
    if claims and background and acquire is None:
        logger.warning(
            "no pool was given to deliver_questions, so %d question(s) are "
            "being answered inline and the worker pass waits on the model "
            "call. Pass acquire=pool.acquire from the tick.", len(claims))
    for claim in claims:
        if not detached:
            actions.append(await answer_one(conn, claim))
            continue
        task = asyncio.create_task(_detached(acquire, claim))
        _LIVE.add(task)
        task.add_done_callback(_LIVE.discard)
        actions.append({"execution_id": claim["execution_id"],
                        "action": "answering",
                        "detail": f"question {claim['proposal_id']} claimed; "
                                  f"answering beside the run"})
    return actions
