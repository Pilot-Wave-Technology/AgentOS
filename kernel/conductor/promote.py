"""DISPATCHING → RUNNING, once the sandbox answers. Dispatch's other half.

`dispatch_admitted` used to do six things in a row, one of which —
`await_ready` — was ninety-five percent of the wall clock. It now stops
after creating the sandbox and recording its id, and this pass finishes
the job: read the generation, claim the lease, take the state to
RUNNING. The edge is the one machine v5 already names,
`{"from": "DISPATCHING", "to": "RUNNING", "predicate": "lease_acquired"}`
— no new state, and the predicate is exactly what happens here.

THE WRITES LIVE IN `promote_row.py`. This file decides — probe, verdict,
budget, one action record per row — and that one executes. The split is
what keeps this under the 200-line cap while the pass learned to ask a
second question; it is a real seam, not a arbitrary cut, and the
transaction discipline is documented there.

WHAT HAPPENS TO A SANDBOX THAT NEVER ANSWERS. The old code had a
natural deadline — `await_ready` raised, and dispatch destroyed the
container and left the row DISPATCHING for reconcile. Splitting the
wait out removes that deadline, and nothing else supplies one:
`reconcile.py` recovers a DISPATCHING row whose compute is GONE, and
calls a DISPATCHING row that still has a live container "healthy", for
ever. So the deadline lives here, and it does exactly what dispatch's
cleanup did — destroy the container, drop the reference, leave the row
DISPATCHING for reconcile to hand back to ADMITTED with the generation
advanced. A pod created but never promoted is still reclaimable.

AND THE DEADLINE ASKS WHY BEFORE IT BLAMES THE CLOCK. One budget for
"pulling a cold image" and "the namespace refused you" told an operator
"the pod never became ready" five minutes after a refusal that was
instant and permanent. `unscheduled.py` reads the pod's own
`PodScheduled` condition and says which; the sentence goes on the row
where a person looks, and the budget is chosen from the answer rather
than from one number that cannot tell the two apart.
"""

from __future__ import annotations

import asyncio
import logging

from kernel.conductor.promote_row import claim, lost, note, reclaim, unnote
from kernel.conductor.readiness import (GONE, PROBE_TIMEOUT, READY, WAITING,
                                        probe)
from kernel.conductor.unscheduled import (SCHEDULING_GRACE_S, budget_for,
                                          ceiling_for, sentence,
                                          why_pending)

logger = logging.getLogger(__name__)

# How long a PLACED sandbox may take to answer before it is thrown away
# and built again. MORE PATIENT THAN THE OLD INLINE WAIT, deliberately:
# `await_ready` gave up after 30 seconds on Docker and 120 on
# Kubernetes, where an init container clones repositories, and this
# replaces both. A slow clone should cost a slow start, not a rebuild
# loop; a wedged pod should not hold an admission slot all afternoon.
#
# STILL 300, AND STILL RIGHT — for the case it was measured against,
# which is a pod that has been PLACED and is pulling an image or cloning
# a repository. It was wrong only as a universal answer: a pod the
# NAMESPACE HAS REFUSED will never become ready, so five minutes of it
# is pure delay, and that one case now has its own number
# (`unscheduled.REFUSED_BUDGET_S`) chosen from the pod's own condition.
#
# A pod merely waiting for a NODE keeps all 300 seconds, deliberately —
# `unscheduled.budget_for` says why, and the short version is that on
# Autopilot the Pending pod IS the request for the node, so a shorter
# budget cancels the thing it is waiting for.
STARTUP_BUDGET_S = 300.0

# Rows with a sandbox created and not yet promoted, oldest wait first.
# `updated_at` is the clock: dispatch stamps it when it records the
# container id, and nothing writes to a DISPATCHING row afterwards
# except the transitions out of it — see `promote_row.note`, which is
# the one write that deliberately does NOT stamp it.
_WAITING_SQL = """
    SELECT id, container_id,
           EXTRACT(EPOCH FROM (now() - updated_at)) AS waited
      FROM executions
     WHERE ($1::text IS NULL OR goal_id = $1)
       AND state = 'DISPATCHING'
       AND container_id IS NOT NULL
     ORDER BY updated_at, id
     LIMIT $2::bigint
"""


