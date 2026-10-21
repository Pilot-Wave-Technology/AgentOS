"""When an execution's compute and its disk may go.

THE CEILING THIS EXISTS FOR IS NOT COST. `_ensure_pvc` creates a claim
per execution, `destroy()` deliberately keeps it — "deleting the claim
here would destroy the workspace every time a Spot pod was preempted,
which is the exact scenario the claim exists to survive" — the orphan
sweep destroys only pods, and no delete-claim call existed anywhere. The
namespace quota is `persistentvolumeclaims: "40"`. So the fortieth
execution is not the fortieth CONCURRENT one; it is the fortieth EVER,
after which nothing can run a story at all.

WHY NOT AT VERIFYING, WHERE COMPUTE IS ALREADY RELEASED. Because
`VERIFYING -> RUNNING` on `completion_rejected` is a real edge: a
rejected completion goes back to the executor. Tearing down there costs
a full pod rebuild on every rejection, and blanks the assistant panel at
exactly the moment somebody is watching for the outcome. The admission
slot is already held through VERIFYING for this reason; the compute was
not, and now is.

WHY THE STORY AND NOT THE EXECUTION. A story may be attempted more
than once and review sits between DONE and accepted, so
the workspace stays reachable until the work is taken.

AN ERRAND IS A STORY FOR THIS PURPOSE. It is tempting to release an
errand on its EXECUTION reaching DONE, since nothing reviews an errand
-- and that is the difference between the machine finishing and a
person deciding they have what they need. An autonomous errand reaches
the first without the second; releasing there takes the tree away while
somebody is reading the output. Both kinds wait for the story.

WHAT THIS REAPER OWNS: the pod an EXECUTION ROW claims in
`container_id`, and the workspace claim named after that execution. Its
authority is the database row, not a label -- which is a stronger claim
than any label makes, and the reason nothing here consults
`kernel.ownership`'s pod predicate: a row saying "this execution is
holding that pod" is the execution itself speaking.

WHAT IT MUST NEVER TOUCH: anything the substrate holds that no execution
row names. A capture Job's pod is never written into `container_id`, and
if a future dispatch path ever wrote one there this reaper would inherit
execution 208's bug through the front door -- so that column is the
invariant to protect, and `kernel/drivers/kubernetes.py::list_workspaces`
is the matching guard on the claim side.

WHAT IT DOES WITH SOMETHING IT CANNOT IDENTIFY: `list_workspaces` never
offers it. A claim whose name is not `ws-exec-<digits>` -- podcheck's,
an operator's -- is not enumerated at all, so this pass cannot reach one
even by mistake. `ephemeral.py` collects the labelled subset of those by
age, which is the narrower statement.

AND SINCE PHASE E, A SECOND PAIR: THE VERIFICATION POD AND ITS CLAIM.
Same authority, same order, and the same reason it belongs here rather
than in a label sweep -- `verify-ws-<id>` is deliberately NOT
`ws-exec-<id>`, so `list_workspaces` will not enumerate it and
`ephemeral.py`'s selector does not match it, which is what keeps every
other sweep off an environment mid-verification and is also why, until
this, NOTHING COLLECTED IT AT ALL. The ceiling is 120 claims and every
execution now holds two of them. Plan 5.7's three triggers are stated
at `verification_reapable` below.

THE POD OUTLIVES THE VERIFIER DEPLOYMENT, AND THAT IS ALLOWED. If the
Deployment dies mid-verification nothing tears the warm pod down --
there is no controller to do it, which is the whole point of a pod that
stays warm across generations. It is bounded rather than leaked: this
pass reads the execution ROW, so it collects the pod on terminal
whether or not a verifier is alive to ask. Do not "fix" that by giving
the pod an owner; an owner is a controller, and a controller would tear
the `.venv` down between rounds.

WHY FAILED IS KEPT, AND KEPT BRIEFLY. FAILED is not "something went
wrong": a leg that raises keeps the run alive, a rejected completion
returns to RUNNING, a failing criterion is a verdict, and a tool error
is a ToolResult. Exactly one FAILED ever has a pod -- `leg_ceiling`,
"N legs without converging" -- and that is precisely the run whose
workspace answers the question. Two hours of no movement is long enough
to look and short enough that stuck runs cannot accumulate into the
outage above. (The other FAILED, `unplaceable`, is a PENDING -> FAILED
write that never dispatched: no pod, no claim, nothing to reclaim.)
"""

from __future__ import annotations

import logging

from kernel.ownership import KERNEL_SELECTOR, execution_in_name, execution_of

logger = logging.getLogger(__name__)

