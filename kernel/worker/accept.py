"""F12 — a person finishing a story the Verifier will not pass.

The Verifier is built to be unarguable, and every defence in Stage F
exists to keep it that way. This is the one door out, and it is here
because an unarguable Verifier with a wrong bar is a trap: F11 shows how
easily a bar goes wrong, the E5 fence stops the model repeating itself,
and `ask_human` is a prompt instruction rather than a mechanism. Without
this the story simply stops.

WHAT THIS IS NOT. It is not a pass. Nothing here writes a verdict,
edits one, or makes `verdicts.passed` say something a Verifier did not
say. The refusal stands exactly as written and a separate append-only
row records that a person finished the story anyway — who, when, out of
which state, over which verdict, and why. Both statements stay true
afterwards, which is the whole reason for the split:

    claim 20 was REFUSED under contract 9
    the story was ACCEPTED by abhishek — "the check needs a network the
    sandbox is not allowed to have"

The executor cannot do this. There is no tool, the way there is no
`propose_completion` on an errand surface — and beneath that, F12's
grant: `agentos_worker` has no INSERT on `acceptances`, so an executor
that somehow issued the statement is refused by Postgres. The tool
surface is the design; the grant is what holds when somebody adds a tool
without reading this file.

THE CONTAINER IS NOT DESTROYED HERE, deliberately. Accepting from
RUNNING leaves a live container on a terminal execution, and the owner
of that problem is reconcile — F2c's rule is that no state may be
invisible to both loops, and the fix belongs where every other reap
lives rather than duplicated at each site that can make a run terminal.
Cancel had the identical leak and neither loop caught it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Long enough that "ok" and "fine" do not get through. This row is the
# only explanation that will exist for why a refused story is finished,
# and it is read by somebody who was not in the room.
MIN_REASON = 10

TERMINAL = ("DONE", "FAILED", "CANCELLED")


class AcceptRefused(Exception):
    """The story cannot be accepted, and why."""


async def _has_accepted_edge(conn, version: int, state: str) -> bool:
    """Does this run's machine permit being accepted from here?

    Asked of the machine the RUN was dispatched under, not the newest
    one. A story started under v4 has no `accepted` edge at all, and
    saying so — with the repair — beats writing a transition its own
    declared machine forbids.
    """
    return await conn.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM run_state_machine m,
             jsonb_array_elements(m.transitions) t
            WHERE m.version = $1 AND t->>'from' = $2
              AND t->>'to' = 'DONE' AND t->>'predicate' = 'accepted')
        """, version, state)


async def _what_is_being_overridden(conn, execution_id: int):
    """The refusal this acceptance overrides, if there is one.

    Both may be NULL and the combination is the point: a row with no
    verdict is a story accepted with nothing ever verified — dispatched
    without a contract, or stopped mid-work — and a reviewer needs to
    tell that apart from a refusal a person overruled.
    """
    row = await conn.fetchrow(
        "SELECT id, claim_seq FROM verdicts WHERE execution_id = $1 "
        "AND passed = false ORDER BY id DESC LIMIT 1", execution_id)
    if row is None:
        return None, None
    return row["id"], row["claim_seq"]


async def _close_open_parks(conn, execution_id: int, by: str) -> int:
    """Close anything the story was waiting on.

    NOT `resolve_request`, whose whole job is to un-park a run back to
    RUNNING so it can carry on. That is the opposite of what is
    happening: the story is finishing, and a question it is no longer
    going to act on should be closed rather than answered.

    Left open they would sit in the mailbox forever, on a story that has
    ended, waiting for a reply that can change nothing.
    """
    return int((await conn.execute(
        """
        UPDATE questions
        SET status = 'ANSWERED', answer_text = $2, answered_by = $3,
            answered_at = now(), verdict = 'moot'
        WHERE execution_id = $1 AND status = 'OPEN'
        """,
        execution_id,
        "The story was accepted as finished while this was waiting, so "
        "it is no longer a question anyone needs to answer.",
        by)).split()[-1])


