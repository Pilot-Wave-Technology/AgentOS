"""The GO's idempotency keys, minted here instead of asked of the model.

WHAT WENT WRONG. The GO's branch of the tool router read the key straight
out of the model's arguments -- `args.get("idempotency_key") or ""` -- and
passed it on. The assistant's branch is guarded: a gate above it refuses an
empty key before dispatch, so a model that forgets one gets a readable
refusal naming the argument. GO's branch sits BELOW that gate, so an empty
key travelled all the way to `validate_proposal` and came back as
`E_NO_IDEMPOTENCY_KEY` from the mailbox -- a refusal about a database
invariant, aimed at a model that never knew it was supposed to supply one.
A person asked the GO to steer a running errand and the steer never landed.

WHY NOT JUST GUARD IT TOO. Because the requirement is wrong at the root.
An idempotency key exists so a RETRY does not land twice; it does not need
to come from the caller, it needs to be the same on a retry and different
on a new intent. A model inventing a random string satisfies neither -- it
is the one participant with no idea whether it is retrying. Deriving the
key from what the call actually is gets both properties for free, and the
assistant's own gate stays exactly as it is: this changes GO's branch only.

THE FENCE. A content hash alone would collapse two deliberate repeats into
one, which is right for some of these verbs and wrong for others. So the
hash is fenced by how far the run has got: the same instruction at the same
point in the run is a retry, the same instruction after the executor has
taken more steps is a new intent. `step_journal.seq` is that fence -- it
advances on every tool call and never moves backwards.

PER-VERB, BECAUSE THE VERBS DIFFER:

  `steer` and `ask_executor` -- hash plus fence. Re-sending an identical
  instruction while the executor has not moved is a retry. Sending it again
  later is somebody repeating themselves on purpose, and it should land.

  `cancel_story` -- hash, no fence. Cancelling twice is cancelling once,
  and collapsing the retry is the desired behaviour at any point.

  `show_card` -- never collapses. Re-posting a card is harmless, and
  silently not posting one somebody asked for twice is not. A parked run
  takes no steps, so the fence cannot move while somebody is looking at a
  card -- exactly when they are most likely to ask again.
"""

from __future__ import annotations

import hashlib
import json
import uuid

# Verbs whose repeat, at the same point in the run, is a retry.
_FENCED = ("steer", "ask_executor")

# Verbs that are idempotent by nature: collapse every repeat.
_UNFENCED = ("cancel_story",)

# Verbs where a collapse loses something a person asked for.
_ALWAYS_NEW = ("show_card",)

_FENCE_SQL = """
    SELECT COALESCE(MAX(j.seq), 0)
      FROM kernel.step_journal j
      JOIN kernel.executions e ON e.id = j.execution_id
     WHERE e.story_id = $1
"""


def _digest(tool: str, goal_id: str, story_id: str, payload: dict) -> str:
    """A stable fingerprint of what this call is asking for."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    raw = f"{tool}|{goal_id}|{story_id}|{body}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


async def _fence(conn, story_id: str) -> int:
    """How far the run has got, or 0 when there is nothing to read.

    FAILS OPEN TO ZERO. An unreadable journal must not stop a steer: the
    cost is that a retry during the outage collapses against the same
    zero fence, which is the safe direction -- one steer instead of two.
    """
    if not (story_id or "").strip():
        return 0
    try:
        return int(await conn.fetchval(_FENCE_SQL, story_id) or 0)
    except Exception:                                       # noqa: BLE001
        return 0


async def derive_key(conn, tool: str, goal_id: str, story_id: str,
                     payload: dict) -> str:
    """The idempotency key for one GO write, when the model gave none.

    A model-supplied key always wins -- the caller checks that first and
    only falls through to here when the argument is missing, so a client
    that does its own retry bookkeeping keeps it.
    """
    if tool in _ALWAYS_NEW:
        return f"go:{tool}:{uuid.uuid4().hex[:16]}"

    digest = _digest(tool, goal_id, story_id, payload)
    if tool in _UNFENCED:
        return f"go:{tool}:{digest}"

    # Everything else is fenced, including a verb nobody has classified
    # yet: fencing is the conservative default, because its failure is a
    # duplicate that had to wait for the run to move, and the other way
    # round the failure is a silent no-op.
    return f"go:{tool}:{digest}:{await _fence(conn, story_id)}"
