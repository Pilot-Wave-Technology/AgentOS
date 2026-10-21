"""Is this execution's sandbox actually reachable, and rebuild it if not.

LIFTED OUT OF `worker/loop.py` UNCHANGED. It moved for two reasons and
neither is tidiness.

The first is that this function DESTROYS PRODUCTION PODS. It is the most
dangerous thing the worker does, and it was buried two thirds of the way
down a 380-line module between the ledger drains and the leg. A reader
looking for "what could delete a live workspace" had no file to open.

The second is the C5 fix. This must only ever run while its caller holds
the execution's `leg_lock` — see `worker/advance.py`, which is now the
only caller and takes the lock before it calls this. Stating that rule
needs somewhere to state it, and a docstring at the top of the file that
owns the destroy is where somebody will read it.

WHY THE RULE EXISTS. The conductor runs two replicas and both see the
same RUNNING row. `bridge_health` is probed with a 3s timeout; the shim
served one request at a time and `env_setup` runs the tree's own
`pip install` / `npm ci`, blocking for minutes. Three seconds into
an install the replica that was NOT running the leg concluded the sandbox
was dead and, on Kubernetes, destroyed the pod:

    execution 192: bridge dead; destroying the pod so dispatch rebuilds it

Measured on NCAL4 runs 191 and 192, both at the first step after plan
approval. From outside it read as "the workspace disappears every time".
A replica that does not hold the leg lock now cannot reach this code at
all, so it cannot form an opinion about liveness and cannot act on one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# The bridge container shares the execution pod's network namespace, so
# from inside that pod its address is loopback and there is nobody to
# ask.
#
# `BRIDGE_HOST` AND NOT THE LITERAL "127.0.0.1", and execution 215
# measured the difference. Both are the right ADDRESS; only one is the
# right SENTINEL. Several call sites decide whether they must cross by
# comparing against this exact string -- `envsetup.py` computes
# `remote = bool(host) and host != BRIDGE_HOST`, and `envrecord.py`
# writes the idiom out as `not host or host == BRIDGE_HOST`. Handing
# them "127.0.0.1" is an address that means "localhost" to the network
# and "somewhere else" to the comparison, so env setup decided it had
# to reach the tree over podfs, tried to derive a workspace token, and
# failed with:
#
#     env setup for 215 failed to run: AGENT_TOKEN_SIGNING_KEY is unset
#
# -- which is a story whose dependencies are silently never installed.
# The same class as the three faces of `is_pod` this module's `locate`
# exists to fix, and the reason a grep for `is_pod` did not find it:
# here the question is spelled as a string comparison.
from kernel.bridge.client import BRIDGE_HOST as LOOPBACK


async def locate(driver, row: dict) -> None:
    """Put this execution's bridge address and tree location ON THE ROW.

    "FROM HERE" IS HALF THE QUESTION, and the version of this that lived
    inline in `_take_leg` only ever answered the other half. It asked
    `is_pod(driver)` -- is the tree in a pod -- and acted as though the
    answer were "so cross to it". For the conductor and the console those
    coincide. For a leg running INSIDE the pod they are opposites, and
    execution 214 died proving it.

    TWO FACTS, AND THEY ARE NOT THE SAME FACT:

      bridge_host     where the shim answers. Loopback in the pod (same
                      network namespace); the pod's IP from outside, and
                      only the cluster knows that.
      workspace_host  EMPTY MEANS LOCAL -- `ExecutionEnvelope` says so in
                      as many words. Empty in the pod, because
                      `k8s_spec` mounts the workspace PVC into the worker
                      container itself. The conductor sets it to the
                      pod's IP because reading locally there would open
                      an empty directory in the conductor's own container
                      and report it as an empty RESULT.

    AND THE ROW ARRIVES CARRYING A WRONG ANSWER, which is why this
    assigns rather than fills a gap. `promote_row` writes
    `executions.workspace_host` at promotion -- correctly, for itself --
    so the pod re-reads the pod's own IP from the database every tick. An
    empty string cannot be told from an unset one, so "don't overwrite
    what is there" would not have worked; this decides.
    """
    from kernel.workspace.podfs_client import (in_execution_pod,  # noqa: PLC0415
                                               is_pod)

    if in_execution_pod():
        row["bridge_host"] = LOOPBACK
        row["workspace_host"] = ""
        return
    if row.get("container_id") and not row.get("bridge_host"):
        row["bridge_host"] = await driver.resolve_bridge_host(
            row["container_id"])
    if row.get("bridge_host") and is_pod(driver):
        row["workspace_host"] = row["bridge_host"]


async def ensure_reachable(conn, driver, row: dict) -> int | None:
    """The sandbox's port, rebuilding it first if its bridge is dead.

    CALL THIS ONLY UNDER THIS EXECUTION'S `leg_lock`. Everything below
    the probe is destructive and the probe itself is a liveness opinion
    formed in three seconds; a worker that is not the one running the leg
    has no business forming it. `worker/advance.py` holds the lock.

    A LABEL IS NOT A LIVE BRIDGE. The sandbox joins the WORKER's network
    namespace, so a worker restart leaves every sandbox Up, still labelled,
    and unreachable — and `resolve_bridge_port` happily returns a port that
    points at nothing. Observed live: a model called `glob` twice, got
    "bridge unreachable" both times, and asked a human what was wrong. It
    was right to.

    This is the involuntary-loss path E7d anticipates. The workspace
    outlives the sandbox, so `acquire` adopts it and the work is not lost.
    Returns None when the sandbox cannot be reached OR rebuilt — the caller
    leaves it for reconcile rather than handing a model dead tools.
    """
    from kernel.bridge.client import bridge_health
    from kernel.conductor.ports import allocate_bridge_port
    from kernel.conductor.sandbox_spec import build_sandbox_spec

    try:
        port = await driver.resolve_bridge_port(row["container_id"])
    except Exception as e:                                  # noqa: BLE001
        logger.warning("execution %s: cannot resolve its port: %s",
                       row["id"], e)
        return None
    # WITH THE HOST, or this probes the CONDUCTOR'S OWN loopback and
    # reports every pod sandbox dead. `bridge_health`'s docstring says
    # so in as many words; this call site was not updated when the
    # parameter was added, and execution 9 spent every tick declaring a
    # perfectly healthy pod unreachable, abandoning the leg, and
    # re-dispatching -- leg 8, zero steps.
    try:
        host = await driver.resolve_bridge_host(row["container_id"])
    except Exception as e:                                  # noqa: BLE001
        logger.warning("execution %s: cannot resolve its host: %s",
                       row["id"], e)
        return None
    if port is not None and await bridge_health(port=port, host=host,
                                                timeout=3):
        return port

    if not row.get("workspace_path"):
        # Without a surviving workspace a rebuild would start from nothing,
        # which is a new run wearing an old id.
        logger.warning("execution %s: unreachable and no workspace to rebuild "
                       "against", row["id"])
        return None

    # THE SUBSTRATE DECIDES WHETHER "REBUILD" IS EVEN A THING. On
    # Kubernetes the sandbox is a container inside the execution's own
    # pod, created with it; there is nothing to attach to a running pod
    # and `sandbox_attachments` refuses by name. Asking it anyway is
    # what produced, once per tick:
    #
    #   rebuild failed: the KubernetesDriver cannot attach a sandbox
    #
    # -- a correct refusal reached at the one point where the caller can
    # do nothing but log it. Destroying the pod is the recovery: the
    # dispatch pass builds another, and the claim survives a destroy on
    # purpose, so the workspace and its commits come back with it.
    if getattr(driver, "SANDBOX_MODEL", "attached") == "pod":
        logger.warning("execution %s: bridge dead; destroying the pod so "
                       "dispatch rebuilds it (the claim, and the tree on "
                       "it, survive)", row["id"])
        try:
            await driver.destroy(row["container_id"], stop_timeout=0)
        except Exception:                                   # noqa: BLE001
            pass                 # already gone is the outcome we wanted
        await conn.execute(
            "UPDATE executions SET container_id = NULL WHERE id = $1",
            row["id"])
        row["container_id"] = None
        return None

    logger.warning("execution %s: bridge dead, rebuilding the sandbox",
                   row["id"])
    try:
        await driver.destroy(row["container_id"], stop_timeout=0)
    except Exception:                                       # noqa: BLE001
        pass                     # already gone is the outcome we wanted
    try:
        port = allocate_bridge_port()
        # Same image on a rebuild as on the first dispatch, or the
        # run is judged against an environment it never worked in.
        image = await conn.fetchval(
            "SELECT c.base_image FROM contracts c JOIN executions e "
            "ON e.contract_id = c.id WHERE e.id = $1", row["id"])
        # The same narrow bind as the first dispatch. A rebuild that
        # mounted the whole volume would hand a resumed run the reach
        # its first leg did not have.
        from kernel.worker.runner import workspace_subpath
        ws = await conn.fetchval(
            "SELECT workspace_path FROM executions WHERE id = $1", row["id"])
        spec = build_sandbox_spec(driver, row["id"], bridge_port=port,
                                  image=image or "",
                                  workspace_subpath=workspace_subpath(ws))
        container_id = await driver.create(spec)
        await driver.await_ready(container_id, timeout=30, bridge_port=port)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("execution %s: rebuild failed: %s", row["id"], e)
        return None

    await conn.execute(
        "UPDATE executions SET container_id = $2 WHERE id = $1",
        row["id"], container_id)
    row["container_id"] = container_id
    return port
