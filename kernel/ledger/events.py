"""The kernel's auditable acts. Decisions, not mechanics.

WHAT BELONGS HERE AND WHAT DOES NOT. A story's run produces 173 journalled
steps and about ten things anybody would ever audit. A tool call is
telemetry -- it belongs in `step_journal`, where it already is, and putting
it on the hash-chained ledger would bury the decisions under the mechanics
and multiply the ledger by twenty for no gain.

An act is auditable when someone could later need to ask WHO DECIDED THAT:

    run.started        work began on a story, under a named executor kind
    run.plan_proposed  the executor said what it intends to do
    run.plan_decided   A PERSON approved or rejected it  <-- the important one
    run.completion_claimed   the executor said it was finished
    run.verdict        the verifier accepted or refused that claim
    run.controlled     a person paused, resumed or cancelled it
    run.finished       the run reached a terminal state

`run.plan_decided` is the reason this module exists. It is a human
authorising machine work, it happened four times on story 1cbc7b3e, and the
ledger had no record that any of them occurred.

EVERY EVENT NAMES A PERSON. `on_behalf_of` carries the human whose thread
owns the story, even when the actor is an executor -- so no kernel act is
attributable only to a robot, and the taximeter can roll all of it up to a
uid. `payload.actor_uid` carries the same person as a plain string, because
that is the shape the taximeter views already read.
"""
from __future__ import annotations

from kernel.ledger.actors import (EXECUTOR, VERIFIER, human_actor,
                                  kernel_actor, principal_for)
from kernel.ledger.emit import append


async def _emit(conn, execution_id: int, event_type: str, payload: dict, *,
                role: str = EXECUTOR, actor_uid: str | None = None,
                key: str | None = None):
    """Resolve the principal chain, then append. The shared spine."""
    ctx = await principal_for(conn, execution_id)
    if not ctx:
        return None

    # A human acting (approving a plan) is the actor. A machine acting is the
    # actor and the human is who it acted FOR. Both shapes name the person.
    human_id = None
    if actor_uid:
        human_id = await human_actor(conn, actor_uid)
    if actor_uid and human_id:
        actor_id, actor_type = human_id, "human"
        on_behalf_of = None
    else:
        actor_id, actor_type = await kernel_actor(
            conn, role, ctx.get("kind"), ctx.get("domain"))
        on_behalf_of = ctx.get("human_actor_id")

    body = {
        "execution_id": execution_id,
        "story_id": ctx.get("story_id"),
        "goal_id": ctx.get("goal_id"),
        "executor_kind": ctx.get("kind"),
        "executor_domain": ctx.get("domain"),
        # The taximeter reads `payload.actor_uid` as a plain string; matching
        # that shape means these events roll up with no view change.
        "actor_uid": actor_uid or ctx.get("human_uid"),
        **payload,
    }
    return await append(
        conn, event_type=event_type, actor_id=actor_id, actor_type=actor_type,
        payload=body, on_behalf_of=on_behalf_of, goal_id=ctx.get("goal_id"),
        thread_id=ctx.get("thread_id"), instance=f"exec:{execution_id}",
        idempotency_key=key)


async def run_started(conn, execution_id: int, generation: int = 1):
    return await _emit(conn, execution_id, "run.started",
                       {"generation": generation},
                       key=f"run.started:{execution_id}:{generation}")


async def plan_proposed(conn, execution_id: int, question_id: int,
                        steps: int = 0, checks: int = 0):
    return await _emit(conn, execution_id, "run.plan_proposed",
                       {"question_id": question_id, "steps": steps,
                        "checks": checks},
                       key=f"run.plan_proposed:{question_id}")


async def plan_decided(conn, execution_id: int, question_id: int,
                       decision: str, actor_uid: str | None,
                       edited_checks: bool = False):
    """A PERSON approving or rejecting a plan.

    The most auditable act in the system, and the one that was missing. The
    actor is the human, not the kernel: they decided, the kernel only asked.
    """
    return await _emit(conn, execution_id, "run.plan_decided",
                       {"question_id": question_id, "decision": decision,
                        "edited_checks": edited_checks},
                       actor_uid=actor_uid,
                       key=f"run.plan_decided:{question_id}:{decision}")


async def completion_claimed(conn, execution_id: int, proposal_id: int,
                             claim_seq: int | None = None):
    return await _emit(conn, execution_id, "run.completion_claimed",
                       {"proposal_id": proposal_id, "claim_seq": claim_seq},
                       key=f"run.completion_claimed:{proposal_id}")


async def verdict(conn, execution_id: int, *, passed: bool,
                  proposal_id: int | None, claim_seq: int | None,
                  criteria: int = 0, failed: int = 0,
                  refusal: str | None = None):
    """The verifier's judgement. Actor is the VERIFIER, not the executor —
    an audit trail that cannot tell the judge from the judged is not one."""
    return await _emit(conn, execution_id, "run.verdict",
                       {"passed": passed, "proposal_id": proposal_id,
                        "claim_seq": claim_seq, "criteria": criteria,
                        "failed_criteria": failed,
                        "refusal": (refusal or "")[:2000] or None},
                       role=VERIFIER,
                       key=f"run.verdict:{execution_id}:{claim_seq}:{proposal_id}")


async def controlled(conn, execution_id: int, action: str,
                     actor_uid: str | None, reason: str | None = None,
                     message: str | None = None, seq: int | None = None):
    """Pause, resume or cancel — always a person, never the machine.

    `seq` distinguishes repeated pauses on one run: they are separate
    decisions and each deserves its own row, so the idempotency key cannot
    be the run alone.
    """
    return await _emit(conn, execution_id, f"run.{action}",
                       {"action": action, "reason": (reason or "")[:500] or None,
                        "message": (message or "")[:2000] or None},
                       actor_uid=actor_uid,
                       key=f"run.{action}:{execution_id}:{seq if seq is not None else 'x'}")


async def finished(conn, execution_id: int, state: str):
    return await _emit(conn, execution_id, "run.finished", {"final_state": state},
                       key=f"run.finished:{execution_id}:{state}")
