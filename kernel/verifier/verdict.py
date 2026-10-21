"""Writing the verdict and moving the run. The only place either happens.

`_apply` lived in `adjudicator.py` and was 142 code lines of it -- a
third of the file, and the third with the most incidents per line. It is
here unchanged, with two blocks that are decisions in their own right
delegated to files of their own: `errored.py` (an error is not an
acceptance) and `second_key.py` (the assessor, and its spend).

WHAT MUST NOT DRIFT FROM HERE. The verdict row is written inside a
transaction that SETs ROLE, exactly once per claim -- `verdicts_one_per_
claim` is the backstop and `verdict_lock` is the exclusion. Everything
after the insert is the state machine: REJECTED arms E5's re-claim
fence, the generation advance stops the next leg colliding on step 0's
idempotency key, and the pass path hands off to `finish_pass` because
the RECOVERY path calls the same function.
"""

from __future__ import annotations

import json
import logging

from kernel.verifier import errored as errored_mod
from kernel.verifier import preempted
from kernel.verifier import refusal as refusal_mod
from kernel.verifier import stuck
from kernel.verifier.second_key import _assess_passing_claim
from kernel.verifier.steer import _tell_executor
from kernel.verifier.verify import refuses, summarise

logger = logging.getLogger(__name__)

# Who wrote it. `verdicts.adjudicator` and `evidence.written_by` both
# carry this, and a second spelling would make "which component decided
# this" a question about string literals.
ADJUDICATOR = "adjudicator"


