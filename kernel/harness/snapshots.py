"""Workspace snapshots — S5 implementation for E3.

The harness owns snapshots at leg boundaries, before park, and on a step
interval.  The model is never asked to be durable (§1.3).

snapshot_preflight() fails closed at startup: if the checkpoint table is
unreachable, the harness refuses to start.  This is the strongest guard
in E3 and gets its own red-check.
"""

import json


class SnapshotPreflightError(Exception):
    """Raised when the snapshot preflight probe fails."""


async def snapshot_preflight(conn, execution_id: int) -> None:
    """Verify that we can write and delete checkpoint rows.

    Writes a probe row and deletes it.  If either fails, raises
    SnapshotPreflightError — the harness must not start without
    snapshot capability.

    Args:
        conn: the connection the harness will use.  The caller MUST pass
            this connection.  In tests that deliberately break the table
            (ALTER TABLE RENAME inside a transaction), the rename is only
            visible on THAT connection — a separate connection would see
            the intact table and pass vacuously.
        execution_id: a valid execution id (must exist in executions table).
    """
    probe_data = json.dumps({"probe": True})
    try:
        # Use a unique seq unlikely to collide: -1 (below any real checkpoint).
        probe_id = await conn.fetchval(
            """
            INSERT INTO kernel.execution_checkpoints
                (execution_id, generation, seq, kind, data)
            VALUES ($1, 0, -1, 'preflight_probe', $2::jsonb)
            RETURNING id
            """,
            execution_id,
            probe_data,
        )
        await conn.execute(
            "DELETE FROM kernel.execution_checkpoints WHERE id = $1",
            probe_id,
        )
    except Exception as e:
        raise SnapshotPreflightError(
            f"Snapshot preflight failed: cannot write/delete checkpoint rows: {e}"
        ) from e


async def snapshot(
    conn,
    execution_id: int,
    generation: int,
    seq: int,
    kind: str,
    data: dict,
) -> int:
    """Write a checkpoint row.  Returns the checkpoint id.

    Uses a plain INSERT — no ON CONFLICT. A collision raises, which is the
    correct behaviour: the caller allocates seq as MAX(seq)+1 WHERE
    execution_id (execution-scoped, monotonic across generations), so the
    UNIQUE index (execution_id, generation, seq) cannot collide under
    normal operation. If it does, that is a bug to surface, not swallow.

    This is the opposite of the journal's former pattern (ON CONFLICT DO
    NOTHING with discarded RETURNING id), where collisions vanished
    silently and let the generation-less idem_key bug survive ES2/ES3.
    Fixed in cycle 1d.

    Args:
        conn: asyncpg connection (search_path must include kernel).
        execution_id: the execution this checkpoint belongs to.
        generation: execution generation at time of snapshot.
        seq: ordering within the execution (monotonic, not per-generation).
        kind: checkpoint type — 'leg_boundary', 'park', 'interval', etc.
        data: checkpoint payload (budget_basis, total_spend, predicate, etc.).
    """
    checkpoint_id = await conn.fetchval(
        """
        INSERT INTO kernel.execution_checkpoints
            (execution_id, generation, seq, kind, data)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        RETURNING id
        """,
        execution_id,
        generation,
        seq,
        kind,
        json.dumps(data),
    )
    return checkpoint_id
