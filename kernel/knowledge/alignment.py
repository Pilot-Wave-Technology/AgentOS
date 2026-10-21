"""Alignment flags — GO saying, once, that work looks off the objective.

The riskiest thing in this stage, and the design is mostly about keeping
it from becoming nagging. An agent that tells people they are off track
will be wrong sometimes, and a wrong one is expensive twice: once
socially, and once because it teaches the team to ignore the next one.

Three constraints, all structural rather than advisory:

**Evidence is required and the DATABASE enforces non-empty.** GO cannot
raise a flag without naming what it is looking at. A prompt saying "cite
your evidence" is advice; a CHECK constraint is not.

**Raised once.** A partial unique index on (goal, fingerprint) WHERE
status='OPEN' means the same observation cannot be posted twice while it
stands. A flag that recurs every planning pass is one people learn to
scroll past, which costs more than never having raised it.

**It asks.** The field is `question`, not `finding` or `warning`, and
that is deliberate — the person on the other end knows things GO does
not, and the common case is that they have a reason.
"""
from __future__ import annotations

import hashlib
import json
import logging

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

MAX_EVIDENCE = 10


def fingerprint(subject_kind: str, subject_id: str, evidence: list) -> str:
    """Stable digest of what this flag is ABOUT.

    Over the subject and the evidence ids — deliberately NOT over the
    question text, so rephrasing the same observation does not slip a
    duplicate past the unique index.
    """
    ids = sorted(str(e.get("id") or "") for e in evidence if isinstance(e, dict))
    material = json.dumps([subject_kind, subject_id, ids], sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _clean(raw) -> list:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_EVIDENCE]:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            continue
        out.append({
            "kind": str(item.get("kind") or "").strip()[:40] or "reference",
            "id": str(item["id"]).strip()[:200],
            "says": str(item.get("says") or "").strip()[:500],
        })
    return out


async def raise_alignment(conn, goal_id: str, actor_id: str, *,
                          subject_kind: str, subject_id: str,
                          question: str, evidence):
    """Ask ONE question about work that looks off the objective."""
    question = (question or "").strip()
    subject_id = (subject_id or "").strip()
    subject_kind = (subject_kind or "").strip() or "story"
    ev = _clean(evidence)

    if not question:
        return Refused("E_NO_QUESTION",
                       "an alignment flag is a QUESTION — the person on the "
                       "other end usually has a reason, and a statement "
                       "gives them nothing to answer")
    if not subject_id:
        return Refused("E_NO_SUBJECT",
                       "say which story or person this is about; a flag "
                       "addressed to the goal is addressed to nobody")
    if not ev:
        return Refused(
            "E_NO_EVIDENCE",
            "name what you are looking at: a decision address "
            "(`cycle2:d1`), a story id, a plan. A flag without evidence is "
            "an opinion, and the first one of those teaches the team to "
            "skip the next ten")

    fp = fingerprint(subject_kind, subject_id, ev)
    row = await conn.fetchrow(
        """
        INSERT INTO alignment_flags
            (goal_id, subject_kind, subject_id, question, evidence,
             fingerprint, raised_by)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        ON CONFLICT (goal_id, fingerprint) WHERE status = 'OPEN'
        DO NOTHING
        RETURNING id
        """,
        goal_id, subject_kind, subject_id, question[:2000],
        json.dumps(ev), fp, actor_id or "go")

    if row is None:
        existing = await conn.fetchval(
            "SELECT id FROM alignment_flags WHERE goal_id = $1 "
            "  AND fingerprint = $2 AND status = 'OPEN'", goal_id, fp)
        logger.info("goal %s: alignment flag already open (%s)", goal_id, existing)
        return {"flag": existing, "raised": False,
                "detail": "this exact observation is already open and "
                          "waiting on an answer — say nothing further; "
                          "asking twice is how a channel gets muted"}

    logger.info("goal %s: alignment flag %s raised on %s %s",
                goal_id, row["id"], subject_kind, subject_id)
    return {"flag": row["id"], "raised": True,
            "subject": {"kind": subject_kind, "id": subject_id},
            "evidence": ev,
            "detail": "posted once. It stays open until somebody answers, "
                      "and it will not be raised again while it does"}


async def resolve_alignment(conn, goal_id: str, actor_id: str, *,
                            flag_id: int, outcome: str, note: str = ""):
    """Record what a person said, so planning stops re-asking.

    `outcome` is free text on purpose. The useful answers are things like
    "deliberate — the vendor changed" and "not deliberate, correcting it",
    and an enum would have to guess that list in advance and be wrong.
    """
    outcome = (outcome or "").strip()
    if not outcome:
        return Refused("E_NO_OUTCOME",
                       "say how it resolved; a flag closed with no outcome "
                       "is the same as one nobody answered, except that it "
                       "no longer shows")
    row = await conn.fetchrow(
        """
        UPDATE alignment_flags
           SET status = 'RESOLVED', outcome = $3, note = $4,
               resolved_by = $5, resolved_at = now()
         WHERE id = $1 AND goal_id = $2 AND status = 'OPEN'
        RETURNING id, subject_kind, subject_id
        """,
        int(flag_id), goal_id, outcome[:1000], (note or "").strip()[:2000],
        actor_id or "go")
    if row is None:
        return Refused("E_NO_SUCH_FLAG",
                       f"no OPEN alignment flag {flag_id} in this goal — it "
                       f"may already be resolved")
    logger.info("goal %s: alignment flag %s resolved", goal_id, flag_id)
    return {"flag": row["id"], "resolved": True, "outcome": outcome,
            "detail": "future planning reads this instead of raising it "
                      "again. Update the brief if it changed where things "
                      "stand"}


async def open_flags(conn, goal_id: str, limit: int = 10) -> list[dict]:
    """Open flags, for `goal_state` and for planning."""
    rows = await conn.fetch(
        "SELECT id, subject_kind, subject_id, question, evidence, created_at "
        "  FROM alignment_flags WHERE goal_id = $1 AND status = 'OPEN' "
        " ORDER BY created_at LIMIT $2", goal_id, limit)
    out = []
    for r in rows:
        ev = r["evidence"]
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except ValueError:
                ev = []
        out.append({"flag": r["id"], "about": {"kind": r["subject_kind"],
                                               "id": r["subject_id"]},
                    "asks": r["question"], "evidence": ev,
                    "since": r["created_at"]})
    return out
