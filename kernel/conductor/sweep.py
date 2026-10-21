"""Reconcile every goal, and count before it deletes anything.

WHY NOT `reconcile_tick(conn, driver, None)`, which is what the previous
plan proposed as "a day of work on code already written and tested".

Sections 3 and 3b of the reconciler filter `WHERE goal_id = $1`. Passed
NULL, both match nothing — `goal_id = NULL` is never true — so the
per-goal keep-set comes back EMPTY and the orphan sweep that follows
sees every live container as unaccounted for. What saves it today is
H1f's per-container check, which reads each execution's own machine
version before reaping; what that check cannot save is a container
carrying no `execution-id` label at all, and the kernel creates those.

So the iteration is explicit: one pass per (goal, machine version), and
ONE global orphan sweep afterwards rather than N repeats of it.

AND THE MACHINE VERSION COMES FROM THE ROW. `reconcile_tick` defaults it
to 1. `executions.machine_version` defaults to 5, and versions 1 through
5 plus 900 exist. Reconciling a version-5 execution against version 1's
park and terminal states means judging a run by a machine it is not on
-- the failure mode section 4's own comment warns about, arriving
through the front door instead.

DRY RUN IS THE DEFAULT, and it is not timidity. This code has never run
in production. The states it reads are correct as far as the E0 gate can
prove, and the E0 gate has never had a goal it did not create. Counting
for a week costs a log line per tick; being wrong once costs a running
story its container. `AGENTOS_RECONCILE_DELETE=1` is the opt-in, and it
is spelled as a positive because a flag that must be UNSET to be safe is
one somebody clears while debugging and forgets.
"""

from __future__ import annotations

import asyncio
import logging
import os

from kernel.conductor.lock import reconcile_lock
from kernel.conductor.orphans import orphan_sweep
from kernel.conductor.reclaim import releasable, release
from kernel.conductor.reconcile import reconcile_tick

logger = logging.getLogger(__name__)

# Summary keys that describe the PASS rather than count an action. Both
# the log line below and the caller's heartbeat walk the summary, and a
# fact about the pass counted as a unit of work would have a tick that
# did nothing report "2 things happened" -- `dry_run` being True already
# did exactly that, and `skipped` would have doubled it.
META_KEYS = ("goals", "dry_run", "skipped")

# B1: no default inside environ.get -- the fallback is spelled out so the
# value the process runs with is visible in the code.
_DELETE_ENV = os.environ.get("AGENTOS_RECONCILE_DELETE")
DELETES = (_DELETE_ENV or "") == "1"


# Goals worth visiting: anything with an execution that is either still
# running, or terminal and still holding compute. The second half is
# section 3b's case and is easy to forget -- a goal whose executions have
# all finished can still be holding a container, and a sweep that only
# looked at live work would leave it there for ever.
GOALS_SQL = """
    SELECT DISTINCT e.goal_id, e.machine_version
      FROM executions e
      JOIN run_state_machine m ON m.version = e.machine_version
     WHERE e.goal_id IS NOT NULL
       AND (NOT (m.terminal_states @> to_jsonb(e.state))
            OR e.container_id IS NOT NULL)
     ORDER BY e.goal_id, e.machine_version
"""


async def reconcile_all(conn, driver, *, dry_run: bool | None = None) -> dict:
    """One reconciliation pass across every goal, for one replica.

    Returns a summary rather than the raw action list: the caller is a
    loop that runs this every tick, and what it needs in a log line is
    how many of each thing happened, not several hundred dicts.

    ONE REPLICA AT A TIME. The conductor runs two, and the keep-set the
    orphan sweep reads is a snapshot that the other replica can
    invalidate by dispatching -- see kernel/conductor/lock.py. The lock
    is taken HERE rather than at the call site so it cannot be taken on
    a different connection from the one the pass queries, which is the
    one way an advisory lock silently guards nothing.

    A replica that does not get it returns `skipped: True` and touches
    nothing. That key is the difference between "the other replica had
    it" and "we ran and there was nothing to do" -- both produce zero
    actions, and a reconciler that had silently stopped running would
    otherwise be indistinguishable from a quiet cluster.
    """
    if dry_run is None:
        dry_run = not DELETES

    async with reconcile_lock(conn) as held:
        if not held:
            logger.info(
                "reconcile: skipped, another replica holds the lock",
                extra={"event": "reconcile.skipped"})
            return {"goals": 0, "dry_run": dry_run, "skipped": True}
        return await _reconcile_locked(conn, driver, dry_run)


