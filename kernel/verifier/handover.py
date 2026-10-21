"""The RWOP handover: take the pod off the disk, and put it back.

PLAN 5.5 PRICES THIS AT ~30 SECONDS AND IT IS NOT AVOIDABLE. The
verification claim is ReadWriteOncePod because a GCE disk takes one
attacher: `pod.pvc_manifest` chose it as the SEQUENCER for exactly this
handover, so that the API server refuses the overlap rather than letting
a criterion read a `.venv` the prepare Job is halfway through writing.

The consequence is a call somebody has to make. A prepare Job submitted
while the warm pod holds the claim never starts -- it is not an error
this module could catch, it is a pod that stays Pending until its
deadline -- so the detach comes first, the Job runs, and the pod is
recreated on the volume the Job just rewrote.

A FILE OF ITS OWN BECAUSE IT IS THE ONLY PART OF `rebuild.py` THAT TALKS
TO A CLUSTER -- and, since 5.17 item 11, it no longer talks to one
directly at all. Everything else in `rebuild.py` is a decision about
hashes and rows; this is three substrate calls, and all three are now
somebody else's module: `reclaim.reap_verification`, `k8s_verify.ensure`
and `Driver.claim_exists`. The last of those was a reach-through into
`driver._core_api()` until the seam existed to take it.
"""

from __future__ import annotations

import logging

from kernel.verifier import pod as verify_pod

logger = logging.getLogger(__name__)


async def on_volume(driver, eid: int, state: dict) -> bool:
    """Is this run's verification on a volume at all, as best we can tell?

    A substrate that would not answer falls back to what we have already
    built for this execution. An `env_key` means a prepare Job wrote one,
    so the run IS on the volume path and `rebuild` must keep deciding for
    it -- whereupon its refresh fails loudly and holds the claim, which
    is the safe direction. A run we have never prepared anything for has
    nothing to be stale about, so it is judged.
    """
    try:
        return await has_claim(driver, eid)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("execution %s: could not read the verification "
                       "claim: %s", eid, exc)
        return bool(state["env_key"])


async def has_claim(driver, execution_id: int) -> bool:
    """Does this execution have a verification volume to prepare onto?

    ASKED BEFORE ANYTHING IS SPENT, and it is the guard that keeps the
    whole rebuild gate inert for a run that is not on the volume path. A prepare
    Job pointed at a claim that does not exist fails, and 5.8 row 5 would
    turn that into `error` on every criterion -- telling an executor its
    manifest is broken because a PVC was never provisioned.

    THROUGH THE SEAM NOW, and that is all that changed. This used to
    reach `driver._core_api()` and import `kubernetes._not_found` --
    a substrate's private module, from a function every substrate calls,
    so a driver with no claims got an `AttributeError` out of the middle
    of a verification. E17 hoisted `run_job_on_claim` for exactly that
    reason and left this behind; 5.17 item 11 is the other half.

    THE NAME IS STILL BUILT HERE. `verify_pod.claim_name` is the
    verifier's own convention -- which execution owns which disk -- and
    the driver is asked about a name in its namespace, nothing more. The
    same split `run_job_on_claim` makes, which also takes `claim` as a
    string the caller chose.
    """
    return await driver.claim_exists(verify_pod.claim_name(execution_id))


async def detach(driver, eid: int) -> None:
    """Take the warm pod off the disk so the prepare Job can attach it.

    THE CLAIM IS ReadWriteOncePod AND THAT IS THE SEQUENCER, not a
    precaution -- `pod.pvc_manifest` says so: a GCE disk takes one
    attacher, so the API server REFUSES the overlap instead of letting a
    criterion read a `.venv` the Job is halfway through writing. Which
    means the Job cannot start while the pod holds it, and the detach is
    a call somebody has to make.

    `reap_verification(keep_claim=True)` is that call and it is the ONE
    place plan 5.7's triggers arrive. The manifest-hash trigger cannot be
    a sweep -- the hash comes from the tree's own manifest bytes and the
    stamp lives on the volume, so the conductor cannot know what is
    wanted and only the thing about to verify does. This module is that
    thing, so this module is the caller.

    `keep_claim=True`, because the disk is the point: the tree, and
    everything on `volume.ENV` the new manifest did not invalidate, are
    what the rebuild is rebuilding ONTO. Plan 5.5 prices the
    detach/attach at ~30s and 5.10 accepts it as the honest cost of an
    environment that genuinely changed.
    """
    from kernel.conductor.reclaim import reap_verification        # noqa: PLC0415

    await reap_verification(driver, eid, why="the committed manifest changed",
                            keep_claim=True)


def claim_capable(driver) -> bool:
    """Does this substrate have persistent claims to verify on?

    ONE SPELLING, because there were three -- `batch.warm_for`,
    `prepare_volume` and this file each asked
    `CLAIM_JOB_MODEL == "job"` in their own words, and a fourth answer
    waiting to disagree is how the tar path came to run on the one
    substrate that is not supposed to use it.

    TRUE MEANS THE POD IS NOT OPTIONAL. On Kubernetes verification runs
    in the verification pod -- that is what it is for, and the base
    image already carries python, node, npx/corepack and git pinned by
    digest (5.2), so the pod is useful the instant it starts and only
    the PROJECT's dependencies wait on a manifest. False is Docker and
    E0, where there is no claim and the tar is not a fallback but the
    only thing that was ever possible.
    """
    return getattr(driver, "CLAIM_JOB_MODEL", "none") == "job"