class SandboxNotReady(RuntimeError):
    """A sandbox was created and never became usable.

    Raised only by the caller that waits — `start`, behind a person
    pressing a button, which must fail loudly rather than return a run
    that will not move. The passes record an action instead.
    """


async def promote_ready(conn, driver, goal_id: str | None = None, *,
                        limit: int | None = None,
                        timeout: float | None = PROBE_TIMEOUT,
                        budget: float = STARTUP_BUDGET_S) -> list[dict]:
    """Promote every dispatched sandbox that is answering. Cheap by design.

    The probes run TOGETHER and touch no database: one connection, one
    round of substrate calls, then the promotions in turn. The pass
    costs one probe timeout, not one per row — which is the whole
    difference between this and the loop it came out of.
    """
    rows = await conn.fetch(_WAITING_SQL, goal_id, limit)
    if not rows:
        return []
    verdicts = await asyncio.gather(
        *(probe(driver, r["container_id"], timeout=timeout) for r in rows),
        return_exceptions=True)
    reasons = await _ask_why(driver, rows, verdicts)
    # THE CEILING FIRST, THEN THE BUDGET. `ceiling_for` lengthens for
    # an install (P3's init container, up to half an hour) and
    # `budget_for` shortens for a refusal -- in that order, because a
    # pod that is refused has no node and is running nothing, so the
    # shortening must win. `budget_for` itself still only ever
    # shortens; see its docstring for why that rule is not bent here.
    return [await settle(conn, driver, r, v,
                         budget_for(_verdict(reasons.get(r["id"])),
                                    ceiling_for(
                                        _verdict(reasons.get(r["id"])),
                                        budget)),
                         reasons.get(r["id"]))
            for r, v in zip(rows, verdicts)]


def _verdict(reason):
    """The verdict half of a `why_pending` answer, or None."""
    return reason[0] if reason else None


async def _ask_why(driver, rows, verdicts) -> dict:
    """Second round, ALSO together: why has each unready pod not started.

    A SECOND `gather` AND NOT A CALL INSIDE `settle`. `settle` runs in a
    serial comprehension, so a substrate read there would cost one round
    trip per row and quietly undo the property that makes this pass
    cheap — `test_the_pass_asks_every_sandbox_at_once_not_one_after_
    another` guards exactly that for the probes, and a read one function
    deeper would have walked around it.

    ONLY THE ROWS PAST `SCHEDULING_GRACE_S`. Every pod is briefly
    unscheduled — the scheduler has not looked at a pod one second old —
    so asking about a fresh one buys an API call per row per tick and a
    false alarm on the board.

    AND ONLY `WAITING`, NOT "ANYTHING THAT IS NOT READY". The third
    verdict is GONE, and `settle` reclaims that row before it ever looks
    at `why` — so asking about it spends a round trip whose answer is
    thrown away, and the answer is a 404 that `quiet` reports at WARNING
    on a completely ordinary path. A log line that fires on the healthy
    case is how a log stops being read.
    """
    suspects = [row for row, verdict in zip(rows, verdicts)
                if not isinstance(verdict, BaseException)
                and verdict[0] == WAITING
                and float(row["waited"] or 0) >= SCHEDULING_GRACE_S]
    if not suspects:
        return {}
    answers = await asyncio.gather(
        *(why_pending(driver, r["container_id"]) for r in suspects),
        return_exceptions=True)
    return {r["id"]: a for r, a in zip(suspects, answers)
            if a is not None and not isinstance(a, BaseException)}


async def promote_one(conn, driver, execution_id: int, *,
                      timeout: float | None = None,
                      budget: float = STARTUP_BUDGET_S) -> dict:
    """The same, for one execution the caller is waiting on.

    `timeout=None` and `budget=0` together reproduce what dispatch used
    to do inline: wait the substrate's own startup budget, and if the
    sandbox is not ready by then destroy it and leave the row for
    reconcile. `budget_for` can only SHORTEN a budget, so that caller's
    zero survives a refusal verdict — but the verdict still reaches the
    person, because it becomes the detail of the `SandboxNotReady` they
    are shown.
    """
    row = await conn.fetchrow(
        "SELECT id, container_id, "
        "EXTRACT(EPOCH FROM (now() - updated_at)) AS waited "
        "FROM executions WHERE id = $1 AND state = 'DISPATCHING' "
        "AND container_id IS NOT NULL", execution_id)
    if row is None:
        return {"execution_id": execution_id, "action": "skip",
                "detail": "not DISPATCHING with a sandbox"}
    verdict = await probe(driver, row["container_id"], timeout=timeout)
    # `WAITING`, not "not READY" — same reason as `_ask_why`: a GONE pod
    # is reclaimed before `why` is read, and asking about it buys a 404
    # reported at WARNING on an ordinary path.
    why = (await why_pending(driver, row["container_id"])
           if verdict[0] == WAITING else None)
    return await settle(conn, driver, row, verdict, budget, why)


