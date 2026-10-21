"""Validation of RA `review_details` — free-form MARKDOWN TEXT (not JSON).

The submit_review tool POSTs {goal_id, agent_id, agent_type, review_id,
status, review_details, reason}. On status='success' we validate the
`review_details` text: it must be non-empty, under the token budget, and contain
every required section heading for that agent_type. We return a precise list of
errors so the reviewer corrects and resubmits.

The PROMPT (AGENTS.md) tells the agent how to fill each section; here we only
enforce presence of the section headings + the size cap.
"""
from __future__ import annotations

from typing import Any

# ~5000 tokens ≈ 20000 chars. Hard ceiling for a single review.
#
# NOT RAISED ALONGSIDE THE INPUT BUDGETS. A reviewer now READS up to a
# million tokens; what it WRITES is read by the convener and then by a
# person, and neither of those got bigger. A long review is not a
# thorough one -- it is one that has not decided what matters.
MAX_REVIEW_CHARS = 20000
MIN_REVIEW_CHARS = 120

# Required section headings per reviewer type (must appear in the markdown).
#
# EVERY STAGE OWNS `## Findings` AND `## Previously open`, and that is
# what makes the ledger work. Findings is where keyed findings are
# emitted; Previously open is where each open finding in scope is
# accounted for. A stage that skipped the second would leave its
# findings open for ever, because silence is deliberately not read as
# "resolved" -- so the heading is enforced here rather than requested in
# a prompt.
REQUIRED_SECTIONS: dict[str, list[str]] = {
    # Is it moving? Arithmetic comes pre-computed in PROGRESS_DELTA.md;
    # this stage says which of it matters.
    "progress": [
        "## Window",
        "## Movement",
        "## Stalled",
        "## Ready but not started",
        "## Findings",
        "## Previously open",
    ],
    # Asked against delivered, side by side. FLAGGED, NOT ADJUDICATED --
    # a summary is where an anomaly gets smoothed away, and "a story did
    # the wrong thing" has to survive to goal_health.
    "story": [
        "## Roster",
        "## Done",
        "## In progress",
        "## Pending",
        "## Blocked",
        "## Mismatches",
        "## Findings",
        "## Previously open",
    ],
    # Is this the right work? `## Objective update` carries PROPOSED
    # REPLACEMENT TEXT or the word none -- "the objective has drifted" is
    # an observation nobody can act on.
    "goal_health": [
        "## Objective vs. reality",
        "## Work that doesn't fit",
        "## Went wrong",
        "## Objective update",
        "## Findings",
        "## Previously open",
    ],
    # The packet. `## New this cycle` and `## Escalating` are separate
    # headings because the difference between them is the whole point of
    # reviewing the same goal every twelve hours.
    "convener": [
        "## Headline",
        "## State of the goal",
        "## New this cycle",
        "## Escalating",
        "## Recommended actions for GO",
        "## Open questions",
    ],
}


def validate_review(agent_type: str, review_details: Any) -> list[str]:
    """Return a list of correction messages (empty = valid)."""
    sections = REQUIRED_SECTIONS.get(agent_type)
    if sections is None:
        return [f"Unknown agent_type {agent_type!r}; expected one of {sorted(REQUIRED_SECTIONS)}."]

    if not isinstance(review_details, str) or not review_details.strip():
        return ["review_details must be a non-empty markdown string."]

    errors: list[str] = []
    text = review_details
    low = text.lower()

    if len(text) > MAX_REVIEW_CHARS:
        errors.append(
            f"review_details is too long ({len(text)} chars). Keep it under ~5000 tokens "
            f"(~{MAX_REVIEW_CHARS} chars) — tighten it, don't drop sections."
        )
    if len(text.strip()) < MIN_REVIEW_CHARS:
        errors.append("review_details is too short — fill every required section with real content.")

    for heading in sections:
        if heading.lower() not in low:
            errors.append(f"missing required section heading '{heading}' (use it verbatim).")

    return errors