async def warm_ready(driver, eid: int) -> bool:
    """Can a criterion actually be run in this execution's pod yet?

    SEPARATE FROM "does the pod exist". `attach` recreates the pod after
    every prepare and returns immediately, `_refresh` records `env_key`
    on the next line, and the row is judgeable about four seconds later
    -- while a pod that was just created has no address for rather
    longer. Measured on execution 218:

        execution 218: verification claim and pod provisioned   tick 72
        environment 3f29992cc385 ready (miss)                   tick 117
        no warm pod (verify-218 has no address yet)             tick 127

    So the FIRST verification after any prepare -- the one that matters,
    because most stories verify once -- lost the race every time and
    fell through to the tar.
    """
    from kernel.drivers import k8s_verify                      # noqa: PLC0415

    try:
        await k8s_verify.address(driver, verify_pod.pod_name(eid))
        return True
    except Exception:                                          # noqa: BLE001
        return False


async def provision(conn, driver, eid: int) -> bool:
    """Create this run's verification claim and pod. E19, and it was the
    ONE thing Phase E never built.

    WHAT WAS WRONG, in the plan's own words (5.19, found on execution
    211 and unchanged on 217): "the code that REAPS verification pods is
    wired, and the code that CREATES them is not." `k8s_verify.ensure`
    had exactly two callers -- `attach`, reached only AFTER a build, and
    `preempted`, which refuses without an existing claim. Every route to
    creating the claim required the claim. So `on_volume` was False for
    every execution that has ever run, `rebuild` never fired a prepare
    Job, `warm_for` never found an `env_key`, and every story in the
    fleet verified down the tar path -- archive, object store, and one
    ephemeral Job per criterion.

    Measured on 217: `exec-217`, `cap-058a1e916137`, three `crit-*`
    Jobs, no `verify-217`, no claim. Identical to 211.

    IDEMPOTENT, because `ensure` adopts. A second call on a live claim
    is the recovery path `preempted` already relies on.

    FAILURE-SAFE, AND THAT IS THE WHOLE SAFETY OF THIS CHANGE. Returning
    False leaves the caller on exactly the path it took before this
    function existed -- `on_volume` False, judged through the tar. So a
    cluster that refuses the PVC, an RBAC gap, a quota, all degrade to
    today's behaviour rather than stopping verification. The volume path
    is an improvement that can fail; it is not a dependency.
    """
    from kernel.workspace.podfs_client import is_pod          # noqa: PLC0415

    if not is_pod(driver):
        # Docker and E0 have no claims. `warm_for` says the same thing
        # at the other end: the tar path there is not a fallback, it is
        # the only thing that was ever possible.
        return False
    try:
        from kernel.drivers import k8s_verify                 # noqa: PLC0415
        from kernel.verifier.writer import load_contract      # noqa: PLC0415

        contract, _ = await load_contract(conn, eid)
        await k8s_verify.ensure(driver, eid,
                                image=verify_pod.image_for(contract),
                                size=verify_pod.size_for(contract))
        logger.info("execution %s: verification claim and pod provisioned",
                    eid, extra={"event": "verify.provision"})
        return True
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("execution %s: could not provision the verification "
                       "volume (%s); this run verifies through the tar "
                       "path", eid, exc)
        return False


async def attach(driver, eid: int, contract: dict | None) -> None:
    """And put the pod back, on the volume the Job just rewrote.

    The other half of `detach`, and `reclaim.reap_verification`'s
    docstring names this sequence: reap, then `ensure`, which is
    otherwise idempotent and would have ADOPTED the stale pod.

    Failure-safe. The environment is built and recorded by the time this
    runs, so a pod that cannot be created yet is a thing the next
    verification's own `ensure` and `reclaim` will settle -- losing the
    build over it would be paying twice for one manifest.
    """
    try:
        from kernel.drivers import k8s_verify                     # noqa: PLC0415

        name = await k8s_verify.ensure(
            driver, eid, image=verify_pod.image_for(contract),
            size=verify_pod.size_for(contract))
        # AND WAIT FOR IT, which this did not do. `_refresh` records
        # `env_key` on the next line and the row is judgeable a tick
        # later; a pod created a moment ago has no address, so
        # `warm_for` refused it and the tar path ran with the freshly
        # built volume sitting there unused. Execution 218, tick 127.
        #
        # `await_ready` probes /health rather than the phase, which is
        # the distinction its own docstring draws: this container is
        # Running within a second and cannot answer for longer than
        # that.
        await k8s_verify.await_ready(driver, name)
    except Exception as exc:                                # noqa: BLE001
        logger.warning("execution %s: the verification pod was not "
                       "recreated after prepare: %s", eid, exc)

