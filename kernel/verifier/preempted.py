"""5.8 row 3 — the verification pod died, and that is NOT a verdict.

FIVE WAYS VERIFICATION FAILS, which blurred into one before E11. Each
row now has an owner, and the table is here because the rows are only
telling apart by being read together:

    1  the checks ran and said no          `fail`. verdict.py: the
                                           refusal, the steer, gen+1.
                                           The pod stays warm; only the
                                           tree re-seeds (5.4 step 9a)
    2  the checks COULD NOT run (OOM, a    `error`. errored.py: undecided,
       missing tool)                       three strikes, then back to
                                           the executor
    3  the run was ended by the CLUSTER    NO VERDICT. This file
       (a preempted node, an eviction)
    4  the verifier Deployment died        NO VERDICT. Already true:
       mid-adjudication                    `verdict_lock` dies with the
                                           connection and the next tick
                                           finds nothing written
    5  the prepare Job failed              `error` on every criterion.
                                           rebuild.py: a genuinely broken
                                           manifest IS about the work

WHY ROW 3 IS THE ONE THAT MATTERS. Rows 1, 2 and 5 all say something
about the work or its declared environment. Row 3 says something about
Google's capacity planning. Reported as `fail` it becomes the one verdict
that cannot be argued with -- a model told its correct work is wrong --
and reported as `error` it spends the strikes that hand the run back to
the executor to repair code that was never broken. It must cost a re-run
and nothing else, which is exactly what `k8s_outcome` already argues one
layer down: "wrongly refusing is worse than wrongly not refusing".

AND THE RE-RUN IS ON THE SURVIVING CLAIM. The tree and the `.venv` live
on a PVC (`volume.py`) that outlives the pod on purpose, so recovery is
`k8s_verify.ensure` called a second time -- idempotent, adopting whatever
is still there -- and not a rebuild of the environment.

CHECKED ONLY WHEN NOTHING REFUSED. A criterion that ran and failed found
the work wrong whatever happened to a second criterion's node, and
letting a preemption suppress a real refusal would make a refusal
avoidable by being unlucky.
"""

from __future__ import annotations

import logging

from kernel.drivers.infra_error import DISRUPTED, kind_of
from kernel.verifier import pod as verify_pod
from kernel.verifier import verify_state
from kernel.workspace.podfs_client import is_pod

logger = logging.getLogger(__name__)

# Consecutive endings by the substrate before this stops being free.
# Preemption is ordinary and a re-run is the right answer to it; a fourth
# in a row is not bad luck, it is a cluster that cannot run this work at
# all, and continuing to buy re-runs would be an invisible loop -- the
# shape `errored.py`'s own bound exists to prevent. Past it the ordinary
# undecided path takes over, strikes and all, so the run reaches a person.
DISRUPTION_LIMIT = 3

# HOW THE SUBSTRATE SAYS IT, AND IT IS NO LONGER A SENTENCE.
# `kernel/drivers/k8s_outcome.disrupted` is the only producer of the
# `DISRUPTED` kind, and `outcome.unrunnable` carries the tagged value
# through untouched as the `reason` of an `error` with `infra` set, so
# this module asks the value what it is instead of reading what it says.
#
# IT USED TO MATCH A PREFIX -- "the cluster ended this run" -- because
# the classifier needs a POD object and adjudication has outcomes, and
# an anti-drift test held the two spellings together. That is fault 10's
# exact shape: a channel written for a person and parsed by a machine.
# It is how a 45MB capture that had WORKED came back as "stored nothing"
# and cost two deploy cycles. The sentence still exists and is still what
# a person reads beside the verdict; it is simply no longer load-bearing.
# See kernel/drivers/infra_error.py.


