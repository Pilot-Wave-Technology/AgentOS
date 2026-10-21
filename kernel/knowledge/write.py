"""Writing knowledge: findings mid-flight, and the cycle entry at the end.

Two writers and no third. Decisions are NOT written here — they are
entries in an approved plan (`kernel/knowledge/decisions.py`), and adding
a way to author one separately would put a model's recollection beside a
document a person signed, in the same list, looking the same.

WHAT "A STORY THEY HAVE WORKED ON" MEANS HERE: a story this assistant
holds a THREAD for. The assistant's own name is enough to answer that,
and since Stage K `threads.agent_id` says whose thread it is. See
`kernel/knowledge/ownership.py` — including why the actor header had to
start naming the caller before any of this could be checked.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from kernel.assistant.context import Refused

logger = logging.getLogger(__name__)

MAX_FINDINGS_PER_CYCLE = 30


async def _current_cycle(conn, story_id: str) -> int:
    """How many plans this story has registered. 0 before the first."""
    from kernel.knowledge.read import PLAN_KIND
    return int(await conn.fetchval(
        f"""
        SELECT count(*) FROM questions q
          JOIN executions e ON e.id = q.execution_id
         WHERE e.story_id = $1 AND q.kind = '{PLAN_KIND}'
        """, story_id) or 0)


def _merge(entries: list, cycle: int, patch: dict, actor: str) -> list:
    """Fold `patch` into the entry for `cycle`, creating it if absent.

    Lists ACCUMULATE rather than replace. A second finding recorded at leg
    5 must not erase the one from leg 3 — the whole value of the record is
    that it survives the turn that wrote it.
    """
    now = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        if entry.get("cycle") == cycle:
            break
    else:
        entry = {"cycle": cycle, "techniques": [], "learned": [],
                 "abandoned": [], "evidence": {}, "provenance": {},
                 "written_by": actor, "at": now}
        entries.append(entry)

    for key in ("techniques", "learned", "abandoned"):
        incoming = patch.get(key)
        if not incoming:
            continue
        have = entry.get(key) or []
        for item in incoming:
            if item not in have:
                have.append(item)
        entry[key] = have[:MAX_FINDINGS_PER_CYCLE]
        # Per FIELD, not per entry: the evidence half is a join over rows
        # that already exist and the model's half is a statement, and a
        # planner has to be able to tell them apart inside one object.
        entry.setdefault("provenance", {})[key] = patch.get(
            "_provenance", "unproven")

    if patch.get("evidence"):
        entry["evidence"] = patch["evidence"]
        entry.setdefault("provenance", {})["evidence"] = "observed"

    entry["at"] = now
    return entries


async def _apply(conn, story_id: str, cycle: int, patch: dict, actor: str):
    """Read-modify-write `stories.knowledge`, inside a row lock.

    `SELECT … FOR UPDATE` because two legs of one run can record findings
    seconds apart and a lost update here loses the fact, silently, which
    is the failure this record exists to prevent.
    """
    async with conn.transaction():
        raw = await conn.fetchval(
            "SELECT COALESCE(knowledge, '[]'::jsonb) FROM stories "
            " WHERE id = $1 FOR UPDATE", story_id)
        entries = json.loads(raw) if isinstance(raw, str) else (raw or [])
        entries = _merge(list(entries), cycle, patch, actor)
        await conn.execute(
            "UPDATE stories SET knowledge = $2::jsonb, updated_at = now() "
            " WHERE id = $1", story_id, json.dumps(entries))
    return entries


async def record_finding(conn, goal_id: str, actor_id: str, story: str,
                         fact: str, why_it_matters: str = ""):
    """A constraint or surprise found mid-flight, against a live story.

    The completion report carries what the executor knew at the end.
    This is for what is learned at leg 3 and would otherwise have to
    survive in a conversation until then.
    """
    fact = (fact or "").strip()
    if not fact:
        return Refused("E_NO_FACT",
                       "a finding needs the fact in one line — what is TRUE "
                       "that nobody knew when the plan was written")
    if not (story or "").strip():
        return Refused("E_NO_STORY",
                       "a finding is recorded against a story; that is what "
                       "makes it findable again by what the story touched")
    from kernel.knowledge.ownership import may_write
    allowed, why_not = await may_write(conn, goal_id, actor_id, story)
    if not allowed:
        return Refused("E_NOT_YOUR_STORY", why_not)

    cycle = max(1, await _current_cycle(conn, story))
    await _apply(conn, story, cycle,
                 {"learned": [{"fact": fact[:500],
                               "why_it_matters": (why_it_matters or "").strip()[:500]}],
                  "_provenance": "unproven"},
                 actor_id)
    logger.info("goal %s: finding recorded on story %s cycle %s",
                goal_id, story, cycle)
    return {"story": story, "cycle": cycle, "recorded": fact[:120],
            "provenance": "unproven",
            "detail": "readable from goal_knowledge, and findable by what "
                      "this story touches"}


async def record_cycle(conn, goal_id: str, story: str, cycle: int,
                       *, techniques=None, learned=None, abandoned=None,
                       evidence=None, actor: str = "executor"):
    """The completion entry: what the executor knew, plus derived evidence.

    Called from the report path, not by a model directly. `evidence` is
    the derived half and arrives already computed — see
    `app/services/story_knowledge.py`.
    """
    patch: dict = {"_provenance": "unproven"}
    if techniques:
        patch["techniques"] = [str(t).strip()[:80] for t in techniques if str(t).strip()]
    if learned:
        patch["learned"] = [l for l in learned if isinstance(l, dict) and l.get("fact")]
    if abandoned:
        patch["abandoned"] = [a for a in abandoned if isinstance(a, dict) and a.get("tried")]
    if evidence:
        patch["evidence"] = evidence
    if len(patch) == 1:
        return None
    return await _apply(conn, story, max(1, cycle), patch, actor)
