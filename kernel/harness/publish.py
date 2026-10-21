"""publish_artifact — the executor's road back for non-git resources.

An edit to a document in the workspace dies with the container; that has
been true all along, and since G1 the grant tool has refused write
relations on non-git kinds rather than let it happen silently. This is
the lift: publishing is the EXPLICIT act that makes a produced file
durable — a new version of the resource, the old one retained.

The executor holds no GCS credentials and needs none: it inserts the
bytes into `resource_publishes` (pending) and NOTIFYs. The control
panel's wake router owns finalization — versioned upload, resource
repointed, summary marked stale, GO woken. If the router is down, the
row waits; publish is durable at the INSERT, exactly like `ask_go`.

WHAT IS REFUSED, and why it is here rather than in a prompt: publishing
against a resource the story does not hold with a write relation is the
same act as writing a repo it was never granted. The story's grant is
the authority; this tool checks it, every call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
WRITE_RELATIONS = ("produces", "modifies")


async def publish_artifact(conn, envelope, execution_id: int,
                           tool_args: dict, tool_call_id: str) -> ToolResult:
    resource_id = (tool_args.get("resource_id") or "").strip()
    path = (tool_args.get("path") or "").strip()
    note = (tool_args.get("note") or "").strip()

    def refuse(msg: str) -> ToolResult:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error=msg, exit_code=1)

    if not resource_id or not path:
        return refuse("E_BAD_ARGS: publish_artifact needs resource_id and "
                      "path (workspace-relative, the file you produced)")

    row = await conn.fetchrow(
        """
        SELECT e.goal_id, e.story_id FROM executions e WHERE e.id = $1
        """, execution_id)
    if row is None or not row["story_id"]:
        return refuse("E_NO_STORY: this run is not attached to a story, so "
                      "there is nothing to publish against")

    held = await conn.fetchval(
        """
        SELECT 1 FROM story_resources
         WHERE story_id = $1 AND resource_id = $2
           AND relation = ANY($3::text[])
        """, row["story_id"], resource_id, list(WRITE_RELATIONS))
    if not held:
        return refuse(
            f"E_NOT_GRANTED: story {row['story_id']} does not hold "
            f"{resource_id!r} with a write relation. Ask your assistant to "
            f"ask GO for produces/modifies on it — publishing without the "
            f"grant would be writing something you were never given.")

    kind = await conn.fetchval(
        "SELECT kind FROM goal_resources WHERE id = $1", resource_id)
    if kind in ("repo", "branch"):
        return refuse("E_IS_GIT: that resource is git-backed — commit and "
                      "let the branch land; publish is for documents, "
                      "files and datasets")

    root = getattr(envelope, "workspace_path", "") or "/workspace"
    full = os.path.normpath(os.path.join(root, path))
    if not full.startswith(os.path.normpath(root)):
        return refuse("E_ENVELOPE_VIOLATION: the path escapes the workspace")

    # P1 · READ WHERE THE FILE IS. `workspace_path` is the POD's path, and
    # on this substrate the conductor has no such directory -- so every
    # `publish_artifact` on GKE opened nothing and returned
    #
    #     E_NO_FILE: cannot read 'report.md': [Errno 2] No such file
    #
    # which reads as the model naming a file it never wrote. The same
    # defect class as `publish_execution` running git in the conductor,
    # and the same fix: podfs is the seam for "the workspace is somewhere
    # else", and `read_workspace_file` already goes through it.
    #
    # OFF THE EVENT LOOP either way -- a local read is the artifact's own
    # size and a remote one is a synchronous urlopen.
    host = getattr(envelope, "workspace_host", "") or ""
    if host:
        from kernel.workspace.podfs_client import (          # noqa: PLC0415
            WorkspaceUnreachable, read_file)

        try:
            content, problem = await asyncio.to_thread(
                read_file, host, root, path,
                execution_id=getattr(envelope, "execution_id", None))
        except WorkspaceUnreachable as exc:
            return refuse(f"E_WORKSPACE_UNREACHABLE: the pod holding this "
                          f"workspace could not be reached, so {path!r} "
                          f"could not be read: {exc}")
        if problem:
            return refuse(f"E_NO_FILE: cannot read {path!r}: {problem}")
    else:
        def _read():
            with open(full, "rb") as fh:
                return fh.read(MAX_BYTES + 1)

        try:
            content = await asyncio.to_thread(_read)
        except OSError as exc:
            return refuse(f"E_NO_FILE: cannot read {path!r}: {exc}")
    if len(content) > MAX_BYTES:
        return refuse(f"E_TOO_LARGE: {path!r} exceeds "
                      f"{MAX_BYTES // (1024 * 1024)}MB; large artifacts "
                      f"belong in a repo or a dataset store")

    pub_id = await conn.fetchval(
        """
        INSERT INTO resource_publishes
            (resource_id, goal_id, story_id, execution_id, filename,
             content, note)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
        """,
        resource_id, row["goal_id"], row["story_id"], execution_id,
        os.path.basename(path), content, note)
    await conn.execute(
        "SELECT pg_notify('resource_published', $1)",
        json.dumps({"publish": pub_id, "goal": row["goal_id"],
                    "resource": resource_id}))
    logger.info("publish_artifact: %s (%d bytes) -> %s as publish %s",
                path, len(content), resource_id, pub_id)
    return ToolResult(
        tool_call_id=tool_call_id,
        output=f"published {path} ({len(content)} bytes) against "
               f"{resource_id} as version-pending #{pub_id}. The control "
               f"panel finalizes it into a new version; the old version is "
               f"retained. Durable now — this survives the container.",
        exit_code=0)
