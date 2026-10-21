"""`goal_knowledge` — what was decided and learned, for whoever asks.

Pairs with `goal_state`: **state is what is happening; knowledge is what
was decided and learned.** One is the board, the other is why the board
looks like that.

Nothing in here writes. Almost nothing in here is stored by this stage
either — the decisions are approved plans, which `register_plan` has
been storing all along, and the evidence is a join over rows that exist.
What was missing was never a writer. It was this.

A QUERY, NEVER A DUMP. The record grows for the life of the goal, and a
tool that returned all of it would be unusable in exactly the goals
where it matters most.

RETRIEVAL IS STRUCTURAL FIRST. `about` resolves against the ids the
evidence already carries — a resource, a path — so "what do we know
about the vendor integration" is answered by findings from stories that
touched that resource, exactly. Prose search is the fallback for what
structure misses ("throttling" finding "rate-limits"). Neither needs a
tag vocabulary, which is the trap in both alternatives: a closed list is
permanent maintenance and always wrong for the next domain, and open
tags written by models converge on near-synonyms that split the record
in two without anyone noticing.
"""
from __future__ import annotations

import logging

from kernel.knowledge.decisions import live as split_live

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


# A plan park is stored in `questions` with kind 'authorization'.
#
# NOT `human_requests`, and not kind 'plan_approval'. The E6h migration
# created `human_requests` and the code has since moved to `questions`;
# the table the migration names does not exist in the database the stack
# runs against. `park.py` maps the CALLER's word to the STORED word
# (`_KIND = {"plan_approval": "authorization"}`) and maps it back on the
# way out, so both names are correct in their own layer and neither is
# the one to put in a query.
#
# Read off a migration file rather than off the code, this was wrong in
# two ways at once and failed with `relation "human_requests" does not
# exist` the first time the surface was called. `test_stage_l_knowledge`
# now asserts this constant against `park._KIND` so the pair cannot part.
PLAN_KIND = "authorization"


async def plans_for(conn, story_id: str) -> list[dict]:
    """Every plan registered for a story, in cycle order, with its verdict.

    Cycle is the ordinal of the plan park within the run, not `generation`:
    a run can burn several legs without registering a plan, and somebody
    counting "the second plan" is not counting legs.
    """
    rows = await conn.fetch(
        f"""
        SELECT q.id, q.payload, q.content_hash, q.status, q.declined,
               q.verdict, q.answered_by, q.answered_at, q.created_at
          FROM questions q
          JOIN executions e ON e.id = q.execution_id
         WHERE e.story_id = $1 AND q.kind = '{PLAN_KIND}'
         ORDER BY q.id
        """,
        story_id)
    out = []
    for cycle, r in enumerate(rows, start=1):
        payload = r["payload"] or {}
        if isinstance(payload, str):
            import json
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        # APPROVED is narrower than ANSWERED, and the difference is the
        # whole value of this record. `park.plan_is_approved` is the
        # authority and this matches it exactly: declined is answered and
        # is not permission, and `revise` is answered and is not
        # permission either — the person asked for a different plan, and
        # reporting its decisions as settled would cite something nobody
        # agreed to.
        approved = (
            r["status"] == "ANSWERED"
            and not r["declined"]
            and (r["verdict"] or "approve") == "approve"
        )
        out.append({
            "cycle": cycle,
            "request_id": r["id"],
            "rationale": payload.get("rationale") or "",
            "steps": payload.get("steps") or [],
            "decisions": payload.get("decisions") or [],
            "hash": r["content_hash"] or "",
            "status": r["status"],
            "verdict": r["verdict"] or ("declined" if r["declined"] else ""),
            "approved_by": (r["answered_by"] or "") if approved else "",
            "approved_at": r["answered_at"] if approved else None,
        })
    return out


async def knowledge_for(conn, goal_id: str, story_ids: list[str]) -> dict:
    """`stories.knowledge` for these stories, keyed by story id."""
    if not story_ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, title, knowledge FROM stories "
        " WHERE goal_id = $1 AND id = ANY($2::text[]) "
        "   AND jsonb_array_length(COALESCE(knowledge, '[]'::jsonb)) > 0",
        goal_id, story_ids)
    out = {}
    for r in rows:
        entries = r["knowledge"]
        if isinstance(entries, str):
            import json
            try:
                entries = json.loads(entries)
            except ValueError:
                entries = []
        out[r["id"]] = {"title": r["title"], "cycles": entries or []}
    return out


async def _stories_touching(conn, goal_id: str, about: str) -> list[str]:
    """Stories whose recorded evidence names this resource id or path.

    The structural half of retrieval. `about` is matched against the
    resources a story was granted AND against the paths its work
    actually changed, because a person asking "what do we know about the
    vendor integration" may name either.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT s.id
          FROM stories s
     LEFT JOIN story_resources sr ON sr.story_id = s.id
     LEFT JOIN goal_resources  gr ON gr.id = sr.resource_id
         WHERE s.goal_id = $1
           AND ( gr.id = $2
              OR gr.name ILIKE '%' || $2 || '%'
              OR gr.repo_name ILIKE '%' || $2 || '%'
              OR s.knowledge::text ILIKE '%' || $2 || '%' )
        """,
        goal_id, about)
    return [r["id"] for r in rows]


async def goal_knowledge(conn, goal_id: str, story: str = "",
                         about: str = "", cycle: int = 0,
                         limit: int = DEFAULT_LIMIT) -> dict:
    """The read. See the module docstring for what it is and is not."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    if story:
        story_ids = [story]
        scope = f"story {story}"
    elif about:
        story_ids = (await _stories_touching(conn, goal_id, about))[:limit]
        scope = f"anything touching {about!r}"
    else:
        rows = await conn.fetch(
            "SELECT id FROM stories WHERE goal_id = $1 "
            " ORDER BY updated_at DESC NULLS LAST LIMIT $2", goal_id, limit)
        story_ids = [r["id"] for r in rows]
        scope = "the whole goal, most recent first"

    per_story = []
    for sid in story_ids:
        plans = await plans_for(conn, sid)
        if cycle:
            plans = [p for p in plans if p["cycle"] == cycle]
        live, superseded = split_live(plans)
        if not (live or superseded or plans):
            continue
        per_story.append({
            "story": sid,
            "cycles": len(plans),
            "decisions": live,
            # Kept, not hidden: "why did we stop doing X" is the question
            # people actually ask, and it is unanswerable from the live
            # set alone.
            "superseded": superseded,
        })

    learned = await knowledge_for(conn, goal_id, story_ids)

    goal_level = await conn.fetch(
        """SELECT id, decision, rationale, decided_by, story_id, created_at
             FROM goal_decisions
            WHERE goal_id = $1 AND retired_at IS NULL
            ORDER BY created_at DESC LIMIT $2""",
        goal_id, limit)

    return {
        "goal": goal_id,
        "scope": scope,
        "stories": per_story,
        "learned": learned,
        "goal_decisions": [
            {"id": d["id"], "decision": d["decision"],
             "rationale": d["rationale"], "by": d["decided_by"],
             "story": d["story_id"] or None, "at": d["created_at"],
             "provenance": "observed"}
            for d in goal_level],
        "detail": (
            "Decisions come from APPROVED PLANS — cite one by its address "
            "(`cycle2:d1`) and anyone can open the plan and see who "
            "approved it. A decision no later plan supersedes is still "
            "live, however many cycles ago it was taken."),
    }