# NAME AND LABEL READERS ONLY, and the docstring above still holds: this
# pass consults no pod OWNERSHIP predicate, because a row saying "this
# execution is holding that object" is a stronger statement than any
# label makes. What is imported here is how to READ an id back off a
# name or a label, which is the step before asking the database -- and
# it is imported rather than re-spelled so a second `int(tail)` cannot
# raise its way out of a sweep the way the first one did.

# How long a FAILED run keeps its pod and its disk with nothing
# happening. Two hours: see the module docstring.
STALE_HOURS = 2

# Plan 5.7's third trigger: a verification pod whose execution has
# wedged without ever reaching a terminal state. TWELVE HOURS, and the
# number is chosen from above rather than from below. A single criterion
# is capped at `kernel/verifier/budget.py::MAX_S` = 2h, a pass runs its
# criteria SERIALLY with a timeout of their SUM, and nothing bounds how
# many a contract may declare -- so no threshold is provably longer than
# a lawful pass, and the guard against being early is that this trigger
# takes THE POD ONLY. Being wrong costs a container start; the `.venv`
# on the claim survives it.
IDLE_HOURS = 12

# The marker that tells `release` below which pair a row is about. A
# second function on `sweep.py`'s side would have done as well and would
# have to be added to a file this phase does not own; this rides the
# list that caller already walks, with its per-row `try/except` and its
# existing summary counter.
VERIFICATION = "verification"

# Executions whose compute and disk may be released, with the reason.
#
# One statement, read by the reconciler. `stories.state` is the trigger
# for the ordinary path -- NOT `executions.state` -- because a story is
# attempted more than once and review sits after DONE.
#
# LEFT JOIN, because an execution need not have a story: an errand
# proposed and cancelled before a story existed still holds a claim, and
# an inner join would leak exactly those.
#
# NO "already released" COLUMN, deliberately -- and KEYED ON THE CLAIM,
# which is the correction that matters.
#
# The first version asked the EXECUTIONS table and required
# `container_id IS NOT NULL`, reasoning that a row stops offering itself
# once its pod is gone. That is true of the pod and false of the disk,
# and the disk is what this releases. `container_id` is nulled the
# moment the pod is destroyed while the claim SURVIVES ON PURPOSE --
# that is the preemption guarantee `destroy()` exists to give. So the
# query excluded precisely the rows that needed it. Observed on `next`:
# five cancelled executions, every `container_id` NULL, five claims
# bound and billing, none of them releasable.
#
# So the sweep starts from the CLAIMS the substrate actually has and
# asks the database about each one. That keeps the self-limiting
# property the column was avoided for -- a claim that is gone is not
# listed, so nothing is offered twice -- and keys it on the thing being
# reclaimed rather than on a proxy for it.
RELEASABLE_SQL = """
    SELECT e.id, e.container_id, e.state,
           CASE
             WHEN s.state IN ('done', 'reviewed') THEN 'story ' || s.state
             WHEN e.state = 'CANCELLED'           THEN 'run cancelled'
             ELSE 'failed and idle for ' || $2 || 'h'
           END AS why
      FROM executions e
      LEFT JOIN stories s ON s.id = e.story_id
     WHERE e.id = ANY($1::bigint[])
       -- AND NOT WHILE IT IS BEING REVIEWED. This releases on the STORY
       -- reaching `done` (see the note above on why the story's signal
       -- and not the execution's), and the review phase runs AFTER the
       -- story reaches `done` — in the same pod, reading the same
       -- workspace. Without this guard the first thing that happens
       -- after acceptance is that the tree the reviewer is about to
       -- read is deleted.
       --
       -- THIS MAKES THE REVIEW DEADLINE LOAD-BEARING. A phase that
       -- never ends now holds the workspace, the slot and the node
       -- indefinitely. `kernel/worker/review_finish.py` ends it on
       -- exhaustion and the sweep ends it on the wall clock; neither is
       -- optional once this line exists.
       AND COALESCE(e.phase, '') <> 'review'
       AND (
            -- AN ERRAND IS A STORY FOR THIS PURPOSE, and a version of
            -- this released on `kind = 'errand' AND state = 'DONE'` --
            -- reasoning that an errand is not reviewed, so its
            -- execution reaching DONE is the end of it. That is wrong
            -- and dangerous. The EXECUTION reaching DONE is the
            -- machine finishing; the STORY reaching done or reviewed
            -- is a person deciding they have what they need. An
            -- autonomous errand reaches the first without the second,
            -- and releasing there destroys the workspace while
            -- somebody is still reading the output it produced.
            --
            -- So both kinds wait for the same signal, and it is the
            -- human's.
            s.state IN ('done', 'reviewed')
            -- AND A STORY SOMEBODY STOPPED. `cancelled` and `rejected`
            -- were in neither list, so a cancelled story kept its pod,
            -- its PVC and its admission slot indefinitely -- observed on
            -- NCAL1, a deleted goal still holding thirteen executions.
            -- `kernel/conductor/abandoned.py` cancels those runs, which
            -- reaches `e.state = 'CANCELLED'` below; this is the
            -- backstop for a run that never took the edge, and a
            -- workspace whose story was abandoned is finished with
            -- either way.
         OR s.state IN ('cancelled', 'rejected')
         OR e.state = 'CANCELLED'
         OR (e.state = 'FAILED'
             AND e.updated_at < now() - ($2 || ' hours')::interval)
           )
     ORDER BY e.id
"""


