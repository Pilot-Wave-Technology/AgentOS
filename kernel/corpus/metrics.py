"""G3 — the four numbers, read off a finished run.

None of these are computed while the run happens. They are derived from
`executions`, `step_journal` and `verdicts` afterwards, which is what
makes them re-derivable: a number a harness reports about itself cannot
be checked, and a number anybody can recompute from the journal can.

    first_attempt_success   the FIRST completion claim passed
    silent_wrong_edit       a claim was made and refused — the executor
                            believed it was finished and was not
    tokens_to_first_write   E5c's floor, already recorded per execution
    boundary_overhead       tokens spent on legs after the first, as a
                            share of the story's whole spend

WHY `first_attempt` AND NOT "did it finish". A story that claims, is
refused, fixes it and passes has been served well by the Verifier and
badly by whatever made it claim early. Collapsing those into one number
hides the thing G4–G6 are trying to move.

BOUNDARY OVERHEAD IS A SHARE, NOT A COUNT. The absolute figure grows
with the task and cannot be compared across a corpus; the share is what
says whether crossing a boundary is getting cheaper. It is an upper
bound and says so: everything after leg 1 is counted, including real
work, because nothing in the journal distinguishes re-reading a file you
already knew about from reading it for the first time. A number that
overstates in a known direction is usable; one that quietly picks a
definition is not.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


# NAMED FIELDS, NEVER "every number in the dict".
#
# This summed `cost.values()`, which was correct exactly as long as the
# dict held nothing but token counts. It stopped being true the moment
# the journal started recording what a step could be CHECKED against:
# `providerTotal` is the whole sum a second time, `sentChars` is
# CHARACTERS and runs about four times the token count, `thinking` is a
# subset of output already counted, and `boundary` is a bool that Python
# adds as 1. Measured on a real step: 13,700 tokens reported as 75,550,
# a 5.5x inflation of total_tokens and of boundary_overhead.
#
# The defect is not the extra keys — recording them was right, and the
# reconciler needs them. It is a sum that inherits every future field by
# default, so a diagnostic added anywhere silently becomes spend here.
_TOKEN_FIELDS = ("input", "output", "cacheRead", "cacheWrite")


def _tokens(cost: dict) -> int:
    """Billed tokens for one step. Thinking is excluded: it is a subset
    of output, so adding it counts the same tokens twice."""
    cost = cost or {}
    return int(sum(v for v in (cost.get(f) for f in _TOKEN_FIELDS)
                   if isinstance(v, (int, float))))


def _context(cost: dict) -> int:
    """What the step had to hold at once — the orientation floor's unit."""
    cost = cost or {}
    return int(sum(v for v in (cost.get(f) for f in
                               ("input", "cacheRead", "cacheWrite"))
                   if isinstance(v, (int, float))))


async def leg_spend(conn, execution_id: int) -> dict:
    """Tokens per generation, from the journal."""
    rows = await conn.fetch(
        "SELECT generation, cost::text FROM step_journal "
        "WHERE execution_id = $1 ORDER BY seq", execution_id)
    per: dict[int, int] = {}
    for r in rows:
        cost = json.loads(r["cost"]) if r["cost"] else {}
        per[r["generation"]] = per.get(r["generation"], 0) + _tokens(cost)
    return per


async def claims(conn, execution_id: int) -> list[dict]:
    """Every completion claim and what became of it, oldest first."""
    rows = await conn.fetch(
        "SELECT claim_seq, passed, generation, created_at FROM verdicts "
        "WHERE execution_id = $1 ORDER BY id", execution_id)
    return [{"claim_seq": r["claim_seq"], "passed": r["passed"],
             "generation": r["generation"]} for r in rows]


