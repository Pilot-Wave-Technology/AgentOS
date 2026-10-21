"""H1 — the mailbox itself. Writing and reading the three vocabularies.

Nothing wrote to `directives` or `questions` before this; `proposals` was
written only by `propose_completion`. The tables were a shape waiting for a
mechanism.

WHAT THE MAILBOX IS FOR. The assistant does not drive execution (H8c): it
puts an intent in a box and the executor picks it up **at a step boundary**.
That indirection is the whole design — a steer applied mid-step would land
between a tool call and its result, and the journal would record a leg that
never happened as written.

DUPLICATE REJECTION. Asking a question already answered returns
`E_ASK_ANSWERED` with the prior answer attached, rather than parking the
run a second time on something a human has already dealt with.
"""

from __future__ import annotations

import json
import logging

from kernel.mailbox.schema import (
    Invalid, validate_directive, validate_proposal, validate_question,
)

logger = logging.getLogger(__name__)


# ── assistant → proposals ────────────────────────────────────────────────

async def submit_proposal(conn, actor_id: str, kind: str, payload: dict,
                          idempotency_key: str,
                          execution_id: int | None = None):
    # `execution_id` keeps its default so a caller that omits it gets the
    # typed refusal below rather than a TypeError -- these arrive from a
    # model, and a refusal it can read is worth more than a traceback.
    """Put an assistant intent in the box. Returns an id or an Invalid."""
    bad = validate_proposal(actor_id, kind, payload, idempotency_key,
                            execution_id)
    if bad:
        return bad

    row = await conn.fetchrow(
        """
        INSERT INTO proposals
            (actor_id, kind, idempotency_key, execution_id, payload)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (actor_id, execution_id, idempotency_key) DO NOTHING
        RETURNING id
        """,
        actor_id, kind, idempotency_key, execution_id, json.dumps(payload))
    if row is None:
        # Not an error: the same key twice is the same intent, which is what
        # an idempotency key is for. Hand back the original.
        existing = await conn.fetchval(
            "SELECT id FROM proposals WHERE actor_id = $1 "
            "AND execution_id = $2 AND idempotency_key = $3",
            actor_id, execution_id, idempotency_key)
        logger.info("proposal %s already exists for key %s",
                    existing, idempotency_key)
        return existing
    return row["id"]


async def pending_proposals(conn, execution_id: int) -> list[dict]:
    """Intents waiting to be applied at the next step boundary."""
    rows = await conn.fetch(
        "SELECT id, actor_id, kind, payload::text FROM proposals "
        "WHERE execution_id = $1 AND status = 'PENDING' ORDER BY id",
        execution_id)
    return [{"id": r["id"], "actor_id": r["actor_id"], "kind": r["kind"],
             "payload": json.loads(r["payload"])} for r in rows]


async def mark_proposal(conn, proposal_id: int, status: str) -> None:
    await conn.execute(
        "UPDATE proposals SET status = $2 WHERE id = $1", proposal_id, status)


# ── executor → questions ─────────────────────────────────────────────────

