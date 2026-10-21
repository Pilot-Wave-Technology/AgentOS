"""F11 — when the bar might be wrong, tell a person without being asked.

Traced end to end, a too-strict criterion already terminates. The
refusal reaches the next leg's prompt, and a second claim carrying the
same reason set is refused BY THE DATABASE — `reason_hash` is taken over
`(code, locator)` and not over the output, so a check with noisy output
still hashes the same and is still blocked. There is no infinite loop.

What there is instead: **the only route from a wrong bar to a person
runs through the model's judgement.** The refusal prompt asks it to call
`ask_human`. Nothing makes it. If it flails, or edits a different file so
the reason set shifts and the fence lets a claim through, the story
stalls and nobody is told. Nothing anywhere counts refusals.

That is the fourth time in this project that a path has existed with
nothing to invoke it, after `adjudicate_pass`, `admit_pending` and
`reconcile_tick`. The rule the pattern earns: *an escape hatch with no
automatic trigger is a design, not a mechanism.*

So the harness counts, and parks by itself.

TWICE, NOT ONCE. One refusal is the system working — the model claimed
early, the check caught it, it goes back and fixes it. Twice on the SAME
criterion means the second attempt was made in full knowledge of the
first refusal and still could not satisfy it, which is the point where
"the work is wrong" stops being the likeliest explanation.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

REFUSALS_BEFORE_ASKING = 2


def _codes(refusal) -> frozenset:
    """The identity of a refusal: which checks failed, not what they said."""
    return frozenset(
        (e.get("code", ""), e.get("locator", "")) for e in (refusal or []))


async def repeated_refusal(conn, execution_id: int) -> dict | None:
    """Has the same criterion now refused this story twice running?

    Compared on the reason SET, the same identity the E5 fence uses — so
    a story making real progress (fixing two of three checks) is not
    caught, because its reason set changed.
    """
    rows = await conn.fetch(
        "SELECT id, claim_seq, refusal::text FROM verdicts "
        "WHERE execution_id = $1 AND passed = false "
        "ORDER BY id DESC LIMIT $2", execution_id, REFUSALS_BEFORE_ASKING)
    if len(rows) < REFUSALS_BEFORE_ASKING:
        return None
    sets = [_codes(json.loads(r["refusal"])) for r in rows]
    if not sets[0] or any(s != sets[0] for s in sets[1:]):
        return None
    return {"count": len(rows), "codes": sorted(c for c, _ in sets[0]),
            "checks": sorted(loc for _, loc in sets[0] if loc),
            "claims": [r["claim_seq"] for r in rows]}


def question_for(repeat: dict) -> str:
    """What the person is actually being asked. Not "it failed again"."""
    checks = "\n".join(f"  - {c}" for c in repeat["checks"]) or "  (unnamed)"
    return (
        f"This story has now been refused {repeat['count']} times running "
        f"on the same check(s):\n\n{checks}\n\n"
        f"The second attempt was made knowing the first refusal and still "
        f"could not satisfy it, so the check may be the problem rather "
        f"than the work.\n\n"
        f"Three things you can do, and the harness is not choosing between "
        f"them:\n"
        f"  - amend the check, if it is asking for the wrong thing\n"
        f"  - accept the story over the Verifier, if the work is right and "
        f"the check cannot be satisfied here\n"
        f"  - tell the executor what it is missing, and it carries on")


async def park_for_help(conn, execution_id: int, generation: int,
                        repeat: dict) -> int | None:
    """Park the story and ask. Returns the question id, or None.

    None when the story is already waiting on something — one open
    request per execution is a database constraint, and a story that has
    already asked for help does not need the harness to ask again.
    """
    from kernel.mailbox.api import ask_question

    # `escalation`, not `clarification`. The evidence floor on a
    # clarification requires three distinct attempts by the EXECUTOR, and
    # the harness has made none — it measured a pattern. Sent as a
    # clarification this was refused by the floor and `park_for_help`
    # returned None every time, which is the auto-park silently never
    # happening: the exact shape of bug this whole increment exists to
    # remove, reproduced inside the fix for it.
    result = await ask_question(
        conn, execution_id, "escalation", question_for(repeat),
        {"question": question_for(repeat), "raised_by": "harness",
         "refusals": repeat["count"], "checks": repeat["checks"]},
        generation=generation,
        content_hash=f"stuck-{execution_id}-{'|'.join(repeat['checks'])}")
    if not isinstance(result, int):
        logger.info("execution %s: could not raise a stuck question (%s)",
                    execution_id, getattr(result, "code", result))
        return None
    await conn.execute(
        "UPDATE executions SET state = 'PARKED_HOT' WHERE id = $1 "
        "AND state = 'RUNNING'", execution_id)
    logger.warning("execution %s parked: refused %d times on %s",
                   execution_id, repeat["count"], repeat["checks"])
    return result
