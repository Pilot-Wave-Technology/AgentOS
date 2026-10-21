"""One sweep: build the executors, hand them to `worker_tick`.

MOVED OUT OF THE CONSOLE, unchanged in behaviour. It lived in
`kernel/console/server.py` next to a debug HTTP handler, which is how
the execution plane came to start only when a port was bound. Nothing
here is new -- the comments below record things learned the hard way and
are kept verbatim rather than re-derived.

`worker_tick` is the ONE worker loop. What this owns is `make_executor`,
because building one needs a database connection and an adapter and
`worker_tick` has no opinion about either. It reads the port and the
workspace FROM THE ROW -- `worker_tick` resolves reachability and puts
them there -- which is what a real worker does and what a closure over a
single `start` call could never do.

This used to be a SECOND implementation of `worker_tick`: its own
`claimable` loop, its own reachability check, its own leg call. It
drifted, in the way a second copy always does, and silently.
`deliver_answers`, `deliver_cancellations` and `adjudicate_pass` were
added to `worker_tick` and never reached here -- so a parked run could
not be answered through the mailbox, a VERIFYING run could not be
cancelled, and NOTHING EVER ADJUDICATED. The repair was not to copy the
three calls across; it was to stop having two loops.
"""

from __future__ import annotations

import os

from kernel.workspace.mounts import writable_scope_for

# Where a workspace lives when the row does not say. B1: the fallback is
# spelled out here rather than hidden inside an environ.get call.
_ROOT_ENV = os.environ.get("AGENTOS_WORKSPACE_ROOT")
WORKSPACE_ROOT = _ROOT_ENV or "/workspace"

# WHICH GOALS THIS WORKER SERVES. None means every goal, which is what a
# standing worker should do -- scoping it to one meant a run started by
# the assistant in any other goal sat at PENDING for ever with the
# worker logging happily underneath, and KER04's first live story did
# exactly that. Narrow it only when a test wants isolation.
#
# The variable keeps its old name so an existing local stack and every
# CI script that sets it keep working.
GOAL_SCOPE = (os.environ["CONSOLE_WORKER_GOAL"]
              if "CONSOLE_WORKER_GOAL" in os.environ else None)


def build_executor(profile, row, plan_approved, *, conn, adapter,
                   actor_prefix, workspace=None, bridge_port=None):
    """ONE construction site for a BridgeToolExecutor.

    There were three: this one, and two closures inside
    `kernel/console/server.py`. They had already drifted -- the console's
    start path built its envelope with NO `identity`, so a run driven
    from the console committed without attribution, and nothing said so.
    That is the same shape as the two worker loops that preceded this
    module, one level down.

    `workspace` and `bridge_port` are arguments rather than always read
    off the row because the console's start path has them from its own
    `start` call and the row does not carry them yet. Defaulting to the
    row keeps every other caller unchanged.
    """
    from kernel.envelope import ExecutionEnvelope
    from kernel.harness.executor import BridgeToolExecutor

    if workspace is None:
        workspace = row.get("workspace_path") or WORKSPACE_ROOT
    if bridge_port is None:
        bridge_port = row.get("bridge_port")
    # Falls back rather than refusing: the console's start path and every
    # Docker caller share a namespace with the sandbox, where localhost
    # is both right and the tightest reach available.
    bridge_host = row.get("bridge_host") or "localhost"

    # E7f — the scope is the WRITABLE mounts, not the root. A story
    # granted three repos and permitted to write one is refused at the
    # tool on the other two.
    mounts = row.get("mounts") or ()
    envelope = ExecutionEnvelope(
        writable_scope=writable_scope_for(workspace, mounts),
        workspace_path=workspace, mounts=mounts,
        # Empty unless the tree is in a pod. See ExecutionEnvelope.
        workspace_host=row.get("workspace_host") or "",
        # Set TOGETHER with the address above: podfs authenticates per
        # execution, so the address alone opens nothing.
        execution_id=row["id"],
        generation=row["generation"],
        allowed_tools=profile.tool_names(),
        identity=row.get("identity"))
    return BridgeToolExecutor(
        envelope=envelope, bridge_port=bridge_port, bridge_host=bridge_host,
        execution_id=row["id"], profile=profile,
        plan_approved=plan_approved,
        # On the row with the other per-leg approvals, so the four
        # `make_executor` closures need no new argument.
        plan_scope=row.get("plan_scope"),
        # Ec3c — which calls a person has approved, put on the row by
        # `worker_tick` for the same reason the bridge port is: this is
        # synchronous and the row is what it is given.
        approved_calls=row.get("approved_calls") or (),
        allowed_hosts=row.get("allowed_hosts"),
        # The PATH the workspace's own committed setups created, so a
        # check may call `pytest` rather than `.venv/bin/pytest` -- the
        # form `setups.REGISTRY` documents and the Verifier requires.
        mounts_env=row.get("mounts_env") or {},
        # THE LEG'S OWN CONNECTION when there is one. `worker_tick` puts
        # it on the row the same way it puts the bridge port there.
        # Closing over the sweep's `conn` instead was invisible while
        # legs ran in turn and becomes "another operation is in
        # progress" the moment two sub-agents touch the database at
        # once. The console's paths have no per-leg connection, so they
        # fall through to the one they opened.
        subagent_context={"conn": row.get("conn") or conn,
                          "adapter": adapter,
                          "actor_id": f"{actor_prefix}-{row['id']}",
                          "depth": 0,
                          "budget_remaining": lambda: 100_000})


