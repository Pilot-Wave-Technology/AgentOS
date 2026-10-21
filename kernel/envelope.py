"""ExecutionEnvelope — agent-level policy for the interceptor chain.

Separate from ExecutionSpec (substrate concerns: image, memory, binds).
The envelope carries the policy that the interceptor chain enforces:
writable paths, allowed tools, workspace location, and generation.

See docs/refactor/decisions/p0-execution-envelope.md for the full rationale.

Attenuation (E7b):
  - allowed_tools: set containment (child <= parent).
  - writable_scope: path-prefix containment via is_path_inside().
    A child scope /ws/src/foo is a valid narrowing of parent /ws/src.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExecutionEnvelope:
    """Agent-level execution policy, threaded to BridgeToolExecutor.

    writable_scope: glob patterns for allowed write targets.
        Empty list = deny all writes (fail closed).
        Stage 1 (envelope) of the interceptor chain checks write calls
        against these patterns. Reads are logged, never denied.
    allowed_tools: tool names the agent may call.
        None = no policy (open surface, all tools allowed).
        Empty list [] = deny all (fail closed).
        Stage 2 (policy) of the interceptor chain checks membership.
    workspace_path: host path to the story worktree (bind-mounted into sandbox).
        Used by story_commit (worker-side git operations).
    generation: monotonic execution generation.
        C7's fence prevents zombie writes after a kill.
    """

    writable_scope: list[str] = field(default_factory=list)
    allowed_tools: Optional[list[str]] = None
    workspace_path: str = ""
    generation: int = 0
    # Who this run is FOR and who is doing it. Resolved once where a
    # database connection exists and carried here, because story_commit
    # runs worker-side with no connection of its own — and identity is a
    # property of the run, not of whatever git config the worker happens
    # to have. None means the commit is recorded as unattributed, which
    # is honest; it is never filled in with a plausible name.
    identity: Optional[object] = None
    # E7e — what the workspace is made of, and which parts may be
    # written. Carried here for the same reason `identity` is:
    # `story_commit` runs worker-side with no database connection, and
    # "which repos may I commit in" is a property of the run rather than
    # of whatever happens to be on disk under the root.
    mounts: tuple = ()

    # I4 -- the address of the container holding the tree, EMPTY when the
    # tree is on this filesystem.
    #
    # Empty is not "unknown", it is "local", and that is the whole
    # safety of it: Docker and E0 leave it unset and every workspace
    # read takes the path it always took. It is set only for a
    # pod-model driver, where reading locally would open a directory in
    # the conductor's own container and report an empty workspace as an
    # empty RESULT -- an artifact emitted with no bytes, a commit with
    # nothing staged, and no error anywhere.
    workspace_host: str = ""

    # WHICH execution's tree, and it travels WITH the address above
    # because neither is usable without the other. `podfs` authenticates
    # per execution (kernel/workspace/podfs_auth.py): the token is
    # derived from this id, so a caller holding `workspace_host` and not
    # this is a caller that knows where the tree is and cannot open it.
    #
    # Carried on the envelope for the same reason `identity` and
    # `mounts` are -- `story_commit` and `read_workspace_file` run
    # worker-side with no database connection, and "which run is this"
    # is a property of the run, not of whatever this process last did.
    #
    # None is correct wherever `workspace_host` is empty: the tree is on
    # this filesystem, nothing crosses, and no token is needed. The two
    # fields are set together or not at all.
    execution_id: Optional[int] = None

    def to_interceptor_context(
        self,
        execution_id: int,
        depth: int = 0,
        budget_remaining: int = 0,
    ) -> dict:
        """Serialise to the shape the TypeScript InterceptorContext expects.

        This is the single place where the Python envelope becomes the JSON
        payload sent over IPC to the interceptor chain. No parallel definition.

        budget_remaining is 0 by default — stage 4 (budget) is a named gap
        until the budget channel from run_leg is threaded.
        """
        return {
            "writable_scope": list(self.writable_scope),
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "budget_remaining": budget_remaining,
            "execution_id": execution_id,
            "depth": depth,
        }


def is_path_inside(parent: str, child: str) -> bool:
    """True when child is inside parent (or equal to it).

    Python equivalent of executor/src/infra/path-guards.ts:isPathInside.
    Used by E7b attenuation to validate that a child's writable_scope
    is a narrowing of the parent's.
    """
    # A non-string target is not a path, and "not a path" must mean
    # "not inside", never a crash. E8's chaos stub reached here with
    # path=12345 and took the whole leg down from inside chain stage 1 —
    # the guard itself was the thing that broke.
    if not isinstance(parent, str) or not isinstance(child, str):
        return False
    rel = os.path.relpath(child, parent)
    return not rel.startswith("..") and not os.path.isabs(rel)