async def ask_question(conn, execution_id: int, kind: str,
                       question_text: str, payload: dict,
                       generation: int = 0, content_hash: str | None = None):
    """Record an executor's question. Returns an id or an Invalid.

    THE ONLY INSERT INTO `questions`. E6i built a second table and a second
    writer for the same act, and the two drifted immediately: one had the
    evidence floor and no generation, the other had a generation and no
    floor. `kernel.harness.park.open_request` now comes through here and
    owns only the side-effect nobody else has — parking the run.

    The evidence floor is enforced in the schema; duplicate rejection is
    enforced here, because it needs the table.
    """
    bad = validate_question(kind, question_text, payload)
    if bad:
        return bad

    # Already answered? Hand the answer back instead of asking again.
    #
    # Identity is the content hash where there is one, and the text
    # otherwise. A plan approval's text is a constant, so matching on text
    # would make the SECOND plan of a run look like a repeat of the first
    # and inherit a verdict passed on different steps.
    if content_hash:
        prior = await conn.fetchrow(
            "SELECT id, answer_text FROM questions WHERE execution_id = $1 "
            "AND content_hash = $2 AND status = 'ANSWERED' "
            "ORDER BY id DESC LIMIT 1", execution_id, content_hash)
    else:
        prior = await conn.fetchrow(
            "SELECT id, answer_text FROM questions WHERE execution_id = $1 "
            "AND question_text = $2 AND status = 'ANSWERED' "
            "ORDER BY id DESC LIMIT 1", execution_id, question_text)
    if prior:
        return Invalid(
            "E_ASK_ANSWERED",
            f"this was already answered: {prior['answer_text']!r} "
            f"(question {prior['id']}). Act on it rather than asking again.")

    row = await conn.fetchrow(
        """
        INSERT INTO questions
            (execution_id, generation, kind, question_text, payload,
             content_hash, proposed_default, blocking_on, reversible)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
        RETURNING id
        """,
        execution_id, generation, kind, question_text, json.dumps(payload),
        content_hash, payload.get("proposed"), payload.get("blocking_on"),
        bool(payload.get("reversible", True)))
    logger.info("execution %s asked question %s (%s)",
                execution_id, row["id"], kind)
    return row["id"]


async def open_questions(conn, execution_id: int) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, kind, question_text, proposed_default, blocking_on, "
        "reversible FROM questions WHERE execution_id = $1 AND status = 'OPEN' "
        "ORDER BY id", execution_id)
    return [dict(r) for r in rows]


async def answer_question(conn, question_id: int, answer_text: str,
                          answered_by: str) -> bool:
    """Answer an open question. False if it was not open."""
    result = await conn.execute(
        """
        UPDATE questions
        SET answer_text = $2, answered_by = $3, answered_at = now(),
            status = 'ANSWERED'
        WHERE id = $1 AND status = 'OPEN'
        """,
        question_id, answer_text, answered_by)
    return result.split()[-1] != "0"


# ── Conductor → directives ───────────────────────────────────────────────

async def issue_directive(conn, execution_id: int, kind: str,
                          generation: int, payload: dict | None = None):
    """Issue a Conductor directive. Returns an id or an Invalid."""
    payload = payload or {}
    bad = validate_directive(kind, generation, payload)
    if bad:
        return bad
    row = await conn.fetchrow(
        """
        INSERT INTO directives (execution_id, kind, generation, payload)
        VALUES ($1, $2, $3, $4::jsonb) RETURNING id
        """,
        execution_id, kind, generation, json.dumps(payload))
    return row["id"]


async def take_directives(conn, execution_id: int, generation: int
                          ) -> list[dict]:
    """Pending directives for THIS generation. Stale ones are discarded.

    The fence is the point: a directive issued for generation 3 and read at
    generation 4 was written about a leg that has already ended. Applying
    it would act on a run that has moved on — the same class of mistake
    C7's journal fence exists to prevent.
    """
    stale = await conn.fetch(
        "SELECT id FROM directives WHERE execution_id = $1 "
        "AND status = 'PENDING' AND generation < $2", execution_id, generation)
    for row in stale:
        await conn.execute(
            "UPDATE directives SET status = 'DISCARDED' WHERE id = $1",
            row["id"])
    if stale:
        logger.info("discarded %d stale directive(s) for execution %s",
                    len(stale), execution_id)

    rows = await conn.fetch(
        "SELECT id, kind, payload::text FROM directives "
        "WHERE execution_id = $1 AND status = 'PENDING' AND generation = $2 "
        "ORDER BY id", execution_id, generation)
    return [{"id": r["id"], "kind": r["kind"],
             "payload": json.loads(r["payload"])} for r in rows]


async def mark_directive(conn, directive_id: int, status: str) -> None:
    await conn.execute(
        "UPDATE directives SET status = $2 WHERE id = $1",
        directive_id, status)
