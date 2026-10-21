"""Pause, resume, cancel — the run, and exactly what the buttons do.

WHY ONE TOOL AND NOT THREE. These are one control surface: the same
three verbs, on the same run, against the same state machine, and a
person says "hold that" and "no, stop it" in the same breath. Three
tools meant three chances to wire one to a different mechanism, which is
what happened -- `pause_errand` wrote `execution_pauses` while the
Pause button submitted a `hold` proposal, so the same word meant two
things and "why is it not paused" had two answers.

THE RUN, NOT THE STORY, and that distinction is the reason this exists
beside `cancel_story` rather than replacing it. `cancel_story` ends the
STORY; for a recurring errand its next run still comes. This ends the
run in flight and leaves the errand alone. A person who wants the whole
errand stopped wants the grant revoked, which is a third thing again.

EVERY ACTION CARRIES A PERSON'S NAME. `human_uid` is required on all
three and refused without, for the reason `cancel_story` gives: these
stop or alter work somebody approved. GO relays a decision; it does not
take one. The same rule the Pause button obeys by construction -- it can
only be pressed by somebody signed in.

MIRRORS `app/services/kernel_run_control.py` DELIBERATELY AND WILL
DRIFT FROM IT IF SOMEBODY IS CARELESS. It cannot import it: that module
lives in `app/` and this is served from a container that ships `kernel/`
and no `app/` at all. What keeps them honest is that both go through
`kernel.mailbox` and `park_resolve` -- the mechanism is shared even
though the two callers are not, so a change to what a `hold` MEANS
reaches both.
"""

from __future__ import annotations

import logging
import uuid

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

ACTIONS = ("pause", "resume", "cancel")

# Mirrors kernel_run_control. `direction` is the question a hold opens --
# `park.apply_hold` writes it, and answering it is what resumes the run.
PAUSABLE = ("RUNNING",)
TERMINAL = ("DONE", "FAILED", "CANCELLED")
HOLD_KIND = "direction"


async def _live_run(conn, story_id: str):
    """The story's run that has not finished, or None.

    Newest first: a story amended after finishing goes DONE -> PENDING
    and gets a second execution, and the controls belong to the live one.
    """
    return await conn.fetchrow(
        "SELECT id, state FROM kernel.executions "
        " WHERE story_id = $1 AND state <> ALL($2::text[]) "
        " ORDER BY id DESC LIMIT 1", story_id, list(TERMINAL))


async def _submit(conn, execution_id: int, kind: str, payload: dict,
                  human_uid: str, actor_id: str = ""):
    """Put an intent in the run's mailbox. The leg takes it at a boundary.

    WHO ACTED AND WHO DECIDED ARE TWO FACTS, and this used to record one.
    `actor_id` was `f"human:{human_uid}"`, so a cancellation GO submitted
    was indistinguishable in the ledger from a person pressing the
    button -- GO vanished from its own act.

    Run 345: GO judged a queueing run to have stalled and cancelled it,
    passing the goal owner's uid because it knew it. `proposals.actor_id`
    read `human:rPeDJ...`, and the audit trail said a person authorised a
    judgment call no person made.

    So the ACTOR is whoever called -- `go:err16` for GO, `human:<uid>`
    for a person -- and the human whose decision is being carried rides
    in the payload as `human_uid`. Both are recorded, neither is
    disguised as the other, and a reader can tell relayed from pressed.
    """
    from kernel.mailbox.api import submit_proposal

    got = await submit_proposal(
        conn, actor_id or f"human:{human_uid}", kind,
        dict(payload, human_uid=human_uid),
        idempotency_key=f"{kind}:{execution_id}:{uuid.uuid4().hex[:12]}",
        execution_id=execution_id)
    if not isinstance(got, int):
        return Refused("E_KERNEL_REFUSED",
                       f"the kernel refused that: "
                       f"{getattr(got, 'message', got)}")
    return None


async def run_control(conn, goal_id: str, story_id: str, action: str,
                      human_uid: str, message: str = "",
                      actor_id: str = "") -> dict | Refused:
    """One of pause / resume / cancel, on this errand's live run."""
    if action not in ACTIONS:
        return Refused("E_UNKNOWN_ACTION",
                       f"{action!r} is not a run control. Use one of: "
                       f"{', '.join(ACTIONS)}.")
    if not (human_uid or "").strip():
        return Refused(
            "E_NO_HUMAN",
            f"{action} stops or alters work somebody approved. Say whose "
            f"decision it is, by uid -- yours is not one. If nobody has "
            f"decided yet, say so on the errand's thread and ask.")
    human_uid = human_uid.strip()

    run = await _live_run(conn, story_id)
    if run is None:
        return Refused("E_NO_RUN",
                       f"{story_id} has no run in flight, so there is "
                       f"nothing to {action}.")

    if action == "pause":
        return await _pause(conn, run, human_uid, message, actor_id)
    if action == "cancel":
        return await _cancel(conn, run, human_uid, message, actor_id)
    return await _resume(conn, run, human_uid, message, actor_id)