async def releasable(conn, driver, stale_hours: int = STALE_HOURS) -> list:
    """Everything an execution is finished with: workspaces, then
    verification pods and their claims.

    TWO PASSES BEHIND ONE NAME so `sweep.py` needs no new call site --
    it already walks this list with a per-row `try/except`, which is the
    property that matters: one claim that will not delete must not stop
    the pass reaching the next, or a single wedged object holds the
    quota closed for everything behind it.
    """
    rows = await _workspaces(conn, driver, stale_hours)
    rows.extend(await verification_reapable(conn, driver))
    return rows


async def _workspaces(conn, driver, stale_hours: int) -> list:
    """Executions holding a workspace that may now go.

    Asks the SUBSTRATE what workspaces exist, then the database which of
    them are finished with. A driver with no reclaimable workspace --
    Docker, whose tree is a directory in a volume the next run reuses --
    returns nothing and this does nothing.
    """
    held = await driver.list_workspaces()
    if not held:
        return []
    rows = list(await conn.fetch(RELEASABLE_SQL, list(held), str(stale_hours)))

    # A claim whose execution row is GONE, which the query above cannot
    # return because there is nothing to join to. The row is deleted and
    # the disk is not, so it can only be found from this side -- and it
    # is the one leak with no other backstop at all: no state to reach
    # a terminal value, no pod to reap, no story to finish.
    known = {r["id"] for r in rows}
    seen = {r["id"] for r in await conn.fetch(
        "SELECT id FROM executions WHERE id = ANY($1::bigint[])", list(held))}
    for eid in sorted(set(held) - seen - known):
        rows.append({"id": eid, "container_id": None,
                     "why": "no execution row"})
    return rows


# Plan 5.7's other two triggers, asked of the execution ROW. `live` is
# read from the run's OWN machine for the reason `orphans.py` gives: a
# run carried forward has different terminal states, and judging one by
# another machine's is how a live run gets reaped.
VERIFY_SQL = """
    SELECT e.id, e.state,
           NOT (m.terminal_states @> to_jsonb(e.state)) AS live,
           (e.updated_at < now() - ($2 || ' hours')::interval) AS idle
      FROM executions e
      JOIN run_state_machine m ON m.version = e.machine_version
     WHERE e.id = ANY($1::bigint[])
"""


async def _verification_pods(driver) -> dict:
    """Verification pods the substrate holds, by execution.

    THE CLASS LABEL AND NOT THE NAME PREFIX. `verify-<id>` is a naming
    convention; `app=agentos-verification` is what the NetworkPolicies
    already select on, so it is the statement the pod actually makes
    about what it is. A pod whose `execution-id` does not parse is
    dropped here rather than guessed at -- the rule every reaper follows.
    """
    from kernel.verifier.pod import APP, APP_LABEL          # noqa: PLC0415

    found = {}
    for c in await driver.list_containers():
        if c["labels"].get(APP_LABEL) != APP:
            continue
        eid = execution_of(c["labels"])
        if eid is not None:
            found[eid] = c["id"]
    return found


async def _verification_claims(driver) -> set:
    """Executions whose verification claim the namespace still holds.

    Its own listing, because `list_workspaces` cannot offer it one: that
    method parses `ws-exec-<digits>` and skips everything else, which is
    exactly the protection that left this claim uncollected. `quiet`
    rather than `call` -- a listing that fails is a tick that reaps
    nothing, not a pass that dies, and `orphan_sweep` is the backstop
    for the pod half in the meantime.
    """
    from kernel.drivers.k8s_call import quiet               # noqa: PLC0415
    from kernel.verifier.pod import CLAIM_PREFIX            # noqa: PLC0415

    listing = await quiet(
        driver._core_api().list_namespaced_persistent_volume_claim,
        what="listing verification claims", namespace=driver.namespace,
        label_selector=KERNEL_SELECTOR)
    if listing is None:
        return set()
    held = {execution_in_name(c.metadata.name or "", CLAIM_PREFIX)
            for c in listing.items}
    held.discard(None)
    return held


