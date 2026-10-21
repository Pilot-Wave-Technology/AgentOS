"""F2b — the adjudicator. The half of the completion path that was missing.

`propose_completion` moves an execution to VERIFYING and does not end the
run. Nothing then answered it: `proposals.refuse()` has no caller, so a
claiming execution sat in VERIFYING permanently. Zero stories have ever
reached DONE, in the whole project.

NO MODEL IN ANY OF THIS. The adjudicator polls, runs criteria that were
fixed before the work began, derives a binary verdict from four-valued
results, and moves the state machine. Every judgement it applies was
made at approval time by somebody else — which is exactly why "declare
done" cannot be the cheap exit for a stuck agent, and therefore why
parking and asking are rational rather than merely hoped for.

TWO PROPERTIES ARE REQUIREMENTS, NOT REFINEMENTS: a refusal must be
resumable, and it must be visible to the next leg. Miss either and the
model re-claims the identical thing, the E5 fence refuses it, and the
run is stuck twice over.

WHAT IS STILL HERE AFTER E3'S SPLIT, and why. This file was 439 code
lines, twice the house ceiling. What stayed is the POLL and the SWEEP --
the two queries that decide what gets judged, the budget that decides how
many at once, and the lock that decides which process judges it. Those
four things are one subject and every one of them was written for a
measured incident, so they are together and they are named in
`test_no_loop_dials_something_that_is_gone.py` and
`test_one_conductor_per_verdict.py` by this path.

What moved out is what happens AFTER a claim has been judged, each with
its own incident and its own file: `verdict.py` (the verdict row and the
state machine), `errored.py` (an error is not an acceptance),
`second_key.py` (the assessor and its spend), `steer.py` (I6, the row the
executor reads), `crashed.py` (the adjudicator itself failing). They are
re-exported at the foot of this file, because `finish_pass.py`,
`kernel/worker/loop.py` and the CI suite import them from here by name.

AND WHAT WAS NEVER HERE, arriving in the same shape rather than as
branches in the sweep: `rebuild.py` decides whether this execution's
verification environment is the one its COMMITTED manifest asks for
(E4/E13, plan 5.10) and `preempted.py` decides whether a verification
that produced no answer produced no answer because the cluster ended it
(E11, plan 5.8 row 3). Both are questions about whether a claim can be
judged at all, which is this file's subject; neither is a question about
the work, which is why neither writes a verdict.
"""

from __future__ import annotations

import logging

from kernel.verifier.crashed import ADJUDICATOR_STRIKES, _adjudicator_failed
from kernel.verifier.environment import deltas_for
from kernel.verifier.parity import concerns, measure
from kernel.verifier.rebuild import environments
from kernel.verifier.steer import _tell_executor
from kernel.verifier.tree import TreeExportError, head_commit
from kernel.verifier.verdict import ADJUDICATOR, _apply
from kernel.verifier.verify import verify_claim
from kernel.verifier.writer import load_contract, record

logger = logging.getLogger(__name__)


async def verifying(conn, goal_id: str | None = None) -> list[dict]:
    """Executions waiting on a verdict AND still able to be given one.

    `container_id IS NOT NULL` is not a detail. Verification fetches the
    committed tree out of the execution pod through podfs, and `archive`
    is a slow operation by design -- a tree legitimately takes a while.
    A VERIFYING run whose pod is gone (a preempted Spot node, an OOM, a
    crash) therefore sent the adjudicator to dial an unrouted address and
    wait, holding a pooled connection AND a thread for the whole timeout,
    six at a time, every tick, for ever.

    That is the same shape as the credential-sweep wedge found on
    2026-09-17: a query naming something external, and code that dials it
    without asking whether it is still there. `claimable` has always
    constrained this way; this one never did.

    A run skipped here is NOT abandoned -- `stranded_verifying` below
    reports it, because a run that cannot be verified must say so rather
    than sit silent.
    """
    return [dict(r) for r in await conn.fetch(
        "SELECT id, kind, domain, generation, workspace_path, contract_id, "
        "goal_id FROM executions WHERE state = 'VERIFYING' "
        "AND container_id IS NOT NULL "
        "AND ($1::text IS NULL OR goal_id = $1) ORDER BY id", goal_id)]


