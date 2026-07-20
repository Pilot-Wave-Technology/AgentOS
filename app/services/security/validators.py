"""Validation of Security Council `review_details` — free-form MARKDOWN TEXT.

The submit_security_review tool POSTs {run_id, agent_id, aspect, review_id,
status, review_details, findings, reason}. On status='success' we validate the
`review_details` text: non-empty, under the token budget, and containing every
required section heading for that aspect. We return a precise list of errors so
the agent corrects and resubmits.

The PROMPT (AGENTS.md) tells the agent how to fill each section; here we only
enforce presence of the section headings + the size cap. The structured
`findings` array is validated/normalized separately (see normalize_findings).
"""
from __future__ import annotations

from typing import Any

# Aspect reports can be a bit longer than goal-council reviews (they enumerate
# concrete findings), but still bounded so one report can't blow a thread.
MAX_REVIEW_CHARS = 24000
MIN_REVIEW_CHARS = 120

# Required section headings per aspect (must appear, verbatim, in the markdown).
REQUIRED_SECTIONS: dict[str, list[str]] = {
    "secrets": [
        "## Summary",
        "## Scope Scanned",
        "## Findings",
        "## Notable Non-Issues",
        "## Recommendations",
    ],
    "sast": [
        "## Summary",
        "## Scope Scanned",
        "## Findings",
        "## Notable Non-Issues",
        "## Recommendations",
    ],
    # P1 aspects — kept here so enabling them needs no validator change.
    "sca": [
        "## Summary",
        "## Scope Scanned",
        "## Findings",
        "## Notable Non-Issues",
        "## Recommendations",
    ],
    "access": [
        "## Summary",
        "## Scope Scanned",
        "## Findings",
        "## Notable Non-Issues",
        "## Recommendations",
    ],
    "infra": [
        "## Summary",
        "## Scope Scanned",
        "## Findings",
        "## Notable Non-Issues",
        "## Recommendations",
    ],
    "convener": [
        "## Executive Summary",
        "## Risk Posture",
        "## Top Findings",
        "## Findings by Aspect",
        "## Remediation Plan",
        "## Coverage",
    ],
}

_VALID_SEVERITY = {"critical", "high", "medium", "low", "info"}


def validate_review(aspect: str, review_details: Any) -> list[str]:
    """Return a list of correction messages (empty = valid)."""
    sections = REQUIRED_SECTIONS.get(aspect)
    if sections is None:
        return [f"Unknown aspect {aspect!r}; expected one of {sorted(REQUIRED_SECTIONS)}."]

    if not isinstance(review_details, str) or not review_details.strip():
        return ["review_details must be a non-empty markdown string."]

    errors: list[str] = []
    text = review_details
    low = text.lower()

    if len(text) > MAX_REVIEW_CHARS:
        errors.append(
            f"review_details is too long ({len(text)} chars). Keep it under "
            f"~{MAX_REVIEW_CHARS} chars — tighten it, don't drop sections."
        )
    if len(text.strip()) < MIN_REVIEW_CHARS:
        errors.append("review_details is too short — fill every required section with real content.")

    for heading in sections:
        if heading.lower() not in low:
            errors.append(f"missing required section heading '{heading}' (use it verbatim).")

    return errors


def normalize_findings(aspect: str, findings: Any) -> tuple[list[dict], list[str]]:
    """Coerce the submitted `findings` array into clean rows for security_findings.

    Returns (rows, errors). A non-list or malformed entries are reported as
    errors (so the agent fixes them); individual fields are normalized/clamped.
    The convener submits an empty list — that is valid.
    """
    if findings is None:
        return [], []
    if not isinstance(findings, list):
        return [], ["findings must be an array of objects (or omitted/empty)."]

    rows: list[dict] = []
    errors: list[str] = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            errors.append(f"findings[{i}] must be an object.")
            continue
        sev = str(f.get("severity") or "info").strip().lower()
        if sev not in _VALID_SEVERITY:
            sev = "info"
        line_val = f.get("line")
        try:
            line = int(line_val) if line_val not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        rows.append({
            "severity": sev,
            "file": str(f.get("file") or "")[:512],
            "line": line,
            "rule": str(f.get("rule") or "")[:256],
            "tool": str(f.get("tool") or "")[:128],
            "title": str(f.get("title") or "")[:256],
            "description": str(f.get("description") or "")[:4000],
        })
    return rows, errors
