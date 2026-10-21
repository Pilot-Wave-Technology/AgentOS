"""E7e — one workspace, several mounts, and the seam above it unchanged.

`acquire` establishes every mount. `checkpoint` commits in each WRITABLE
mount that has changes and returns **one ref per mount**. `publish` hands
each of them onward.

WHY A SET AND NOT A REF. N commits across N repositories cannot be
atomic — there is no transaction spanning two git remotes, and pretending
otherwise would put a lie in the audit trail at exactly the point it
matters. So a checkpoint IS a set, the story records every sha in it, and
a partial checkpoint is visible as a partial checkpoint rather than as a
success that quietly dropped a repo.

WHY THE PROTOCOL DOES NOT CHANGE SHAPE. Everything above this composes
`acquire · checkpoint · publish` and must not learn how many repos a
story has; that is the same rule the driver seam already lives by — *"the
rule: nothing above the driver may know which driver it is."* A caller
that wants one ref takes the first; a caller that wants all of them was
previously getting a wrong answer.

READ-ONLY MOUNTS ARE SKIPPED, NOT COMMITTED-AND-DISCARDED. A doc mounted
as input has no remote to push to and no business carrying a commit, and
a `git add -A` in it would silently create one. Whether a mount may be
written is a per-story permission the contract states (E7f enforces it at
the tool); here it decides only whether the mount is a candidate at all.
"""

from __future__ import annotations

import logging
import os

from kernel.workspace.bindings import driver_for
from kernel.workspace.driver import CheckpointRef, WorkspaceError, WorkspaceSpec

logger = logging.getLogger(__name__)

# Said once, because two operations report it and one consumer reads it.
# `publish_execution` decides whether DONE may be granted by looking for
# this exact detail, and a second spelling of the same sentence would
# make that check silently stop matching.
MOUNT_MISSING = "the mount is not present"


class CompositeDriver:
    """Fans the three operations out over a spec's mounts."""

    name = "composite"

    def acquire(self, spec: WorkspaceSpec) -> str:
        """Establish every mount. Returns the workspace ROOT.

        Idempotent per mount, because `acquire` is also the rebuild path
        after a lost sandbox: a mount already present is adopted, and
        re-cloning over it would discard exactly the uncommitted work a
        resume exists to keep.
        """
        os.makedirs(spec.root, exist_ok=True)
        for mount in spec.mounts:
            at = os.path.join(spec.root, mount.at) if mount.at else spec.root
            driver = driver_for(mount.binding)
            try:
                driver.acquire(WorkspaceSpec(
                    execution_id=spec.execution_id, root=at,
                    source=mount.source, ref=mount.ref,
                    story_id=spec.story_id,
                    resource_id=mount.resource_id or ""))
            except WorkspaceError:
                # Named, and re-raised. One mount failing is the whole
                # workspace failing: a story that comes up missing a repo
                # it was granted will fail later, further from the cause,
                # as "the file is not there".
                logger.exception("execution %s: mount %s could not be "
                                 "established", spec.execution_id, mount.label)
                raise
        logger.info("execution %s: %d mount(s) established at %s",
                    spec.execution_id, len(spec.mounts), spec.root)
        return spec.root

    def checkpoint(self, root: str, message: str, mounts=()) -> tuple:
        """Commit in every writable mount that changed. One ref each."""
        refs = []
        for mount in mounts:
            if not mount.writable:
                continue
            at = os.path.join(root, mount.at) if mount.at else root
            if not os.path.isdir(at):
                refs.append(CheckpointRef(
                    kind=mount.binding, ref="", at=mount.at,
                    detail=MOUNT_MISSING))
                continue
            ref = driver_for(mount.binding).checkpoint(at, message)
            refs.append(CheckpointRef(kind=ref.kind, ref=ref.ref, at=mount.at,
                                      detail=ref.detail))
        return tuple(refs)

    def publish(self, root: str, mounts=()) -> tuple:
        """Hand each writable mount onward. May be a no-op per mount."""
        out = []
        for mount in mounts:
            if not mount.writable:
                continue
            at = os.path.join(root, mount.at) if mount.at else root
            if not os.path.isdir(at):
                # A MOUNT THAT IS NOT THERE IS A RESULT, NOT A SKIP.
                # `checkpoint` above records it and this dropped it, so
                # the tuple could not tell "considered, nothing to push"
                # from "never looked at" -- the exact distinction the
                # `changed()` docstring calls the only one that matters.
                # `publish_execution` reads this tuple to decide whether
                # DONE may be granted, so a workspace the adjudicator
                # cannot see (the second conductor replica; a volume
                # reclaimed before the verdict) yielded zero refs,
                # "nothing to push", and a story marked delivered with
                # every commit still local. That is the [ahead 7] the
                # whole delivery seam exists to make impossible.
                out.append(CheckpointRef(
                    kind=mount.binding, ref="", at=mount.at,
                    detail=MOUNT_MISSING))
                continue
            ref = driver_for(mount.binding).publish(at)
            out.append(CheckpointRef(kind=ref.kind, ref=ref.ref, at=mount.at,
                                     detail=ref.detail))
        return tuple(out)


def changed(refs) -> tuple:
    """The refs that actually recorded something.

    `checkpoint` returns a row per writable mount including the ones with
    nothing to commit, because "this repo was considered and had no
    changes" and "this repo was not looked at" are different facts and
    only one of them is fine.
    """
    return tuple(r for r in refs if r.ref)
