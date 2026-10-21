"""Decisions, as they are actually recorded: inside approved plans.

Nothing here writes a decision. `register_plan` already stores a plan's
steps, rationale and checks, hashes them, and parks the run on a
`questions` row — kind `authorization`, which is what `park._KIND` maps
the caller's word `plan_approval` to. A decision taken while working a
story IS an entry in a plan a human signed.

(The E6h migration called that table `human_requests`. It does not exist
in the database the stack runs against; reading the name off the
migration instead of off `park.py` is how the first version of this
failed on its first call.)

So this module normalises and reads. A separate decisions table would
mean re-authoring, in a weaker voice, something already approved — and
would replace the strongest provenance in the system (a content hash and
a named approver) with the weakest (a model's recollection).

WHY DECISIONS ARE ADDRESSABLE INDIVIDUALLY. The obvious reading is that
cycle 2's plan supersedes cycle 1's, and it is wrong: a plan is a BUNDLE.
Plan B may keep the framework choice and change only the storage one.
Superseding at plan granularity announces that everything in A is stale,
so a planner works around a decision that still stands. Hence
`supersedes` per entry, and a decision nobody replaced stays live however
many cycles later it is read.
"""
from __future__ import annotations

# A plan cycle is the ordinal of its plan_approval park within the run:
# the first plan is cycle 1. Not `generation` — a run can burn several
# legs without registering a plan, and a person counting "the second
# plan" is not counting legs.
MAX_DECISIONS = 20


def normalise(raw) -> list[dict]:
    """Clean a plan's `decisions` argument into storable entries.

    Lenient about shape and strict about substance: a decision with no
    `what` is dropped rather than stored empty, because a live decision
    that says nothing is worse than an absent one — it occupies the
    record and answers no question.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, item in enumerate(raw[:MAX_DECISIONS], start=1):
        if isinstance(item, str):
            item = {"what": item}
        if not isinstance(item, dict):
            continue
        what = str(item.get("what") or "").strip()
        if not what:
            continue
        entry = {
            "id": str(item.get("id") or f"d{i}").strip(),
            "what": what[:500],
            "why": str(item.get("why") or "").strip()[:1000],
        }
        supersedes = str(item.get("supersedes") or "").strip()
        if supersedes:
            entry["supersedes"] = supersedes[:120]
        out.append(entry)
    return out


def address(cycle: int, decision_id: str) -> str:
    """The id a decision is cited by: `cycle2:d1`.

    Citable is the point. A decision referenced without an address is an
    assertion; with one, anyone can open the plan and check who approved
    it and when.
    """
    return f"cycle{cycle}:{decision_id}"


def _cycle_of(addr: str) -> int:
    """The cycle an address names, or 0 for anything unparseable.

    0 is deliberate: an address nobody can read supersedes nothing,
    because `0 < any real cycle` is the only comparison that then holds
    against a plan trying to use it.
    """
    head = str(addr or "").split(":", 1)[0]
    if head.startswith("cycle") and head[5:].isdigit():
        return int(head[5:])
    return 0


def live(plans: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split every plan's decisions into (live, superseded).

    `plans` is in cycle order, each `{cycle, decisions, approved_by,
    approved_at, hash}`. A decision is superseded when a LATER plan names
    its address in `supersedes`. Earlier plans cannot retire later ones,
    which is the only ordering rule here and the one that makes a replay
    deterministic.
    """
    superseded_by: dict[str, str] = {}
    for plan in plans:
        for d in plan.get("decisions") or []:
            target = d.get("supersedes")
            # ONLY BACKWARDS. A plan may retire a decision taken before
            # it and never one taken after — an executor writing
            # `supersedes: cycle2:d1` in cycle 1 is either confused or
            # guessing at an address that did not exist yet, and honouring
            # it would delete a decision a person approved LATER. The gate
            # for this exists because the first version had the rule in a
            # docstring and not in the code.
            if target and _cycle_of(target) < plan["cycle"]:
                superseded_by[target] = address(plan["cycle"], d["id"])

    live_out, dead_out = [], []
    for plan in plans:
        for d in plan.get("decisions") or []:
            addr = address(plan["cycle"], d["id"])
            row = {
                **d,
                "address": addr,
                "cycle": plan["cycle"],
                "approved_by": plan.get("approved_by") or "",
                "approved_at": plan.get("approved_at"),
                "plan_hash": plan.get("hash") or "",
                # An approved plan is the strongest provenance available:
                # a row, a content hash, and a person who said yes.
                "provenance": "observed" if plan.get("approved_by")
                              else "unproven",
            }
            if addr in superseded_by:
                row["superseded_by"] = superseded_by[addr]
                dead_out.append(row)
            else:
                live_out.append(row)
    return live_out, dead_out
