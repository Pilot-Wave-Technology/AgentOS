"""Ec2g — "what did that errand produce", answered without reading bytes.

`read_artifact` has existed since H8 and needs an ID. An id comes from the
footer of a bounded output, which means the assistant could read an
artifact only if it had already seen the step that made one. For an errand
that is exactly backwards: the run happened without the assistant watching,
the tree is gone, and the artifacts are the entire result. Asked "what did
that errand find", the assistant had nowhere to look.

So this is the index, and `read_artifact` stays the reader. The split
matters: a list that returned content would put the bytes back into the
context that bounding kept them out of, and the summary is what makes the
list usable — `emit_artifact` requires one for this reason.

SUPERSEDED VERSIONS ARE HIDDEN BY DEFAULT. A chain is a history and its
head is the answer. Showing every version of one report as a separate row
gives a reader three answers and no current one; `history=True` asks for
the rest.

SCOPED TO THE GOAL, like every other read on this surface. The assistant
serves one goal and one person, and an artifact from another goal is not
theirs to list.
"""

from __future__ import annotations

from kernel.assistant.read import Refused

# A list, not a dump. Ten rows is a conversational answer; a hundred is a
# document nobody reads, and `story` or `role` narrows it.
DEFAULT_LIMIT = 10
MAX_LIMIT = 50


def _row(r) -> dict:
    # `resource` IS THE POINT OF D2 AND WAS INVISIBLE HERE. Every
    # catalogued artifact is already a grantable goal resource, and GO
    # -- whose prompt says so -- listed the artifacts, saw no resource
    # id, and registered three duplicate rows of its own pointing at the
    # same `artifact://` URIs. Advice in a prompt lost to the shape of
    # the data in front of it. The id is derivable from the hash, so
    # naming it here costs nothing and removes the inference.
    from kernel.harness.artifact_resource import resource_id_for

    return {"artifact": r["artifact_id"], "role": r["role"],
            "name": r["name"], "summary": r["summary"] or "",
            "bytes": r["bytes"], "media_type": r["media_type"],
            "story": r["story_id"], "execution": r["execution_id"],
            "at": r["created_at"].isoformat() if r["created_at"] else "",
            "resource": resource_id_for(r["artifact_id"]),
            "supersedes": r["supersedes"]}


async def list_artifacts(conn, goal_id: str, story: str = "",
                         role: str = "", contains: str = "",
                         history: bool = False, limit: int = DEFAULT_LIMIT):
    """What this goal's runs produced, newest first.

    `contains` matches the NAME and the SUMMARY, which is why the summary
    is a required field on `emit_artifact` — searching a list of hashes
    finds nothing, and the executor is the only party that could have
    written what the thing says.
    """
    goal_id = (goal_id or "").strip()
    if not goal_id:
        return Refused("E_NO_GOAL", "list_artifacts needs a goal")

    from kernel.verifier.artifact_kind import ROLES

    role = (role or "").strip().lower()
    if role and role not in ROLES:
        return Refused("E_BAD_ROLE",
                       f"{role!r} is not an artifact role; expected one of "
                       f"{', '.join(ROLES)}")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    like = f"%{contains.strip()}%" if (contains or "").strip() else None

    rows = await conn.fetch(
        """
        SELECT m.artifact_id, m.role, m.name, m.summary, m.bytes,
               m.media_type, m.supersedes, m.created_at,
               m.execution_id, e.story_id
          FROM kernel.artifact_manifest m
          LEFT JOIN kernel.executions e ON e.id = m.execution_id
         WHERE COALESCE(e.goal_id, m.goal_id) = $1
           AND ($2::text IS NULL OR COALESCE(e.story_id, m.story_id) = $2)
           AND ($3::text IS NULL OR m.role = $3)
           AND ($4::text IS NULL OR m.name ILIKE $4 OR m.summary ILIKE $4)
         ORDER BY m.id DESC
         LIMIT $5
        """, goal_id, (story or "").strip() or None, role or None, like,
        # Over-fetch so hiding superseded rows does not silently return
        # fewer than asked for.
        limit * 3)

    superseded = {r["supersedes"] for r in rows if r["supersedes"]}
    out = [_row(r) for r in rows
           if history or r["artifact_id"] not in superseded]
    hidden = len(rows) - len(out)

    return {"goal": goal_id, "story": (story or "").strip() or None,
            "artifacts": out[:limit],
            "count": len(out[:limit]),
            # Named rather than silent: a reader who expected four reports
            # and sees one should be told the other three are older
            # versions, not that they are missing.
            "superseded_hidden": hidden if not history else 0,
            # NO PARAMETER NAME HERE. This function is SHARED: the
            # assistant's spec declares `artifact` and GO's declares
            # `artifact_id`, so a footer naming either one hands half its
            # callers a parameter their own tool does not take. Both are
            # accepted now (`_artifact_id`), and the fix for the text is
            # to stop teaching a third source of truth.
            "detail": ("read one with read_artifact, passing the id — this "
                       "list carries summaries, never content")}