async def verification_reapable(conn, driver, *,
                                idle_hours: int = IDLE_HOURS) -> list:
    """Verification objects their execution is finished with. Plan 5.7.

    | plan 5.7 trigger          | what goes             |
    | the execution is terminal | the pod AND the claim |
    | idle for `idle_hours`     | the POD only          |
    | the manifest hash changed | not here -- see below |

    and one the plan does not list because it is not a trigger so much
    as a hole: NO EXECUTION ROW AT ALL takes both. That is the leak with
    no other backstop -- no state left to reach a terminal value, no
    story to finish, and a claim name no other sweep parses.

    THE IDLE TRIGGER KEEPS THE DISK, which is `k8s_verify.release`'s
    "two triggers and not three" read from this side: an idle pod's
    environment is still the one the next round wants, and deleting it
    there rebuilds a `.venv` because nobody was looking -- the cost this
    whole design exists to remove. The claim still goes: a wedged run
    reaches a terminal state or loses its row eventually, and either one
    takes it on a later tick.

    THE MANIFEST-HASH TRIGGER IS NOT A SWEEP AND CANNOT BE. The hash is
    computed from the tree's own manifest bytes
    (`verifier/setups.py::fingerprint`) and the stamp that records it
    lives on the volume, so the conductor cannot know what is wanted.
    Whoever is about to verify knows -- `volume.stale` is that predicate
    -- and calls `reap_verification` below before `k8s_verify.ensure`,
    which is otherwise idempotent and would happily ADOPT the stale pod.

    AND IT IS QUIET WHEN THERE IS NOTHING. Both halves are driven from
    what the substrate actually holds, so an object already taken is not
    listed and is never offered twice -- which is what keeps an idle
    execution from reporting the same reap every tick for ever.
    """
    # `claim_capable`, NOT `driver.name`. This read
    # `getattr(driver, "name", "") != "kubernetes"` and
    # `KubernetesDriver` HAS NO `name` ATTRIBUTE -- so the getattr
    # returned "", the guard was true on every substrate, and this
    # function returned [] on every tick since it was written. The
    # listing, the triage and the deletion below have never run.
    #
    # INVISIBLE UNTIL E19. Nothing created verification claims, so a
    # reaper that collected none was indistinguishable from one that
    # worked. E19 makes one per coding story, which turns a dormant bug
    # into a 10GiB leak per story against the 120-claim ceiling W1
    # records. Measured: `verify-ws-218` still Bound thirteen hours
    # after execution 218 reached DONE, and `verify-ws-219` beside it.
    #
    # THE SAME SHAPE AS `is_pod`: a fact about the substrate asked in a
    # way the substrate does not answer. `claim_capable` is the question
    # actually being asked -- "does this substrate have persistent
    # claims" -- it is the one spelling the rest of the kernel now uses,
    # and a driver that grows or loses claims changes one declaration
    # rather than a string comparison in a file that never sees it.
    from kernel.verifier.handover import claim_capable      # noqa: PLC0415

    if not claim_capable(driver):
        # Docker has no verification pod: `stage.py` still runs criteria
        # as containers there and removes them itself.
        return []

    pods = await _verification_pods(driver)
    claims = await _verification_claims(driver)
    held = sorted(set(pods) | claims)
    if not held:
        return []

    rows = {r["id"]: r for r in
            await conn.fetch(VERIFY_SQL, held, str(idle_hours))}
    out = []
    for eid in held:
        row = rows.get(eid)
        if row is None:
            # The row is gone and the objects are not. Nothing else can
            # find these: no state left to reach a terminal value, no
            # story to finish, and the claim is invisible to every other
            # sweep by design. The same leak `_workspaces` handles for
            # the workspace claim, one object further on.
            why, keep_claim = "no execution row", False
        elif not row["live"]:
            why, keep_claim = f"execution {row['state']}", False
        elif row["idle"]:
            why, keep_claim = f"nothing has moved for {idle_hours}h", True
        else:
            continue
        if not pods.get(eid) and (keep_claim or eid not in claims):
            continue                    # nothing left here to take
        out.append({"kind": VERIFICATION, "id": eid, "why": why,
                    "keep_claim": keep_claim,
                    "has_pod": eid in pods, "has_claim": eid in claims})
    return out


