"""Where the money went: by spender, and by the conversation that caused it.

THE QUESTION THE COST PAGE COULD NOT ANSWER. It could say what was spent and
on which model. It could not say who spent it, because two of the four
spenders never reached the ledger -- and it could not say which conversation
caused it, because no row carried a story. Both are now in the data; this
reads it.

WHAT "ADDS UP" MEANS HERE, and it is checked rather than asserted. Every
priced call falls in exactly one spender bucket and exactly one thread (or
the unattributed one), so:

    assistant + orchestrator + executor + verifier + other  = total
    attributed + unattributed                               = total

`reconciles` on the returned dict is that comparison, done in Python against
the same numbers the page renders. If it ever goes False the page says so
out loud, because a cost report that has quietly stopped balancing is one
people keep trusting for months.

UNPRICED IS NOT FREE. A model with no rate row contributes NULL, not zero,
all the way through. It is surfaced as a count so the page can say "and 1,475
tokens we cannot value" instead of rounding them away.
"""
from __future__ import annotations

import logging

from app.database import get_pool

_log = logging.getLogger(__name__)

# The order they read in on the page: the human's conversation first, then
# what that conversation set in motion, then everything else.
SPENDERS = ("assistant", "orchestrator", "executor", "verifier", "other")

_TOLERANCE = 1e-6


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def money(v) -> str:
    """Sub-cent spend must not render as $0.00 — that reads as free."""
    n = _f(v)
    if n and abs(n) < 0.01:
        return f"${n:.4f}"
    return f"${n:,.2f}"


async def spend_by_role(goal_id: str | None = None) -> dict:
    """Totals per spender, plus the reconciliation check.

    Never raises: the cost panel is a report and must not be able to take
    the dashboard down with it.
    """
    pool = get_pool()
    try:
        if goal_id:
            rows = await pool.fetch(
                "SELECT * FROM v_cost_reconciliation WHERE goal_id = upper($1)",
                goal_id)
        else:
            rows = await pool.fetch("SELECT * FROM v_cost_reconciliation")
    except Exception as exc:                                # noqa: BLE001
        _log.warning("cost attribution unavailable: %s", exc)
        return {"available": False, "roles": [], "total": 0.0,
                "reconciles": True, "unpriced_calls": 0}

    per = {s: 0.0 for s in SPENDERS}
    total = attributed = unattributed = 0.0
    unpriced_calls = unpriced_tokens = 0
    for r in rows:
        for s in SPENDERS:
            per[s] += _f(r[f"{s}_cost"])
        total += _f(r["total_cost"])
        attributed += _f(r["attributed_cost"])
        unattributed += _f(r["unattributed_cost"])
        unpriced_calls += int(r["unpriced_calls"] or 0)
        unpriced_tokens += int(r["unpriced_tokens"] or 0)

    reconciles = (abs(sum(per.values()) - total) < _TOLERANCE
                  and abs(attributed + unattributed - total) < _TOLERANCE)
    if not reconciles:
        # LOUD. This is the invariant the whole feature rests on, and the
        # way it would otherwise be discovered is somebody noticing months
        # later that two panels disagree.
        _log.error("cost does not reconcile: roles=%.6f attributed=%.6f "
                   "unattributed=%.6f total=%.6f",
                   sum(per.values()), attributed, unattributed, total)

    return {
        "available": bool(rows),
        "total": total,
        "total_str": money(total),
        "attributed": attributed,
        "unattributed": unattributed,
        "unattributed_str": money(unattributed),
        "reconciles": reconciles,
        "unpriced_calls": unpriced_calls,
        "unpriced_tokens": unpriced_tokens,
        "roles": [
            {"role": s, "cost": per[s], "cost_str": money(per[s]),
             "pct": (per[s] / total * 100.0) if total else 0.0}
            for s in SPENDERS if per[s]
        ],
    }


async def threads(goal_id: str | None = None, limit: int = 25) -> list[dict]:
    """One row per conversation: its own spend, and the work it started.

    A thread's total is the assistant's turns PLUS the executor legs of its
    story PLUS the verifier runs that judged them. Reported together because
    that is the number a person means by "what did this cost" -- the
    assistant's own turns are usually the small part, and showing only those
    is how the executor stayed invisible.
    """
    pool = get_pool()
    where = "WHERE goal_id = upper($1)" if goal_id else ""
    args = [goal_id] if goal_id else []
    try:
        rows = await pool.fetch(f"""
            SELECT thread_id,
                   max(goal_id)                                  AS goal_id,
                   max(story_id)                                 AS story_id,
                   COALESCE(max(story_title), max(thread_title)) AS title,
                   sum(cost)                                     AS cost,
                   sum(tokens)                                   AS tokens,
                   sum(cache_read_tokens)                        AS cached,
                   sum(unpriced_calls)                           AS unpriced,
                   jsonb_object_agg(role, round(COALESCE(cost, 0), 6))
                                                                 AS by_role
              FROM mv_cost_by_thread {where}
             GROUP BY thread_id
             ORDER BY sum(cost) DESC NULLS LAST
             LIMIT {int(limit)}""", *args)
    except Exception as exc:                                # noqa: BLE001
        _log.warning("thread cost unavailable: %s", exc)
        return []

    out = []
    for r in rows:
        by_role = r["by_role"] or {}
        if isinstance(by_role, str):
            import json
            by_role = json.loads(by_role)
        out.append({
            "thread_id": r["thread_id"],
            "goal_id": r["goal_id"],
            "story_id": r["story_id"],
            "title": r["title"] or "(untitled conversation)",
            "cost": _f(r["cost"]),
            "cost_str": money(r["cost"]),
            "tokens": int(r["tokens"] or 0),
            "cached": int(r["cached"] or 0),
            "unpriced": int(r["unpriced"] or 0),
            # Ordered, so the bar segments are in the same order every row.
            "parts": [{"role": s, "cost": _f(by_role.get(s)),
                       "cost_str": money(by_role.get(s))}
                      for s in SPENDERS if _f(by_role.get(s))],
        })
    return out
