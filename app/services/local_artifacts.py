"""An artifact emitted by a run in somebody's editor.

THE DELIVERABLE THAT IS NOT A DIFF. An errand produces an ANSWER -- run the
thing, read what came back, say what it means -- and the pod errand profile is
built around that: `artifact[role=report]` is its definition of done. A coding
story's deliverable is usually the tree, but usually is not always; a
benchmark, a migration note, an analysis is a real output with nowhere else to
go. Locally neither could emit anything at all.

WHY IT COULD NOT. `kernel.artifact_manifest.execution_id` was NOT NULL, and a
local run creates no execution -- the conductor is never involved. The bytes
were never the problem: `aput(data: bytes)` is the whole storage signature and
the key is a sha256 of the content, so both lanes already write the same bucket
and dedup across lanes for free. Only the CATALOGUE was execution-shaped.
`migrations/artifacts_can_belong_to_a_local_story.sql` gives a row a story and
a goal instead, with a CHECK that it has exactly one owner.

GOAL_ID IS ON THE ROW, not reached through the execution join, because the
reads filter on it: `GET /goals/{pid}/artifacts` joined executions and read
`e.goal_id`, so a story-keyed row would have been stored, catalogued, and
silently invisible.
"""

from __future__ import annotations

import logging

from app.database import get_pool
from kernel.harness.emit import MAX_BYTES, ROLES, media_type_for

logger = logging.getLogger(__name__)


class ArtifactRefused(ValueError):
    """Named, so the refusal can say which rule and why."""


def _check(role: str, name: str, data: bytes) -> None:
    if role not in ROLES:
        raise ArtifactRefused(
            f"role must be one of {', '.join(ROLES)}; got {role!r}")
    if not (name or "").strip():
        raise ArtifactRefused(
            "an artifact with no name is a row nobody can find again")
    if not data:
        raise ArtifactRefused(
            "an artifact with no bytes is a manifest row with nothing behind it")
    if len(data) > MAX_BYTES:
        raise ArtifactRefused(
            f"{len(data):,} bytes exceeds the {MAX_BYTES:,} an artifact may be")


async def _prior(pool, goal_id: str, role: str, name: str) -> str:
    """What this supersedes, by (goal, role, name) -- across BOTH lanes.

    The chain links a rerun to the run before it, and which substrate each ran
    on is not something the reader cares about: re-running an errand locally
    that previously ran in a pod should supersede the pod's report, not fork
    the chain. `COALESCE(e.goal_id, m.goal_id)` is what makes one chain out of
    two lanes.

    ORDERED BY `id`: `now()` is the transaction's start time, so two emits in
    one transaction share a timestamp and ordering by it picks arbitrarily --
    building a chain that forks, which a reader walking it sees as two heads.
    """
    return await pool.fetchval(
        """
        SELECT m.artifact_id
          FROM kernel.artifact_manifest m
          LEFT JOIN kernel.executions e ON e.id = m.execution_id
         WHERE COALESCE(e.goal_id, m.goal_id) = $1
           AND m.role = $2 AND m.name = $3
         ORDER BY m.id DESC LIMIT 1
        """, goal_id, role, name) or ""


async def emit_local_artifact(story_id: str, role: str, name: str, data: bytes,
                              summary: str = "", media_type: str = "",
                              supersedes: str = "") -> dict:
    """Store an artifact for a local run and catalogue it. Raises ArtifactRefused."""
    _check(role, name, data)
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT goal_id, COALESCE(work_kind, 'story') AS work_kind "
        "  FROM stories WHERE id = $1", story_id)
    if story is None:
        return {"ok": False, "reason": "story_not_found"}
    goal_id = story["goal_id"]

    from kernel.artifacts.aio import aput
    artifact_id = await aput(data)

    chain = (supersedes or "").strip() or await _prior(pool, goal_id, role, name)
    # Self-reference would make the chain a loop and hang every reader walking it.
    if chain == artifact_id:
        chain = ""

    await pool.execute(
        """
        INSERT INTO kernel.artifact_manifest
            (story_id, goal_id, artifact_id, role, name, media_type, summary,
             supersedes, bytes)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NULLIF($8, ''), $9)
        ON CONFLICT (story_id, artifact_id) WHERE story_id IS NOT NULL
        DO UPDATE SET role = EXCLUDED.role, name = EXCLUDED.name,
                      summary = EXCLUDED.summary, media_type = EXCLUDED.media_type
        """,
        story_id, goal_id, artifact_id, role, name.strip(),
        (media_type or "").strip() or media_type_for(name),
        summary or "", chain, len(data))

    # AND THE GOAL OWNS IT. The manifest makes the artifact readable; the
    # catalogue makes it GRANTABLE to later work. Without this an errand's
    # report can inform what GO plans and cannot reach the run that acts on
    # it -- the chain the errand shape exists for.
    # IN ITS OWN SAVEPOINT, which is not belt-and-braces -- `emit.py` says why
    # and I did not copy it: a failed statement POISONS the enclosing
    # transaction in Postgres, and catching the Python exception does nothing
    # about that. Every statement after it dies with
    # `InFailedSQLTransactionError`. The manifest row above is the thing that
    # KEEPS the work, so a catalogue write must never be able to take it down.
    #
    # Caught by a test re-emitting one report twice: the second hit
    # `uq_goal_resource_kind_name` and the whole transaction went with it.
    #
    # `pool` is an asyncpg Pool in production (each execute runs on its own
    # connection, so there is no enclosing transaction to poison) and a single
    # Connection under test. Only the latter has `.transaction()`, and only the
    # latter needs it.
    resource_id = None
    try:
        from kernel.harness.artifact_resource import register_for

        async def _catalogue():
            return await register_for(
                pool, artifact_id=artifact_id, role=role, name=name.strip(),
                summary=summary or "", media_type=(media_type or "").strip()
                or media_type_for(name), bytes_len=len(data), goal_id=goal_id,
                story_id=story_id,
                produced_by=f"{story['work_kind']} {story_id}")

        if hasattr(pool, "transaction"):
            async with pool.transaction():
                resource_id = await _catalogue()
        else:
            resource_id = await _catalogue()
    except Exception as exc:  # noqa: BLE001
        # Same rule as the pod path: the manifest row is written, and a
        # catalogue failure must not lose the artifact.
        logger.warning("local artifact %s emitted but not catalogued: %s",
                       artifact_id[:12], exc)

    logger.info("local artifact %s emitted by story %s (%s, %d bytes)",
                artifact_id[:12], story_id, role, len(data))
    return {"ok": True, "artifact_id": artifact_id, "bytes": len(data),
            "role": role, "name": name.strip(), "supersedes": chain or None,
            "resource_id": resource_id}