async def _pause(conn, run, human_uid: str, reason: str,
                 actor_id: str = ""):
    if run["state"] not in PAUSABLE:
        return Refused(
            "E_NOT_WORKING",
            f"that run is {run['state'].lower().replace('_', ' ')}, not "
            f"working, so there is no leg to stop. A run parked on a plan "
            f"is already waiting for a person.")
    bad = await _submit(conn, run["id"], "hold",
                        {"decision": reason.strip() or
                         "A person paused this run from its GO thread.",
                         "why_now": "paused from the errand's thread"},
                        human_uid, actor_id)
    if bad is not None:
        return bad
    logger.info("go run_control: pause run=%s for %s", run["id"], human_uid)
    return {"execution_id": run["id"], "action": "pause",
            "detail": "it stops at its next step boundary, not mid-step, "
                      "so a step already in flight finishes first"}


# A run in one of these has not started. There is no leg, no pod and no
# executor -- only a place in a queue.
NOT_STARTED = ("PENDING", "ADMITTED", "DISPATCHING")


async def _cancel(conn, run, human_uid: str, reason: str,
                  actor_id: str = ""):
    # RUN 345. A pod that cannot be placed sets `stall_reason` while the
    # run is still DISPATCHING; the check-in reported a stall; GO asked
    # the executor what was happening -- there being no executor to
    # answer -- and cancelled eighty seconds in. `promote_row` documents
    # ninety seconds Unschedulable as normal.
    #
    # Refused here rather than left to the prompt, because the reasoning
    # that produces it is sound and will recur: the run IS stalled, it IS
    # producing nothing, and cancelling IS what you do with a dead run.
    # What GO cannot see is that a queue is not the work.
    #
    # NOT A BAN ON CANCELLING, a ban on cancelling BLIND: a person can
    # still stop a queued run from the board, which goes nowhere near
    # this path.
    if run["state"] in NOT_STARTED:
        return Refused(
            "E_NOT_STARTED",
            f"that run is {run['state'].lower()} -- it has not started, "
            f"so there is nothing running to stop. It is waiting for the "
            f"cluster to place its pod, which routinely takes a minute or "
            f"two and sometimes longer. Cancelling frees no capacity, so "
            f"it cannot make the wait shorter; it only throws away the "
            f"errand's place in the queue. Say it is queued and leave it. "
            f"If somebody wants it gone regardless, that is the Cancel "
            f"button on the board and it is theirs to press.")
    bad = await _submit(conn, run["id"], "cancel",
                        {"reason": reason.strip() or
                         "Cancelled by a person from the errand's thread."},
                        human_uid, actor_id)
    if bad is not None:
        return bad
    logger.info("go run_control: cancel run=%s by %s for %s",
                run["id"], actor_id or "human", human_uid)
    return {"execution_id": run["id"], "action": "cancel",
            "detail": "a running leg stops at its next step boundary; a "
                      "parked one ends on the next sweep. THIS ENDS THE "
                      "RUN, NOT THE ERRAND -- the errand stays approved "
                      "and can be run again."}


async def _resume(conn, run, human_uid: str, message: str,
                  actor_id: str = ""):
    """Release the hold. A message rides along as a STEER, acted on first.

    THE STEER GOES IN BEFORE THE UN-PARK. `take_proposals` drains the
    mailbox at the top of the step loop, before the first model call, so
    a steer submitted now is read before work continues. Submitted after
    the un-park it would be a step late, which is the whole thing the
    ordering exists to avoid.
    """
    qid = await conn.fetchval(
        "SELECT id FROM kernel.questions WHERE execution_id = $1 "
        " AND kind = $2 AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
        run["id"], HOLD_KIND)
    if qid is None:
        return Refused(
            "E_NOT_HOLDING",
            f"that run is {run['state'].lower().replace('_', ' ')} and is "
            f"not holding on a pause. If it is parked on a PLAN, a person "
            f"approves that from its card -- you cannot, and neither can "
            f"this.")

    steered = bool(message.strip())
    if steered:
        bad = await _submit(conn, run["id"], "steer",
                            {"instruction": message.strip()}, human_uid)
        if bad is not None:
            return bad

    # Never empty, and never the word "approved": `resolve_request` would
    # otherwise have the executor read "WHAT THEY SAID: approved" about a
    # pause in which nobody approved anything.
    said = ("A person paused this run and left you an instruction. It is "
            "in front of you as a standing steer -- read it and act on it "
            "before you carry on." if steered else
            "Carry on. Nothing to change -- this was a pause, not a "
            "decision about the work.")

    from kernel.harness.park_resolve import resolve_request

    await resolve_request(conn, qid, said, answered_by="go",
                          verdict="approve", human_uid=human_uid)
    logger.info("go run_control: resume run=%s for %s (steer=%s)",
                run["id"], human_uid, steered)
    return {"execution_id": run["id"], "action": "resume",
            "steered": steered,
            "detail": "the hold is released" +
                      (" and your instruction is in front of it"
                       if steered else "")}