async def measure(conn, execution_id: int) -> dict:
    """The four numbers for one run, plus what they were derived from."""
    row = await conn.fetchrow(
        "SELECT state, generation, tokens_to_first_write, "
        "steps_to_first_write, context_at_first_write "
        "FROM executions WHERE id = $1", execution_id)
    if row is None:
        return {"execution_id": execution_id, "error": "no such execution"}

    verdicts = await claims(conn, execution_id)
    per_leg = await leg_spend(conn, execution_id)
    total = sum(per_leg.values())
    after_first = sum(v for g, v in per_leg.items() if g > 0)

    passed = [v for v in verdicts if v["passed"]]
    refused = [v for v in verdicts if not v["passed"]]

    return {
        "execution_id": execution_id,
        "state": row["state"],
        "legs": row["generation"] + 1,
        # None, not False, when nothing was ever claimed. A run that
        # never got far enough to claim has not failed the first attempt
        # — it has no first attempt, and averaging it in as a zero would
        # blame the executor for a dispatch problem.
        "first_attempt_success": (verdicts[0]["passed"] if verdicts
                                  else None),
        "claims": len(verdicts),
        "silent_wrong_edit": bool(refused),
        "refusals": len(refused),
        "reached_done": row["state"] == "DONE",
        "verified": bool(passed),
        "tokens_to_first_write": row["tokens_to_first_write"],
        "context_at_first_write": row["context_at_first_write"],
        "steps_to_first_write": row["steps_to_first_write"],
        "total_tokens": total,
        "tokens_after_leg_1": after_first,
        "boundary_overhead": (after_first / total) if total else None,
        "spend_by_leg": {str(k): v for k, v in sorted(per_leg.items())},
    }


def _mean(values):
    values = [v for v in values if v is not None]
    return (sum(values) / len(values)) if values else None


def aggregate(runs: list[dict]) -> dict:
    """The corpus-level figures. Rates over runs that HAD an attempt.

    A run that never claimed is excluded from the rate rather than
    counted as a failure, and the exclusion is reported: a rate whose
    denominator moved silently is a rate two people will read
    differently.
    """
    attempted = [r for r in runs if r.get("first_attempt_success") is not None]
    first_ok = [r for r in attempted if r["first_attempt_success"]]
    wrong = [r for r in attempted if r["silent_wrong_edit"]]
    return {
        "runs": len(runs),
        "attempted": len(attempted),
        "not_attempted": len(runs) - len(attempted),
        "first_attempt_success_rate": (
            len(first_ok) / len(attempted) if attempted else None),
        "silent_wrong_edit_rate": (
            len(wrong) / len(attempted) if attempted else None),
        "reached_done": sum(1 for r in runs if r.get("reached_done")),
        "mean_context_at_first_write": _mean(
            [r.get("context_at_first_write") for r in runs]),
        "mean_tokens_to_first_write": _mean(
            [r.get("tokens_to_first_write") for r in runs]),
        "mean_boundary_overhead": _mean(
            [r.get("boundary_overhead") for r in runs]),
        "mean_legs": _mean([r.get("legs") for r in runs]),
    }


def render(summary: dict, runs: list[dict]) -> str:
    """The figures as text, for a person and for the progress file."""
    def pct(x):
        return "—" if x is None else f"{x * 100:.0f}%"

    def num(x):
        return "—" if x is None else f"{x:,.0f}"

    def one_dp(x):
        return "—" if x is None else f"{x:.1f}"

    lines = [
        f"  runs                        {summary['runs']}"
        f" ({summary['attempted']} claimed,"
        f" {summary['not_attempted']} never got that far)",
        f"  first-attempt success       "
        f"{pct(summary['first_attempt_success_rate'])}",
        f"  silent-wrong-edit rate      "
        f"{pct(summary['silent_wrong_edit_rate'])}",
        f"  reached DONE                "
        f"{summary['reached_done']}/{summary['runs']}",
        # BOTH, side by side, and labelled by the question each answers.
        # One number here read as the other is what made 432,403 look
        # like an orientation floor.
        f"  orientation floor (context) "
        f"{num(summary.get('mean_context_at_first_write'))}"
        f"   <- what a leg must hold",
        f"  cost to first write (spend) "
        f"{num(summary['mean_tokens_to_first_write'])}"
        f"   <- what getting there cost",
        f"  mean boundary overhead      "
        f"{pct(summary['mean_boundary_overhead'])}  (upper bound)",
        f"  mean legs                   {one_dp(summary['mean_legs'])}",
        "",
    ]
    # EVERY FIELD OPTIONAL, DELIBERATELY.
    #
    # A run that dies before the harness can record anything produces a
    # partial dict, and this reporter used to raise KeyError on it — so
    # the one run whose failure most needed reporting was the one that
    # took the report down with it, taking the other runs' results too.
    # A reporting path must survive the failures it exists to describe.
    for r in runs:
        label = r.get("task") or r.get("execution_id") or "?"
        state = r.get("state") or "NO STATE"
        lines.append(
            f"    {str(label):<22} {state:<10} "
            f"legs={r.get('legs', '—')} claims={r.get('claims', '—')} "
            f"first={r.get('first_attempt_success')} "
            f"ttfw={num(r.get('tokens_to_first_write'))}"
            + (f"  ⚠ {r['error']}" if r.get("error") else ""))
    return chr(10).join(lines)
