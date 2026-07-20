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
MAX_REVIEW_CHARS = 20000
MIN_REVIEW_CHARS = 120

# Required section headings per reviewer type (must appear in the markdown).
REQUIRED_SECTIONS: dict[str, list[str]] = {
    "goal_health": [
        "## Alignment",
        "## Objective & Scope",
        "## Strengths",
        "## Findings",
        "## What Could Be Better",
        "## Recommendations",
    ],
    "progress": [
        "## Window",
        "## Goal Progress",
        "## Stuck (Long-Running)",
        "## Story Progress",
        "## Reconciliation",
        "## Recommended GO Actions",
    ],
    "convener": [
        "## Headline",
        "## State of the Goal",
        "## Top Issues",
        "## Recommended Actions for GO",
        "## Open Questions",
    ],
    "story": [
        "## Verdict",
        "## Scope & Outcome",
        "## Cycle History",
        "## Decisions Review",
        "## Quality",
        "## Issues",
        "## Recommendations",
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
