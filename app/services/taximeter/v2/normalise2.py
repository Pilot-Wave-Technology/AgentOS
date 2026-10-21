"""The two pieces of maths v2 adds: a clamp, and the efficiency pool.

BOTH EXIST BECAUSE EFFICIENCY IS A RATIO AND RATIOS HAVE TAILS. v1
scored cost with a "lower is better" complement; v2 scores priority
points PER DOLLAR and PER HOUR, which is the right question but turns
one cheap story into an unbounded rate. A person who delivered a
critical story in ten minutes on a free model is genuinely efficient;
they are not a thousand times more efficient than everybody else, and
without a clamp their score would take the whole of N and leave the
rest of the team at nothing.

THE CLAMP IS A BAND AROUND THE MEDIAN, not a fixed ceiling: it scales
with the population, so a team that gets uniformly faster is not
squashed. A 483x outlier is the shape actually seen in production -- it
is what retired v4's reciprocal-flip formula -- and the band is why the
same input is survivable now.

THE POOL IS WHO IS ELIGIBLE, which is a separate question from how
well they did. Scoring efficiency over everybody would rank people who
finished nothing this period against people who did, and
"non-participant" is not the same claim as "inefficient".

Pure: numbers in, numbers out, no pool and no clock.
"""

from __future__ import annotations

from statistics import median

#: How far from the median a rate may sit before it is pulled in.
#: Quarter to quadruple: wide enough that ordinary variation is
#: untouched, narrow enough that one freak story cannot take the column.
CLAMP_LOW = 0.25
CLAMP_HIGH = 4.0


def clamp_to_median_band(rates: dict[str, float]) -> dict[str, float]:
    """Pull every rate into [median/4, 4*median]. Zeros are left alone.

    ZEROS ARE NOT CLAMPED UP. A zero here means "did not participate",
    and lifting it to a quarter of the median would pay somebody for
    work they did not do -- the opposite of what the band is for.

    With fewer than two participants there is no population to be an
    outlier against, so nothing is clamped.
    """
    live = [v for v in rates.values() if v > 0]
    if len(live) < 2:
        return dict(rates)
    mid = median(live)
    if mid <= 0:
        return dict(rates)
    lo, hi = mid * CLAMP_LOW, mid * CLAMP_HIGH
    return {uid: (0.0 if v <= 0 else min(max(v, lo), hi))
            for uid, v in rates.items()}


def pool_scores(rates: dict[str, float], pool: set[str], active_n: int,
                ) -> dict[str, float]:
    """Score `rates` within `pool`, so the pool's scores sum to N.

    THE POOL IS THE PEOPLE WHO FINISHED SOMETHING this period. Everyone
    else scores 0 -- not because they were inefficient, but because
    efficiency is a question about delivered work and they delivered
    none. Ranking them anyway would be answering a question nobody
    asked.

    SOMEONE IN THE POOL WITH NO MEASURABLE RATE SCORES THE POOL
    AVERAGE, `N/c`. That is the "cost not applicable" case: a story
    worked entirely in VS Code records no spend, so its owner has no
    cost rate at all. Zero would punish them for the absence of a
    number, and the pool average is the honest "no evidence either
    way". `c` is the pool size, so the pool still sums to N.

    With fewer than two measurable rates there is nothing to rank
    against, and everyone in the pool gets the average for the same
    reason `_share_complement_scores` pins a single contributor.
    """
    c = len(pool)
    if not c:
        return {}
    average = active_n / c
    measurable = {u: rates.get(u, 0.0) for u in pool if rates.get(u, 0.0) > 0}
    if len(measurable) < 2:
        return {u: round(average, 5) for u in pool}

    total = sum(measurable.values())
    # Each measurable person takes their share of the measurable
    # portion; the unmeasurable ones take the average, and the
    # measurable portion is whatever is left after that.
    unmeasurable = [u for u in pool if u not in measurable]
    reserved = average * len(unmeasurable)
    share_of = active_n - reserved
    out = {u: round(average, 5) for u in unmeasurable}
    for uid, rate in measurable.items():
        out[uid] = round(share_of * rate / total, 5)
    return out
