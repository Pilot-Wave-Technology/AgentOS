"""Kernel decisions onto the ledger, drained from the tables that hold them.

A DRAIN, NOT EMIT CALLS SCATTERED THROUGH THE KERNEL, for three reasons that
each decided it on their own.

COMPLETENESS. Execution state is written from many places, and
`execution_transitions` is filled by a DATABASE TRIGGER on
`kernel.executions` -- so the trigger is the one choke point every path
already goes through. Emitting from Python call sites means finding them
all, and the next one somebody adds is a silent hole in the audit trail. The
`check_story_state_ledger.sh` guard exists because that exact thing happened
with story state: eleven writers, two of which emitted.

SAFETY. An audit trail must not be able to break what it audits. A drain
runs beside the work rather than inside it, so a failure leaves the ledger
behind -- visible, and fixable by running again -- instead of failing a
verdict.

HISTORY. Every act already taken is still in these tables, so the same pass
that keeps up is also the backfill. There is no separate one-shot script to
write, get wrong, and re-run.

Idempotent by the ledger's own `idempotency_key`, so re-running is normal
rather than dangerous.
"""
from __future__ import annotations

import logging

from kernel.ledger.actors import (EXECUTOR, VERIFIER, from_provenance,
                                  kernel_actor, principal_for, uid_of)
from kernel.ledger.emit import append

logger = logging.getLogger(__name__)

# Terminal states end a run; DISPATCHING -> RUNNING begins one. The other
# ~250 transitions are the machine breathing -- park, un-park, re-verify --
# and belong in `execution_transitions`, not on a hash-chained audit trail.
_ENDED = ("DONE", "FAILED", "CANCELLED")

# What a proposal MEANS, as an auditable act. `answer` and `question` are
# conversation and stay off the ledger; the rest are decisions.
_PROPOSAL_EVENT = {
    "completion": "run.completion_claimed",
    "cancel": "run.cancelled",
    "hold": "run.paused",
    "steer": "run.steered",
    "replan": "run.replanned",
}


async def drain_all(conn, limit: int = 500) -> list[dict]:
    """Every pass, in the order things happen. Returns tick actions."""
    from kernel.ledger.resources import drain_resource_events

    total = 0
    # `_resources` folds in the control panel's second event store. It rides
    # this pass rather than getting its own runner because there is one
    # periodic tick and one idempotency mechanism, and a second of either is
    # how the two stores diverged in the first place.
    for pas in (_transitions, _plans, _proposals, _verdicts,
                drain_resource_events):
        try:
            total += await pas(conn, limit)
        except Exception as exc:                            # noqa: BLE001
            logger.warning("ledger drain pass %s failed: %s",
                           pas.__name__, exc)
    return [{"action": "ledger_drain", "events": total}] if total else []


async def _ctx(conn, cache: dict, execution_id: int) -> dict:
    """The principal chain, or {} for a run that cannot be ledgered.

    `ledger_events.context_goal_id` is NOT NULL, and 73 executions in this
    database have no goal — 60 of them `null-exec-test` fixtures. Letting
    those reach the INSERT produced a failed statement and a WARNING per
    row per tick: sixty lines of noise that would bury the one real failure
    anybody needs to see. Skipped here instead, deliberately and silently.
    """
    if execution_id not in cache:
        got = await principal_for(conn, execution_id)
        cache[execution_id] = got if got.get("goal_id") else {}
    return cache[execution_id]


def _body(ctx: dict, execution_id: int, extra: dict) -> dict:
    return {"execution_id": execution_id, "story_id": ctx.get("story_id"),
            "goal_id": ctx.get("goal_id"), "executor_kind": ctx.get("kind"),
            "executor_domain": ctx.get("domain"),
            "actor_uid": ctx.get("human_uid"), **extra}


async def _write(conn, ctx, execution_id, event_type, actor, payload, key,
                 on_behalf_of=True) -> int:
    actor_id, actor_type = actor
    got = await append(
        conn, event_type=event_type, actor_id=actor_id, actor_type=actor_type,
        payload=_body(ctx, execution_id, payload),
        # A human acting IS the principal; naming them twice would read as
        # somebody acting on their own behalf, which is noise.
        on_behalf_of=(ctx.get("human_actor_id")
                      if on_behalf_of and actor_type != "human" else None),
        goal_id=ctx.get("goal_id"), thread_id=ctx.get("thread_id"),
        # The specific executor/verifier, not just its kind.
        instance=f"exec:{execution_id}",
        idempotency_key=key)
    return 1 if got else 0