def disrupted(results) -> str | None:
    """The sentence naming what ended this run, or None.

    ANY criterion is enough. Nothing here found the work wrong -- the
    caller has already established that -- so one criterion whose node
    was reclaimed means the set of answers is incomplete, and an
    incomplete set is not an answer.

    BOTH HALVES ARE ASKED. `infra` says the ending was about the
    substrate at all and the KIND says which of plan 5.8's rows it was;
    an untagged value is `UNKNOWN`, which is row 2 -- the direction that
    spends strikes and reaches a person, rather than the one that buys
    re-runs for ever.

    THE SENTENCE IS STILL WHAT COMES BACK, because `not_the_work` puts
    it on the story's stall card and in the action it reports. The kind
    decides; the sentence explains.
    """
    for _, outcome in results:
        detail = outcome.detail or {}
        reason = detail.get("reason")
        if detail.get("infra") and kind_of(reason) == DISRUPTED:
            return reason
    return None


async def not_the_work(conn, driver, row: dict, results,
                       contract: dict | None = None) -> dict | None:
    """Row 3's answer, or None when this is not row 3.

    None also RESETS the counter: a verification that reached an answer,
    of any kind, ended whatever run of disruptions preceded it.
    """
    eid = row["id"]
    reason = disrupted(results)
    if reason is None:
        await verify_state.decided(conn, eid)
        return None

    count = await verify_state.disrupted(conn, eid)
    if count > DISRUPTION_LIMIT:
        # Out of free re-runs. Deliberately NOT a verdict even here --
        # the caller's next step is `errored_claim`, which is undecided
        # too -- but it does now spend strikes, so the run reaches the
        # executor and then a person instead of being re-run for ever.
        logger.error("execution %s: the cluster has ended verification %s "
                     "times running; letting the ordinary error path take "
                     "it", eid, count)
        return None

    await _say(conn, eid, reason, count)
    recovered = await _recreate(driver, eid, contract)
    logger.warning("execution %s: verification ended by the substrate "
                   "(%s/%s) -- no verdict, re-running%s", eid, count,
                   DISRUPTION_LIMIT, recovered)
    return {"execution_id": eid, "action": "verification_disrupted",
            "detail": f"{reason} -- no verdict was written; the run is "
                      f"verified again next tick (attempt {count} of "
                      f"{DISRUPTION_LIMIT})"}


async def _say(conn, eid: int, reason: str, count: int) -> None:
    """On the board, because a silent re-run reads as a stopped story.

    The Executions board and the story's stall card both read
    `stall_reason`, and four minutes of "nothing is happening" is what
    every incident on 2026-09-17 looked like from the outside.
    """
    why = (f"verification was ended by the cluster and produced no "
           f"verdict ({reason}); re-running (attempt {count} of "
           f"{DISRUPTION_LIMIT}). This says nothing about the work.")
    try:
        await conn.execute(
            "UPDATE executions SET stall_reason = $2, updated_at = now() "
            "WHERE id = $1", eid, why[:500])
    except Exception:                                       # noqa: BLE001
        logger.warning("could not record the disruption for %s", eid,
                       exc_info=True)


async def _recreate(driver, eid: int, contract: dict | None) -> str:
    """Put the verification pod back on the claim it already has.

    ONLY WHEN THERE IS A CLAIM TO PUT IT BACK ON. An execution with no
    verification PVC is one whose criteria ran as ephemeral Jobs, and
    creating a warm pod for it here would be inventing a pod nobody asked
    for -- on an RWOP disk, which is the one resource in this design that
    cannot be held twice.

    Failure-safe: this is recovery, and a recovery that raises would turn
    "no verdict, try again" into `_adjudicator_failed`, which is a
    different row of 5.8 with a different bound.
    """
    if not is_pod(driver):
        return ""
    try:
        from kernel.drivers import k8s_verify                # noqa: PLC0415
        from kernel.verifier.handover import has_claim      # noqa: PLC0415

        # ONE PROBE, NOT TWO. `handover` asks the same question before
        # `rebuild` fires a prepare Job at a claim, and a second spelling
        # of "does this execution have a verification volume" is a second
        # answer waiting to disagree.
        if not await has_claim(driver, eid):
            return ""
        await k8s_verify.ensure(driver, eid,
                                image=verify_pod.image_for(contract),
                                size=verify_pod.size_for(contract))
        return " (verification pod recreated on the surviving claim)"
    except Exception as exc:                                # noqa: BLE001
        logger.warning("execution %s: could not recreate the verification "
                       "pod: %s", eid, exc)
        return " (the verification pod could not be recreated yet)"
