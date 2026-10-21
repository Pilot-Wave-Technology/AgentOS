"""What this run is waiting on, and what it was last told.

SPLIT OUT OF `park.py` (plan 11.5c). Two reads, no writes, and that is
the reason they are a module rather than an appendix to `park_open`:
these are what the EXECUTOR calls at the top of every leg, on the hot
path, while `park_open`'s writes happen once when a leg ends.

BOTH TRANSLATE BACK THROUGH `_OUT` before returning. Callers — the
prompt builder, the console, the tests — speak `question` and
`plan_approval`; the table stores `clarification` and `authorization`.
Returning the stored spelling would leak the schema into the prompt a
model reads, and a future reader must not "simplify" the translation
away on the grounds that the two dicts look redundant.
"""

from __future__ import annotations

import json

from kernel.harness.park_kinds import _OUT

async def pending_request(conn, execution_id: int) -> dict | None:
    """The open request, if the run is waiting on one."""
    row = await conn.fetchrow(
        "SELECT id, kind, payload::text, content_hash, created_at "
        "FROM questions WHERE execution_id = $1 AND status = 'OPEN'",
        execution_id,
    )
    if row is None:
        return None
    return {"id": row["id"], "kind": _OUT.get(row["kind"], row["kind"]),
            "payload": json.loads(row["payload"]),
            "content_hash": row["content_hash"]}


async def answered_since(conn, execution_id: int, generation: int) -> dict | None:
    """The most recent answered request from a prior generation.

    Read at the start of a leg so the answer reaches the model as a
    message. Scoped to prior generations: an answer to THIS leg's own
    question would mean the leg never parked.
    """
    row = await conn.fetchrow(
        """
        SELECT id, kind, payload::text, answer_text, answered_by,
               declined, verdict, step_notes::text
        FROM questions
        WHERE execution_id = $1 AND status = 'ANSWERED'
          AND generation < $2
        ORDER BY answered_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        execution_id, generation,
    )
    if row is None:
        return None
    return {"id": row["id"], "kind": _OUT.get(row["kind"], row["kind"]),
            "payload": json.loads(row["payload"]),
            "answer": row["answer_text"], "answered_by": row["answered_by"],
            "declined": row["declined"], "verdict": row["verdict"],
            "step_notes": json.loads(row["step_notes"] or "[]")}