async def settle(conn, driver, row, verdict, budget: float,
                 why: tuple[str, str] | None = None) -> dict:
    """One row, one verdict, one action record.

    `why` is what the substrate said about a pod that has not started —
    `(REFUSED | UNSCHEDULED, the scheduler's own message)`, or None when
    the pod is placed, the driver is Docker, or the control plane could
    not be asked. It changes two things and deliberately NOT a third:
    the sentence a person reads, and the budget. It does not touch the
    state machine — a refused pod takes the same path out as any other
    sandbox that never answered, because `reconcile` is what knows how
    to hand a DISPATCHING row back with the generation advanced.
    """
    eid, cid = row["id"], row["container_id"]
    if isinstance(verdict, BaseException):
        # `gather` returns rather than raises, so one sandbox nobody
        # predicted cannot cancel the promotion of the others.
        logger.warning("readiness of %s: %s", eid, verdict)
        return {"execution_id": eid, "action": "waiting",
                "detail": f"probe raised {type(verdict).__name__}"}
    state, detail = verdict

    if state == READY:
        generation = await claim(conn, eid, cid, driver)
        if generation is not None:
            return {"execution_id": eid, "action": "promoted",
                    "detail": f"RUNNING on {cid[:12]}, generation "
                              f"{generation} ({detail})"}
        return await lost(conn, driver, eid, cid)

    # Measured BEFORE the probe, which is what makes `budget=0` mean
    # "this was your one chance" rather than depending on a clock
    # reading nothing guarantees is non-zero.
    waited = float(row["waited"] or 0)
    if state == GONE:
        return await reclaim(conn, driver, eid, cid, detail)

    # WHY, BEFORE HOW LONG. The probe's own detail for an unready pod is
    # "not ready yet: <timeout>", which is the clock describing itself.
    # When the substrate has said something better, that wins — and it
    # is what the budget is chosen from.
    if why is not None:
        detail = sentence(*why)
        budget = budget_for(why[0], budget)

    if waited >= budget:
        return await reclaim(conn, driver, eid, cid, (
            f"{detail} — gave up after {waited:.0f}s of {budget:.0f}s"
            if why else
            f"no answer in {waited:.0f}s (budget {budget:.0f}s): {detail}"))

    # SAID ON THE ROW, NOT ONLY IN THE RETURN VALUE (§7b: nothing waits
    # without a `stall_reason`). The action record below reaches a log
    # line and an in-memory list; `stall_reason` is the column the board
    # reads. Only when the substrate actually said something — "not
    # ready yet" is not news and would churn the column every tick.
    #
    # The action stays "waiting" on purpose: the row IS still waiting,
    # and callers that switch on the action string must not have to
    # learn two new ones to keep working. The news is in the detail.
    if why is not None:
        await note(conn, eid, cid, detail)
    elif waited >= SCHEDULING_GRACE_S:
        # AND A REASON THAT STOPPED BEING TRUE IS TAKEN OFF THE ROW.
        # §7b's invariant has a second half nobody writes down: a run
        # that is no longer refused must stop saying it is. The pod that
        # spent ninety seconds Unschedulable and has now been PLACED is
        # pulling an image for the next two minutes, and for those two
        # minutes the board would still read "no node has room for this
        # pod yet" — a false sentence, in the one column an operator
        # trusts. `claim` clears it at RUNNING; that is too late.
        #
        # THE TRANSIENT CASE IS ACCEPTED ON PURPOSE. `why_pending` also
        # answers None when the control plane could not be reached, so a
        # blip erases a true note — for one tick, four seconds, after
        # which it is asked again and rewritten. Four seconds of a blank
        # column against five minutes of a wrong one is not a close call.
        await unnote(conn, eid, cid)
    return {"execution_id": eid, "action": "waiting",
            "detail": f"{cid[:12]} after {waited:.0f}s: {detail}"}
