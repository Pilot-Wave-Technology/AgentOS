"""One execution, one leg, under one lock. C5.

WHAT WAS TRUE BEFORE. `worker_tick` called `_advance_one`, and
`_advance_one` did three things in this order:

    port = await ensure_reachable(conn, driver, row)   # probe, maybe DESTROY
    setup_action = await ensure_setup(conn, row)       # pip install, <=120s
    await run_one_leg(conn, ...)                       # <- the lock was HERE

So the mutual exclusion the design leans on everywhere — "one worker per
execution, enforced here because this is the single funnel every caller
goes through" — began AFTER the two most destructive steps had already
run. The conductor runs two replicas and `claimable` returns the same
RUNNING row to both. Only one of them would go on to hold the leg lock;
the other had already probed, already decided, and already acted.

THE COST, MEASURED. `bridge_health` is probed with a 3s timeout. The shim
served one request at a time and `env_setup` runs the tree's own
`pip install` / `npm ci`, blocking for minutes. Three seconds into
a leg-holder's install, the NON-holding replica concluded the sandbox was
dead, and because SANDBOX_MODEL is "pod" on Kubernetes it destroyed the
pod out from under the leg that was running:

    execution 192: bridge dead; destroying the pod so dispatch rebuilds it

NCAL4 runs 191 and 192, both at the first step after plan approval. From
outside: "the workspace disappears every time."

THE FIX IS THE ORDER, not the timeout. A 30s probe would have moved the
race, not closed it — two replicas sharing one sandbox is the bug, and
the lock is the thing that already knows how to say which one owns it.
So: take `leg_lock` FIRST, and do nothing observable before it is held. A
replica that does not hold the lock now returns "busy" having called
neither the driver nor the bridge; it cannot destroy anything because it
never forms the opinion that something is dead.

AND `env_setup` MOVED INSIDE IT TOO, deliberately. Three reasons:

  * It WRITES. A sentinel in the workspace, SETUP_FAILED.md, the scout's
    SETUP_SUGGESTED.md drafts, and `executions.stall_reason`. Two
    replicas running the tree's installer against one workspace at once
    is exactly the double-write the leg lock exists to prevent.
  * Its once-per-sandbox marker is check-then-act (`_already_done` then
    `run_setups` then `_mark_done`). Without mutual exclusion that is a
    race, and the losing side re-runs a 120s install for nothing.
  * It is the 120-second window in which the bridge cannot answer. Inside
    the lock, the only process that could probe during that window is the
    one holding it — and it does not probe again. The interaction that
    destroyed pod 192 stops existing rather than becoming less likely.

The price is that the lock is now held for setup as well as the leg —
seconds to minutes longer in the worst case. That costs the other replica
nothing: its correct behaviour was always "busy, come back next tick",
and a tick is four seconds.

WHAT IS STILL OUTSIDE. `claimable` (a read), and the decision of which
rows to hand here. Nothing destructive, nothing that writes.

THE CONNECTION IS HELD FOR ALL OF IT, AND D2 WAS ASKED WHETHER THAT WEDGES
A POD. It does not. The answer is an argument rather than a change, because
every shape that shortens the hold reopens something worse, so it is written
out here in full instead of being re-derived by whoever asks next.

11.5i named this as the shape that made `verifying()` claim twenty of the
pool's sixteen connections (plan 7c) -- "each adjudication holds a pooled
connection for the WHOLE verification ... and `verifying()` has no LIMIT". The
resemblance is real and the difference is the clause at the end. That fault
was N unbounded holders against a pool of sixteen. This is N holders where N
is `max_concurrent`, a number the caller must supply, and after D2 that number
is derived from the pool THIS PROCESS built rather than from the conductor's
(`worker/concurrency.pool_safe_fanout`). In an execution pool of two it is
ONE. A bounded hold against a pool sized for it is not the unbounded-holder
fault; it is what `LEG_POOL_MAX = 2` was chosen to carry, and `loopbridge`
says so where it picks the number -- "2 as the ceiling only so that a leg
needing a second connection for a moment waits on nothing".

MEASURED, NOT ASSUMED: NOTHING UNDER HERE ASKS FOR A SECOND CONNECTION. Every
function this reaches -- `ensure_reachable`, `ensure_setup`, `run_setups`,
`leg_body`, `_take_leg`, `run_leg` and all fourteen `loop_*` modules -- takes
`conn` as a parameter. There is no `pool.acquire()`, no `make_pool` and no
`asyncpg.connect` anywhere in the leg path; grep the tree and the only
acquisition that reaches this code is the one that produced `conn`. So the
pod's arithmetic is one connection in use out of two, with one spare it never
needs. The day that stops being true is the day this paragraph is wrong, which
is why it names the property rather than the number.

AND THE HOLD IS BOUNDED, WHICH IT WAS NOT WHEN 11.5i WAS WRITTEN. "Holds a
pooled connection across setup" described a call that blocked for the length
of a `pip install`. E14 changed who waits: `run_setups` gives the whole pass
one `SETUP_WAIT_S` (120s) of patience and then returns `setup_running`, and
the branch below hands the slot back. So the worst hold is 120s plus one leg's
own lease clock, not an unbounded install -- and the 120s case ENDS the pass
rather than continuing into the leg.

WHY NOT SIMPLY RELEASE IT ACROSS SETUP. Because `leg_lock` is the connection.
An advisory lock is held by the session that took it and dies with it, so
handing the connection back mid-setup releases the lock -- and the lock
covering setup is the entire C5 fix argued above: the other replica walks in
three seconds into an install, concludes the bridge is dead, and destroys the
pod. Interposing anything between the leg and its raw asyncpg connection is
the same trap wearing a wrapper: a proxy that forgets to forward `fetchval`
breaks `pg_try_advisory_lock` loudly, and one that forwards it to a DIFFERENT
pooled connection breaks it silently, releasing the lock at the wrong moment
with nothing to show for it. The connection is passed through untouched here
for that reason and must stay that way.
"""

