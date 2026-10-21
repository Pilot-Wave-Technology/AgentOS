"""Uploading a document that already exists REPLACES it.

WHAT HAPPENED WITHOUT THIS. The chat upload did a bare INSERT into
`goal_resources`, which carries `uq_goal_resource_kind_name UNIQUE
(goal_id, kind, name)`. Uploading a revised `PRICE_LIST.xlsx` therefore
raised a unique violation nobody caught, and the person saw an internal
error — no message, no explanation, and no way to tell a duplicate name
from a broken server. The resource-panel route guards the same
constraint and refuses politely; this one simply did not know.

WHY REPLACE RATHER THAN REFUSE. Refusing is defensible and it is what
the other route does, but it makes "Ops revised the price list" a
four-step operation: find the resource, archive it, upload again, then
re-grant it to every story that held the old one. Every one of those
steps is a chance to end up with two documents of the same name where
one is a ghost, and the grants pointing at the ghost.

THE ID IS THE THING THAT MUST NOT CHANGE. Grants, story links and the
blob path are all keyed on `resource_id`, so replacing in place means
every story that could read the old document can read the new one,
with nothing to re-grant and nothing to notice. A new id would leave
the old grants pointing at the previous revision — which reads as "the
update did not propagate" and is really "you are holding the old one".

AND THE CACHED SUMMARY IS INVALIDATED. `summary_stale` exists precisely
for this and nothing was setting it when a blob changed underneath the
row. A brief is a summary OF a file; when the file changes the summary
is wrong, and a stale brief is worse than no brief because the reader
cannot tell.

WHAT IS DELIBERATELY NOT DONE HERE: no version history. The previous
bytes are gone once the object is overwritten. That is the same
contract the bucket already had, and inventing revision tracking as a
side effect of a bug fix would be a much larger change than the one
being asked for.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def existing_document(conn, goal_id: str, name: str) -> dict | None:
    """The live document of this name in this goal, if there is one.

    Archived rows are ignored: archiving is how a person says "this one
    is finished with", and quietly reviving one because the name matches
    would undo their decision.
    """
    row = await conn.fetchrow(
        """
        SELECT id, file_size_bytes, gcs_uri, file_type
          FROM goal_resources
         WHERE goal_id = $1 AND kind = 'document' AND name = $2
           AND COALESCE(status, 'active') <> 'archived'
         LIMIT 1
        """,
        goal_id, name)
    return dict(row) if row else None


async def replace_document(conn, resource_id: str, goal_id: str, name: str,
                           gcs_uri: str, ext: str, size: int) -> None:
    """Point an existing resource row at freshly uploaded bytes.

    The row keeps its id, its grants and its place in every story that
    holds it. What changes is the file behind it and the fact that the
    cached summary no longer describes it.
    """
    await conn.execute(
        """
        UPDATE goal_resources
           SET uri = $3, gcs_uri = $3, file_type = $4, file_size_bytes = $5,
               summary_stale = TRUE, updated_at = now()
         WHERE id = $1 AND goal_id = $2
        """,
        resource_id, goal_id, gcs_uri, ext, size)
    logger.info("resource %s (%s) replaced in goal %s: %d bytes, summary "
                "marked stale", resource_id, name, goal_id, size)


async def record_replacement(conn, resource_id: str, goal_id: str,
                             actor: str, filename: str, ext: str,
                             size: int, previous_size, via: str) -> None:
    """The audit trail for a replacement, on the two logs that have one.

    `previous_size` is carried because it is the cheapest signal that a
    replacement actually changed something -- an identical size is not
    proof of an identical file, but a DIFFERENT size is proof of a
    different one, and that is the question somebody asks first when a
    document update appears not to have landed.
    """
    import json

    await conn.execute(
        """
        INSERT INTO resource_events (resource_id, goal_id, event_type,
                                     actor, details)
        VALUES ($1, $2, 'updated', $3, $4)
        """,
        resource_id, goal_id, actor,
        json.dumps({"filename": filename, "type": ext, "size": size,
                    "previous_size": previous_size,
                    "replaced_in_place": True, "uploaded_via": via}))
