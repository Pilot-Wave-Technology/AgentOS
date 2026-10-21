"""THE SECOND KEY — the assessor, and what it costs.

Lifted out of `_apply` unchanged. The mechanical verdict is only as good
as the checks, and the checks were written by whoever did the work; this
is the half that asks the question the checks did not. It can only
refuse, and every ambiguous outcome accepts.

Its spend is ledgered here too, because the two are one decision: a
verifier that quietly costs money nobody can attribute is a verifier
somebody switches off for the wrong reason.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _assess_passing_claim(conn, driver, contract, env_report,
                                eid: int, claim_seq: int):
    """A (criterion, Outcome) pair when the assessor refuses, else None.

    Shaped as a criterion result so a refusal from here travels every
    path a failing check already travels -- the executor is told, the
    verdict card shows it, the story goes back for work. Nothing
    downstream needs to know an agent produced it.
    """
    from kernel.verifier.outcome import Outcome

    # THE GATE IS THE TREE, NOT THE TAR, and that one word is why the
    # second key vanished on the volume path. `_tree_tar` is set by
    # `stage`, the warm path does not call `stage`, so this returned
    # None before the assessor was ever reached -- silently, on every
    # verification that used the pod.
    if driver is None:
        return None
    try:
        from kernel.verifier.assess import assess

        task = await conn.fetchval(
            "SELECT task FROM executions WHERE id = $1", eid)
        plan = await conn.fetchval(
            "SELECT payload::text FROM questions "
            "WHERE execution_id = $1 AND kind = 'authorization' "
            "ORDER BY id DESC LIMIT 1", eid)
        # Collected whatever happens below, INCLUDING the except: the
        # assessor accepts on failure, and a run that timed out still spent
        # what it spent. Ledgering only the clean paths would have made the
        # worst runs look like the cheapest.
        spent: list = []
        # WHICH MODEL JUDGES, and it is the GOAL's choice, not the
        # story's. `assess.py` is explicit that a model which can pass
        # work makes verification unreproducible -- so the judge is a
        # standard the goal sets. A story that could pick its own judge
        # could pick a lenient one.
        #
        # NOT FROZEN the way the executor's is. An assessment is one
        # call, not a journal, so there is nothing to make unreadable --
        # and a goal that raises its standards should have the next
        # verification use the new model, including a re-verification of
        # work already done.
        #
        # Never fatal: a lookup that fails leaves the deployment default,
        # which is what every caller got before this existed.
        judge = None
        try:
            from kernel.harness.models import choice        # noqa: PLC0415
            judge = await choice.judge_for(conn, eid)
        except Exception:                                   # noqa: BLE001
            logger.warning("execution %s: no verifier model resolved; the "
                           "deployment default judges", eid, exc_info=True)
        try:
            from kernel.verifier.inspect_tree import for_run  # noqa: PLC0415

            tree = await for_run(driver, env_report, contract)
            # WHAT THIS STORY CHANGED, ahead of the tree. "" when the
            # execution pod is gone or a lane will not resolve, and the
            # assessor reads the tree as it always did.
            from kernel.verifier.inspect_tree import (          # noqa: PLC0415
                changed_for)

            summary = await changed_for(conn, driver, eid)
            if tree is None:
                # Neither a pod nor an archive. Accepting is the safe
                # direction: an assessor that cannot see the work must
                # not refuse it.
                return None
            found = await assess(driver, contract or {}, task or "",
                                 plan or "", tree,
                                 usage_out=spent, model=judge,
                                 changed=summary)
        finally:
            await _ledger_assessor_spend(conn, eid, claim_seq, spent)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("assessor not run (accepting): %s", e)
        return None
    if not found:
        return None

    logger.warning("execution %s: claim %s passed its checks and the "
                   "assessor REFUSED: %s", eid, claim_seq,
                   found["what"][:160])
    explain = found["what"] + chr(10) + chr(10) + "Evidence:" + chr(10) \
        + found["evidence"]
    if found.get("fix"):
        explain += chr(10) + chr(10) + "Fix: " + found["fix"]
    return (
        {"id": "assessor", "label": "goal",
         "spec": {"run": "(assessor: does this do what the story asked?)"}},
        Outcome(passes="refuse",
                detail={"reason": found["what"],
                        "output": found["evidence"],
                        "explain": explain}),
    )


async def _ledger_assessor_spend(conn, eid: int, claim_seq: int,
                                 spent: list) -> None:
    """Verifier model spend into `token_calls`, attributed to the story.

    THE VERIFIER'S COST BELONGS TO THE WORK IT VERIFIES. It is not overhead
    charged to the platform: a story whose assessor ran four times because
    the work kept coming back cost that, and reporting it anywhere else
    would flatter the story and hide where the money went. Same goal, same
    story, same execution as the executor's rows -- only the role differs.

    Never raises. A verdict must not be lost because its accounting was.
    """
    if not spent:
        return
    try:
        from kernel.usage.sink import record, run_scope, usage_of

        goal_id, story_id = await run_scope(conn, eid)
        for i, cost in enumerate(spent):
            usage = usage_of(cost)
            if usage is None:
                continue
            await record(conn, execution_id=eid, role="verifier",
                         dedupe_key=f"verify:{eid}:{claim_seq}:{i}",
                         usage=usage, goal_id=goal_id, story_id=story_id)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("assessor spend not ledgered (execution=%s): %s",
                       eid, exc)
