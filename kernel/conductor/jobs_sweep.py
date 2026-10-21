"""Verifier Jobs nobody is waiting for any more.

A criterion runs as a Job and `run_job` deletes it in a `finally`. That
covers the ordinary path and nothing else:

  * if the conductor dies mid-verification the `finally` never runs, and
    the only backstop is `ttlSecondsAfterFinished: 300` -- which starts
    counting when the Job COMPLETES. A Job wedged Active is bounded by
    `activeDeadlineSeconds` alone, and until then it holds CPU, memory
    and a slot against `count/jobs.batch: 150`.
  * if the execution is CANCELLED while its criteria are running --
    which is now routine, because deleting a goal or a story cancels its
    runs -- nothing tells the Jobs. They run to completion, against a
    tree whose verdict nobody will read, and bill for it.

THE LABELS WERE ALREADY THERE. `k8s_job.job_manifest` stamps
`managed-by: kernel` and `execution-id`, so the question "whose Job is
this, and is that execution still interested" was always answerable --
nothing was asking it.

WHY DELETING IS SAFE HERE AND EVIDENCE IS NOT LOST. `run_job` reads the
pod and its logs BEFORE the `finally`, so an ordinary failure has
already been captured by the time anything here could see it. The Jobs
this reaps are ones whose caller is gone, and a verdict nobody is
waiting for is not evidence.

WHAT THIS REAPER OWNS: kernel-labelled JOBS -- the controller objects
themselves, not pods. Deleting the Job is how a Job's pod is properly
ended; the pods go with it under background propagation, which is why
`orphan_sweep` has no business reaching past a controller to the pod
underneath.

WHAT IT MUST NEVER TOUCH: a Job whose labels name an owner who is not
us, and a Job whose execution is still interested in the answer.

WHAT IT DOES WITH SOMETHING IT CANNOT IDENTIFY: kept. No `execution-id`
label, or one that does not parse -- both mean nobody here can say whose
Job this is, and an unidentified Job is somebody's hand-made debugging
run until proven otherwise.
"""

from __future__ import annotations

import logging

from kernel.ownership import (KERNEL_SELECTOR, OWNER_LABEL, may_reap_job,
                              parse_execution_id)

logger = logging.getLogger(__name__)

# Which Jobs are ours to judge: `managed-by` AND `OWNER_LABEL`, both,
# deliberately. `managed-by` alone would include a Job an operator made
# by hand from our manifest, and `execution-id` alone would match a
# label somebody else chose. Both names come from `kernel.ownership`,
# where the two are defined next to the four other reapers' rules.
#
# ONE SPELLING OF THE SELECTOR. It was the third of four copies of
# `managed-by=kernel` -- with both Kubernetes listers and the Docker
# driver's container filter -- and four copies are four chances for one
# to be edited into matching nothing. A sweep that lists nothing reports
# success.
SELECTOR = KERNEL_SELECTOR

# Is this execution still interested in a verdict? Its OWN machine
# decides, for the reason `orphans.py` gives: a run carried forward has
# different terminal states.
LIVE_SQL = """
    SELECT NOT (m.terminal_states @> to_jsonb(e.state))
      FROM executions e
      JOIN run_state_machine m ON m.version = e.machine_version
     WHERE e.id = $1
"""


async def _wanted(conn, execution_id: str) -> bool:
    """True when the execution still wants this Job's answer.

    AN UNPARSEABLE OR MISSING OWNER IS KEPT, not reaped. The same rule
    `list_workspaces` applies to claims whose name does not parse, and
    the same rule `kernel.ownership` states for every reaper: a sweep
    that destroys what it cannot identify is a sweep that destroys
    somebody's hand-made debugging Job.
    """
    eid = parse_execution_id(execution_id)
    if eid is None:
        return True
    live = await conn.fetchval(LIVE_SQL, eid)
    # No row at all: the execution is gone, so nobody is waiting.
    return bool(live) if live is not None else False


async def sweep_jobs(conn, driver, *, dry_run: bool = False) -> list:
    """Delete verifier Jobs whose execution has finished or gone."""
    from kernel.drivers.k8s_call import quiet                # noqa: PLC0415
    from kernel.drivers.kubernetes import _api               # noqa: PLC0415

    # `claim_capable`, NOT `driver.name`. `unscheduled.py` wrote this
    # down and nobody acted on it: "that idiom is BROKEN -- no driver in
    # this tree defines `.name` ... so the guard is always true and the
    # sweep behind it returns `[]` unconditionally". This sweep has
    # therefore never run.
    from kernel.verifier.handover import claim_capable      # noqa: PLC0415

    if not claim_capable(driver):
        # Docker runs criteria as containers and removes them itself.
        return []

    client, _ = _api()
    batch = client.BatchV1Api()
    listing = await quiet(batch.list_namespaced_job,
                          what="listing verifier jobs",
                          namespace=driver.namespace,
                          label_selector=SELECTOR)
    if listing is None:
        return []

    actions = []
    for job in listing.items:
        name = job.metadata.name
        labels = dict(job.metadata.labels or {})
        owner = labels.get(OWNER_LABEL)
        # `may_reap_job` AND NOT `may_reap_pod`, which is the distinction
        # `kernel.ownership` spells out: a Job wears `agentos.ephemeral`
        # too, and on a Job that says "throwaway", not "a controller owns
        # me". Reading the pod rule onto Jobs would skip every Job there
        # is and leave this looking like a clean cluster.
        if (owner is None or not may_reap_job(labels)
                or await _wanted(conn, owner)):
            continue
        if dry_run:
            actions.append({"execution_id": owner, "action": "job_orphan",
                            "detail": f"{name} would be deleted"})
            continue
        # Background propagation, the same as `run_job`'s own delete: the
        # pods go with the Job and nothing here waits for them.
        await quiet(batch.delete_namespaced_job,
                    what=f"deleting orphaned job {name}",
                    name=name, namespace=driver.namespace,
                    propagation_policy="Background")
        logger.info("job %s deleted: execution %s is finished with it",
                    name, owner, extra={"event": "reconcile"})
        actions.append({"execution_id": owner, "action": "job_reaped",
                        "detail": name})
    return actions
