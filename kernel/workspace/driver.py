"""E7d — the workspace is the profile's, not the spine's.

Three operations, and git is one binding of them rather than the shape
itself:

    acquire(spec)       establish a workspace, or re-establish one after
                        the sandbox was lost
    checkpoint(msg)     make the current state durable and return a ref
    publish()           hand the work to wherever it belongs

WHAT THIS REPAIRS. `snapshot()` is named *workspace snapshot* and writes a
metadata row — budget_basis, total_spend, predicate, steps — touching no
file. Work survives a kill today for two incidental reasons: committed work
is in git, and the host directory outlives the process. Neither is a
harness guarantee, and a workspace copied from a drive has neither. A
business story could run to completion with no durable record of its output
at all.

So `checkpoint` is the spine's promise and `story_commit` becomes the
coding profile's binding of it — the same move as `propose_completion`
being absent from an errand: what differs is data, not a branch.

LIFECYCLE, decided 2 September 2026. The workspace lives until the story or
errand completes; the sandbox is hot for that whole time and is torn down
only at the end. `acquire` must still be able to rebuild against a
surviving workspace, because never CHOOSING to release compute is a policy
and not a guarantee — a node dies, a container OOMs, and reconcile already
demotes PARKED_HOT when compute vanishes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class WorkspaceSpec:
    """Everything a driver needs to establish a workspace."""

    execution_id: int
    root: str                       # where the workspace should live
    # E7e: what the workspace is MADE OF. Empty means the single-source
    # form below, which every execution created before E7e uses and the
    # console still starts stories with.
    mounts: tuple = ()
    source: Optional[str] = None    # one repo URL, or a folder to copy in
    ref: Optional[str] = None       # branch/commit for git bindings
    # Which STORY this workspace serves. The git binding needs it to mint
    # per-story credentials from the control panel's broker -- a story
    # holding a repo read-only gets a read-only token, and that scoping
    # lives against the story, not the execution.
    story_id: str = ""
    # WHICH grant this mount is. A document is fetched from the control
    # panel scoped to (story, resource) -- the grant is the
    # authorisation, so both halves have to reach the driver.
    resource_id: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CheckpointRef:
    """What a checkpoint produced. `kind` says which binding made it."""

    kind: str          # "git" | "archive" | "none"
    ref: str           # commit sha, artifact id, or ""
    at: str = ""       # E7e: WHICH mount this is a checkpoint of
    detail: str = ""


class WorkspaceError(Exception):
    """The driver could not do what was asked. Never raised past the tool."""


class WorkspaceDriver(Protocol):
    """The seam. Bindings implement it; nothing above knows which is in use."""

    name: str

    def acquire(self, spec: WorkspaceSpec) -> str:
        """Establish the workspace and return its path. Idempotent.

        Idempotent because it is also the rebuild path: a leg resuming
        after its sandbox was lost calls the same function, and an
        already-present workspace must be adopted rather than clobbered.
        """
        ...

    def checkpoint(self, path: str, message: str) -> CheckpointRef:
        """Make the current state durable."""
        ...

    def publish(self, path: str) -> CheckpointRef:
        """Hand the work onward. May be a no-op."""
        ...
