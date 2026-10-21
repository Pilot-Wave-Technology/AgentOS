"""Containers no execution claims any more.

EXTRACTED FROM reconcile.py's section 4, unchanged in behaviour, because
it has two callers now and they need different keep-sets.

`reconcile_tick` is per-goal and passes the containers ITS goal's
executions hold. That was the only caller, and it is why the sweep was
written inside the per-goal pass. `reconcile_all` runs across every goal
and passes the union — which is the set this always needed: a container
belonging to another goal's live execution is not an orphan, and the
per-goal version only avoided reaping it because of the label check
below, not because it knew.

TERMINAL IS READ FROM THE EXECUTION'S OWN MACHINE. A run carried forward
has different terminal states, and judging one by another machine's is
how a live run gets reaped.

WHAT THIS REAPER OWNS: pods the kernel created DIRECTLY -- an execution's
worker pod, and nothing else. Those have no controller, so if this does
not collect one, nothing will.

AND ONE IT DOES NOT OWN BUT STILL BACKS UP. Phase E's VERIFICATION pod
is a bare Pod on purpose (a Job runs to completion; a warm pod does
not), so it classifies KERNEL and this sweep was already collecting it
on terminal -- correctly, and by accident, since nobody had decided it
should. Plan 5.7 gives it to `reclaim.py`, whose authority is the
execution row rather than a label. This stays as the BACKSTOP, because
`reclaim` reaches it through one more API listing than this does and a
listing that fails must not mean a tier-sized pod lives for ever. What
changed is that the reap now SAYS it was a verification pod, and that
it no longer writes `container_id` on a row that never named it.

WHAT IT MUST NEVER TOUCH: a pod a Job owns (`kernel.ownership` says
which, and says why the test is the label and not the name), and a pod
labelled as somebody else's. `jobs_sweep.py` deals with Jobs whose
EXECUTION has finished, which is the real gap this sweep was reaching
for when it destroyed execution 208's capture.

WHAT IT DOES WITH SOMETHING IT CANNOT IDENTIFY: a pod naming an
execution it cannot parse is KEPT -- the rule `jobs_sweep._wanted` and
`list_workspaces` already follow. A pod naming NO execution is not
unidentified: it is identified as an orphan, and it is the case this
sweep exists for.
"""

from __future__ import annotations

import logging

from kernel.ownership import (EPHEMERAL_POD_LABEL, Owner, execution_of,
                              owner_label, pod_owner)

logger = logging.getLogger(__name__)

# Is this execution still entitled to compute? Its OWN machine decides.
LIVE_SQL = """
    SELECT NOT (m.terminal_states @> to_jsonb(e.state))
      FROM executions e
      JOIN run_state_machine m ON m.version = e.machine_version
     WHERE e.id = $1
"""

# A pod a JOB owns is not this sweep's business. Measured on execution
# 208, and it is the conductor reaping its own verifier.
#
# `capture_manifest` stamps `managed-by: kernel` -- correct, it IS ours
# -- so `list_containers()` returns every capture pod. It stamps
# `execution-id` only when the spec carries one, and NEITHER
# `prepare._prepare_mount` NOR `environment.build_deps` sets it. So the
# sweep saw a kernel-owned pod with no execution, concluded "no matching
# execution", and called `destroy(cid, stop_timeout=0)` on a Job that
# was seven seconds into a `pip install`:
#
#     Killing  Stopping container work
#     Killing  Stopping container capture
#
# and the verifier then reported "the Job failed and its pod is gone,
# so there is no record of what the criterion did" -- true, and the
# conductor had done it.
#
# INTERMITTENT, WHICH IS WHY IT LOOKED LIKE FLAKE. A capture runs about
# 28 seconds and reconcile lands every 15 ticks, so it only bites when
# the two overlap. Three of four captures survived; the fourth read as
# an infrastructure hiccup.
#
# THE LABEL IS THE RIGHT TEST, not the execution id. Stamping the id
# (done separately) improves provenance, but the INVARIANT is that a
# pod with its own controller has its own lifecycle: the Job decides
# when it dies, `run_job` deletes it in a `finally`, and
# `ttlSecondsAfterFinished` is the backstop. A second reaper racing that
# controller can only ever be wrong -- and `jobs_sweep.py` already
# covers Jobs whose execution has finished, which is the real gap this
# sweep was reaching for.
#
# THE TEST ITSELF NOW LIVES IN `kernel.ownership`, with the other four
# reapers', because one instance of a rule is a fix and five spellings
# of it are the next incident. This name stays as the local spelling of
# it for readers who arrive here from the log line.
EPHEMERAL_LABEL = EPHEMERAL_POD_LABEL


