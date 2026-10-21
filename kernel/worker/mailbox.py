"""H1a — the mailbox for runs that have no leg in flight.

`kernel.harness.intake` handles the intents that are about a leg that is
running. This handles the ones that are not, and the distinction is not a
tidiness preference: a step boundary is the only safe place to inject a
message, and a run that is PARKED or VERIFYING has no steps.

    answer  a run with an open question has already parked, so there is
            no leg to apply the answer to. Applied here, between legs.

    cancel  intake.apply_cancellation is guarded on RUNNING, because the
            machine has no cancel edge from anywhere else the loop can
            see. Machine v3 added edges from PENDING, ADMITTED, PARKED_*
            and VERIFYING — every state a run can sit in — because a
            human could not abandon a story waiting on a question nobody
            would answer, or on a Verifier that does not exist yet. The
            console had a cancel button that was a no-op on exactly the
            runs worth cancelling.

WHY NOT JUST CALL resolve_request FROM THE CONSOLE. The console does, and
that path stays: a person answering in the UI is answering directly. The
mailbox path exists for the assistant, which is not a person and does not
hold a connection — it submits an intent with an idempotency key and the
worker applies it. The two converge on `resolve_request`, so a run cannot
be un-parked twice by being answered both ways: the UPDATE is guarded on
`status = 'OPEN'`.
"""

from __future__ import annotations

import logging

from kernel.harness.park import close_open_questions, resolve_request
from kernel.mailbox.api import mark_proposal

logger = logging.getLogger(__name__)

# Where a run can sit without a leg. RUNNING is absent on purpose: a
# running leg drains its own mailbox at a step boundary, and cancelling
# it from out here would stop it between a tool call and its result.
NO_LEG_STATES = ("PENDING", "ADMITTED", "DISPATCHING",
                 "PARKED_HOT", "PARKED_COLD", "VERIFYING")


async def _parked_with_answers(conn, goal_id: str | None):
    return await conn.fetch(
        """
        SELECT p.id AS proposal_id, p.actor_id, p.execution_id,
               p.payload->>'question_id' AS question_id,
               p.payload->>'text'        AS text,
               p.payload->>'declined'    AS declined,
               p.payload->>'verdict'     AS verdict
        FROM proposals p
        JOIN executions e ON e.id = p.execution_id
        WHERE p.status = 'PENDING' AND p.kind = 'answer'
          AND e.state IN ('PARKED_HOT', 'PARKED_COLD')
          AND ($1::text IS NULL OR e.goal_id = $1)
        ORDER BY p.id
        """,
        goal_id)


async def deliver_answers(conn, goal_id: str | None = None) -> list[dict]:
    """Apply pending `answer` proposals to parked runs. Returns what it did.

    Each answer is checked against the question it names before it is
    applied: an answer to a question belonging to another execution, or to
    one already closed, is REJECTED with the reason recorded rather than
    dropped. A proposal that vanishes without a verdict is indistinguishable
    from one that was never submitted.
    """
    actions: list[dict] = []
    for row in await _parked_with_answers(conn, goal_id):
        eid, pid = row["execution_id"], row["proposal_id"]
        try:
            qid = int(row["question_id"])
        except (TypeError, ValueError):
            await mark_proposal(conn, pid, "REJECTED")
            actions.append({"execution_id": eid, "action": "answer_rejected",
                            "detail": f"question_id {row['question_id']!r} "
                                      f"is not an id"})
            continue

        question = await conn.fetchrow(
            "SELECT execution_id, status, kind FROM questions WHERE id = $1",
            qid)
        if question is None or question["execution_id"] != eid:
            # Answering another run's question would un-park this one on
            # a reply written about different work.
            await mark_proposal(conn, pid, "REJECTED")
            actions.append({"execution_id": eid, "action": "answer_rejected",
                            "detail": f"question {qid} does not belong to "
                                      f"this execution"})
            continue
        if question["status"] != "OPEN":
            await mark_proposal(conn, pid, "SUPERSEDED")
            actions.append({"execution_id": eid, "action": "answer_late",
                            "detail": f"question {qid} was already answered"})
            continue

        # H8a — an assistant may relay a rejection, never an approval.
        #
        # Refused HERE as well as in `resolve_request`, and the
        # duplication is deliberate. The guard downstream is the one that
        # holds for every caller; this one exists so the assistant gets a
        # rejection it can read and act on, rather than an exception
        # surfacing from a layer it never called. The default matters:
        # an `answer` carrying no verdict settles as `approve`, so
        # silence used to be permission.
        verdict = (row["verdict"] or "").strip().lower()
        if not verdict:
            verdict = ("declined" if (row["declined"] or "").lower() == "true"
                       else "approve")
        if question["kind"] == "authorization" and verdict == "approve":
            await mark_proposal(conn, pid, "REJECTED")
            actions.append({
                "execution_id": eid, "action": "answer_refused",
                "detail": f"question {qid} is a plan approval; approval is "
                          f"reserved to a person (§1.3). Relay it, or send "
                          f"verdict='revise' with what to change"})
            continue

        await resolve_request(conn, qid, row["text"] or "",
                              answered_by=row["actor_id"],
                              declined=verdict != "approve",
                              verdict=verdict)
        await mark_proposal(conn, pid, "ACCEPTED")
        logger.info("execution %s un-parked by proposal %s", eid, pid)
        actions.append({"execution_id": eid, "action": "answered",
                        "detail": f"question {qid} answered by "
                                  f"{row['actor_id']}; the run resumes"})
    return actions