async def stranded_verifying(conn, goal_id: str | None = None) -> list:
    """VERIFYING runs whose pod has gone. Says so; never dials.

    Skipping these silently would trade a wedge for an invisible stall --
    the run would sit in VERIFYING indefinitely with `stall_reason` NULL,
    which is the failure Phase S exists to end.
    """
    rows = await conn.fetch(
        "SELECT id FROM executions WHERE state = 'VERIFYING' "
        "AND container_id IS NULL "
        "AND ($1::text IS NULL OR goal_id = $1) ORDER BY id LIMIT 20",
        goal_id)
    out = []
    for row in rows:
        why = ("cannot be verified: the execution pod is gone, so the "
               "committed tree cannot be fetched. A preempted node or a "
               "crashed pod does this; the work may still be on the "
               "branch.")
        try:
            await conn.execute(
                "UPDATE executions SET stall_reason = $2, "
                "stalled_at = COALESCE(stalled_at, now()) WHERE id = $1 "
                "AND stall_reason IS DISTINCT FROM $2", row["id"], why)
        except Exception:                                   # noqa: BLE001
            logger.warning("could not record why %s cannot verify",
                           row["id"], exc_info=True)
        logger.warning("execution %s is VERIFYING with no pod", row["id"],
                       extra={"event": "tick"})
        out.append({"execution_id": row["id"], "action": "verify_stranded",
                    "detail": why})
    return out


async def _claim(conn, execution_id: int):
    """The propose_completion being adjudicated: its seq and proposal row."""
    row = await conn.fetchrow(
        "SELECT seq FROM step_journal WHERE execution_id = $1 "
        "AND tool = 'propose_completion' ORDER BY seq DESC LIMIT 1",
        execution_id)
    if row is None:
        return None, None
    proposal = await conn.fetchval(
        "SELECT id FROM proposals WHERE execution_id = $1 "
        "AND kind = 'completion' ORDER BY id DESC LIMIT 1", execution_id)
    return row["seq"], proposal


