"""H1a -- the assistant's mailbox, drained at the step boundary.

HERE AND NOWHERE ELSE, and the reason is a message-list invariant rather
than tidiness. Between steps the conversation is coherent for every
provider; INSIDE a step it is a tool call waiting for its result, and an
injected user message would split that pair and be rejected.

DRAINED BEFORE THE REQUEST IS BUILT, so an intent that arrived during
the previous model call reaches THIS one rather than the next. A steer
that lands a step late has already paid for a step of work in the
direction it was trying to change.

THREE DIFFERENT THINGS ARRIVE THROUGH ONE DOOR and they are not variants
of each other:

  a HOLD    a person asked a question, the answer turned up something
            they must decide, and work must not build on the assumption
            it questions. THE SAME PARK A PLAN APPROVAL TAKES -- the leg
            ends at the boundary, the sandbox stays hot, answering
            resumes it. Nothing here is a new way for a story to end.
  a STOP    cancellation. Recorded, then the leg ends.
  a STEER   a standing amendment, which only extends the message list.
            `kernel/harness/loop_preamble.py` reloads it every leg,
            because an injection made here dies with this leg's list.

A REFUSED PARK IS NOT AN ERROR. The commonest cause is that the run is
already parked on something else, so a person is already being asked and
this leg has nothing to add by ending. Falling through is the honest
outcome, and a future reader must not "fix" it into a raise.
"""

from __future__ import annotations

from kernel.harness.intake import apply_cancellation, take_proposals
from kernel.harness.loop_endings import Fired
from kernel.harness.park import ParkRequested, apply_hold


async def drain_intake(conn, execution_id: int, generation: int,
                       tool_executor, current_messages: list[dict]):
    """Returns `(ending, parked)` -- both None when the leg carries on.

    It MUTATES `current_messages` and `tool_executor` on purpose: a steer
    has to land in the list the next request is built from, and a revoked
    plan has to block the very next write in THIS leg. The durable record
    of both is in the database; these two are the in-flight half.
    """
    # ── H1a: the assistant's mailbox, drained at the step boundary ───
    # Here and nowhere else. Between steps the message list is coherent
    # for every provider; inside a step it is a tool call waiting for
    # its result, and an injected user message would split the pair.
    #
    # Drained BEFORE the request is built so an intent that arrived
    # during the previous model call reaches this one rather than the
    # next — a steer that lands a step late has already paid for a step
    # of work in the direction it was trying to change.
    intake = await take_proposals(conn, execution_id)
    if intake.held:
        # A person asked a question, the answer turned up something
        # they have to decide, and the decision has to be made before
        # more work builds on the assumption it questions.
        #
        # THE SAME PARK A PLAN APPROVAL TAKES, deliberately: the leg
        # ends at the boundary, the sandbox stays hot, and answering
        # resumes it. Nothing here is a new way for a story to end —
        # the run is stopped, not finished, and the console, the
        # cancel edge and the card sweeper all already know what a
        # parked run is.
        held_on = await apply_hold(conn, execution_id, generation,
                                   intake.hold or {})
        if held_on is not None:
            park = ParkRequested(
                held_on, "direction",
                intake.stop_reason or "a person must decide")
            return Fired("parked",
                         {"request_id": held_on, "kind": "direction",
                          "summary": intake.stop_reason}), park
        # THE PARK WAS REFUSED, so nothing stopped. Commonest cause is
        # that the run is already parked on something else, in which
        # case a person is already being asked and the leg has nothing
        # to add by ending. Falling through is the honest outcome.
    if intake.stop and not intake.held:
        await apply_cancellation(conn, execution_id, intake.stop_reason)
        return Fired(
            intake.stop,
            {"reason": intake.stop_reason or "no reason given"}), None
    if intake.revoke_plan and tool_executor is not None:
        # Blocks the very next write in THIS leg. The durable record is
        # in the database; without this the model would get one more
        # step of changes under a plan already withdrawn.
        setattr(tool_executor, "_plan_approved", False)
    current_messages.extend(intake.messages)
    return None, None
