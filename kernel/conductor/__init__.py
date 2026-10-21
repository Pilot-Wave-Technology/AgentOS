"""kernel.conductor — control-plane actor for the execution pipeline.

Drives executions through PENDING → ADMITTED → DISPATCHING → RUNNING.
No LLM, no executor behavior — pure state-machine transitions driven
by database reads and substrate dispatch.

The single entry point is conductor_tick, which chains:
  Phase 1 (admit)   — inside a transaction
  Phase 2 (dispatch) — outside a transaction
"""

from __future__ import annotations

import logging

from kernel.conductor.admit import admit_pending
from kernel.conductor.dispatch import NotAdmittedError, dispatch_admitted
from kernel.conductor.env_guard import CredentialLeakError, assert_no_credentials
from kernel.conductor.loop import run_loop
from kernel.conductor.promote import promote_ready
from kernel.conductor.reconcile import reconcile_tick
from kernel.conductor.contract_config import ContractRefused
from kernel.conductor.spec_builder import build_spec

logger = logging.getLogger(__name__)

__all__ = [
    "conductor_tick",
    "reconcile_tick",
    "run_loop",
    "admit_pending",
    "dispatch_admitted",
    "promote_ready",
    "build_spec",
    "assert_no_credentials",
    "CredentialLeakError",
    "NotAdmittedError",
]


async def conductor_tick(
    conn,
    driver,
    env_allowlist: list[str],
    goal_id: str,
    *,
    ready_timeout: float | None = None,
) -> list[str]:
    """One tick: admit pending, dispatch admitted, promote whatever answers.

    Transaction boundaries are explicit and load-bearing:

    Phase 1 — admit: runs INSIDE a transaction so FOR UPDATE SKIP LOCKED
    actually holds row locks. Without this, asyncpg autocommits and two
    replicas double-claim.

    Phase 2 — dispatch: runs OUTSIDE a transaction. Holding a tx across
    driver.create() pins a DB connection for the whole call, and it used
    to be held across the readiness wait as well — the reason that wait
    is now phase 3 instead of the bulk of phase 2.

    Phase 3 — promote: the readiness waits happen TOGETHER rather than
    one after another. Four sandboxes at 27 seconds each cost 108
    seconds in the old serial loop and cost one startup here.

    `ready_timeout` is how long phase 3 may spend asking. None — the
    default — means the substrate's own startup budget, so a tick still
    carries an execution from PENDING all the way to RUNNING, which is
    what every caller of this function expects and what its name
    promises. A caller that wants the tick to return promptly and let
    the NEXT one promote passes `readiness.PROBE_TIMEOUT`; that is what
    the production worker pass does in `worker/runner.dispatch_pass`.

    Args:
        conn: asyncpg connection (search_path includes kernel).
        driver: substrate Driver instance (DockerDriver at C4).
        env_allowlist: list of env var names safe to forward to executors.
        goal_id: the goal to scan for pending executions.
        ready_timeout: seconds to spend on each readiness probe, or None
            for the driver's own budget.

    Returns:
        List of container_ids created by the substrate driver.
    """
    # ── Phase 1: admit — inside a transaction ────────────────────────────
    # FOR UPDATE SKIP LOCKED requires an active transaction to hold row
    # locks. Without this, asyncpg autocommits and two replicas see the
    # same PENDING rows.
    async with conn.transaction():
        admitted_ids = await admit_pending(conn, goal_id)
    # Transaction committed. Row locks released.

    # ── Phase 2: dispatch — OUTSIDE a transaction ────────────────────────
    # Level-triggered: query all ADMITTED executions, not just the ones
    # phase 1 just admitted.  C6 recovery moves crashed executions to
    # ADMITTED — they must be picked up here without re-admission.
    # Each dispatch is its own set of autocommitted statements. Holding a
    # transaction across driver.create() would pin this connection for
    # the length of that call — and until the split it would have pinned
    # it for the container's entire startup time as well.
    admitted_rows = await conn.fetch(
        "SELECT id FROM executions "
        "WHERE goal_id = $1 AND state = 'ADMITTED' "
        "ORDER BY created_at, id",
        goal_id,
    )
    container_ids = []
    for row in admitted_rows:
        eid = row["id"]
        try:
            full_row = await conn.fetchrow(
                "SELECT * FROM executions WHERE id = $1", eid,
            )
            # H1b: the contract, when there is one. LEFT JOIN in spirit
            # — an errand has no contract and a story has none until it
            # is approved, and both must still dispatch.
            contract_row = None
            if full_row.get("contract_id"):
                found = await conn.fetchrow(
                    "SELECT * FROM contracts WHERE id = $1",
                    full_row["contract_id"])
                contract_row = dict(found) if found else None
            # I2 -- the mount list travels with the spec, because on a
            # pod substrate the workspace is composed inside the pod and
            # the driver is the only thing that can carry it there. Read
            # here because this is where the connection is; the pod has
            # no database and must not.
            from kernel.workspace.mounts import mounts_for

            spec = build_spec(full_row, env_allowlist,
                              contract_row=contract_row,
                              mounts=await mounts_for(conn, eid))
            cid = await dispatch_admitted(conn, driver, eid, spec)
        except ContractRefused as e:
            # REFUSED AT ADMISSION, not discovered by a leg. The story
            # stays where it is with the reason recorded — a container
            # built from an unusable contract would orient, run out and
            # look like a model that would not work.
            logger.error("execution %s refused at dispatch: %s", eid, e)
            await conn.execute(
                "UPDATE executions SET stall_reason = 'contract_refused', "
                "stalled_at = COALESCE(stalled_at, now()) WHERE id = $1", eid)
            continue
        except NotAdmittedError:
            # Another replica won the CAS — skip this execution.
            # All other exceptions (driver, lease, generation) propagate
            # and abort the tick, which is correct.
            continue
        container_ids.append(cid)

    # ── Phase 3: promote — the sandboxes that are answering ──────────────
    # Level-triggered like phase 2, and for the same reason: a sandbox
    # created by an earlier tick (or by another replica) that has since
    # come up must reach RUNNING without being re-created. The probes run
    # concurrently inside this call, so the phase costs one readiness
    # budget however many rows it finds.
    for action in await promote_ready(conn, driver, goal_id,
                                      timeout=ready_timeout):
        if action["action"] != "promoted":
            logger.info("execution %s: %s — %s", action["execution_id"],
                        action["action"], action["detail"])

    return container_ids