async def adjudicate_one(conn, driver, row: dict) -> dict:
    """Verify one claim and act on the verdict. Returns what happened."""
    eid = row["id"]
    contract, criteria = await load_contract(conn, eid)
    if contract is None or not criteria:
        # DELIBERATELY NOT A VERDICT. An execution with no definition of
        # done cannot be done, and inventing a default would be this
        # component deciding what the work was for. Reported every tick
        # so it is visible rather than silent — a person can cancel it,
        # which machine v3 made possible. H1b makes a contract mandatory
        # at dispatch, which is where this stops happening.
        return {"execution_id": eid, "action": "no_contract",
                "detail": "VERIFYING with no contract or no criteria; "
                          "nothing defines done for this run"}

    claim_seq, proposal_id = await _claim(conn, eid)
    if claim_seq is None:
        return {"execution_id": eid, "action": "no_claim",
                "detail": "VERIFYING with no propose_completion in the "
                          "journal — it did not get here by claiming"}

    already = await conn.fetchval(
        "SELECT passed FROM verdicts WHERE execution_id = $1 "
        "AND claim_seq = $2", eid, claim_seq)
    if already is not None:
        # ── A VERDICT IS NOT THE END OF THE TICK ──────────────────────
        #
        # What follows a PASSING verdict — push the work, then park on
        # an acceptance — is not written down anywhere durable while it
        # is happening. If the process dies in that gap, or the park is
        # refused, the run sits in VERIFYING with a passed verdict and
        # this guard returns `already_adjudicated` for ever. Watched
        # live on KER13: cycle two verified, passed, and stopped, and
        # the log printed the same line every four seconds.
        #
        # The comment further down promises "the next adjudicator tick
        # tries the park again". It could not: this return is upstream
        # of it. So the guard now asks the question it should always
        # have asked — did the run actually GET anywhere after the
        # verdict? — and finishes the job if not.
        #
        # Re-running the whole adjudication is still refused, which is
        # what this guard is for: no second container, no second
        # verdict. Only the tail is resumed, and `publish_execution` is
        # idempotent (pushing commits already pushed is a no-op).
        if already:
            unfinished = await conn.fetchval(
                "SELECT 1 FROM executions WHERE id = $1 AND state = "
                "'VERIFYING' AND NOT EXISTS (SELECT 1 FROM questions "
                "WHERE execution_id = $1 AND status = 'OPEN')", eid)
            if unfinished:
                logger.warning("execution %s: claim %s passed but the run "
                               "never left VERIFYING — finishing the tail",
                               eid, claim_seq)
                from kernel.verifier.finish_pass import (
                    finish_passed_claim)
                # `row["generation"]`, not a local: this guard runs
                # BEFORE the verification body that binds one.
                return await finish_passed_claim(
                    conn, eid, row["generation"], claim_seq, row)
        # One verdict per claim. Re-running would either collide on the
        # unique index or, worse, spend a container to reach the same
        # answer while the state machine has already moved.
        return {"execution_id": eid, "action": "already_adjudicated",
                "detail": f"claim {claim_seq} was already "
                          f"{'passed' if already else 'refused'}"}

    repo = row.get("workspace_path")

    # E7f — THE DIGEST IS A SET. A claim is judged against a tree, and
    # with several repositories that is several trees; one sha names a
    # fraction of what was checked, so re-running "at that digest" would
    # reconstruct a different world.
    #
    # It also stops the root being assumed to be a repository at all.
    # After E7e a multi-repo workspace has its git under `repos/*`, and
    # `head_commit(root)` on it raises -- so every such story would be
    # unverifiable, reported as "no committed tree" while holding several.
    from kernel.workspace.mounts import mounts_for
    from kernel.verifier.tree import tree_state
    mounts = await mounts_for(conn, eid)

    # WHEREVER THE TREE IS. This read `/workspace` on the CONDUCTOR,
    # which has no such path on Kubernetes, so every claim reached
    # "no committed tree to verify" -- the refusal below -- for work
    # that had committed. See `tree_state`.
    digest, commit = await tree_state(conn, eid, repo, mounts)
    if not commit:
        # Not fatal when the mounts carry commits: the root is simply not
        # the repository any more.
        commit = next((sha for _, sha in digest if sha), None)
    if not commit:
        return {"execution_id": eid, "action": "no_commit",
                "detail": "no committed tree to verify; the Verifier "
                          "checks commits, and there is none"}

    # F5: what the executor installed, so the Verifier reproduces it
    # rather than failing a criterion for a missing dependency.
    deltas = await deltas_for(conn, eid)
    env_report: dict = {}
    # WHERE THE TREE IS, for the archive as well as for the digest.
    # `tree_state` above already crossed to the pod to find the commit;
    # this hands the same address to the thing that has to take the tree
    # away. Without it `archive_in_pod("")` refused and every criterion
    # came back `error` — a story that had committed real work, judged
    # as unverifiable.
    from kernel.verifier.tree import pod_address
    host = await pod_address(conn, eid, driver)
    # E19: THE WARM POD, WHEN THERE IS ONE. `rebuild.environments`
    # has already run for this execution -- it takes rows out of this
    # pass until the volume matches the committed manifest -- so by
    # the time a row is judged, the claim holds the tree and the
    # environment and the pod is attached to it. `warm_for` returns
    # None on any doubt and the tar path runs instead.
    from kernel.verifier import batch                  # noqa: PLC0415
    warm = await batch.warm_for(conn, driver, eid)
    results = await verify_claim(driver, contract, criteria, repo, commit,
                                 mounts=mounts, env_deltas=deltas,
                                 report=env_report,
                                 workspace_host=host, execution_id=eid,
                                 warm=warm)
    logger.info("execution %s: judged at %s", eid,
                ", ".join(f"{at or '<root>'}@{sha[:8]}"
                          for at, sha in digest) or "one tree")
    await record(conn, eid, claim_seq, results, written_by=ADJUDICATOR)

    # F6: how well the verification environment matched the working
    # one. Measured per verdict, because the way a Verifier stops
    # being worth having is drift, and the only defence against
    # drift is a number somebody looks at.
    parity = measure(contract, deltas, env_report, results)
    for note in concerns(parity):
        logger.warning("execution %s parity: %s", eid, note)
    return await _apply(conn, row, claim_seq, proposal_id, results,
                        parity, driver=driver, contract=contract,
                        env_report=env_report)


