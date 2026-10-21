"""Dispatch — ADMITTED → DISPATCHING: create the sandbox, and return.

IT USED TO WAIT HERE, and that wait was the largest single source of
latency in the conductor. This function did six things inline: CAS the
state, create the container, `await_ready` it, read the generation,
insert the lease, CAS to RUNNING. The third took about 27 seconds for a
real sandbox and the other five took milliseconds, and dispatch is
called from a serial `for` loop — so four sandboxes cost about 108
seconds during which the pass advanced nothing else, and twenty stories
from cold was about nine minutes of a conductor that looked wedged.

So readiness left. This creates the sandbox, records its id with the row
still DISPATCHING, and returns — about a second. `promote.py` picks the
row up when the bridge answers and does the last three steps there, over
the DISPATCHING → RUNNING edge machine v5 already declares.

STILL OUTSIDE A TRANSACTION, and more so than before. Holding one across
`create()` + `await_ready()` pinned a DB connection for the entire
container startup; there is now no `await_ready()` here to hold it
across, and the two statements that remain are autocommitted. The CAS
guard on the state column (WHERE state = 'ADMITTED') is what prevents
double-dispatch, and it needs no long-lived transaction to do it.

On driver failure the container is destroyed (idempotent) and the state
stays DISPATCHING. C6 reconcile handles recovery. Do NOT transition back
to ADMITTED — that creates a retry loop without C6's orphan sweep. The
same guarantee now covers a sandbox that starts and never answers:
`promote.py` destroys it and leaves the row exactly here, for reconcile.
"""

from __future__ import annotations


async def _why_not_admitted(conn, execution_id: int) -> str:
    """Say what actually happened, in the state the row is in.

    The CAS guard reports one fact — the row was not ADMITTED — and the
    message used to attribute it to a cause it had not checked: "another
    replica may have claimed it". A person then looks for a replica.

    The common cause is the opposite of a race: the row is still PENDING
    because admission found no free slot. Before H1c nothing ever released
    one, so a ceiling of 4 meant the fifth story EVER started could not be
    admitted — and the error named a replica that did not exist.
    """
    row = await conn.fetchrow(
        "SELECT state, kind, domain, goal_id FROM executions WHERE id = $1",
        execution_id)
    if row is None:
        return "no such execution"
    if row["state"] != "PENDING":
        return (f"it is {row['state']}, not ADMITTED — another worker has "
                f"already taken it")

    free, total = await conn.fetchrow(
        """
        SELECT count(*) FILTER (
                   WHERE execution_id IS NULL OR released_at IS NOT NULL),
               count(*)
        FROM admission_slots
        WHERE goal_id = $1 AND domain = $2 AND kind = $3
        """,
        row["goal_id"], row["domain"], row["kind"])
    if total == 0:
        return (f"still PENDING: no admission bucket exists for "
                f"{row['goal_id']}/{row['kind']}/{row['domain']}")
    if free == 0:
        held = await conn.fetch(
            "SELECT s.execution_id, e.state FROM admission_slots s "
            "LEFT JOIN executions e ON e.id = s.execution_id "
            "WHERE s.goal_id = $1 AND s.domain = $2 AND s.kind = $3 "
            "AND s.released_at IS NULL ORDER BY s.slot_index",
            row["goal_id"], row["domain"], row["kind"])
        who = ", ".join(f"#{h['execution_id']} {h['state']}" for h in held)
        return (f"still PENDING: all {total} admission slot(s) for "
                f"{row['kind']}/{row['domain']} are held by {who}. A slot "
                f"comes back when its run reaches a terminal state; cancel "
                f"one to make room.")
    return f"still PENDING with {free} of {total} slot(s) free — admission ran but did not take it"


class NotAdmittedError(Exception):
    """Raised when a CAS guard finds the execution is no longer ADMITTED.

    Another replica claimed the row between the SELECT and the UPDATE.
    Distinct from RuntimeError so conductor_tick can catch exactly this
    case and continue the loop without swallowing driver/lease/generation
    failures (the C5 lesson).
    """

    pass


async def dispatch_admitted(conn, driver, execution_id: int, spec) -> str:
    """Give an ADMITTED execution a sandbox. Leaves the row DISPATCHING.

    Runs OUTSIDE a transaction — do not wrap this in conn.transaction().

    Args:
        conn: asyncpg connection (search_path includes kernel).
        driver: substrate Driver instance (DockerDriver at C4).
        execution_id: the execution row id, must be in ADMITTED state.
        spec: ExecutionSpec built by spec_builder.build_spec.

    Returns:
        container_id from the substrate driver. The execution is
        DISPATCHING and holds that id; `promote.promote_ready` takes it
        to RUNNING when the sandbox answers.

    Raises:
        NotAdmittedError: if the CAS guard finds zero rows (another replica took it).
        Any driver exception: re-raised after cleanup.
    """
    # ── Step a: CAS guard — ADMITTED → DISPATCHING ───────────────────────
    result = await conn.execute(
        """
        UPDATE executions
        SET state = 'DISPATCHING', updated_at = now()
        WHERE id = $1 AND state = 'ADMITTED'
        """,
        execution_id,
    )
    # asyncpg returns "UPDATE N" — parse the row count
    rows_affected = int(result.split()[-1])
    if rows_affected == 0:
        raise NotAdmittedError(
            f"execution {execution_id} is not ADMITTED: "
            + await _why_not_admitted(conn, execution_id)
        )

    # ── Step b: create the container ─────────────────────────────────────
    container_id = None
    try:
        container_id = await driver.create(spec)

        # ── Step c: RECORD IT, STILL DISPATCHING ─────────────────────────
        # Immediately, and this is the part that used to be 27 seconds
        # late. The container id was written only at the RUNNING step, so
        # for the whole of the readiness wait a live container existed
        # that no database row named: the orphan sweep could take it, a
        # cancel could not reap it, and reconcile read the row as
        # "DISPATCHING with no compute" and handed it back to ADMITTED —
        # then dispatch's own CAS to RUNNING found no DISPATCHING row and
        # silently leaked the container it had just built.
        #
        # The CAS is the same guard in a different place. `container_id
        # IS NULL` makes the double-dispatch the caller's comment warns
        # about ("two live containers for the same execution id")
        # impossible rather than merely unlikely, and zero rows means
        # nobody will ever claim what we just created — so it is
        # destroyed by the handler below, exactly as a driver failure is.
        recorded = await conn.execute(
            """
            UPDATE executions
            SET container_id = $1, updated_at = now()
            WHERE id = $2 AND state = 'DISPATCHING' AND container_id IS NULL
            """,
            container_id,
            execution_id,
        )
        if int(recorded.split()[-1]) == 0:
            raise RuntimeError(
                f"execution {execution_id} stopped being a DISPATCHING row "
                f"with no sandbox while one was being created — "
                f"{container_id[:12]} belongs to nobody and is destroyed")
    except Exception:
        # Cleanup: destroy whatever was created (idempotent).
        # Leave state as DISPATCHING — C6 reconcile handles recovery.
        if container_id is not None:
            try:
                await driver.destroy(container_id, stop_timeout=0)
            except Exception:
                pass  # destroy is idempotent; swallow cleanup errors
        raise

    # Steps d–f (generation, lease, RUNNING) are kernel/conductor/promote.py.
    return container_id
