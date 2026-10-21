"""The file explorer, when the workspace is inside an execution's pod.

WHY THE PANEL WAS EMPTY. `kernel_workspace.py` reads the tree over HTTP
from `KERNEL_WORKSPACE_URL` -- "the container that holds the workspace
volume". That sentence is true on Docker, where one container holds one
volume for everything, and false on Kubernetes, where every execution
has its own pod, its own ReadWriteOncePod claim and its own address.

And the variable was never set on `next`. It is read in one place and
written in one place, `local-development/env/local.values` -- the same
defect, in a different file, as `GIT_CREDENTIAL_URL`. So `enabled()`
returned False, the page quietly fell back to the old gateway-RPC path
that walks the ASSISTANT's directory, and under the kernel the
assistant holds no files. Nothing errored. The panel was simply empty,
which reads as "the workspace did not build" when the workspace was
complete the whole time.

SO THE ADDRESS IS PER EXECUTION, from `executions.workspace_host` --
the column the pod writes when it composes -- and the transport is
`podfs`, the same read-only surface the verifier and `emit_artifact`
already use. One port: 9101, the worker's, never 9100. The sandbox's
bridge runs arbitrary bash, and the explorer is a viewer.

IN A THREAD, because `podfs_client` is `urllib` and this is called from
an async handler. The module this supplements carries a paragraph about
exactly that mistake -- a synchronous fetch on the event loop, which
turned into the server waiting on its own socket -- and repeating it
here would be the same bug one layer over.
"""

from __future__ import annotations

import asyncio
import functools
import logging

logger = logging.getLogger(__name__)

# What the pod serves its workspace at. The pod's own root, not a path
# on this process's filesystem -- reading it here would open nothing.
ROOT = "/workspace"


async def host_for(story_id: str) -> tuple[str, int | None]:
    """The pod holding this story's workspace, and WHICH execution it is.

    THE NEWEST EXECUTION THAT STILL HAS A POD. A story is attempted more
    than once and each attempt had its own pod; the one worth showing is
    the live one. `container_id IS NOT NULL` is the test for "the pod
    was not destroyed" -- the same column the reclaim pass learned not
    to use for the CLAIM, and the right one for the POD.

    IT RETURNS THE ID TOO because `podfs` authenticates per execution
    (kernel/workspace/podfs_auth.py) and the token is derived from it.
    Returning the address alone would hand the panel somewhere it cannot
    open, and the 401 would read as an unreachable pod. Two facts about
    one row, read in one query, returned together so no caller can hold
    half of them.
    """
    from app.database import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, workspace_host FROM kernel.executions "
            "WHERE story_id = $1 AND container_id IS NOT NULL "
            "  AND coalesce(workspace_host, '') <> '' "
            "ORDER BY id DESC LIMIT 1", story_id)
    if not row:
        return "", None
    return (row["workspace_host"] or ""), row["id"]


async def _call(host: str, execution_id: int | None, what: str, **args):
    from kernel.workspace import podfs_client

    return await asyncio.to_thread(
        functools.partial(podfs_client.explore, host, what, ROOT,
                          execution_id=execution_id, **args))


async def tree(story_id: str) -> dict | None:
    """Every file in the pod's workspace, in the shape the page renders.

    None means "not a pod workspace" -- the caller falls through to the
    HTTP transport, which is still how Docker and E0 serve this.
    """
    host, eid = await host_for(story_id)
    if not host:
        return None
    try:
        out = await _call(host, eid, "tree")
    except Exception as e:                                  # noqa: BLE001
        # NAMED, NOT SWALLOWED. An empty tree and an unreachable pod
        # look identical on the page, and only one of them is about the
        # work. This is the failure the whole panel was hiding.
        logger.warning("workspace tree for story %s: %s", story_id, e)
        return {"entries": [], "truncated": False, "mounts": [],
                "changed": 0,
                "detail": f"the pod holding this workspace did not answer: {e}"}

    # ALREADY FLATTENED. The pod's explorer walks the workspace root and
    # returns mount-prefixed paths -- `repos/acme__api/src/x.py` -- which
    # is the shape the page wants and the shape the executor sees, so
    # there is no mount stitching to do on this side.
    entries = [{"path": e.get("path", ""), "type": "file",
                "size": e.get("size", 0), "status": e.get("status") or "",
                "writable": bool(out.get("writable")), "mtimeMs": 0}
               for e in (out.get("entries") or [])]
    return {"entries": entries, "truncated": bool(out.get("truncated")),
            "mounts": [{"at": out.get("at") or "",
                        "writable": bool(out.get("writable"))}],
            "changed": sum(1 for e in entries if e["status"])}


async def read_file(story_id: str, path: str) -> dict | None:
    """One file out of the pod. None means "not a pod workspace"."""
    host, eid = await host_for(story_id)
    if not host:
        return None
    try:
        out = await _call(host, eid, "read", rel=path)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("workspace file %s for story %s: %s",
                       path, story_id, e)
        return {"error": f"the pod holding this workspace did not answer: {e}"}
    if out.get("error"):
        return {"error": out["error"]}
    # NO `_split` HERE. The HTTP transport has to break a flattened path
    # back into (mount, relative) because the console serves per mount;
    # the pod's explorer takes the workspace-relative path directly, so
    # splitting it would be undoing work and getting it wrong on any
    # mount whose `at` prefixes another.
    return {"path": path, "content": out.get("text", ""),
            "mime": "text/plain", "size": out.get("size", 0),
            "truncated": bool(out.get("truncated"))}