async def reap_verification(driver, execution_id, *, why: str,
                            keep_claim: bool = False) -> dict:
    """Take the verification pod, and unless told otherwise its claim.

    THE ONE PLACE ALL OF PLAN 5.7'S TRIGGERS ARRIVE, including the
    manifest-hash one that no sweep can see: a caller about to verify
    against a changed manifest calls this and then `ensure`, and gets a
    new pod instead of the stale warm one `ensure` would otherwise adopt.

    AND `keep_claim=True` IS THE REBUILD-IN-PLACE FORM. Plan 5.7 says
    "replace pod and PVC" and that is the default here, but the claim is
    ReadWriteOncePod: a prepare Job cannot attach it while the warm pod
    holds it. A caller that means to rebuild `ENV` on the SURVIVING disk
    -- `verifier/rebuild.py`'s level-triggered Job -- passes True, which
    performs exactly the detach plan 5.5 prices at ~30s and keeps
    everything on the volume the new manifest did not invalidate.

    POD FIRST, THEN CLAIM -- the order `release` argues for above and
    for the same reason: a claim deleted while a pod still writes to it
    is a running container whose disk vanishes underneath it, which the
    API server permits and the kubelet discovers later as I/O errors
    nobody is watching for.

    ALREADY GONE IS SUCCESS on both halves (`k8s_verify` swallows
    not-found), so this is safe to call on a pod that was collected by
    `orphan_sweep` earlier in the same pass.
    """
    from kernel.drivers import k8s_verify                   # noqa: PLC0415
    from kernel.verifier.pod import pod_name                # noqa: PLC0415

    await k8s_verify.destroy(driver, pod_name(execution_id))
    reclaimed = (False if keep_claim
                 else await k8s_verify.release(driver, execution_id))
    disk = "kept" if keep_claim else f"released={reclaimed}"
    logger.info("verification pod for execution %s reaped (%s); claim %s",
                execution_id, why, disk, extra={"event": "reconcile"})
    return {"execution_id": execution_id, "action": "verification_reaped",
            "detail": f"{why}; claim {disk}"}


async def _release_verification(driver, row, dry_run: bool) -> dict:
    if dry_run:
        # NAMES WHAT IS ACTUALLY THERE. A dry run that claims it would
        # take a claim already gone is a count somebody re-enables
        # deletes on the strength of -- the mistake `orphan_sweep`'s
        # own dry-run test exists to prevent.
        took = [w for w, t in (("pod", row["has_pod"]),
                               ("claim", row["has_claim"]
                                and not row["keep_claim"])) if t]
        return {"execution_id": row["id"],
                "action": "would_reap_verification",
                "detail": f"verification {' and '.join(took)} ({row['why']})"}
    return await reap_verification(driver, row["id"], why=row["why"],
                                   keep_claim=row["keep_claim"])


async def release(conn, driver, row, dry_run: bool = True) -> dict:
    """Destroy the pod, then the claim. In that order, and not before
    the work is durable.

    THE ORDER IS THE GUARD. A claim deleted while a pod still writes to
    it is a running execution whose disk vanishes underneath it; the API
    server permits the delete and the kubelet discovers it later, which
    surfaces as I/O errors inside a container nobody is looking at.

    `release_workspace` is separate from `destroy` on purpose.
    `destroy()` must keep meaning "the pod only", because that is the
    preemption path the claim exists to survive -- folding the delete
    into it would reclaim a workspace every time a Spot node went away.

    THE POD IS DESTROYED BY ID FROM THE ROW, not found by label, which
    is why this needs no ownership predicate: `row` came from
    `releasable`, which starts at the claims the substrate holds and
    asks the database which executions are finished with them. Nothing
    reaches this function that an execution did not name.

    TWO PAIRS NOW, ONE LIST. A row `verification_reapable` produced is
    about the OTHER pair and is dispatched first -- it names no
    `container_id` and its claim is not the workspace, so falling
    through would release the wrong disk for the right execution.
    """
    if row.get("kind") == VERIFICATION:
        return await _release_verification(driver, row, dry_run)

    eid, cid = row["id"], row.get("container_id")
    action = {"execution_id": eid, "detail": row.get("why") or "releasable"}
    if dry_run:
        action["action"] = "would_release"
        return action

    if cid:
        await driver.destroy(cid, stop_timeout=0)
    released = await driver.release_workspace(eid)
    await conn.execute(
        "UPDATE executions SET container_id = NULL, updated_at = now() "
        "WHERE id = $1", eid)
    action["action"] = "released"
    action["reclaimed"] = bool(released)
    logger.info("execution %s released (%s); claim reclaimed=%s",
                eid, action["detail"], released)
    return action
