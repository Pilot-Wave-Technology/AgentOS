"""What happens after a claim PASSES: push it, then ask a person.

WHY THIS IS ITS OWN FUNCTION. It is called from two places and used to
be written in only one of them. The normal path runs it straight after
writing a passing verdict. The recovery path runs it when a later tick
finds a claim that passed and a run that never left VERIFYING.

That second case is not hypothetical. Watched live on KER13: cycle two
verified, the verdict was written, and the run stopped there. Every
adjudicator tick after that printed

    {'action': 'already_adjudicated', 'detail': 'claim 63 was already passed'}

once every few seconds, for ever. The guard that prints it sits UPSTREAM
of this code, so the comment further down promising that "the next
adjudicator tick tries the park again" was describing something that
could not happen. The gap between writing a verdict and parking on an
acceptance is not recorded anywhere durable, so anything that interrupts
it — a restart, an exception, a refused park — stranded the run with its
work verified, pushed, and invisible.

IDEMPOTENT ON PURPOSE, because the recovery path re-runs it. Pushing
commits that are already pushed is a no-op, and `open_request` refuses a
second open question rather than creating one.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


async def finish_passed_claim(conn, eid: int, generation: int,
                              claim_seq: int, row: dict,
                              summary: str = "") -> dict:
    """Push, then park on acceptance (or finish, when nobody must accept).

    `summary` is the verification summary when the caller has one; the
    recovery path does not, and says so rather than inventing one.
    """
    from kernel.harness.park import open_request
    from kernel.workspace.deliver import publish_execution

    # ── DONE MEANS DELIVERED ──────────────────────────────────────────
    #
    # The verdict says the work is good; this says the team can get it.
    # The first live story passed verification with its seven commits
    # still local to the sandbox — the engine then merged the remote
    # branch it had cut, which held NOTHING, and announced a landing
    # that never happened.
    delivered, detail = await publish_execution(conn, eid)
    if not delivered:
        from kernel.worker import publish_block

        await conn.execute(
            "UPDATE executions SET stall_reason = $2 "
            "WHERE id = $1 AND state = 'VERIFYING'",
            eid, f"publish failed: {detail}"[:500])

        # THE SAME SPLIT AS THE UNVERIFIED PATH, and it has to be made
        # twice because the two paths are two callers of one publish:
        # a branch the executor can rebase goes back to the executor, a
        # forge refusal parks on the second one. Fixing only `loop.py`
        # would leave every VERIFIED story bouncing on a 404 until it
        # ran out of legs, which is the more expensive half.
        if not publish_block.is_executors(detail):
            count = await publish_block.note_forge_refusal(conn, eid)
            if count >= publish_block.PARK_AFTER:
                asked = await publish_block.park_on_forge_refusal(
                    conn, eid, generation, detail, count)
                if asked is not None:
                    return {"execution_id": eid,
                            "action": "publish_blocked",
                            "detail": f"claim {claim_seq} passed but the "
                                      f"forge refused the push {count} "
                                      f"times; parked for a person"}

        # BACK TO RUNNING AND A LEG SPENT, only when the executor is
        # being asked to do something about it.
        await conn.execute(
            "UPDATE executions SET state = 'RUNNING', "
            "generation = generation + 1 "
            "WHERE id = $1 AND state = 'VERIFYING'", eid)
        from kernel.verifier.adjudicator import _tell_executor
        await _tell_executor(
            conn, eid, generation,
            "Your work PASSED verification but could not be pushed: "
            + detail + ". Check the branch state (diverged? protected?), "
            "resolve, and propose completion again.", "vpub")
        logger.warning("execution %s: claim %s passed but publish FAILED: %s",
                       eid, claim_seq, detail)
        return {"execution_id": eid, "action": "publish_failed",
                "detail": f"claim {claim_seq} passed but the work could "
                          f"not be pushed: {detail}"}

    from kernel.worker.publish_block import clear_forge_refusals
    await clear_forge_refusals(conn, eid)

    # ── DONE IS A PERSON'S WORD (when the deployment says so) ─────────
    #
    # Verification passing proves the checks; it does not prove the
    # person has nothing to add. Errands stay auto-done — bounded by
    # definition, with nothing to review.
    if (os.environ.get("AGENTOS_HUMAN_DONE") == "1"
            and (row.get("kind") or "story") != "errand"):
        # THE CLAIM IS THE IDENTITY, AND IT HAS TO BE IN THE HASH.
        #
        # `ask_question` dedupes on `content_hash` when there is one and
        # ON THE TEXT otherwise — its own comment says a plan approval's
        # text is a constant, which is why plans pass a hash. Acceptance
        # text is a constant too and nobody passed one.
        #
        # An earlier fix put `claim` in the PAYLOAD, which is not hashed,
        # so it changed nothing. Watched on KER13: cycle one's acceptance
        # was answered "also add a file telling us how to run the app";
        # cycle two verified, passed, pushed, and its acceptance was
        # refused as a repeat of that — same words — leaving the run in
        # VERIFYING with the work delivered and nobody asked.
        #
        # A second acceptance after a second verification is a new
        # decision about new work. The claim seq is what makes it new,
        # so it goes where identity is actually read from.
        # MERGE FIRST FOR A STORY THAT OWES ONE, exactly as the
        # unverified path does -- see `kernel.worker.completion_park`.
        # Verification says the work is good; it does not put the work
        # on trunk, and "accept as done" before it lands asks about
        # something that is not there yet.
        from kernel.worker.completion_park import kind_for

        park_kind = await kind_for(conn, eid)
        got = await open_request(
            conn, eid, generation, park_kind,
            {"question": ("verification passed and the work is pushed — "
                          "merge its pull request to land it, or say what "
                          "more this story should do")
             if park_kind == "landing" else
             ("verification passed — accept as done, or "
              "describe what more this story should do"),
             "claim": claim_seq,
             "summary": summary or f"claim {claim_seq} passed its checks",
             "published": detail,
             "branches": []},
            content_hash=f"{park_kind}-{eid}-{claim_seq}")
        if isinstance(got, int):
            await conn.execute(
                "UPDATE executions SET verify_error_count = 0, "
                "stall_reason = NULL WHERE id = $1", eid)
            logger.info("execution %s: claim %s passed -> awaiting "
                        "acceptance (question %s)", eid, claim_seq, got)
            return {"execution_id": eid, "action": "awaiting_acceptance",
                    "detail": f"claim {claim_seq} passed and pushed; a "
                              f"person decides done or more work"}

        # A GATE THAT OPENS WHEN ITS OWN MACHINERY FAILS IS NOT A GATE.
        # This used to fall through to DONE, so the one rule the
        # acceptance park exists to enforce — done is a person's word,
        # never the system's — was bypassed by a dedupe collision,
        # silently. The work is verified and pushed; nothing is lost by
        # waiting, and NOW the next tick really does try again.
        logger.error("execution %s: acceptance park refused (%s) -- NOT "
                     "marking done; a person must still accept", eid, got)
        await conn.execute(
            "UPDATE executions SET stall_reason = $2, updated_at = now() "
            "WHERE id = $1",
            eid, f"verified and pushed, but the acceptance question could "
                 f"not be opened: {getattr(got, 'message', got)}"[:500])
        return {"execution_id": eid, "action": "acceptance_blocked",
                "detail": f"claim {claim_seq} passed and pushed, but nobody "
                          f"could be asked to accept it -- the story stays "
                          f"unfinished rather than finishing itself"}

    from kernel.worker.finish import finish_execution

    await finish_execution(conn, eid, only_from=("VERIFYING",))
    logger.info("execution %s: claim %s passed -> DONE (%s)",
                eid, claim_seq, detail)
    return {"execution_id": eid, "action": "done",
            "detail": f"claim {claim_seq} passed: {summary or detail}"}
