"""Ec4d — leases outlive their runs unless something goes looking.

WHY A SWEEP AND NOT A CALL AT EACH EXIT. An execution reaches a terminal
state in five different places -- `accept`, two in `worker/loop`, one in
`runner`, one in `park` -- and releasing in each is the shape this
codebase keeps paying for: one fact written five times, four of which
stay right. The fifth is the one that leaks.

It also covers what no exit path can. A worker killed mid-run never
reaches any of its exits, and that is exactly the case where a
credential is left on disk with nobody tracking it. The legacy broker
grew a watchdog for this same reason: "control panel restarted
mid-task".

THE TTL IS STILL THE REAL BOUND. This deletes the FILE, which is what
stops the run reading it again; neither provider can recall a token
already handed out. A sweep that ran once an hour would be useless on
its own and is not what makes this safe -- it is the second line behind
a short lifetime.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# States after which no further step will run. A lease surviving one of
# these belongs to nobody.
TERMINAL = ("DONE", "FAILED", "CANCELLED")


async def sweep(conn, limit: int = 200, since_seconds: int = 0) -> dict:
    """Delete leases belonging to executions that have finished.

    Returns `{"checked", "released"}`. Never raises for one bad row: a
    sweep that stops at the first unreadable workspace leaves every later
    lease in place, which is the opposite of what it is for.

    `since_seconds` narrows to runs that finished RECENTLY, which is what
    makes this affordable to call on every tick.

    F4 -- THE CADENCE WAS THE PROBLEM, NOT THE MECHANISM. The full sweep
    ran every 75 ticks, and a tick is a worker pass PLUS a 4s sleep, so
    its real period was 5-7 minutes against a 15-minute TTL: a finished
    run's credential stayed on disk for most of its life. The fix is not
    a release call at each of the five terminal exits -- that is the "one
    fact written five times" this module's docstring exists to refuse --
    but to run the NARROW form every tick and keep the full form as the
    backstop it was always meant to be.
    """
    from kernel.credentials.issue import lease_id_for
    from kernel.workspace.credentials import lease_path

    window = int(since_seconds or 0)
    rows = await conn.fetch(
        f"""
        SELECT id, workspace_path, container_id FROM executions
         WHERE state = ANY($1::text[])
           AND workspace_path IS NOT NULL AND workspace_path <> ''
           -- `container_id` IS SELECTED BUT NOT FILTERED ON, and the
           -- difference matters. A first version put
           -- `AND container_id IS NOT NULL` here and broke the LOCAL
           -- release path entirely: on Docker and E0 there is no pod, no
           -- container_id, and the lease is a file on this filesystem
           -- that must still be unlinked. The Kubernetes wedge is a
           -- property of DIALLING, not of selecting -- so the guard
           -- belongs at the call, not in the query. Caught by
           -- `test_the_sweep_releases_finished_runs_and_spares_live_ones`,
           -- which is exactly the coverage that should have caught it.
           {"AND updated_at > now() - ($3 || ' seconds')::interval"
            if window else ""}
         ORDER BY updated_at DESC
         LIMIT $2
        """, *( (list(TERMINAL), int(limit), str(window)) if window
                else (list(TERMINAL), int(limit)) ))

    # THE LEASES ARE NOT ON THIS FILESYSTEM, on a pod substrate. This
    # sweep is the release path -- "the narrow sweep IS the release path
    # now" -- and it was reading the CONDUCTOR's disk, where no lease has
    # ever been written. So it found nothing, released nothing, and
    # reported zero every tick: a credential's real bound became its TTL
    # alone, with the file living on the claim until reclaim took it.
    from kernel.drivers.registry import resolve
    from kernel.workspace.podfs_client import is_pod

    on_pod = is_pod(resolve())

    checked = released = 0
    for row in rows:
        checked += 1
        try:
            path = lease_path(row["workspace_path"], lease_id_for(row["id"]))
        except Exception:                                   # noqa: BLE001
            continue
        if on_pod:
            if await _release_in_pod(conn, row):
                released += 1
                logger.info("credential sweep: released execution %s's "
                            "lease in its pod", row["id"])
            continue
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
            released += 1
            if window:
                # The narrow sweep IS the release path now, so this is
                # the ordinary case and says so. Warning here would mean
                # a warning on every completed cloud run, which is how a
                # log stops being read.
                logger.info(
                    "credential sweep: released execution %s's lease at "
                    "%s", row["id"], path)
            else:
                # The BACKSTOP found one. Reaching here means the narrow
                # sweep never saw this run finish -- a worker killed
                # mid-run, or a terminal transition older than the
                # window. Worth a warning, and the interesting number is
                # how often it happens.
                logger.warning(
                    "credential sweep: execution %s finished outside the "
                    "prompt window with its lease still on disk; deleted "
                    "%s", row["id"], path)
        except OSError as exc:
            logger.warning("credential sweep: could not delete %s: %s",
                           path, exc)

    if released:
        logger.info("credential sweep: %d/%d lease(s) released (%s)",
                    released, checked,
                    f"last {window}s" if window else "full")
    return {"checked": checked, "released": released}


async def _release_in_pod(conn, row) -> bool:
    """Revoke a lease inside the execution pod. False when there is none.

    THE POD HAS TO STILL EXIST, and for a TERMINAL execution it usually
    does not -- `destroy()` nulls `container_id` and reclaim has normally
    been there first. Dialling one anyway is the wedge that cost a day:
    every four seconds this opened a TCP connection to an unrouted pod IP
    and held a pool thread for the full timeout, until the pool emptied
    and every Kubernetes and podfs call in the process queued behind it.
    Found by the stack dump the liveness probe now takes on its way out:
    a worker thread in `socket.create_connection`, while the main thread
    sat idle in `selectors.select`.

    A lease inside a container that no longer exists died with it. The
    TTL remains the bound for anything skipped here.
    """
    if not row.get("container_id"):
        return False
    return await _release_in_pod_now(conn, row)


async def _release_in_pod_now(conn, row) -> bool:
    """Delete one lease inside the execution's pod.

    A pod that is already gone is the ORDINARY case at the end of a run,
    not a failure: its filesystem went with it and the claim is the
    reclaim sweep's to take. Reported as "not released" and nothing
    louder, because a warning on every finished cloud run is how a log
    stops being read.
    """
    import asyncio
    import functools

    from kernel.credentials.issue import lease_id_for
    from kernel.workspace.browse import workspace_host
    from kernel.workspace.podfs_client import (WorkspaceUnreachable,
                                               revoke_credentials)

    try:
        host = await workspace_host(conn, row["id"])
        if not host:
            return False
        return await asyncio.to_thread(
            functools.partial(revoke_credentials, host,
                              row["workspace_path"], lease_id_for(row["id"]),
                              execution_id=row["id"]))
    except WorkspaceUnreachable:
        # The pod is gone. Ordinary, and not worth a line every sweep:
        # the lease died with the container that held it.
        return False
    except Exception as exc:                                # noqa: BLE001
        # ANYTHING ELSE IS NOT THAT. `except (WorkspaceUnreachable,
        # Exception)` caught both under one silent `return False`, so a
        # genuinely broken revoke path -- a bad payload, a missing op, a
        # permission change -- was indistinguishable from a pod that had
        # simply exited, and logged nothing at all. A credential that was
        # not revoked is the one failure in this module that matters.
        logger.warning("could not revoke the lease for execution %s: "
                       "%s: %s", row["id"], type(exc).__name__, exc)
        return False