# ── E3 · the first of two budgets, and it is ADJUDICATION'S ───────────
#
# How many verifications may run at once in ONE conductor. Each holds a
# pooled connection (POOL_MAX is 16) and a criterion container for its
# whole life, so this is a connection budget before it is anything else.
# Half the pool, leaving the other half for the tick's own passes.
#
# NOT SHARED WITH THE DRY RUN, and that is the whole of E3. Adjudication
# is throughput-sensitive -- nobody is watching a verdict arrive -- and
# a dry run is latency-sensitive, because a person is sitting in front of
# an approval card waiting for it. One semaphore would let a queue of the
# first starve the second, which is the case the two numbers exist to
# keep apart. The dry run's is `kernel/verifier/dryrun.py`'s
# DRYRUN_CONCURRENCY, and 6 + 4 of POOL_MAX 16 is deliberate: it leaves
# the sweep's own connection and the heartbeat's room to be taken.
VERIFY_CONCURRENCY = 6


def verify_concurrency() -> int:
    """Read per call, so a run can change it without a restart."""
    import os                                               # noqa: PLC0415

    if "AGENTOS_VERIFY_CONCURRENCY" not in os.environ:
        return VERIFY_CONCURRENCY
    try:
        return max(1, int(os.environ["AGENTOS_VERIFY_CONCURRENCY"]))
    except ValueError:
        return VERIFY_CONCURRENCY


