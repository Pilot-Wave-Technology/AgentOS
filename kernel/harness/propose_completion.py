"""propose_completion tool — E5 increment of Stage E.

One claim, idempotent, the run does not end.

Re-claim guard (R1): a BEFORE INSERT trigger (proposals_reclaim_fence)
rejects an insert whose reason_hash matches an existing REJECTED row for
the same execution_id.  The trigger RAISEs, which aborts the transaction.
The tool wraps the INSERT in a savepoint so the raise rolls back only the
claim attempt — the step-journal write for this tool call must survive.

Catches asyncpg.RaiseError matching "proposals reclaim fence" (re-raises
anything else, same pattern as C7's step_journal generation fence check).

The reason set contains fail criteria only — never flake, never error.
A flaky test in the reason set would permanently block re-claiming,
which is the Fb2 pathology.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

import asyncpg


class InvalidReasonSetError(Exception):
    """Raised when the reason set contains non-fail criteria."""


@dataclass(frozen=True)
class ProposalResult:
    """Result of a propose_completion call."""
    accepted: bool
    proposal_id: Optional[int]
    message: str


def _compute_reason_hash(reason_set: list[tuple[str, str]]) -> str:
    """SHA-256 of the sorted, serialised reason set."""
    sorted_set = sorted(reason_set, key=lambda x: (x[0], x[1]))
    serialised = json.dumps(sorted_set, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode()).hexdigest()


def _validate_reason_set(reason_set: list[tuple[str, str]]) -> None:
    """Ensure the reason set contains only fail criteria.

    Never flake, never error — those would permanently block re-claiming
    (the Fb2 pathology).
    """
    for code, locator in reason_set:
        if not code or not isinstance(code, str):
            raise InvalidReasonSetError(f"Invalid reason code: {code!r}")
        # flake and error are forbidden in the reason set
        code_lower = code.lower()
        if "flake" in code_lower:
            raise InvalidReasonSetError(
                f"Reason set must contain fail criteria only, got flake: {code!r}"
            )
        if code_lower.startswith("e_error") or code_lower == "error":
            raise InvalidReasonSetError(
                f"Reason set must contain fail criteria only, got error: {code!r}"
            )


async def propose_completion(
    conn,
    execution_id: int,
    actor_id: str,
    reason_set: list[tuple[str, str]],
    idempotency_key: str,
) -> ProposalResult:
    """Propose completion of a story execution.

    Args:
        conn: asyncpg connection (search_path must include kernel).
        execution_id: the execution claiming completion.
        actor_id: who is proposing (assistant identity).
        reason_set: list of (code, locator) pairs — fail criteria only.
        idempotency_key: caller-built key, e.g. completion:{eid}:{count}.

    Returns:
        ProposalResult with accepted=True if the claim was inserted,
        accepted=False if it was a duplicate (ON CONFLICT) or if the
        re-claim guard trigger fired (unchanged reason set).
    """
    # Validate reason set — fail only
    _validate_reason_set(reason_set)

    reason_hash = _compute_reason_hash(reason_set)
    reason_set_json = json.dumps(
        [list(pair) for pair in sorted(reason_set, key=lambda x: (x[0], x[1]))],
        separators=(",", ":"),
    )

    # Wrap in a savepoint so the trigger RAISE rolls back only the claim
    # attempt, not the surrounding transaction (step-journal writes survive).
    try:
        async with conn.transaction(isolation="read_committed") as tx:
            # This is actually a savepoint within the outer transaction
            proposal_id = await conn.fetchval(
                """
                INSERT INTO proposals
                    (actor_id, kind, idempotency_key, execution_id, payload,
                     reason_hash, reason_set)
                VALUES ($1, 'completion', $2, $3, '{}',
                        $4, $5::jsonb)
                -- THREE columns, matching proposals_idempotency.
                -- H10b widened this index to include execution_id --
                -- one key reused across three stories used to be ONE
                -- proposal, with calls two and three reporting
                -- success having written nothing -- and updated
                -- mailbox/api.py but not here. A two-column ON
                -- CONFLICT matches no index, so every completion
                -- proposal has raised InvalidColumnReferenceError
                -- since. Nothing caught it because the gate that
                -- covers it is not in the H-series I had been
                -- running.
                ON CONFLICT (actor_id, execution_id, idempotency_key)
                DO NOTHING
                RETURNING id
                """,
                actor_id,
                idempotency_key,
                execution_id,
                reason_hash,
                reason_set_json,
            )
    except asyncpg.RaiseError as e:
        if "proposals reclaim fence" in str(e):
            return ProposalResult(
                accepted=False,
                proposal_id=None,
                # F11 — the old message was accurate and said nothing
                # about what to do instead. The instruction to ask was a
                # leg ago, in a prompt this refusal does not repeat, and
                # a model that reads only this has been told it is stuck
                # and not how to stop being stuck.
                message=(
                    "Re-claim refused: nothing has changed since the last "
                    "refusal — the same checks fail for the same reasons, "
                    "so this claim would be adjudicated to the same answer. "
                    "Either fix what the refusal named, or, if you believe "
                    "a check is asking for something impossible or "
                    "contradicts the task, say so with ask_human and name "
                    "the check. A person can amend it or accept the story "
                    "over the Verifier; you cannot, and repeating the "
                    "claim will not become a different answer."),
            )
        raise  # re-raise anything else — same pattern as C7

    if proposal_id is None:
        # ON CONFLICT — duplicate idempotency key
        return ProposalResult(
            accepted=False,
            proposal_id=None,
            message="Duplicate claim (idempotency key already exists)",
        )

    return ProposalResult(
        accepted=True,
        proposal_id=proposal_id,
        message="Completion proposed (PENDING)",
    )
