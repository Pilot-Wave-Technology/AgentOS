"""Reading findings back out of a reviewer's markdown.

TOLERANT ON THE WAY IN, STRICT ON THE KEY. Everything about a finding can
be rephrased between cycles and it still matches, because matching is on
the key alone. So the parser fights for the key and shrugs at the rest:
the separator may be an em-dash, an en-dash or a hyphen, the severity may
be absent, the scope may be spelled `story: ABC` or `story:ABC`. What it
will not do is invent a key, because a finding without one cannot dedup
and would re-raise itself every twelve hours -- exactly the failure the
ledger exists to prevent.

THE FORMAT THE PROMPTS ASK FOR:

    - [some-kebab-key] (high, story:AB12) what was found -- why it matters
    - [another-key] (medium, goal) what was found -- why it matters

and, under `## Previously open`, one line per open finding in scope:

    - [some-kebab-key] still open -- nothing has changed
    - [another-key] resolved -- the story landed on cycle 14

A LINE THAT DOES NOT PARSE IS DROPPED, NOT GUESSED AT. A half-understood
finding written into the ledger under a mangled key is worse than a
missing one: it never matches again, so it reappears as new every cycle
and the count that makes escalation meaningful becomes noise.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# `- [key] (severity, scope) title -- why`, with everything after the key
# optional. Severity and scope are read if present and defaulted if not.
_FINDING = re.compile(
    r"^\s*[-*]\s*\[(?P<key>[a-z0-9][a-z0-9-]{0,78})\]\s*"
    r"(?:\((?P<meta>[^)]*)\)\s*)?"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)

# The three answers a reviewer may give about an open finding. Ordered
# longest-first so `no longer applies` is not shadowed by a prefix match.
_STATUSES = (
    ("no longer applies", "wont_fix"),
    ("still open", "open"),
    ("resolved", "resolved"),
    ("wont fix", "wont_fix"),
)

_SEVERITIES = ("high", "medium", "low")

# One of these splits the claim from its reason. Models reach for all
# three and the choice carries no meaning.
_SPLIT = re.compile(r"\s+(?:--|—|–)\s+|\s+-\s+")


def _section(text: str, heading: str) -> list[str]:
    """The lines under one `## heading`, up to the next heading of any level."""
    out: list[str] = []
    want = heading.strip().lower()
    inside = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("#"):
            inside = line.lstrip("# ").strip().lower().startswith(want)
            continue
        if inside and line.strip():
            out.append(line)
    return out


def _meta(meta: str) -> tuple[str, str, str]:
    """`(severity, scope, story_id)` from the parenthesised part."""
    severity, scope, story = "medium", "goal", ""
    for part in (meta or "").split(","):
        token = part.strip().lower()
        if token in _SEVERITIES:
            severity = token
        elif token.startswith("story"):
            # `story:AB12`, `story: AB12`, or a bare `story` with the id
            # elsewhere -- the last of which we cannot use, so it stays a
            # goal-scoped finding rather than a story one pointing nowhere.
            _, _, sid = token.partition(":")
            if sid.strip():
                scope, story = "story", sid.strip().upper()
        elif token == "goal":
            scope = "goal"
    return severity, scope, story


def findings(review_text: str, raised_by: str) -> list[dict]:
    """Every parseable finding under `## Findings`."""
    out: list[dict] = []
    seen: set[str] = set()
    for line in _section(review_text, "findings"):
        m = _FINDING.match(line)
        if not m:
            continue
        key = m.group("key").strip().lower()
        rest = (m.group("rest") or "").strip()
        if not rest or key in seen:
            # A repeated key inside ONE review is the reviewer saying the
            # same thing twice, not two findings. First wins.
            continue
        seen.add(key)
        severity, scope, story = _meta(m.group("meta") or "")
        halves = _SPLIT.split(rest, maxsplit=1)
        out.append({
            "key": key,
            "title": halves[0].strip()[:400],
            "body": (halves[1].strip() if len(halves) > 1 else "")[:4000],
            "severity": severity,
            "scope": scope,
            "story_id": story or None,
            "raised_by": raised_by,
        })
    return out


def accounted(review_text: str) -> dict[str, tuple[str, str]]:
    """`{key: (status, detail)}` from `## Previously open`.

    A key absent from this mapping was NOT accounted for, and the caller
    must leave it open. That is the whole contract: silence is "the
    reviewer did not look", which is not "fixed".
    """
    out: dict[str, tuple[str, str]] = {}
    for line in _section(review_text, "previously open"):
        m = _FINDING.match(line)
        if not m:
            continue
        key = m.group("key").strip().lower()
        rest = ((m.group("meta") or "") + " " + (m.group("rest") or "")).lower()
        for phrase, status in _STATUSES:
            if phrase in rest:
                detail = (m.group("rest") or "").strip()
                out[key] = (status, detail[:1000])
                break
    return out
