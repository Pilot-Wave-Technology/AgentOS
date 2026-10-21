"""E7b — a child's envelope is a strict narrowing of its parent's.

SA1–SA6. The property that has to hold is one sentence: **a child cannot
reach anything its parent could not.** Everything here exists to make that
checkable rather than asserted, because the failure mode is silent — a
child with a wider scope does useful work and nobody notices until it
writes somewhere it should not have been able to.

So attenuation is computed AND verified. `derive_child_envelope` narrows;
`assert_attenuated` re-checks the result against the parent and raises if
the narrowing did not hold. Computing and checking in one place would let a
bug in the computation certify itself.

Two flavours, from E7c:

  investigate  reads only. writable_scope is emptied, and the editing tools
               leave the surface — both, because either alone would be a
               half-guard: an empty scope with `write` still present makes
               every write a refusal the child pays a step for.

  build        its own worktree, merged by the parent. Writable scope is
               that worktree alone, which must sit inside the parent's.

Neither flavour may delegate further (no `subagent`: depth 1 is the ceiling
E7b sets) and neither may claim the story complete (no `propose_completion`:
a child finishing its errand is not the story being done).
"""

from __future__ import annotations

from kernel.envelope import ExecutionEnvelope, is_path_inside

FLAVOURS = ("investigate", "build")

# Removed from every child, whatever the flavour.
_NEVER_INHERITED = frozenset({
    "subagent",            # depth 1 is the ceiling
    "propose_completion",  # only the parent's run can be complete
    "register_plan",       # the plan belongs to the story, not the errand
    "ask_human",           # a child's question has no one to route it to
})

# Removed additionally, per flavour.
_FLAVOUR_DENY = {
    # `bash` is denied here for a reason worth stating: chain stage 1 scopes
    # writes by PATH, and bash has no path argument, so it is waved through
    # — a documented gap. An investigate child with an empty writable_scope
    # and a shell is therefore not read-only at all; it just has to type
    # `sh -c 'echo x > f'` instead of calling write. Removing the shell is
    # what makes "reads only" true rather than intended. The background
    # shell variants go with it, for the same reason.
    "investigate": frozenset({
        "write", "edit", "story_commit",
        "bash", "bash_background", "bash_output", "bash_kill",
    }),
    # `build` keeps the shell: it exists to make a change and run the tests
    # on it. Its scope is therefore advisory against a determined shell —
    # but the parent's is too, and attenuation's claim is "no wider than the
    # parent", which still holds. Stage 1 constraining bash (carried debt)
    # is what would make it enforceable for both.
    "build": frozenset({"story_commit"}),  # the parent merges and commits
}


class AttenuationError(Exception):
    """The child envelope is not a narrowing of the parent's."""


def derive_child_envelope(
    parent: ExecutionEnvelope,
    flavour: str,
    child_workspace: str | None = None,
) -> ExecutionEnvelope:
    """Narrow a parent envelope for one child. Never widens."""
    if flavour not in FLAVOURS:
        raise AttenuationError(
            f"unknown flavour {flavour!r}; expected one of {', '.join(FLAVOURS)}"
        )

    denied = _NEVER_INHERITED | _FLAVOUR_DENY[flavour]
    parent_tools = parent.allowed_tools
    if parent_tools is None:
        # An open parent surface must not become an open child surface.
        # None means "no policy", and inheriting that would hand the child
        # everything including the tools above.
        raise AttenuationError(
            "parent has an open tool surface (allowed_tools=None); a child "
            "cannot be attenuated from a policy that does not exist"
        )
    child_tools = [t for t in parent_tools if t not in denied]

    if flavour == "investigate":
        scope: list[str] = []
        workspace = parent.workspace_path
    else:
        if not child_workspace:
            raise AttenuationError(
                "build flavour requires a child workspace (its own worktree)"
            )
        if parent.workspace_path and not is_path_inside(
                parent.workspace_path, child_workspace):
            raise AttenuationError(
                f"child workspace {child_workspace!r} is outside the parent's "
                f"{parent.workspace_path!r} — that is a widening, not a "
                "narrowing"
            )
        scope = [child_workspace]
        workspace = child_workspace

    return ExecutionEnvelope(
        writable_scope=scope,
        allowed_tools=child_tools,
        workspace_path=workspace,
        generation=parent.generation,
        # A sub-agent is the SAME execution, so it works against the same
        # tree in the same pod and needs the same workspace token. These
        # two are inherited rather than narrowed -- narrowing happens on
        # scope and tools above, which is where containment lives. A
        # child that lost them would read the conductor's own filesystem
        # and report an empty workspace as an empty result.
        workspace_host=parent.workspace_host,
        execution_id=parent.execution_id,
    )


def assert_attenuated(parent: ExecutionEnvelope, child: ExecutionEnvelope) -> None:
    """Verify the child really is a narrowing. Raises AttenuationError.

    Deliberately independent of derive_child_envelope: this is the check
    that would catch a bug in the derivation, so it must not share its
    reasoning. Called at dispatch, on whatever envelope is about to be
    handed to a child, however it was produced.
    """
    # Tools: strict subset.
    if child.allowed_tools is None:
        raise AttenuationError(
            "child has an open tool surface; a child surface must be explicit"
        )
    if parent.allowed_tools is None:
        raise AttenuationError("parent has an open tool surface")
    widened = set(child.allowed_tools) - set(parent.allowed_tools)
    if widened:
        raise AttenuationError(
            f"child gains tool(s) the parent lacks: {', '.join(sorted(widened))}"
        )
    forbidden = set(child.allowed_tools) & _NEVER_INHERITED
    if forbidden:
        raise AttenuationError(
            f"child retains tool(s) no child may hold: "
            f"{', '.join(sorted(forbidden))}"
        )

    # Writable scope: every child path must sit inside some parent path.
    for path in child.writable_scope:
        if not any(is_path_inside(p, path) for p in parent.writable_scope):
            raise AttenuationError(
                f"child may write {path!r}, which is outside every parent "
                f"scope {parent.writable_scope!r}"
            )

    # Generation: a child writing at a different generation would slip the
    # C7 fence that exists to stop zombie writes after a kill.
    if child.generation != parent.generation:
        raise AttenuationError(
            f"child generation {child.generation} != parent "
            f"{parent.generation}; the C7 fence would not apply"
        )


def slice_budget(parent_remaining: int, requested: int | None = None) -> int:
    """Carve a child's budget out of what the parent has left.

    Half, capped by any explicit request. Half rather than all because the
    parent must be able to read the child's answer and act on it — a child
    that consumes the remainder leaves a parent that cannot use the result
    it paid for.
    """
    if parent_remaining <= 0:
        return 0
    half = parent_remaining // 2
    return min(half, requested) if requested else half