def legs_run_in_the_pod() -> bool:
    """Does an execution pod drive its own leg? D2's cutover, asked once.

    ONE VARIABLE, NOT A SWITCH OF OUR OWN. The answer is
    `k8s_sqlproxy.from_env()`, which is `AGENTOS_EXECUTION_SQL_INSTANCE`
    not being `off`. That is not a convenient proxy for the question --
    it IS the question. A pod runs its own leg only if it can hold
    `leg_lock`, and it can hold the lock only through the Cloud SQL
    proxy this variable configures; §10 rejected every design where the
    pod runs a leg whose lock lives somewhere else, because a conductor
    deploy would then release a lock protecting a running leg.

    So a second variable saying "legs run in pods now" would be a second
    declaration of one fact, and the failure when the two disagree is
    the worst one available: either NOBODY claims a leg and the fleet is
    silently idle, or BOTH do and two models run tool calls against one
    workspace, which is what `lease.py` exists to prevent.

    NO DEFAULT ARGUMENT, read per call rather than at import: the same
    two rules `from_env` itself follows, and a test needs to set it.

    THIS IS ALSO WHY `off` IS PERMANENT. E0, a laptop and every test in
    this suite have no Cloud SQL instance and no pod, and the leg is
    correct in the conductor there. See `k8s_sqlproxy.OFF`, whose
    comment used to say D2 would remove it.
    """
    from kernel.drivers import k8s_sqlproxy                  # noqa: PLC0415

    return k8s_sqlproxy.from_env() is not None


def conductor_leg_slots() -> int:
    """How many legs THIS conductor may take. Zero once the pods do it.

    D2 · "the conductor dispatches and observes" (plan §6). Most of
    `worker_tick` stays exactly as it is: answers, cancellations, stale
    holds, questions, the kill switch, the schedule sweep, the drains,
    admission, dispatch and `report_stuck`. What stops is the last
    stanza -- claiming a row and running its leg -- and it stops by
    being offered no slots, which is a number the tick already honours
    in all three of its modes (supervisor, gather, serial).

    ZERO RATHER THAN A BRANCH IN `worker_tick`, deliberately. That
    function is not in this wave's remit, and more importantly a branch
    there would be a SECOND place that decides where legs run. The
    number is the decision, and it is made here, once, beside the
    executor that would otherwise have been built for a leg nobody
    takes.

    IT TAKES MORE THAN THE LEG WITH IT, AND SAYING "everything else
    stays" WOULD BE THE EXPENSIVE KIND OF WRONG. `worker_tick` reaches
    the leg through `advance.advance_one`, and that function is the
    funnel C5 built: it takes `leg_lock`, then `ensure_reachable`, then
    `ensure_setup`, and only then the leg. Offering zero slots does not
    remove a leg from a sequence -- it removes the sequence. So a
    deployment where this returns 0 is also a deployment where nothing
    probes a dead bridge and nothing runs the workspace's committed
    setups, and the symptom of the second is the one `envsetup`'s own
    comment warns about: a pod that comes up on the right image, plans,
    is approved, and then runs every command against a tree with
    nothing installed -- silently, because "no setups found" is a
    legitimate answer.

    THE DEPLOY COUPLING, stated because it is the way this goes wrong.
    Flipping `AGENTOS_EXECUTION_SQL_INSTANCE` off `off` stands the
    conductor down, so the SAME deploy owes four things: start
    `kernel.harness.leg_runner` in the pod's worker container
    (`k8s_workspace.worker_command()` runs podfs alone today), put the
    model key where the leg can reach it (`sandbox_spec.
    POD_ENV_ALLOWLIST` is a closed list and carries none), and move
    reachability and setup to the side of the claim the leg is now on.
    None of the four is done, which is why that variable still ships
    `off` everywhere, and D2 lands the seam rather than the cutover.
    Doing one without the rest is a fleet that admits and dispatches and
    never runs a leg.
    """
    from kernel.worker.concurrency import leg_concurrency     # noqa: PLC0415

    return 0 if legs_run_in_the_pod() else leg_concurrency()


async def worker_pass(pool, driver, adapter, supervisor=None
                      ) -> list[dict]:
    """One sweep. Delegates to `worker_tick`."""
    from kernel.harness.loop import run_leg
    from kernel.worker.loop import worker_tick

    async with pool.acquire() as conn:
        def make_executor(profile, row, plan_approved=None):
            return build_executor(profile, row, plan_approved, conn=conn,
                                  adapter=adapter, actor_prefix="worker")

        # `acquire` gives each leg its own connection (leg_lock is
        # connection-scoped); `supervisor` is what stops this pass WAITING
        # for them. Without the second, a run that becomes claimable while
        # a batch is in flight waits for the whole batch — two seconds of
        # skew between two approvals serialised KER17 completely.
        #
        # BOTH ARE STILL PASSED WHEN THE SLOTS ARE ZERO, and that is not
        # waste. `supervisor` carries the legs a PREVIOUS pass started,
        # and the drain at shutdown waits on it; dropping it on the pass
        # that first reads zero would abandon whatever is in flight at
        # the moment the cutover lands.
        return await worker_tick(
            conn, driver, run_leg, make_executor, adapter,
            goal_id=GOAL_SCOPE, max_concurrent=conductor_leg_slots(),
            workspace_root=WORKSPACE_ROOT, acquire=pool.acquire,
            supervisor=supervisor)