async def _apply(conn, row: dict, claim_seq: int, proposal_id, results,
                 parity: dict | None = None, driver=None,
                 contract: dict | None = None,
                 env_report: dict | None = None) -> dict:
    """Write the verdict and move the run. The only place either happens."""
    eid, generation = row["id"], row["generation"]
    refused = refuses(results)

    # AN ERROR IS NOT AN ACCEPTANCE. `errored.py` carries the whole
    # argument and the retry bound; a non-None answer means the claim is
    # UNDECIDED, so no verdict row is written and the run stays in
    # VERIFYING for the next tick.
    #
    # E11 SPLITS ONE MORE ROW OFF THE FRONT OF IT. 5.8 row 3: a run the
    # CLUSTER ended -- a reclaimed Spot node, an eviction -- did not fail
    # to run for any reason the executor can act on, so it is neither a
    # refusal nor a strike. `preempted.py` recreates the pod on its
    # surviving claim and the next tick verifies again; the ordinary
    # error policy below owns everything else, including an OOM.
    #
    # ASKED ONLY WHEN NOTHING REFUSED, and that is the same `if` rather
    # than a new one: a criterion that ran and found the work wrong is
    # still right about it whatever happened to another criterion's node.
    if not refused:
        ended = await preempted.not_the_work(conn, driver, row, results,
                                             contract)
        if ended is not None:
            return ended
        undecided = await errored_mod.errored_claim(
            conn, eid, generation, claim_seq, results)
        if undecided is not None:
            return undecided

    # ── THE SECOND KEY, on work whose checks all passed ──────────────
    #
    # The mechanical verdict is only as good as the checks, and the
    # checks were written by whoever did the work. A guard of `test -f
    # README.md` beside a bare `pytest` passes with three trivial tests
    # and a service that does nothing the specification asked for --
    # and every layer agrees the story is done.
    #
    # So an agent gets the same tree and the same offline container and
    # asks the question the checks did not: does this actually do what
    # the story asked? It can ONLY refuse. Two keys to pass, either key
    # to refuse -- which keeps the verdict reproducible, because a
    # model can never bless work that no command verified, while
    # catching the case where the commands never asked.
    #
    # Every ambiguous outcome accepts: no api key, a timeout, an
    # adapter error, nothing found. A verifier that blocks delivered
    # work when its model is slow is one nobody keeps switched on.
    if not refused:
        assessed = await _assess_passing_claim(
            conn, driver, contract, env_report, eid, claim_seq)
        if assessed is not None:
            refused = True
            results = list(results) + [assessed]

    entries = refusal_mod.build(results) if refused else []
    if refused:
        entries = await _explain(conn, driver, contract, env_report,
                                 eid, entries, parity)

    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE agentos_verifier")
        await conn.execute(
            "INSERT INTO verdicts (execution_id, claim_seq, proposal_id, "
            "passed, refusal, generation, adjudicator, parity) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8::jsonb)",
            eid, claim_seq, proposal_id, not refused,
            json.dumps(entries), generation, ADJUDICATOR,
            json.dumps(parity or {}))

    if refused:
        await conn.execute(
            "UPDATE executions SET verify_error_count = 0 WHERE id = $1",
            eid)
        first = next((e for e in entries if isinstance(e, dict)), {})
        note = (first.get("explain")
                or f"claim refused: {json.dumps(entries, default=str)[:800]}")
        await _tell_executor(conn, eid, generation,
                             "Your completion claim was REFUSED by "
                             "verification. " + note, "vref")

    if not refused:
        return await _passed(conn, row, claim_seq, generation, results)

    # REFUSED. Three things, and all three are required.
    #
    # The proposal goes REJECTED, which ARMS E5's re-claim fence: a
    # second claim carrying the same reason set is refused by the
    # database before it reaches anyone. That is what stops the model
    # answering a refusal by repeating itself.
    if proposal_id is not None:
        await conn.execute(
            "UPDATE proposals SET status = 'REJECTED' WHERE id = $1",
            proposal_id)
    # Back to RUNNING, and the generation advances — a refused claim
    # ended a leg, so the next attempt is a new one. Without the advance
    # the next leg collides on step 0's idempotency key, which is the
    # bug H1d fixed on the crash path.
    await conn.execute(
        "UPDATE executions SET state = 'RUNNING', "
        "generation = generation + 1 WHERE id = $1 AND state = 'VERIFYING'",
        eid)
    logger.info("execution %s: claim %s refused (%d reason(s)) -> RUNNING",
                eid, claim_seq, len(entries))

    # F11 — the same check refusing twice running is the harness's cue to
    # ask a person, whether or not the model thought to. The refusal
    # prompt tells it to use `ask_human`; nothing made it, and if it does
    # not the story stalls with nobody informed. This is the trigger that
    # turns that escape hatch from a design into a mechanism.
    repeat = await stuck.repeated_refusal(conn, eid)
    if repeat:
        asked = await stuck.park_for_help(conn, eid, generation + 1, repeat)
        if asked:
            return {"execution_id": eid, "action": "refused_and_asked",
                    "detail": f"claim {claim_seq} refused, and this is "
                              f"refusal {repeat['count']} on the same "
                              f"check(s) — parked to ask whether the bar "
                              f"is wrong (question {asked})"}

    # `.get`, because not every entry HAS a code. The investigator's
    # note is prepended as `{"explain": ...}` -- no code, by design, it
    # is prose about the others -- and this crashed on it with
    # KeyError: 'code'.
    #
    # The damage was out of all proportion to the typo. The refusal had
    # already been applied correctly: verdict written, executor told,
    # story back to RUNNING. Only this summary line failed, and the
    # exception then set stall_reason to "verification crashed", so a
    # verification that worked perfectly reported itself as a platform
    # fault -- and the assistant, reading that, told a person the
    # verifier was still running when it had finished minutes earlier.
    codes = [e["code"] for e in entries if isinstance(e, dict) and e.get("code")]
    return {"execution_id": eid, "action": "refused",
            "detail": f"claim {claim_seq} refused: "
                      f"{', '.join(codes) or 'see the verdict'}"}


