"""Reading one stage's findings into the next stage's inputs.

A FAILED UPSTREAM SAYS SO. This is the whole module, and it exists
because the alternative has already cost a review: when an input renders
empty, a reviewer reads it as "there was nothing" rather than "this could
not be read", and reports confidently on an absence that was really a
gap. The gateway story reviewer judged a description against a plan it
never received for exactly this reason.

So a missing or failed stage never produces an empty document. It
produces a document that states what failed and instructs the reader to
treat it as a coverage gap -- which is itself something the convener must
report to the orchestrator.

NOTHING BLOCKS. Every consumer here runs whether or not its upstream
succeeded; that is why these return prose rather than raising.
"""

from __future__ import annotations

import json
import logging

from app.database import get_pool

logger = logging.getLogger(__name__)

_LABEL = {
    "goal_health": "Goal health",
    "progress": "Progress",
    "story": "Story state",
    "convener": "Convener",
}


def as_text(value) -> str:
    """Stage output is a JSONB column holding the review markdown."""
    if value is None:
        return ""
    if isinstance(value, str):
        # A JSON-encoded string arrives double-quoted; a bare one does not.
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        return decoded if isinstance(decoded, str) else value
    return str(value)


async def of(run_id: str, stage: str) -> str:
    """The stage's review text, or a document saying why there isn't one."""
    label = _LABEL.get(stage, stage)
    pool = get_pool()
    try:
        row = await pool.fetchrow(
            "SELECT status, output, error FROM council_review_stages "
            "WHERE run_id = $1 AND stage = $2", run_id, stage)
    except Exception as exc:                                # noqa: BLE001
        logger.exception("council: reading %s of run %s", stage, run_id)
        return _gap(label, "could not be read", str(exc))

    if row is None:
        return _gap(label, "did not run", "no stage row exists for this run")

    text = as_text(row["output"]).strip()
    if row["status"] == "done" and text:
        return text

    return _gap(label, f"status `{row['status']}`",
                (row["error"] or "").strip() or "no reason was recorded")


def _gap(label: str, what: str, why: str) -> str:
    """A coverage gap, written so it cannot be mistaken for a clean result."""
    return (
        f"# {label} — NOT AVAILABLE\n\n"
        f"This reviewer {what}: {why}\n\n"
        f"**Treat this as a coverage gap, not as an absence of problems.** "
        f"Nothing looked at what this stage covers, so you have no evidence "
        f"either way. Say so in your report and name the stage — a review "
        f"that quietly omits a failed reviewer reads as a complete review "
        f"that happened to be quiet, and quiet is the wrong conclusion.\n")


async def failed_stages(run_id: str) -> list[str]:
    """Which stages of this run did not finish cleanly, for the packet."""
    pool = get_pool()
    try:
        rows = await pool.fetch(
            "SELECT stage FROM council_review_stages "
            "WHERE run_id = $1 AND status <> 'done' ORDER BY stage", run_id)
    except Exception:                                       # noqa: BLE001
        logger.exception("council: failed-stage scan for run %s", run_id)
        return []
    return [r["stage"] for r in rows]