async def adjudicate_pass(conn, driver, goal_id: str | None = None,
                          acquire=None, supervisor=None) -> list[dict]:
    """One sweep over everything waiting on a verdict.

    CONCURRENT WHEN THE CALLER SUPPLIES `acquire` AND `supervisor`,
    serial otherwise -- the same shape `worker_tick` uses for legs, and
    for the same reason one level down.

    WHY IT HAD TO STOP BEING SERIAL. This is awaited in the middle of
    the worker tick, and `adjudicate_one` awaits a criterion container to
    completion. So a single slow test suite blocked the ENTIRE tick:
    admission, dispatch, claiming new legs, the ledger drain, the
    credential sweep, the schedule sweep. Three criteria at fifteen
    minutes each is forty-five minutes in which no other story in the
    goal can take a step, and nothing anywhere says why.

    The tick comment two functions up already made this argument about
    answering a question -- "an answer is a model call taking minutes,
    and this pass is serial, so answering inline would put every other
    story behind one person's question". A test suite is the same
    sentence with a different noun, and the reasoning was not carried
    across.

    ONE TASK PER EXECUTION, not one for the pass. The supervisor keys on
    execution id and refuses a second spawn for one already in flight,
    so a long adjudication is not restarted by the next tick four
    seconds later. Give it a supervisor of its OWN, not the leg one: an
    execution is either legging or verifying and never both, so sharing
    would work by accident rather than by design.

    ONE CONDUCTOR PER VERDICT, and this is where that became true. The
    in-flight supervisor is per PROCESS, so with `replicas: 2` both
    conductors ran the full sweep, both spent a criterion container
    reaching the same answer, and the loser died on the unique index --
    measured on execution 203, claim 56. `verdict_lock` makes the
    exclusion real; see kernel/worker/lease.py.
    """
    from kernel.worker.lease import verdict_lock
    rows = await verifying(conn, goal_id)
    # Said before anything is dialled, so a run that CANNOT be verified is
    # visible rather than merely absent from the list above.
    stranded = await stranded_verifying(conn, goal_id)
    # E4/E13 — AND A RUN THAT CANNOT BE VERIFIED **YET**. The committed
    # manifest is what the verification environment is keyed on (5.10),
    # and a model can commit a new one and claim in the same breath. A
    # claim taken mid-rebuild would be checked against a half-replaced
    # `.venv`, which in the remove-a-dependency direction is a check
    # PASSING THAT SHOULD FAIL — so `environments` takes those rows out
    # of this pass, with a reason, and fires the prepare Job for every
    # manifest that moved, including on runs still WORKING. The skipped
    # rows join `stranded` because this list is what the tick did not
    # judge and why, and both paths below return it.
    rows, waiting = await environments(conn, driver, goal_id, rows,
                                       acquire=acquire, supervisor=supervisor)
    stranded = stranded + waiting

    if supervisor is not None and acquire is not None:
        actions = []
        busy = supervisor.in_flight()
        # BOUNDED, the way the leg path is. Each adjudication holds a
        # pooled connection for the WHOLE verification -- a criterion
        # container, minutes -- and `verifying()` has no LIMIT. Twenty
        # stories reaching VERIFYING together therefore claimed twenty of
        # the sixteen connections in the pool, and the next tick's
        # `pool.acquire()` waited for one that was never coming back.
        #
        # The leg path has always sliced by `free_slots`; this one was
        # written without it and the asymmetry was the bug.
        free = supervisor.free_slots(verify_concurrency())
        if free <= 0:
            return [{"execution_id": None, "action": "verify_saturated",
                     "detail": f"{len(busy)} verification(s) already "
                               f"running; the rest wait for the next tick"}]
        for row in rows[:free + len(busy)]:
            eid = row["id"]
            if eid in busy:
                continue
            if supervisor.free_slots(verify_concurrency()) <= 0:
                break

            async def _adjudicate(row=row):
                # Its own connection: adjudication holds one for the
                # whole verification, and borrowing the sweep's would
                # put every other pass behind the slowest suite --
                # which is the thing being fixed. It is also what the
                # lock needs: an advisory lock lives on a connection,
                # so acquire and release must be the same one.
                async with acquire() as own:
                    async with verdict_lock(own, row["id"]) as held:
                        if not held:
                            # The other conductor is already spending a
                            # container on this. Not an error, and not
                            # worth waiting for: next tick finds the
                            # verdict written and takes the `already`
                            # path.
                            return {"execution_id": row["id"],
                                    "action": "adjudicating_elsewhere",
                                    "detail": "another conductor holds "
                                              "this execution's verdict"}
                        try:
                            return await adjudicate_one(own, driver, row)
                        except Exception as exc:            # noqa: BLE001
                            logger.exception(
                                "adjudicate: execution %s failed", row["id"])
                            return await _adjudicator_failed(own, row, exc)

            if supervisor.spawn(eid, _adjudicate()):
                actions.append({"execution_id": eid,
                                "action": "adjudicating",
                                "detail": "verification started"})
        return stranded + actions

    actions = []
    for row in rows:
        try:
            # THE SERIAL PATH NEEDS IT TOO. One process here, but not
            # necessarily one process in the deployment: E0 is single,
            # `replicas: 2` is not, and which path a conductor takes
            # depends on whether it was given a supervisor -- not on how
            # many conductors exist.
            async with verdict_lock(conn, row["id"]) as held:
                if not held:
                    actions.append({"execution_id": row["id"],
                                    "action": "adjudicating_elsewhere",
                                    "detail": "another conductor holds "
                                              "this execution's verdict"})
                    continue
                actions.append(await adjudicate_one(conn, driver, row))
        except Exception as e:                              # noqa: BLE001
            # One execution's failure must not stop the sweep; the others
            # are unrelated runs that have done nothing wrong.
            #
            # BUT IT MUST NOT RETRY IN SILENCE EITHER. A crash here left
            # the run in VERIFYING and the next tick tried again, for
            # ever, with nothing on the board to say why -- the run had
            # simply stopped, and the only evidence was a traceback in a
            # log nobody watches. Seen live: a worker restart landing
            # mid-adjudication left half-written evidence, every retry
            # collided with it, and the story sat in VERIFYING all
            # afternoon looking like slow work.
            logger.exception("adjudicate: execution %s failed", row["id"])
            actions.append(await _adjudicator_failed(conn, row, e))
    return stranded + actions


# RE-EXPORTED, NOT RE-IMPLEMENTED. `kernel/worker/loop.py` and
# `finish_pass.py` import `_tell_executor` from this module, the CI suite
# imports `_apply`, and `test_adjudication_off_the_tick` monkeypatches
# `_adjudicator_failed` onto it. The split moved the code; it must not
# move the import path, because a name that answers to two spellings is
# how two copies of `worker_tick` came to exist.
__all__ = ["ADJUDICATOR", "ADJUDICATOR_STRIKES", "VERIFY_CONCURRENCY",
           "TreeExportError", "adjudicate_one", "adjudicate_pass",
           "head_commit", "stranded_verifying", "verify_claim",
           "verify_concurrency", "verifying", "_adjudicator_failed",
           "_apply", "_tell_executor"]