from __future__ import annotations

import logging

from kernel.worker.envsetup import ensure_setup
from kernel.worker.lease import leg_lock
from kernel.worker.reachability import ensure_reachable

logger = logging.getLogger(__name__)


async def advance_one(conn, driver, row: dict, run_leg_fn, make_executor,
                      adapter, *, reachable=None) -> list[dict]:
    """Move one execution forward by one leg. Returns its actions.

    The connection is a parameter rather than a closure because in the
    concurrent path it is this leg's own, and everything below — the lock,
    the probe, the setup, the leg, and the sub-agents the executor spawns
    — must use that one. `leg_lock` is connection-scoped: acquire and
    release happen here, on this connection, around the whole of it.

    Factored out of `worker_tick` so the serial and concurrent paths run
    the SAME code — the alternative is two copies of reachability, setup
    and leg, and the worker's own history (`worker_pass` was a second
    `worker_tick` that drifted until three features were unreachable from
    the console) is the argument against that.
    """
    actions: list[dict] = []
    try:
        async with leg_lock(conn, row["id"]) as held:
            if not held:
                # Not an error and not worth waiting for: somebody else
                # has this leg. Come back on the next tick — having
                # touched neither the driver nor the bridge, which is
                # the whole of C5.
                return [{"execution_id": row["id"], "action": "busy",
                         "detail": "another worker is running this "
                                   "execution's leg"}]
            await _under_the_lock(conn, driver, row, run_leg_fn,
                                  make_executor, adapter, actions, reachable=reachable)
    except Exception as e:                                  # noqa: BLE001
        # One execution's failure must not stop the loop; the others are
        # unrelated runs that have done nothing wrong. The lock is
        # already released by the time this runs — `leg_lock`'s unlock is
        # in a `finally`, so an exception raised anywhere inside the
        # block unwinds through it. Whatever actions were recorded before
        # the failure are kept: a setup that ran and then a leg that blew
        # up is two facts, not one.
        logger.exception("worker: execution %s failed", row["id"])
        actions.append({"execution_id": row["id"], "action": "error",
                        "detail": f"{type(e).__name__}: {e}"})
    return actions


