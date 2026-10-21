"""What must be true before a tool runs at all.

SPLIT OUT OF `executor.py`, which was 415 code lines — twice the house
ceiling. What lives here is the PRE-FLIGHT: the target a call names, the
interceptor chain's answer about it, what a demand for approval becomes,
and the set of tools that may act before a plan has been approved. None
of it is routing, and all of it is policy, which is the seam.

WHAT STAYED IN `executor.py`, DELIBERATELY. The E6h refusal itself —
the `E_PLAN_NOT_APPROVED` message — did not move, because
`test_emit_and_plan_gate.test_the_refusal_names_what_is_still_open`
reads `kernel/harness/executor.py` by path, finds `E_PLAN_NOT_APPROVED`
and asserts the next 700 characters name `register_plan` and
`_OPEN_BEFORE_PLAN`. Moving the refusal would leave that guard looking
at a file the refusal is no longer in. The SET moved; the refusal that
reads it did not, and it still says `_OPEN_BEFORE_PLAN` by name, which
is the thing the guard is actually checking.

`_OPEN_BEFORE_PLAN` and `needs_approved_plan` are re-exported from
`executor.py`, because `test_emit_and_plan_gate` imports both from
there by name.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from kernel.harness.chain import ToolCall, InterceptorContext
from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)

# Per-tool target extraction table (P4).
# write/edit -> args["path"] (denied if outside writable_scope)
# read  -> args["path"] (logged only, never denied by writable_scope)
# bash  -> None (named gap — no path argument, stage 1 waves through)
# story_commit -> None (worker-side, not path-scoped)
_WRITE_TOOLS = {"write", "edit"}  # tools whose target is denied by stage 1

# E6h: a profile with `requires_plan` may not act until a human has
# approved a plan. `requires_plan` was a flag nothing read — a profile
# could declare it must plan and the harness would let it edit and commit
# regardless, which is a guard that exists only in a prompt.
#
# Ec2a — INVERTED, AND THAT IS THE FIX. This was a list of tools that
# NEEDED a plan, so a tool nobody thought about was ungated. Four were:
# `env_setup`, `env_install` and `publish_artifact` all reach past the
# workspace, and all three ran freely before any human saw the intent.
# Listing what is FORBIDDEN means the list has to be complete; listing what
# is PERMITTED means an omission is a needless refusal that says its own
# name on the first call, instead of a silent hole.
#
# WHAT EARNS A PLACE HERE: the tool inspects something already granted —
# the workspace, the goal's record, a stored artifact, the profile's own
# skills — or it talks to the person whose approval is being waited on.
# Nothing here changes anything, and nothing here reaches outside what the
# run was already given.
_OPEN_BEFORE_PLAN = frozenset({
    "read", "glob", "grep", "list_dir",   # the workspace, as granted
    "skill",                              # the profile's own instructions
    "goal_knowledge",                     # what this goal already settled
    "artifact_read",                      # an output already stored
    "ask_human",                          # talk to the approver
    "register_plan",                      # the way out of this gate

    # WATCHING AND STOPPING A JOB THAT IS ALREADY RUNNING. Starting one
    # is not here -- `bash_background` stays gated, because beginning
    # work before anybody has seen the intent is the whole point of the
    # gate. These four do not begin anything: the reach was granted when
    # the job started, and all they add is the ability to see it and to
    # stop it.
    #
    # THE CASE THAT MADE THIS NECESSARY IS AN AMENDMENT. A person
    # amends the plan while a forty-minute suite is running; the profile
    # goes back to needing approval, and with these gated the leg could
    # no longer poll the job, wait for it, or kill it. The job runs on
    # invisibly, the model has no way to learn its result, and the only
    # exit is the stall clock -- which is the silence this phase exists
    # against. Fail-closed was right when no job could exist before a
    # plan; a job can now outlive the approval it started under.
    "bash_output", "bash_wait", "bash_jobs", "bash_kill",
})

# `web_fetch` is the borderline one and it is gated deliberately. It only
# GETs, through an SSRF guard that re-checks every redirect, so it CHANGES
# nothing — by the "may not change anything" reading it belongs above. It
# is gated on REACH instead: the workspace is bounded and already granted,
# the internet is not, and a plan is where an executor says which sources
# it needs. One extra approval round for a plan that wants to read a doc is
# a visible cost; egress before anybody has seen the intent is not.
#
# `bash_background` stays gated for the same reason `web_fetch` does, and
# more plainly: starting work is the thing a plan exists to authorise.
# Watching and stopping a job that is already running is not starting
# work, which is why those four sit in the set above.


def needs_approved_plan(tool_name: str) -> bool:
    """Is this tool gated until a plan is approved? Unknown means yes."""
    return tool_name not in _OPEN_BEFORE_PLAN


def _extract_target(tool_name: str, tool_args: dict) -> Optional[str]:
    """Extract the target path for the interceptor chain.

    Only write tools have an enforceable path. read has a path but the
    chain must not deny reads based on writable_scope — we do not send
    a target for reads so stage 1 waves them through.
    bash and story_commit have no path — target is None.
    """
    # A target that is not a string is still a target: coercing it keeps
    # the call inside stage 1's check, where it is refused for being
    # outside the scope. Returning None would drop it from the check and
    # wave it through — failing open on exactly the malformed input a
    # guard exists for.
    def _as_text(value):
        return value if isinstance(value, str) else (
            None if value is None else str(value))

    if tool_name in _WRITE_TOOLS:
        return _as_text(tool_args.get("path"))
    return None


async def _ask_permission(ex, tool_name: str, tool_args: dict,
                          target: str, tool_call_id: str):
    """Park on a `permission` question. None when it could not park.

    RETURNS None RATHER THAN RAISING ON FAILURE, and the caller then
    falls back to the plain refusal. A run with no database context —
    the null-executor tests, a console dispatch — has nobody to ask,
    and turning that into an exception would break paths that never
    wanted a park. The refusal is the honest answer there.
    """
    ctx = ex._subagent_context
    if ctx is None or ctx.get("conn") is None:
        return None
    if ex._envelope.generation is None:
        return None

    from kernel.harness.effects import classify
    from kernel.harness.destructive import reason as _why
    from kernel.harness.park import ParkRequested
    from kernel.harness.permission import ask

    got = await ask(
        ctx["conn"], ex._execution_id, ex._envelope.generation,
        tool_name, tool_args, classify(tool_name, tool_args),
        target or "", _why(tool_args.get("command", "")))
    if not isinstance(got, int):
        # A malformed or duplicate ask. NOT a park — parking on a
        # question nobody was shown strands the run silently, which is
        # worse than the refusal the caller falls back to.
        logger.warning("permission ask refused for execution %s: %s",
                       ex._execution_id, got)
        return None
    raise ParkRequested(
        got, "permission",
        f"parked for approval of one {tool_name} call")


async def run_gate(ex, tool_name: str, tool_args: dict,
                   tool_call_id: str) -> Optional[ToolResult]:
    """Run the interceptor chain. None to proceed, a ToolResult to refuse.

    Opted out (`chain=None`, the adapter-seam tests) means no gate at
    all, which is why the whole body hangs off that check rather than
    the caller doing it: the opt-out is part of the policy, not part of
    the routing.
    """
    if ex._chain is None:
        return None

    target = _extract_target(tool_name, tool_args)
    # Resolve relative paths the way the SANDBOX resolves them —
    # against the workspace root. The shim does
    # `join(WORKSPACE, path)` for any non-absolute path, so a guard
    # comparing the raw string against an absolute scope answers a
    # different question than the one that matters and refuses
    # every ordinary relative path.
    #
    # Found by E9's positive control: two real legs wrote nothing
    # because `edit src/counter.py` was refused four times running,
    # and the model — correctly — gave up and asked a human.
    if target and not os.path.isabs(target) and ex._envelope.workspace_path:
        target = os.path.normpath(
            os.path.join(ex._envelope.workspace_path, target))

    ctx = InterceptorContext(
        writable_scope=list(ex._envelope.writable_scope),
        allowed_tools=list(ex._envelope.allowed_tools) if ex._envelope.allowed_tools is not None else None,
        # Named gap: stage 4 (budget) has no route from the loop.
        # Set to a large value so stage 4 does not fire — it would
        # deny every call if budget_remaining were 0.
        budget_remaining=1_000_000,
        execution_id=ex._execution_id,
        depth=0,
        approved_calls=ex._approved_calls,
        effect_policy=getattr(ex._profile, "effect_policy", None),
    )

    call = ToolCall(tool=tool_name, args=tool_args, target=target)
    result = ex._chain(call, ctx)

    if result.proceed:
        return None

    denial = result.denial
    # Ec3c — AN APPROVAL DEMAND BECOMES A PARK, not a refusal.
    # Refusing was the deadlock: the corrective said to ask a
    # person, and nothing carried the answer back, so the same
    # call was refused identically for twelve legs. Now the run
    # stops, a person sees this exact call, and their yes is
    # read at the start of the next leg.
    if denial is not None and denial.code == "E_APPROVAL_REQUIRED":
        parked = await _ask_permission(
            ex, tool_name, tool_args, target, tool_call_id)
        if parked is not None:
            return parked
    corrective = denial.corrective if denial else "denied by interceptor chain"
    return ToolResult(
        tool_call_id=tool_call_id,
        output="",
        error=corrective,
        exit_code=1,
    )


__all__ = ["_OPEN_BEFORE_PLAN", "_WRITE_TOOLS", "_ask_permission",
           "_extract_target", "needs_approved_plan", "run_gate"]