async def orphan_sweep(conn, driver, keep, *, dry_run: bool = False,
                       live_containers=None) -> list[dict]:
    """Destroy containers no live execution claims. Returns actions.

    `keep` is the set of container ids executions still hold. `dry_run`
    counts without destroying or clearing rows — see sweep.py for why
    that is the default there.
    """
    # The class label Phase E's pod wears, read here only so a reap can
    # NAME what it took -- plan 13: "a reap logs what it took and why",
    # and "container exec-207 destroyed" is a lie when the object was a
    # verification pod. Imported inside the function for the reason
    # `jobs_sweep` and `ephemeral` import the driver that way: a Docker
    # deployment must not pull the verifier's module graph in to run a
    # sweep that will find nothing.
    from kernel.verifier.pod import APP, APP_LABEL         # noqa: PLC0415

    if live_containers is None:
        live_containers = await driver.list_containers()

    actions: list[dict] = []
    for c in live_containers:
        cid = c["id"]
        if cid in keep:
            continue

        # NOT OURS TO END. Either a controller already owns this pod's
        # lifecycle -- see the note at EPHEMERAL_LABEL, this sweep killed
        # live capture pods mid-install -- or a label names an owner who
        # is not us. Both are decided in one place now.
        if pod_owner(c["labels"]) is not Owner.KERNEL:
            continue

        eid_label = owner_label(c["labels"])
        if eid_label:
            eid = execution_of(c["labels"])
            if eid is None:
                # IT NAMES AN OWNER WE CANNOT READ, so it is kept -- and
                # before this, `int(eid_label)` raised here and took the
                # whole pass down with it, so one mislabelled pod stopped
                # every later reap in the same tick.
                logger.warning(
                    "container %s has an unreadable %s label (%r); kept",
                    cid[:12], "execution-id", eid_label,
                    extra={"event": "reconcile.unidentified"})
                continue
            # EXISTING IS NOT ENOUGH -- IT MUST STILL BE LIVE.
            #
            # This skipped every container whose execution existed, in
            # any state, on the reasoning that another goal's tick would
            # deal with it. Section 3b only queries terminal executions
            # IN ITS OWN GOAL, so a cancelled run in a goal nobody
            # reconciles fell through both and held its container and
            # its bridge port for ever. Twelve accumulated in one
            # session, up to two hours old, and the first symptom was an
            # unrelated gate failing with "no free port in 9200-9399".
            live = await conn.fetchval(LIVE_SQL, eid)
            if live:
                continue                    # live, just not in the keep-set
            if live is False and not dry_run:
                # Terminal and still holding compute. Clear the row so
                # the next tick does not find it again -- BUT ONLY IF
                # THE ROW NAMES THIS POD. An execution now owns two
                # kernel pods, its worker and its verification pod, and
                # `container_id` is the worker's alone: nulling it on a
                # verification pod's account would tell every other loop
                # the sandbox is gone while it is still running. The
                # predicate costs one parameter and makes this safe for
                # whatever third pod kind arrives next.
                await conn.execute(
                    "UPDATE executions SET container_id = NULL "
                    "WHERE id = $1 AND container_id = $2", eid, cid)

        kind = ("verification pod" if c["labels"].get(APP_LABEL) == APP
                else "container")
        why = ("its execution is terminal" if eid_label
               else "no matching execution")
        if dry_run:
            logger.info("reconcile would destroy %s %s: %s",
                        kind, cid[:12], why,
                        extra={"event": "reconcile.dry_run"})
        else:
            await driver.destroy(cid, stop_timeout=0)
            # SAYS WHAT IT TOOK AND WHY, in the log and not only in the
            # returned action -- plan 13. The returned list reaches a
            # counter; the person reading at 2am reaches the log.
            logger.info("%s %s destroyed: %s", kind, cid[:12], why,
                        extra={"event": "reconcile"})
        actions.append({
            "action": "would_reap" if dry_run else "reap",
            "execution_id": eid if eid_label else None,
            "detail": (f"{kind} {cid[:12]} "
                       + ("would be destroyed: " if dry_run
                          else "destroyed: ") + why),
        })
    return actions
