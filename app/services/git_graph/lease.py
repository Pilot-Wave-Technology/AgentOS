"""The merge lease (spec §5) — a per-(repo, trunk) exclusive ACTIVE slot on top of
the FIFO git_merge_queue. At most one node is 'active' (the lease holder) per
(repo, trunk); the rest wait by enqueue_seq. The DB partial-unique index
(git_merge_queue_one_active) makes a double-promote impossible; promote-next is a
single CAS run ONLY in the single-leader engine.

Step 5 builds the MECHANICS (promote, zombie-sweep). The producer that enqueues a
ready node and the consumer that DRIVES the active holder (open PR / grind loop)
land in build §6/§7 — until then an 'active' holder just RESERVES the slot, nothing
drives it. Release happens elsewhere: `_finalize_merged` sets the row 'merged' on a
detected merge, and the cancel path sets it 'parked' — both free the slot, and the
next `promote_pending` fills it.
"""
from __future__ import annotations

import logging

import asyncpg

_log = logging.getLogger(__name__)


def _rows_affected(status: str) -> int:
    try:
        return int(status.split()[-1])
    except Exception:
        return 0


async def promote_pending(conn) -> int:
    """For each (repo, trunk) that has waiting nodes and NO active holder, promote the
    FIFO head (lowest enqueue_seq) to 'active'. Leader-only.

    Safety: the `NOT EXISTS (... status='active')` guard inside the UPDATE + the
    partial-unique index together prevent two actives. In the brief two-leader
    rollover window the loser's UPDATE raises UniqueViolation (the index) — caught
    and treated as 'already promoted by the other leader'.

    IMPORTANT: each group's UPDATE must run in AUTOCOMMIT (no surrounding
    `conn.transaction()`), so a UniqueViolation aborts only that statement and the
    loop can `continue`. Wrapping the loop in an explicit transaction would poison it
    on the first violation (the next statement would raise InFailedSQLTransaction)."""
    groups = await conn.fetch(
        "SELECT DISTINCT git_repo_id, trunk_branch FROM git_merge_queue q "
        "WHERE status = 'waiting' AND trunk_branch IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM git_merge_queue a "
        "    WHERE a.git_repo_id = q.git_repo_id AND a.trunk_branch = q.trunk_branch "
        "      AND a.status = 'active')"
    )
    promoted = 0
    for g in groups:
        try:
            row = await conn.fetchrow(
                "UPDATE git_merge_queue SET status = 'active', active_since = now() "
                "WHERE node_id = ("
                "    SELECT node_id FROM git_merge_queue "
                "    WHERE git_repo_id = $1 AND trunk_branch = $2 AND status = 'waiting' "
                "    ORDER BY enqueue_seq ASC LIMIT 1) "
                "  AND NOT EXISTS (SELECT 1 FROM git_merge_queue a "
                "    WHERE a.git_repo_id = $1 AND a.trunk_branch = $2 AND a.status = 'active') "
                "RETURNING node_id",
                g["git_repo_id"], g["trunk_branch"],
            )
        except asyncpg.UniqueViolationError:
            # Another (rollover) leader took the slot first — fine, skip.
            continue
        if row:
            promoted += 1
    return promoted


async def sweep_zombie_active(conn) -> int:
    """Leader-startup sweep: release 'active' queue rows whose NODE is TERMINAL — a
    crash can leave a row 'active' after its node settled, which would block
    promotion forever (promote only fires when the slot is free). MERGED → 'merged';
    ABANDONED/ESCALATED → 'parked'.

    An IN-FLIGHT node (MERGING/QUEUED/…) is LEFT active on purpose: the detection
    poll (§4) + the §6 grind loop re-drive it; it isn't a zombie. This sweep only
    reclaims slots held by already-settled nodes."""
    res = await conn.execute(
        "UPDATE git_merge_queue q "
        "SET status = CASE WHEN n.state = 'MERGED' THEN 'merged' ELSE 'parked' END "
        "FROM git_nodes n "
        "WHERE q.node_id = n.id AND q.status = 'active' "
        "  AND n.state IN ('MERGED', 'ABANDONED', 'ESCALATED')"
    )
    return _rows_affected(res)
