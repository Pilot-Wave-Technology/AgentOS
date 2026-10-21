"""D2 — an emitted artifact becomes a resource the goal owns.

THE GAP THIS CLOSES. `emit_artifact` wrote `kernel.artifact_manifest` and
stopped. A manifest row is readable by AGENTS through `list_artifacts`
and `read_artifact`, and it is not a RESOURCE — so it cannot be granted,
cannot be mounted, and no later EXECUTOR can open it.

That breaks the chain the errand shape exists for. "Find out why checkout
is slow and fix it" is an errand whose report is the input to a story —
and the report could inform what GO planned while being unreachable by
the run that acted on it.

WHY ON EMIT RATHER THAN A SECOND TOOL. `publish_artifact` already exists
and needs the story to hold the target resource with a write relation
FIRST — so the errand must have been granted something it does not know
the name of, before it has produced the thing. Registering on emit needs
no second call, no pre-registered target, and no grant; and the
provenance is recorded by construction rather than by a model remembering
to mention it.

PROVENANCE IS THE POINT, not decoration. A report in the catalogue that
does not say which errand produced it is a document of unknown
authority — and the first question anybody asks of a finding is where it
came from. The row carries the story, the run and the role.

WHICH ARTIFACTS. `report`, `dataset` and `method` — the roles whose whole
purpose is to be read later. `log` and `figure` stay out: registering
every log would fill the catalogue with scratch, and a catalogue nobody
can scan is one nobody uses. `method` is included because a dataset
without the script that produced it is not reproducible.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The roles that outlive the run that made them. Deliberately not every
# role: see the module docstring.
CATALOGUED_ROLES = ("report", "dataset", "method")

# `goal_resources.kind` must be something `grant_resource` will accept
# and `mount_for` knows how to lay out. A dataset is a dataset; a report
# and a method are documents.
_KIND = {"report": "document", "dataset": "dataset", "method": "document"}


def resource_id_for(artifact_id: str) -> str:
    """Stable id derived from the content hash.

    CONTENT-ADDRESSED, so re-emitting identical bytes re-registers the
    same row rather than growing the catalogue by one every time an
    errand runs. A new version of a report has different bytes and gets
    its own row, chained by `supersedes` on the manifest.
    """
    return f"art_{artifact_id[:12]}"


async def register(conn, execution_id: int, artifact_id: str, role: str,
                   name: str, summary: str, media_type: str,
                   bytes_len: int) -> str | None:
    """Catalogue an artifact emitted by an EXECUTION. Thin wrapper.

    NEVER RAISES PAST THE CALLER. This runs inside `emit_artifact`, and
    emitting is how an errand keeps its work — a catalogue write failing
    must not lose the artifact. The manifest row is already committed by
    the time this runs; the resource is the convenience on top.
    """
    row = await conn.fetchrow(
        "SELECT goal_id, story_id, kind FROM executions WHERE id = $1",
        execution_id)
    if row is None:
        return None
    return await register_for(
        conn, artifact_id=artifact_id, role=role, name=name, summary=summary,
        media_type=media_type, bytes_len=bytes_len,
        goal_id=row["goal_id"], story_id=row["story_id"],
        produced_by=(f"{row['kind'] or 'run'} {row['story_id']}"
                     if row["story_id"] else f"run {execution_id}"),
        execution_id=execution_id)


async def register_for(conn, *, artifact_id: str, role: str, name: str,
                       summary: str, media_type: str, bytes_len: int,
                       goal_id: str, story_id: str | None,
                       produced_by: str,
                       execution_id: int | None = None) -> str | None:
    """Catalogue an artifact from the facts, whichever lane produced it.

    SPLIT OUT so a LOCAL run can catalogue too. `register` reads its facts
    from `executions`, and a local story has none — but everything the
    insert below actually needs (goal, story, who made it) a local run
    knows. Without this an errand run in an editor could emit a report and
    the report could not be GRANTED to later work, which is the whole chain
    the errand shape exists for (D2).
    """
    if role not in CATALOGUED_ROLES:
        return None
    if not goal_id:
        return None

    rid = resource_id_for(artifact_id)
    # WHO MADE THIS, in the name itself as well as the columns. The name
    # is what a person sees in a picker and what an agent grants BY, and
    # a catalogue of four files called "report.md" is one nobody can use.
    #
    # AND WHICH VERSION, which is not decoration. `goal_resources` has
    # carried `UNIQUE (goal_id, kind, name)` since migration 014, long
    # before artifacts existed. Without the hash, re-running an errand that
    # produces `report.md` computes the SAME label for different bytes: the
    # insert's `ON CONFLICT (id)` cannot absorb a conflict on a different
    # index, so it raised, the caller's savepoint swallowed it, and version
    # 2 was never catalogued at all. Silently -- the manifest chained the
    # new artifact correctly while the grantable resource stayed stuck on
    # version 1, which for an errand means the answer later work can reach
    # is the stale one.
    #
    # Reproduced on the pod path before changing anything; this is not a
    # local-lane defect, it is one the local lane's tests found.
    label = f"{name} — from {produced_by} ({artifact_id[:8]})"

    import json

    # PROVENANCE AS STRUCTURED DATA, not only in the name. The name is
    # what a person reads; this is what a query can filter on — "every
    # artifact story X produced", "what did run 7287 leave behind".
    provenance = {
        "artifact_id": artifact_id, "artifact_role": role,
        "execution_id": execution_id, "story_id": story_id,
        "produced_by_kind": produced_by.split(" ")[0], "produced_by": produced_by,
        "original_name": name, "bytes": bytes_len,
    }
    await conn.execute(
        """
        INSERT INTO goal_resources
            (id, goal_id, kind, name, uri, human_context, created_by,
             role, default_writable, status, metadata, file_type,
             file_size_bytes)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'artifact', false, 'active',
                $8::jsonb, $9, $10)
        ON CONFLICT (id) DO UPDATE
           SET name = EXCLUDED.name, human_context = EXCLUDED.human_context,
               uri = EXCLUDED.uri, metadata = EXCLUDED.metadata
        """,
        rid, goal_id, _KIND.get(role, "document"), label,
        # The artifact URI is how a reader gets the bytes back: the
        # content store is addressed by hash, and the hash IS the id.
        f"artifact://{artifact_id}",
        (summary or "")[:2000] or f"{role} emitted by {produced_by}",
        produced_by, json.dumps(provenance), media_type, bytes_len)

    # READ-ONLY BY POLICY, and that is not timidity. An artifact is a
    # RECORD of what a run found; a later story editing it in place would
    # rewrite history that other work has already cited. A new finding is
    # a new artifact, chained by `supersedes`.
    logger.info("artifact %s catalogued as %s for goal %s (%s)",
                artifact_id[:12], rid, goal_id, produced_by)
    return rid