async def deliver_cancellations(conn, goal_id: str | None = None) -> list[dict]:
    """Apply pending `cancel` proposals to runs that have no leg.

    A run that is RUNNING is left alone: its own leg drains the mailbox at
    a step boundary and stops itself there, which is the only point where
    stopping does not split a tool call from its result.

    The transition is checked against the run's OWN machine version rather
    than assumed. A run dispatched under v2 has no cancel edge from
    PARKED_HOT, and forcing one would write a transition the console then
    flags red — the honest outcome is to say so and leave the proposal
    pending.
    """
    rows = await conn.fetch(
        f"""
        SELECT p.id AS proposal_id, p.execution_id, p.payload->>'reason' AS reason,
               e.state, e.machine_version
        FROM proposals p
        JOIN executions e ON e.id = p.execution_id
        WHERE p.status = 'PENDING' AND p.kind = 'cancel'
          AND e.state = ANY($1::text[])
          AND ($2::text IS NULL OR e.goal_id = $2)
        ORDER BY p.id
        """,
        list(NO_LEG_STATES), goal_id)

    actions: list[dict] = []
    for row in rows:
        eid, pid, state = row["execution_id"], row["proposal_id"], row["state"]
        legal = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM run_state_machine m,
                     jsonb_array_elements(m.transitions) t
                WHERE m.version = $1
                  AND t->>'from' = $2 AND t->>'to' = 'CANCELLED')
            """,
            row["machine_version"], state)
        if not legal:
            actions.append({
                "execution_id": eid, "action": "cancel_illegal",
                "detail": f"machine v{row['machine_version']} has no "
                          f"{state} -> CANCELLED edge; left pending"})
            continue

        await conn.execute(
            "UPDATE executions SET state = 'CANCELLED' WHERE id = $1 "
            "AND state = $2", eid, state)
        # The card goes with the run. This is the path a PARKED story
        # takes -- it is cancelled while an open question is exactly
        # what it is parked ON -- so without this the thread keeps
        # asking for a decision nobody can now make.
        await close_open_questions(conn, eid, row["reason"] or "")
        await mark_proposal(conn, pid, "ACCEPTED")
        logger.info("execution %s cancelled from %s: %s", eid, state,
                    row["reason"] or "no reason given")
        actions.append({"execution_id": eid, "action": "cancelled",
                        "detail": f"{state} -> CANCELLED: "
                                  f"{row['reason'] or 'no reason given'}"})
    return actions


async def expire_stale_holds(conn, goal_id: str | None = None) -> list[dict]:
    """Retire `hold` proposals that no leg will ever take.

    A HOLD IS APPLIED BY A RUNNING LEG, at a step boundary, and nowhere else.
    So one submitted to a run that has already left RUNNING has no one to
    apply it — and left PENDING it does not merely do nothing, it waits. A
    verification that fails sends the run back to RUNNING, the next leg drains
    the mailbox, and the run pauses itself minutes later for a reason nobody
    remembers asking about.

    Observed on KER20 run 1657: a person pressed Pause in the same SECOND the
    executor proposed completion. The submit was accepted — the run was
    RUNNING when it was checked — and by the time it landed the run was
    VERIFYING. Nothing was wrong with either act; they simply raced.

    SUPERSEDED, not ACCEPTED, because it was a real intent that was overtaken
    — the same word `take_proposals` uses for a steer that arrives behind a
    cancel. The person can press Pause again if the run comes back, and this
    time they will mean it about the run in front of them.

    Terminal runs are included: a hold on a finished story is as dead as one
    on a verifying one, and leaving it PENDING keeps a row that reads like
    something outstanding.
    """
    rows = await conn.fetch(
        """
        SELECT p.id, p.execution_id, e.state
          FROM proposals p
          JOIN executions e ON e.id = p.execution_id
         WHERE p.kind = 'hold' AND p.status = 'PENDING'
           AND e.state <> 'RUNNING'
           AND ($1::text IS NULL OR e.goal_id = $1)
         ORDER BY p.id
        """, goal_id)

    out: list[dict] = []
    for r in rows:
        await mark_proposal(conn, r["id"], "SUPERSEDED")
        logger.info("hold %s on execution %s expired: the run is %s and no "
                    "leg will take it", r["id"], r["execution_id"], r["state"])
        out.append({"execution_id": r["execution_id"], "action": "hold_expired",
                    "detail": f"the run reached {r['state']} before the pause "
                              f"could be applied — press Pause again if it "
                              f"starts working"})
    return out