async def _explain(conn, driver, contract, env_report, eid: int,
                   entries: list, parity) -> list:
    """THE EXPLAINER. Advisory by contract: the verdict above is
    already decided and nothing the model says can move it. What
    it adds is the sentence a person can act on -- the first live
    refusal said only ModuleNotFoundError, taught its author
    nothing, and bought us 503 vendored files. Failure-safe: no
    key or a down model leaves the raw entries standing alone."""
    try:
        task = await conn.fetchval(
            "SELECT task FROM executions WHERE id = $1", eid)
        note = ""
        # THE TREE, NOT THE TAR -- same one-word fix as `second_key`.
        # On the volume path there is no archive, and testing for one
        # meant the investigator never ran there: a refusal with no
        # explanation, which is the half of a verdict a person acts on.
        if driver is not None:
            # THE INVESTIGATOR: an agent with the exact tree and an
            # identical offline sandbox, finding out why -- after
            # the verdict, never touching it.
            from kernel.verifier.investigate import investigate
            # THE GOAL'S JUDGE, the same one the assessor uses. Resolved
            # through `choice.judge_for` rather than re-derived here, so
            # the two cannot drift into judging one run on two models.
            # Never fatal: the deployment default investigates if the
            # lookup fails, which is what happened before this existed.
            judge = None
            try:
                from kernel.harness.models import choice     # noqa: PLC0415
                judge = await choice.judge_for(conn, eid)
            except Exception:                                # noqa: BLE001
                logger.warning("execution %s: no verifier model resolved "
                               "for the investigator; the deployment "
                               "default applies", eid)
            from kernel.verifier.inspect_tree import for_run  # noqa: PLC0415

            tree = await for_run(driver, env_report, contract)
            found = None if tree is None else await investigate(
                driver, contract or {}, task or "", entries, tree,
                model=judge)
            if found:
                note = (found.get("why", "")
                        + ("\n\nFix: " + found["fix"]
                           if found.get("fix") else "")
                        + (f"\n\n[{found['category']}]"
                           if found.get("category") else ""))
        if not note:
            # No tree in hand (legacy path) or the agent came back
            # empty: the one-shot explainer still reads the entries.
            from kernel.verifier.assist import explain_refusal
            note = await explain_refusal(task or "", entries,
                                         setup_report=parity)
        if note:
            entries = [{"explain": note}] + entries
            await conn.execute(
                "UPDATE executions SET stall_reason = $2, "
                "updated_at = now() WHERE id = $1", eid, note[:500])
    except Exception:                                       # noqa: BLE001
        logger.exception("investigator failed; the raw refusal stands")
    return entries


async def _passed(conn, row: dict, claim_seq: int, generation: int,
                  results) -> dict:
    """A PASS, and what still has to be true before the story finishes."""
    eid = row["id"]
    # A STORY DOES NOT FINISH WHILE SOMEBODY'S INTENT IS
    # UNDELIVERED. A person may amend a story at any point, and
    # "any point" includes the seconds between a claim and its
    # verdict. An amendment that arrived then has no future leg to
    # land on once the state reaches DONE, and would be silently
    # lost — which makes "at any point" a claim the system does not
    # keep.
    #
    # The verdict itself is written either way and stays true: the
    # criteria in force at the time of the claim were met. What
    # changes is that the story keeps going, because there is now
    # more to do.
    outstanding = await conn.fetchval(
        "SELECT count(*) FROM proposals WHERE execution_id = $1 "
        "AND status = 'PENDING' AND kind IN ('steer', 'replan')", eid)
    if outstanding:
        await conn.execute(
            "UPDATE executions SET state = 'RUNNING', "
            "generation = generation + 1 WHERE id = $1 "
            "AND state = 'VERIFYING'", eid)
        logger.info("execution %s: claim %s passed but %d intent(s) "
                    "undelivered -> RUNNING", eid, claim_seq, outstanding)
        return {"execution_id": eid, "action": "passed_with_more",
                "detail": f"claim {claim_seq} passed, and {outstanding} "
                          f"amendment(s) have not reached the executor "
                          f"yet — the story continues rather than "
                          f"finishing with somebody's intent dropped"}
    # The tail of a pass — push, then ask a person — lives in
    # `finish_pass` because the RECOVERY path needs it too: a tick
    # that finds a passed claim whose run never left VERIFYING
    # calls the same function. It was written here only, so an
    # interruption between the verdict and the park stranded the
    # run and every later tick returned `already_adjudicated`.
    from kernel.verifier.finish_pass import finish_passed_claim

    return await finish_passed_claim(
        conn, eid, generation, claim_seq, row,
        summary=summarise(results))