async def _under_the_lock(conn, driver, row: dict, run_leg_fn, make_executor,
                          adapter, actions: list[dict],
                          reachable=None) -> None:
    """Probe, set up, run the leg. Every line here needs the lock held.

    `actions` is appended to rather than returned so that work already
    done survives a later failure — see the handler above.
    """
    # Reachability first, now that we own the execution. Handing a model
    # a sandbox whose bridge is dead produces a leg of failed tool calls
    # and a confused ask_human, which is what happened before this check
    # existed.
    # INJECTED, NOT BRANCHED, and execution 214 is why.
    #
    # `ensure_reachable` asks the KUBERNETES API where the pod is
    # and may DESTROY it. Both are conductor work. Run from inside
    # the execution pod it fails twice over: the call is routed
    # through the egress proxy, which refuses it (`Tunnel
    # connection failed: 403`), and `agentos-execution` is bound to
    # NO ROLE AT ALL -- deliberately, because that is the identity a
    # model's bash can reach. A pod asking the cluster where it is,
    # and reserving the right to delete itself, is incoherent.
    #
    # A PARAMETER RATHER THAN `if is_pod(driver)`. A substrate
    # branch here is exactly what D3 exists to delete, and it would
    # put the decision in the callee instead of the caller that
    # knows. The conductor passes nothing and keeps today's
    # behaviour; `pod_loop` passes a local step.
    probe = reachable or ensure_reachable
    port = await probe(conn, driver, row)
    if port is None:
        actions.append({"execution_id": row["id"], "action": "unreachable",
                        "detail": "no live sandbox; left for reconcile"})
        return
    row["bridge_port"] = port
    # WHERE THE BRIDGE AND THE TREE ARE, AND IT HAS TO BE HERE.
    #
    # MEASURED ON EXECUTIONS 215 AND 216. `locate` lived in `_take_leg`,
    # which `leg_body` reaches twenty lines BELOW -- and `ensure_setup`
    # runs in between. So setup read the row as the database handed it
    # over, with `workspace_host` still holding the pod IP that
    # `promote_row` wrote (correctly, for the conductor). Its own line is
    #
    #     host = row.get("workspace_host") or BRIDGE_HOST
    #
    # so a non-empty value means "cross", and the pod tried to reach its
    # own tree over podfs, derive a workspace token, and failed:
    #
    #     env setup for 216 failed to run: AGENT_TOKEN_SIGNING_KEY is unset
    #
    # Not fatal -- the leg plans and runs -- so the only symptom is a
    # story whose dependencies were never installed. Twice over, because
    # the first attempt at this fixed a different thing (the BRIDGE_HOST
    # sentinel, which `ensure_setup` never reads) and the error did not
    # move.
    #
    # `_take_leg` still calls it too, for `run_one_leg`, `runner.drive`
    # and the console, which do not come through here. Calling it twice
    # is harmless: it assigns the same answers, and on the conductor the
    # second pass finds `bridge_host` already set and asks the cluster
    # nothing.
    from kernel.worker.reachability import locate            # noqa: PLC0415

    await locate(driver, row)
    # The leg's own connection goes on the row in `leg_body`, at the one
    # funnel every leg goes through — it was set here, which left
    # `runner.drive` and the console reaching a leg without one.
    #
    # The workspace's committed setups, once per sandbox. Before the
    # first leg so the model wakes into an environment that matches what
    # the Verifier will later rebuild from the same files — a dependency
    # the tree forgot fails HERE, with the author present, not at
    # verification.
    setup_action = await ensure_setup(conn, row)
    if setup_action:
        actions.append(setup_action)
    if setup_action and setup_action["action"] == "setup_running":
        # THE ENVIRONMENT COMES FIRST, and E14 did not change that -- it
        # changed who waits. The install is a job in the sandbox under a
        # budget the pod enforces, so this pass hands the slot back
        # instead of holding the lock for it; the next tick re-attaches
        # to the same job by key and asks again.
        #
        # NOT "run the leg anyway". A model that wakes into a tree whose
        # `pip install` is half done runs its tests against missing
        # imports and concludes the dependencies are broken, which is
        # the failure env_setup runs before the first leg to prevent.
        # A tick is four seconds; an install is minutes, and nothing is
        # lost by asking again.
        return
    # The leg itself, WITHOUT re-taking the lock we are already holding.
    # Imported here rather than at module scope because `worker/loop.py`
    # imports this module: the cycle is real and the deferred import is
    # the seam. It also keeps this file importable without the model
    # SDKs, which is what lets the C5 tests run anywhere.
    from kernel.worker.loop import leg_body

    actions.append(await leg_body(conn, driver, row, run_leg_fn,
                                  make_executor, adapter))
