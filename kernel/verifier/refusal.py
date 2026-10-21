"""F2b — a refusal, and how it reaches the leg that has to act on it.

Two properties the plan makes requirements rather than refinements:

    a refusal must be RESUMABLE
    a refusal must be VISIBLE to the next leg

Miss the second and the model claims the identical thing again — and the
E5 fence then refuses the re-claim by zero rowcount, so the run is stuck
twice over: once because it does not know what was wrong, and again
because the mechanism that would have told it now blocks it.

NEVER IN `next step`. R4 makes `next step` the model's own unverified
field; a refusal is the harness's verified fact. Putting one there mixes
what was measured with what was guessed, in the one place the next leg
trusts most.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# How much of a check's output travels into the next leg's prompt. A
# refusal that pastes 200KB of pytest output is a refusal that costs the
# leg its context — the orientation floor E5c measures, spent on
# something the model cannot act on past the first screen.
LOCATOR_CHARS = 1200
MAX_ENTRIES = 8


def build(results) -> list[dict]:
    """Typed codes with bounded locators, from criterion outcomes.

    `fail` ONLY. A `flake` or an `error` did not find the work wrong, and
    a refusal permanently blocks re-claiming — so putting either here
    would kill a story for a rate limit or a container that would not
    start.
    """
    entries = []
    for criterion, outcome in results:
        if not outcome.refuses:
            continue
        detail = outcome.detail or {}
        # The OUTCOME's own code wins when it has one. Derived from the
        # criterion kind unconditionally, a check-surface refusal came
        # out as CRITERION_COMMAND_FAILED with "check surface hash" as
        # the command and no output — and a real story answered it with
        # "all tests pass and the code changes are correct, I cannot
        # understand" and asked a human. It was right: nothing in that
        # refusal named what had actually happened.
        code = detail.get("code") or (
            f"CRITERION_{str(criterion.get('kind', '?')).upper()}_FAILED")
        entry = {
            "code": code,
            "criterion_id": criterion.get("id"),
            "locator": (detail.get("command") or "")[:200],
            "exit_code": detail.get("exit_code"),
            "output": (detail.get("output") or "")[-LOCATOR_CHARS:],
        }
        if not entry["output"] and detail.get("reason"):
            # A refusal with no output has to say something. `reason` is
            # what the non-command refusals carry.
            entry["output"] = str(detail["reason"])[:LOCATOR_CHARS]
        entries.append(entry)
    if len(entries) > MAX_ENTRIES:
        kept, dropped = entries[:MAX_ENTRIES], len(entries) - MAX_ENTRIES
        kept.append({"code": "REFUSALS_TRUNCATED", "criterion_id": None,
                     "locator": f"{dropped} further criterion(s) also failed",
                     "exit_code": None, "output": ""})
        # Said out loud, not silently dropped: a bounded list that does
        # not admit it is bounded reads as a complete one.
        logger.info("refusal truncated: %d of %d entries kept",
                    MAX_ENTRIES, len(entries))
        return kept
    return entries


def format_for_prompt(verdict: dict) -> str:
    """Render a refusal for the leg that has to answer it."""
    entries = verdict.get("refusal") or []
    lines = [
        "## Your completion claim was refused",
        "",
        "This is the harness's verified result, not a suggestion. The "
        "checks below were fixed in the contract before the work began, "
        "and they were run in a fresh container against the committed "
        "tree.",
        "",
    ]
    for i, e in enumerate(entries, 1):
        lines.append(f"{i}. **{e.get('code')}**")
        if e.get("code") == "CHECK_SURFACE_CHANGED":
            # This one needs explaining or it reads as a broken check.
            # The model has done nothing wrong in the code and every
            # test may pass; what changed is a file the CONTRACT counts
            # as part of the checks.
            lines.append(
                "   Your code may be correct and the tests may all pass. "
                "That is not what this says. A file the contract counts "
                "as part of the CHECKS has changed since the contract "
                "was approved, so the approval no longer covers what is "
                "being checked.")
            lines.append(
                "   If you edited a test, a conftest or a build file on "
                "purpose — because the task asked for it — this contract "
                "cannot judge that work and a human has to re-approve "
                "it. Say so with ask_human; do not try to put the file "
                "back, and do not re-claim.")
        if e.get("locator"):
            lines.append(f"   check: `{e['locator']}`")
        if e.get("exit_code") is not None:
            lines.append(f"   exit: {e['exit_code']}")
        if e.get("output"):
            body = e["output"].strip()
            lines.append("   output:")
            lines.extend(f"     {ln}" for ln in body.splitlines()[-30:])
        lines.append("")
    lines.append(
        "Fix what these say and claim again. A claim carrying the SAME "
        "reason set is refused by the database before it reaches anyone "
        "— so if you cannot make a check pass, say why with ask_human "
        "rather than re-claiming.")
    return "\n".join(lines)


async def latest_refusal(conn, execution_id: int, generation: int):
    """The most recent refusal from a PRIOR generation, or None.

    Scoped to prior generations for the same reason `answered_since` is:
    a refusal of the leg you are in would mean the leg never ended.
    """
    row = await conn.fetchrow(
        """
        SELECT id, claim_seq, refusal::text, generation, created_at
        FROM verdicts
        WHERE execution_id = $1 AND passed = false AND generation < $2
        ORDER BY id DESC LIMIT 1
        """,
        execution_id, generation)
    if row is None:
        return None
    return {"id": row["id"], "claim_seq": row["claim_seq"],
            "refusal": json.loads(row["refusal"]),
            "generation": row["generation"]}