async def accept(conn, execution_id: int, reason: str,
                 by: str = "console") -> dict:
    """Finish a story on a person's authority. Returns what happened."""
    reason = (reason or "").strip()
    row = await conn.fetchrow(
        "SELECT state, machine_version, contract_id FROM executions "
        "WHERE id = $1", execution_id)

    # EVERY REFUSAL BEFORE ANY WRITE — the same ordering f10's amend had
    # to be corrected into. A half-applied acceptance would leave a
    # record saying a person finished a story that is still running.
    if row is None:
        raise AcceptRefused(f"no execution {execution_id}")
    if len(reason) < MIN_REASON:
        raise AcceptRefused(
            "an acceptance needs a reason, and this row is the only "
            "explanation that will exist for why a story the Verifier "
            "refused was finished anyway. Say what the Verifier got "
            "wrong, or why the work is good enough without it.")
    state = row["state"]
    if state == "DONE":
        raise AcceptRefused("this story is already DONE.")
    if state in TERMINAL:
        raise AcceptRefused(
            f"this story is {state}, so there is no work waiting to be "
            f"accepted. Accepting means work was done and is good enough "
            f"unverified; start a new story instead.")
    if not await _has_accepted_edge(conn, row["machine_version"], state):
        raise AcceptRefused(
            f"a run in {state} under machine v{row['machine_version']} "
            f"cannot be accepted — its machine has no such edge. Carry it "
            f"forward to the current machine first. (A story that has "
            f"never run has nothing to accept; cancel it.)")

    verdict_id, claim_seq = await _what_is_being_overridden(conn, execution_id)

    # `questions` belongs to the worker, so this happens OUTSIDE the role
    # raise below — agentos_human may write exactly one table.
    closed = await _close_open_parks(conn, execution_id, by)

    async with conn.transaction():
        # The only write agentos_human is granted, plus the four columns
        # of `executions` it may set. Raised for the statement and
        # dropped at commit, the same move writer.py makes for a verdict.
        await conn.execute("SET LOCAL ROLE agentos_human")
        acceptance_id = await conn.fetchval(
            "INSERT INTO acceptances (execution_id, claim_seq, verdict_id, "
            "from_state, reason, accepted_by) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            execution_id, claim_seq, verdict_id, state, reason, by)
        # Guarded on the state we validated against. If the story moved
        # while a person was typing — a verdict landed, the worker parked
        # it — the acceptance rolls back with it rather than writing a
        # transition out of a state the run is no longer in.
        from kernel.worker.finish import finish_execution

        ok = await finish_execution(conn, execution_id,
                                    only_from=(state,))
        if not ok:
            raise AcceptRefused(
                f"this story left {state} while you were deciding. Look "
                f"at it again — what it is waiting for may have changed.")

    detail = (f"accepted out of {state}"
              + (f", overriding the refusal of claim {claim_seq}"
                 if verdict_id else ", with nothing ever verified")
              + (f"; {closed} open question(s) closed" if closed else ""))
    logger.info("execution %s ACCEPTED by %s: %s", execution_id, by, detail)
    return {"execution_id": execution_id, "action": "accepted",
            "acceptance_id": acceptance_id, "from_state": state,
            "overrode_verdict": verdict_id, "detail": detail}


async def acceptance_of(conn, execution_id: int) -> dict | None:
    """The acceptance that finished this story, if one did.

    Read by the console on every render. A story that was accepted must
    never display as a plain DONE: an override you cannot see afterwards
    makes DONE mean nothing, which would undo the reason the verdict was
    left alone in the first place.
    """
    row = await conn.fetchrow(
        "SELECT id, claim_seq, verdict_id, from_state, reason, accepted_by, "
        "created_at FROM acceptances WHERE execution_id = $1 "
        "ORDER BY id DESC LIMIT 1", execution_id)
    if row is None:
        return None
    return {"id": row["id"], "claim_seq": row["claim_seq"],
            "verdict_id": row["verdict_id"], "from_state": row["from_state"],
            "reason": row["reason"], "by": row["accepted_by"],
            "at": row["created_at"].isoformat(timespec="seconds")}
