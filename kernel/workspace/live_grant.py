"""Granting a resource to a story that is already running.

A resource is not always known when a story starts. The assistant works
out what is needed, asks GO, and GO grants — and that can land while an
executor is mid-leg. Making the grant real then is three things, and
skipping any one of them leaves a grant that looks done and is not.

WHY THE CONTAINER DOES NOT HAVE TO CHANGE. The sandbox binds the story's
ROOT as a volume subpath, and every mount is a subdirectory inside that
root. So cloning a new mount into the workspace makes it appear in the
running container immediately — same volume, same bind. There is nothing
to restart.

WHY IT IS READABLE NOW AND WRITABLE NEXT LEG. `writable_scope` is built
once per leg and interceptor stage 1 checks every write against it. An
envelope that changed underneath a running leg would mean a write
refused at step 12 and allowed at step 13, with the journal showing no
reason for either. So the mount is readable at once and writable from
the next leg — and the executor is TOLD that, rather than discovering it
as an inexplicable refusal.

WHY THE EXECUTOR IS TOLD AT ALL. A directory that appears silently is
one the model will not use, because nothing in its context mentions it.
The grant is not the clone; the grant is the clone plus the sentence.
"""

from __future__ import annotations

import logging
import os

from kernel.workspace.composite import CompositeDriver
from kernel.workspace.driver import WorkspaceSpec
from kernel.workspace.mounts import Mount, grant, mounts_for, record

logger = logging.getLogger(__name__)


async def _live_executions(conn, goal_id: str, story_id: str) -> list:
    """Runs of this story that are not finished, with their machines."""
    rows = await conn.fetch(
        """
        SELECT e.id, e.workspace_path, e.state
          FROM executions e
          JOIN run_state_machine m ON m.version = e.machine_version
         WHERE e.goal_id = $1 AND e.story_id = $2
           AND NOT (m.terminal_states @> to_jsonb(e.state))
         ORDER BY e.id
        """,
        goal_id, story_id)
    return [dict(r) for r in rows]


async def grant_to_story(conn, goal_id: str, story_id: str, mount: Mount,
                         granted_by: str = "go", relation: str = "",
                         why: str = "") -> dict:
    """Attach a resource to a story, and to whatever is running it now.

    Returns what happened, per run — the caller reports this to a person,
    and "granted" with nothing said about the live execution is exactly
    the ambiguity that makes somebody wonder whether it took.
    """
    # WHAT COMES BACK, NOT WHAT WENT IN. `grant` upgrades an errand's
    # checkout to writable, and everything below is built from the
    # Mount rather than from the stored relation: `record` writes
    # `execution_mounts.writable` from it, the note tells the executor
    # whether it may write, and the reply tells the GO. Passing the
    # original through would have told all three the opposite of what
    # the story now holds.
    mount, relation = await grant(conn, goal_id, story_id, mount,
                                  granted_by=granted_by,
                                  relation=relation, why=why)

    live = await _live_executions(conn, goal_id, story_id)
    if not live:
        return {"granted": mount.at, "writable": mount.writable,
                "relation": relation, "applied_to": [],
                "detail": "recorded on the story; the next run inherits it"}

    driver = CompositeDriver()
    applied = []
    for run in live:
        eid, root = run["id"], run["workspace_path"]
        await record(conn, eid, (mount,))
        if not root:
            applied.append({"execution_id": eid, "state": run["state"],
                            "detail": "no workspace yet; it will be "
                                      "established at dispatch"})
            continue
        try:
            # Every mount, not just the new one: `acquire` adopts what is
            # already there, so this establishes the addition and leaves
            # live work alone. Passing only the new mount would work too
            # and would drift the moment acquire grows a step that reads
            # the whole set.
            driver.acquire(WorkspaceSpec(
                execution_id=eid, root=root,
                mounts=await mounts_for(conn, eid)))
        except Exception as exc:                            # noqa: BLE001
            logger.exception("grant: could not establish %s for %s",
                             mount.at, eid)
            applied.append({"execution_id": eid, "state": run["state"],
                            "detail": f"clone failed: {exc}"})
            continue

        note = (
            f"A resource has been granted to this story: `{mount.at}` is "
            f"now in your workspace"
            + (". You may write to it from your next leg — the write scope "
               "is fixed for the leg you are in."
               if mount.writable else
               " (read-only).")
            + " It was not there when you started; look at it before "
              "assuming what it contains."
        )
        await conn.execute(
            """
            INSERT INTO directives (execution_id, generation, kind, payload)
            SELECT $1, e.generation, 'resume', $2::jsonb
              FROM executions e WHERE e.id = $1
            """,
            eid, _payload(note))
        applied.append({"execution_id": eid, "state": run["state"],
                        "detail": f"{mount.at} established; the executor is "
                                  f"told at its next step boundary"})

    return {"granted": mount.at, "writable": mount.writable,
            "relation": relation, "applied_to": applied}


def _payload(note: str) -> str:
    import json
    return json.dumps({"note": note, "reason": "resource_granted"})


def mount_for_resource(kind: str, source: str, writable: bool,
                       resource_id: str = "", at: str = "",
                       ref: str = "", name: str = "") -> Mount:
    """The mount a granted resource becomes. `at` overrides the layout.

    `name` matters for an `artifact://` document: its source ends in a
    content hash, so without the row's own name it lands at
    docs/<64 hex characters>.
    """
    from kernel.workspace.mounts import mount_for

    base = mount_for(kind, source, writable=writable, ref=ref or None,
                     resource_id=resource_id or None, name=name or None)
    if at:
        return Mount(at=at.strip("/"), binding=base.binding,
                     source=base.source, ref=base.ref,
                     writable=base.writable, kind=base.kind,
                     resource_id=base.resource_id)
    return base


def workspace_of(root: str, mount: Mount) -> str:
    return os.path.join(root, mount.at) if mount.at else root