async def _transitions(conn, limit: int) -> int:
    rows = await conn.fetch(
        """SELECT t.id, t.execution_id, t.from_state, t.to_state, t.generation
             FROM execution_transitions t
            WHERE t.to_state = ANY($1::text[])
               OR (t.to_state = 'RUNNING' AND t.from_state = 'DISPATCHING')
         ORDER BY t.id DESC LIMIT $2""", list(_ENDED), limit)
    cache, n = {}, 0
    for r in rows:
        ctx = await _ctx(conn, cache, r["execution_id"])
        if not ctx:
            continue
        started = r["to_state"] == "RUNNING"
        n += await _write(
            conn, ctx, r["execution_id"],
            "run.started" if started else "run.finished",
            await kernel_actor(conn, EXECUTOR, ctx.get("kind"), ctx.get("domain")),
            {"generation": r["generation"]} if started
            else {"final_state": r["to_state"], "from_state": r["from_state"]},
            f"run.transition:{r['id']}")
    return n


async def _plans(conn, limit: int) -> int:
    """A plan proposed, and A PERSON DECIDING ON IT.

    The decision is the reason this module exists: it is a human authorising
    machine work and the ledger had no record of it. The actor is the human,
    not the kernel — they decided, the kernel only asked.
    """
    rows = await conn.fetch(
        """SELECT q.id, q.execution_id, q.status, q.answered_by, q.declined,
                  jsonb_array_length(COALESCE(q.checks, '[]'::jsonb)) AS checks
             FROM questions q
            WHERE q.kind = 'authorization'
         ORDER BY q.id DESC LIMIT $1""", limit)
    cache, n = {}, 0
    for r in rows:
        ctx = await _ctx(conn, cache, r["execution_id"])
        if not ctx:
            continue
        n += await _write(
            conn, ctx, r["execution_id"], "run.plan_proposed",
            await kernel_actor(conn, EXECUTOR, ctx.get("kind"), ctx.get("domain")),
            {"question_id": r["id"], "checks": r["checks"]},
            f"run.plan_proposed:{r['id']}")

        if r["status"] != "ANSWERED":
            continue
        # THROUGH THE SAME RESOLVER as proposals. `answered_by` speaks the
        # same vocabulary — `assistant:ker05`, `kernel`, an email — and
        # treating it as a bare uid attributed five assistant approvals and
        # four kernel auto-approvals to the EXECUTOR, i.e. recorded the
        # executor approving its own plan. `resolved` is False only when the
        # string names nobody we know, and then the event says so.
        actor_id, actor_type, resolved = await from_provenance(
            conn, r["answered_by"], ctx)
        decision = "rejected" if r["declined"] else "approved"
        n += await _write(
            conn, ctx, r["execution_id"], "run.plan_decided",
            (actor_id, actor_type),
            # ONE FORM. `decided_by` is a uid like every other identity key
            # in a payload — the source column holds an email, and copying
            # it verbatim is how one key came to hold emails, prefixed
            # provenance strings and bare slugs all at once. The original is
            # kept beside it under a name that says what it is, because an
            # audit trail should not discard the string it was given.
            {"question_id": r["id"], "decision": decision,
             "decided_by": await uid_of(conn, actor_id),
             "decided_by_raw": r["answered_by"],
             "actor_unresolved": None if resolved else True},
            f"run.plan_decided:{r['id']}")
    return n


async def _proposals(conn, limit: int) -> int:
    rows = await conn.fetch(
        """SELECT p.id, p.execution_id, p.kind, p.status, p.actor_id
             FROM proposals p WHERE p.kind = ANY($1::text[])
         ORDER BY p.id DESC LIMIT $2""", list(_PROPOSAL_EVENT), limit)
    cache, n = {}, 0
    for r in rows:
        ctx = await _ctx(conn, cache, r["execution_id"])
        if not ctx:
            continue
        # Read off the stamped provenance rather than guessed: a `hold` is a
        # person pausing, a `steer` may be the verifier or a person, and only
        # the row knows which.
        actor_id, actor_type, resolved = await from_provenance(
            conn, r["actor_id"], ctx)
        n += await _write(
            conn, ctx, r["execution_id"], _PROPOSAL_EVENT[r["kind"]],
            (actor_id, actor_type),
            {"proposal_id": r["id"], "status": r["status"],
             "provenance": r["actor_id"],
             "actor_unresolved": None if resolved else True},
            f"run.proposal:{r['id']}")
    return n


async def _verdicts(conn, limit: int) -> int:
    rows = await conn.fetch(
        """SELECT v.id, v.execution_id, v.claim_seq, v.proposal_id, v.passed,
                  v.refusal::text AS refusal
             FROM verdicts v ORDER BY v.id DESC LIMIT $1""", limit)
    cache, n = {}, 0
    for r in rows:
        ctx = await _ctx(conn, cache, r["execution_id"])
        if not ctx:
            continue
        n += await _write(
            conn, ctx, r["execution_id"], "run.verdict",
            # THE VERIFIER, not the executor. An audit trail that cannot
            # tell the judge from the judged is not one.
            await kernel_actor(conn, VERIFIER, ctx.get("kind"), ctx.get("domain")),
            {"verdict_id": r["id"], "passed": r["passed"],
             "claim_seq": r["claim_seq"], "proposal_id": r["proposal_id"],
             "refusal": (r["refusal"] or "")[:2000] or None},
            f"run.verdict:{r['id']}")
    return n
