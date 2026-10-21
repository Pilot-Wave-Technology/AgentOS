"""E7b/E7c — running the child, which is what was never wired.

`subagent.py` had the policy from the start: derive an attenuated
envelope, refuse past the depth ceiling, slice a budget, build the
child's prompt, bound its answer. It was complete, it was tested, and
ONLY THE TESTS IMPORTED IT. `executor.py` called
`tools_worker.subagent(...)`, a function nobody wrote, so every dispatch
raised AttributeError -- while `subagent` sat in the profile surface
looking available.

That is the defect this repository names in its own corpus and in
E6f's docstring: "a function existed, a test called it directly, and
nothing joined it to the flow". The policy half being excellent is what
made it survive; there was nothing to find by reading either file.

WHAT THE CHILD SHARES AND WHAT IT DOES NOT. It runs on the parent's
execution and generation, so its work is part of this run and reaps with
it. It is separated by DEPTH -- every row it writes is at depth+1, which
is what stops a parent being credited with its child's steps and looking
alive while doing nothing. Its envelope is attenuated, its budget is a
slice the parent can still afford to read the answer out of, and its
executor is built with NO dispatch context, so a grandchild is refused
by construction as well as by MAX_DEPTH.

A REFUSAL IS A RESULT. Depth, attenuation and budget all come back as
`SubagentRefused`, and each becomes a ToolResult the model reads and
acts on -- not an exception that ends the leg.
"""

from __future__ import annotations

import logging

from kernel.harness.subagent import (SubagentRefused, bound_answer,
                                     prepare_dispatch)
from kernel.harness.models.types import ToolResult

logger = logging.getLogger(__name__)


async def subagent(*, parent_envelope, parent_profile, conn, adapter,
                   execution_id: int, generation: int, actor_id: str,
                   parent_depth: int, parent_budget_remaining: int,
                   bridge_port: int, tool_args: dict,
                   tool_call_id: str) -> ToolResult:
    """Dispatch one child and return its answer as this call's result."""
    flavour = str(tool_args.get("flavour") or "investigate")
    task = str(tool_args.get("task") or "")

    try:
        plan = prepare_dispatch(
            parent_envelope=parent_envelope,
            parent_depth=parent_depth,
            parent_budget_remaining=parent_budget_remaining,
            flavour=flavour,
            task=task,
            child_workspace=tool_args.get("workspace"),
            requested_budget=tool_args.get("budget"),
        )
    except SubagentRefused as refused:
        # The three refusals are the point of prepare_dispatch, and each
        # is something the model can do something about.
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error=f"E_SUBAGENT_REFUSED: {refused}",
                          exit_code=1)

    from kernel.harness.executor import BridgeToolExecutor
    from kernel.harness.loop import run_leg

    child_executor = BridgeToolExecutor(
        envelope=plan["envelope"],
        bridge_port=bridge_port,
        execution_id=execution_id,
        profile=parent_profile,
        # NO subagent dispatch context: a child that reached this tool would be
        # told there is no dispatch context, which is the same answer
        # MAX_DEPTH gives and does not depend on it. Database connection
        # is preserved so worker-side tools (like register_action_template) function.
        subagent_context={"conn": conn},
        # A child never registers a plan, so it must not be blocked
        # waiting for one to be approved.
        plan_approved=True,
    )

    logger.info("subagent: running child at depth %d for execution %s",
                plan["depth"], execution_id)
    result = await run_leg(
        conn=conn, adapter=adapter, execution_id=execution_id,
        generation=generation, actor_id=actor_id,
        messages=plan["messages"],
        profile=parent_profile,
        token_budget=plan["token_budget"],
        step_budget=plan["step_budget"],
        tool_executor=child_executor,
        # A child has no prior leg of its own, and rehydrating would
        # hand it the PARENT's brief as though it were its own history.
        rehydrate=False,
        depth=plan["depth"],
    )

    answer = bound_answer(result.get("final_text") or "")
    logger.info("subagent: child finished, %d step(s), predicate=%s",
                result.get("steps", 0), result.get("predicate"))
    return ToolResult(tool_call_id=tool_call_id, output=answer, exit_code=0)
