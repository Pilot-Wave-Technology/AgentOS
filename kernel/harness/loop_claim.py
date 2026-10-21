"""propose_completion: the one thing the model may assert about the outcome.

A CLAIM USED TO COST LESS THAN A TEST. `propose_completion` took prose
about work the model said it had done, and nothing required the plan's
own checks to have been run. The careless sequence and the honest one
looked identical at this point and only diverged in the verifier --
minutes later, in another container, with this executor's context
already discarded.

So the gate is here, in front of the record. Running the plan's checks
costs one repeat of a suite the verifier is about to run anyway, and a
failure comes back as the TOOL RESULT, so the executor gets the output
while it still has the code in mind. A refused claim is not recorded and
does not end the run: it is a message to an executor that is still
holding the problem.

WHY THIS IS A FILE AND NOT A BRANCH IN THE LOOP. It is the only place in
a leg where the model's word becomes a durable assertion, and three
different things hang off that moment -- the self-check gate, the
idempotent claim, and Stage L's record of what the work taught. Reading
them together is how anyone can see that the order is: test, then claim,
then learn.

THE `claim_count` ARITHMETIC IS DELIBERATE and must not be "tidied".
`idem` is built from the count BEFORE this claim, so a retried leg
rebuilds the same idempotency key; the proposal reports the count
INCLUDING it, which is what the loop's return value has always meant by
`claim_count`. The caller advances its own counter only when a proposal
comes back, so a refused claim does not burn a key.
"""

from __future__ import annotations

import logging

from kernel.harness.models.types import ToolResult
from kernel.harness.propose_completion import propose_completion

logger = logging.getLogger(__name__)


async def settle_claim(conn, tool_executor, tc_args: dict, tc_id: str, *,
                       execution_id: int, actor_id: str,
                       claim_count: int) -> tuple[ToolResult, dict | None]:
    """Test the claim, then record it. Returns the tool result, and the
    proposal if one was actually made.

    A `None` proposal means the run's own checks refused it -- nothing
    was written, the executor has the failing output, and the leg
    carries on.
    """
    proposal = None
    reason_set_raw = tc_args.get("reason_set", [])
    # Normalise at the boundary: accept str | dict | list/tuple,
    # emit (code, locator) tuples. A bare string becomes (str, "").
    reason_set = []
    for r in (reason_set_raw or []):
        if isinstance(r, str):
            reason_set.append((r, ""))
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            reason_set.append((str(r[0]), str(r[1])))
        elif isinstance(r, dict):
            reason_set.append((r.get("code", ""), r.get("locator", "")))
        else:
            reason_set.append((str(r), ""))

    # ── WHAT THE RECORD SAYS, BEFORE THE CLAIM IS WRITTEN ─
    #
    # ARTIFACT CHECKS ONLY, and cheaply: one indexed read of
    # the manifest per check. The executor cannot satisfy
    # one without actually emitting the thing, so this is
    # measured rather than attested -- and a failure comes
    # back as the tool result, so the run fixes it with the
    # context still live instead of parking a person to say
    # the deliverable is missing.
    #
    # COMMAND CHECKS ARE NOT RE-RUN HERE. They ran as a
    # foreground `bash` pinned to the 120s ceiling, which
    # meant a repository whose suite takes longer could
    # never claim at all; and running them uncapped would
    # repeat, at every claim attempt, a suite the executor
    # has just finished running as its work. It reports the
    # numbers instead. When the platform switch turns
    # verification on they are re-run in a clean container,
    # which is what that switch is for.
    from kernel.harness.selfcheck import (refusal_text,
                                          run_own_checks)
    selfcheck = await run_own_checks(conn, execution_id)

    if selfcheck["failed"]:
        # NOT RECORDED AS A CLAIM. The run continues and the
        # executor gets the failing output as its tool
        # result, which is the whole point: it still holds
        # the context to fix what broke.
        claim_result = ToolResult(
            tool_call_id=tc_id, output="",
            error=refusal_text(selfcheck), exit_code=1)
        logger.info("execution %s: completion refused by the record "
                    "(%s of %s artifact checks failed)", execution_id,
                    len(selfcheck["failed"]), selfcheck["ran"])
    else:
        idem = f"completion:{execution_id}:{claim_count}"

        pc_result = await propose_completion(
            conn, execution_id, actor_id, reason_set, idem,
        )

        # Stage L — what the WORK taught, recorded here
        # because here is where it is known.
        #
        # The plan said what would be done and why; only the
        # run can say what turned out to be true. And only
        # the executor knows it first-hand: an assistant
        # summarising afterwards is reading a diff, and it
        # would sound exactly as certain.
        #
        # Written at the CLAIM rather than left for the
        # report to collect, so a claim that is later
        # refused still leaves its findings behind — the
        # runs worth learning from are disproportionately
        # the ones that did not go straight through.
        #
        # Best-effort by design: a story must never fail to
        # complete because a note about it could not be
        # filed.
        try:
            from kernel.knowledge.write import record_cycle
            from kernel.knowledge.read import plans_for
            srow = await conn.fetchrow(
                "SELECT goal_id, story_id FROM executions "
                " WHERE id = $1", execution_id)
            if srow and srow["story_id"]:
                cycles = await plans_for(conn, srow["story_id"])
                await record_cycle(
                    conn, srow["goal_id"], srow["story_id"],
                    len(cycles) or 1,
                    techniques=tc_args.get("techniques"),
                    learned=tc_args.get("learned"),
                    abandoned=tc_args.get("abandoned"),
                    actor=actor_id)
        except Exception:                       # noqa: BLE001
            logger.exception(
                "execution %s: recording what the work taught "
                "failed; the claim stands", execution_id)
        proposal = {
            "accepted": pc_result.accepted,
            "proposal_id": pc_result.proposal_id,
            "message": pc_result.message,
            "claim_count": claim_count + 1,
        }
        passed = ("" if selfcheck["skipped"] else
                  f"Your {selfcheck['ran']} check(s) passed here "
                  f"first. ")
        claim_result = ToolResult(
            tool_call_id=tc_id,
            output=passed + pc_result.message,
            exit_code=0 if pc_result.accepted else 1)
    return claim_result, proposal