async def _reconcile_locked(conn, driver, dry_run: bool) -> dict:
    """The pass itself. Only ever called with the reconcile lock held."""
    pairs = await conn.fetch(GOALS_SQL)
    actions: list[dict] = []

    for row in pairs:
        # sweep_orphans=False: the orphan sweep is GLOBAL and belongs
        # after the loop. Left on, it would run once per goal, and each
        # run would consider every container on the host with only this
        # goal's executions in its keep-set.
        actions.extend(await reconcile_tick(
            conn, driver, row["goal_id"],
            machine_version=row["machine_version"],
            sweep_orphans=False, dry_run=dry_run))

    # ── X: RUNS WHOSE REASON FOR EXISTING HAS GONE ──────────────────
    #
    # BEFORE the pod and claim sweeps below, deliberately. This cancels
    # through the mailbox, so the pod, the claim, the admission slot and
    # the criterion Jobs all follow from the state change -- and doing it
    # first means the sweeps that come after see the terminal state in
    # this same pass rather than the next one.
    #
    # A deleted GOAL already had a teardown; a cancelled STORY never did,
    # and `stories.state = 'cancelled'` is written in at least two places
    # that know nothing about executions. A sweep is the only form that
    # catches the next writer too -- and the rows already stranded.
    from kernel.conductor.abandoned import cancel_abandoned  # noqa: PLC0415

    if not dry_run:
        actions.extend(await cancel_abandoned(conn))

    # ── AND A REVIEW THAT CANNOT REPORT ON ITSELF ───────────────────
    #
    # BEFORE the reclaim pass below, deliberately. `reclaim` now stands
    # off while `phase = 'review'`, so a wedged review holds its
    # workspace, its slot and its node until something ends the phase --
    # and this is that something. Running it first means the release
    # happens in THIS pass rather than twenty seconds later in the next.
    from kernel.conductor.review_deadline import sweep as review_sweep

    try:
        actions.extend(await review_sweep(conn, dry_run=dry_run))
    except Exception:                                       # noqa: BLE001
        # The pass also reaps pods. A sweep that dies here must not take
        # that with it.
        logger.exception("review deadline sweep failed")

    # The keep-set is every container any execution still claims, across
    # all goals -- which is the set the sweep always needed and could not
    # have while it ran inside a per-goal pass.
    keep = {r["container_id"] for r in await conn.fetch(
        "SELECT container_id FROM executions WHERE container_id IS NOT NULL")}
    actions.extend(await orphan_sweep(conn, driver, keep, dry_run=dry_run))

    # ── X3: AND THE VERIFIER'S JOBS ─────────────────────────────────
    #
    # `run_job` deletes its own Job in a `finally`, which covers the
    # ordinary path and nothing else: a conductor that dies mid-
    # verification leaves the Job to `ttlSecondsAfterFinished`, and that
    # only starts counting once the Job COMPLETES. A cancelled execution
    # -- now routine, since deleting a goal or story cancels its runs --
    # leaves its criteria running against a tree whose verdict nobody
    # will read. The labels to find them were always there.
    from kernel.conductor.jobs_sweep import sweep_jobs       # noqa: PLC0415

    actions.extend(await sweep_jobs(conn, driver, dry_run=dry_run))

    # ── C: the three leaks with no collector at all ─────────────────
    #
    # An ephemeral claim (podcheck's) that `list_workspaces` will not
    # touch because its name is not `ws-exec-<digits>`; an envcache that
    # expires on read and never unlinks; and admission slots whose
    # execution died before it could transition, so the release TRIGGER
    # never fired. Each one silently consumes a ceiling -- the PVC quota,
    # the disk, the goal's concurrency -- and the symptom always arrives
    # somewhere else as "nothing runs".
    from kernel.conductor.admit import release_dead_slots    # noqa: PLC0415
    from kernel.conductor.ephemeral import sweep_ephemeral   # noqa: PLC0415
    from kernel.verifier.prepare import evict_cache          # noqa: PLC0415

    actions.extend(await sweep_ephemeral(conn, driver, dry_run=dry_run))
    if not dry_run:
        actions.extend(await release_dead_slots(conn))
        await asyncio.to_thread(evict_cache)

    # ── I6: THE DISK, WHICH NOTHING HAS EVER RECLAIMED ───────────────
    #
    # The orphan sweep above destroys PODS. A claim outlives its pod on
    # purpose -- `destroy()` keeps it so a preempted Spot pod does not
    # take the workspace with it -- and nothing ever deleted one. The
    # namespace quota is 40 claims, so that is not a cost leak: it is
    # forty executions EVER, after which nothing can run.
    #
    # After the orphan sweep, because both destroy pods and this one
    # also deletes the disk underneath: letting the orphan sweep run
    # first means a pod it would have reaped is already gone rather than
    # being reaped a tick later against a claim that no longer exists.
    # `driver` first, because the claims are the substrate's to enumerate
    # and the database only says which of them are finished with. Asking
    # the database first is what the first version did, and it could not
    # see a single one of them: `container_id` is nulled when the pod
    # dies and the claim outlives it on purpose.
    for row in await releasable(conn, driver):
        try:
            actions.append(await release(conn, driver, row, dry_run=dry_run))
        except Exception:                                   # noqa: BLE001
            # ONE EXECUTION'S FAILURE IS NOT THE SWEEP'S. A claim that
            # will not delete -- still attached, API hiccup -- must not
            # stop the reconciler reaching the next one, or a single
            # wedged workspace holds the quota closed for everything.
            logger.exception("reclaim: execution %s", row["id"])

    # `skipped: False` is present on the ran-it path too, so a reader
    # of the summary never has to tell "did not skip" apart from "this
    # summary predates the lock".
    summary: dict = {"goals": len(pairs), "dry_run": dry_run,
                     "skipped": False}
    for act in actions:
        name = act.get("action") or "unknown"
        summary[name] = summary.get(name, 0) + 1

    would = "would have " if dry_run else ""
    logger.info("reconcile: %d goal(s), %s%s",
                len(pairs), would,
                ", ".join(f"{v} {k}" for k, v in sorted(summary.items())
                          if k not in META_KEYS) or "nothing to do",
                extra={"event": "reconcile"})
    return summary
