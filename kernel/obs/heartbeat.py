"""Alive by a row, silent by the absence of one.

USE. Wrap a pass, not a loop:

    async with beat(conn, "worker.ledger_drain", every=4.0) as hb:
        rows = await drain_all(conn)
        hb.did(len(rows))

On the way out it upserts one row: this pass ran, at this time, from
this build, having done this much. If the body raised, the row records
the error and the exception still propagates -- a heartbeat that
swallows what it is measuring is worse than none.

WHY PER PASS. The worker's tick catches its own exceptions per pass, on
purpose: the runs are the product and the drains are hygiene, and
letting hygiene take down the product is how a safety measure becomes an
outage. The consequence is that a loop-level heartbeat stays green while
three passes inside it fail silently for a week. So each pass owns a
row.

WHY IT CANNOT LIVE IN kernel/obs/__init__. Everything else there is
pure: a contextvar, a field table, a formatter. This needs a database
handle, and importing asyncpg into the logging path would make logging
depend on the thing it most needs to report on.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from kernel.obs import alive
from kernel.obs import fields as F

logger = logging.getLogger(__name__)

UPSERT = """
    INSERT INTO public.loop_heartbeats
        (loop_name, instance_id, component, build_sha, last_tick_at,
         expected_interval_s, tick_count, work_count,
         last_error, last_error_at, error_count)
    VALUES ($1, $2, $3, $4, now(), $5, 1, $6, $7,
            CASE WHEN $7::text IS NULL THEN NULL ELSE now() END,
            CASE WHEN $7::text IS NULL THEN 0 ELSE 1 END)
    ON CONFLICT (loop_name, instance_id) DO UPDATE SET
        component           = EXCLUDED.component,
        build_sha           = EXCLUDED.build_sha,
        last_tick_at        = EXCLUDED.last_tick_at,
        expected_interval_s = EXCLUDED.expected_interval_s,
        tick_count          = public.loop_heartbeats.tick_count + 1,
        work_count          = public.loop_heartbeats.work_count
                              + EXCLUDED.work_count,
        -- The last error is KEPT after a recovery. A pass that fails
        -- every third tick is invisible in a gauge and obvious here.
        last_error    = COALESCE(EXCLUDED.last_error,
                                 public.loop_heartbeats.last_error),
        last_error_at = COALESCE(EXCLUDED.last_error_at,
                                 public.loop_heartbeats.last_error_at),
        error_count   = public.loop_heartbeats.error_count
                        + EXCLUDED.error_count
"""


def instance_id() -> str:
    """One copy of one process. Never empty -- see the migration."""
    return (os.environ.get("HOSTNAME")
            or os.environ.get("K_REVISION")
            or os.environ.get("COMPUTERNAME")
            or "unknown")


class Beat:
    """What the body of a pass reports back."""

    __slots__ = ("work", "started")

    def __init__(self) -> None:
        self.work = 0
        self.started = time.monotonic()

    def did(self, n: int = 1) -> None:
        """Count real work. `tick_count` says the pass ran; this says it
        found something, and the two together are what separates a loop
        that stopped from one that is idle."""
        self.work += int(n or 0)


@asynccontextmanager
async def beat(conn, loop_name: str, every: float | None = None):
    """Record one pass of `loop_name`. Re-raises whatever the body did."""
    hb = Beat()
    error: str | None = None
    try:
        yield hb
    except Exception as exc:                                # noqa: BLE001
        # Truncated: this column is read on a dashboard, and a 40kB
        # traceback in a table cell helps nobody. The full one is in the
        # log, joined by the same ids.
        error = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        # BEFORE the database write, and deliberately. This attests that
        # the EVENT LOOP turned; the row below attests that the database
        # is reachable too. They are different facts, and when the second
        # fails the first is exactly what a liveness probe still needs --
        # see kernel/obs/alive.py.
        # WITH THE INTERVAL. `every` is what lets the liveness probe
        # judge this loop against its OWN cadence instead of against the
        # fastest loop in the process -- see kernel/obs/alive.py.
        alive.touch(loop_name, every)
        proc = F.process_fields()
        try:
            await conn.execute(
                UPSERT, loop_name, instance_id(), proc["component"],
                proc["build_sha"], every, hb.work, error)
        except Exception:                                   # noqa: BLE001
            # A heartbeat that can take down the loop it measures is a
            # liability, not an instrument. Log and carry on -- a missing
            # row reads as "late", which is the correct alarm anyway.
            logger.warning("heartbeat write failed for %s", loop_name,
                           exc_info=True)


async def stale(conn, factor: float = 3.0) -> list:
    """Loops whose last tick is more than `factor` intervals ago.

    The alerting query, kept beside the writer so the two cannot drift.
    A loop with no `expected_interval_s` is skipped rather than guessed
    at -- an alert on an invented threshold is an alert that gets muted.
    """
    return await conn.fetch(
        """
        SELECT loop_name, instance_id, component, build_sha,
               last_tick_at, expected_interval_s, tick_count, work_count,
               last_error, error_count,
               EXTRACT(EPOCH FROM (now() - last_tick_at)) AS age_s
          FROM public.loop_heartbeats
         WHERE expected_interval_s IS NOT NULL
           AND now() - last_tick_at
               > ($1::numeric * expected_interval_s) * INTERVAL '1 second'
         ORDER BY age_s DESC
        """, factor)


async def split_brain(conn, within_seconds: int = 600) -> list:
    """Loops running under more than one build at once.

    On 25 Aug 2026 two Cloud Run revisions ran background loops through a
    rollover and the OLD one won races by finishing last. You cannot
    close that window on Cloud Run; you can see it in seconds.
    """
    return await conn.fetch(
        """
        SELECT loop_name, count(DISTINCT build_sha) AS builds,
               array_agg(DISTINCT build_sha) AS shas
          FROM public.loop_heartbeats
         WHERE last_tick_at > now() - ($1 * INTERVAL '1 second')
           AND build_sha IS NOT NULL AND build_sha <> 'unset'
         GROUP BY loop_name
        HAVING count(DISTINCT build_sha) > 1
        """, within_seconds)
