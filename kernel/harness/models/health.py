"""Is this provider answering? The durable half of the circuit breaker.

The in-memory breaker in `breaker.py` stops one process calling a
provider that is down. This is the part a DIFFERENT process can read —
specifically the Conductor, which must stop admitting new work into an
outage rather than admitting it to stall.

STALE ROWS ARE IGNORED, AND THAT IS THE WHOLE DESIGN. A worker killed
mid-outage leaves `state = 'open'` behind with nobody left to clear it.
If admission trusted that row it would refuse every story for ever, and
the failure would look exactly like a queue that stopped draining for no
reason. So an open row that has not been refreshed inside the staleness
bound is treated as closed.

That is failing OPEN, deliberately. The two mistakes are not
symmetrical: admitting into an outage costs a stall that resumes on its
own the moment the provider returns, and refusing to admit costs a queue
nobody drains and no signal saying why. When the evidence has expired,
the cheaper error is to let the work through and let the leg discover
the truth.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# How long an `open` row is believed without a refresh. Three probe
# intervals: long enough that a live worker in a real outage always
# refreshes inside it, short enough that a dead one stops blocking
# admission within a couple of minutes.
STALE_AFTER_S = 90.0


async def mark_open(conn, model: str, kind: str) -> None:
    """Record that this model is not answering. Idempotent per model.

    `opened_at` is preserved across repeated calls so it measures the
    OUTAGE, not the last tick that noticed it; `updated_at` moves every
    time, which is what keeps the row from going stale while a worker is
    still watching.
    """
    await conn.execute(
        """
        INSERT INTO provider_health (model, state, last_kind, opened_at,
                                     updated_at)
        VALUES ($1, 'open', $2, now(), now())
        ON CONFLICT (model) DO UPDATE
           SET state = 'open',
               last_kind = EXCLUDED.last_kind,
               opened_at = CASE WHEN provider_health.state = 'open'
                                THEN provider_health.opened_at
                                ELSE now() END,
               updated_at = now()
        """,
        model, kind)


async def mark_closed(conn, model: str) -> None:
    """Record a call that worked — the only evidence a provider is back."""
    await conn.execute(
        """
        UPDATE provider_health
           SET state = 'closed', opened_at = NULL, updated_at = now()
         WHERE model = $1 AND state <> 'closed'
        """,
        model)


async def open_models(conn, stale_after_s: float = STALE_AFTER_S) -> list:
    """Models believed to be down right now. Stale rows are not believed."""
    rows = await conn.fetch(
        """
        SELECT model, last_kind,
               EXTRACT(EPOCH FROM (now() - updated_at)) AS age_s
          FROM provider_health
         WHERE state = 'open'
        """)
    live = []
    for row in rows:
        if float(row["age_s"] or 0) > stale_after_s:
            logger.info("provider_health row for %s is %.0fs old and no "
                        "longer believed; admission proceeds",
                        row["model"], float(row["age_s"] or 0))
            continue
        live.append({"model": row["model"], "kind": row["last_kind"],
                     "age_s": float(row["age_s"] or 0)})
    return live
