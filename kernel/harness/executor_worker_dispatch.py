"""The worker-side dispatch table: which function answers each name.

This is the second half of `executor.py`'s routing. That file decides
WHERE a call runs — sandbox or worker, off the catalog's `side=`. This
one decides WHAT answers it once the answer is known to be worker-side,
and it is the `if`-chain that used to be `BridgeToolExecutor
._worker_execute`. Grep for `_worker_execute` and land here; the move is
wave 4's, and nothing about the routing changed with it.

WHY WORKER-SIDE AT ALL is stated once, in `executor.py` beside the two
sets. The short version: the sandbox holds no credentials. Everything
here either needs one (`story_commit` needs git), needs the worker's own
disk (`skill` reads the profile), or needs the database (everything
else).

THE REPEATED SHAPE IS THE POINT, not duplication to be factored away.
Seven of these tools refuse when `subagent_context["conn"]` is missing,
and each refusal says what THAT tool cannot do without it — `env_install`
says an install the Verifier cannot see is worse than none, `ask_human`
says the run cannot record what it is waiting for. A single generic
"no database context" would be shorter and would tell the model nothing
it can act on, which is the failure `env_guard`'s refusal was rewritten
to avoid. `_NEEDS_CONN` carries the reason per tool rather than deleting
it.

WHAT THE GUARDS READ, since this text moved out from under two of them.
`test_tool_bindings` regexes `tools_worker.X` dispatches out of the
executor family and asserts `tools_worker` defines each `X` — the bug it
generalises is `web_fetch` being routed to a function that did not
exist, every call answering `E_TOOL_RAISED: AttributeError`. That regex
matches BOTH `tools_worker.skill(` and `tools_worker.web_fetch,`,
because a name handed to `asyncio.to_thread` is dispatched without being
called here. So: when adding a branch, keep the attribute access
literal. A local alias, a `getattr`, or a dict built from strings would
each make this module's dispatches invisible to the guard, and the guard
would then pass on an ever-smaller set without ever saying so.
`test_emit_and_plan_gate` likewise requires the `emit_artifact` import
below to be spelled out; do not collapse it into `publish`'s branch.
"""

from __future__ import annotations

import asyncio

from kernel.harness import tools_worker
from kernel.harness.executor_env_install import env_install
from kernel.harness.executor_tools import (
    dispatch_subagent, env_setup, goal_knowledge, remote_commit,
    remote_sync)
from kernel.harness.models.types import ToolResult

# WHICH TOOLS NEED A CONNECTION, and what each of them loses without
# one. Membership is the gate — a name absent here is dispatched with no
# connection check at all, which is deliberate: `subagent` takes its
# connection from the dispatch context itself, and an UNKNOWN name must
# still reach the "unimplemented worker tool" answer at the bottom
# rather than being told the database is missing. Getting that backwards
# turns every typo into a misleading infrastructure complaint.
_NEEDS_CONN = {
    # The criterion and its declared budget both live in the contract,
    # which the sandbox cannot read -- deciding WHICH command and WHICH
    # budget is the whole worker-side part of this tool.
    "run_check": "run_check: no database context",
    "goal_knowledge": "goal_knowledge: no database context",
    "submit_story_review": "submit_story_review: no database context, so "
                           "the review cannot be recorded and the story "
                           "cannot leave 'done'",
    "sync_trunk": "sync_trunk: no database context, so the trunk lane "
                  "to merge cannot be looked up",
    "publish_artifact": "publish_artifact: no database context",
    "emit_artifact": "emit_artifact: no database context",
    "ask_human": "ask_human: no database context, so this run cannot "
                 "record what it is waiting for",
    "reply_to_human": "reply_to_human: no database context, so the answer "
                      "cannot be recorded and the person would keep "
                      "waiting for one already given",
    "register_plan": "register_plan: no database context, so this run "
                     "cannot record what it is waiting for",
    "env_setup": "env_setup: no database context, so the workspace's "
                 "mounts cannot be read",
    "env_install": "env_install: no database context, so this install "
                   "could not be recorded — and an install the Verifier "
                   "cannot see is worse than none",
    "register_action_template": "register_action_template: no database context",
}


def _conn(ex, tool_name: str, tool_call_id: str):
    """The connection, or the refusal that names what is lost without it.

    Returns `(conn, None)` or `(None, ToolResult)`. Two returns rather
    than raising, because every caller here answers the model with a
    ToolResult and an exception would take the leg down instead.
    """
    ctx = ex._subagent_context
    if ctx is not None and ctx.get("conn") is not None:
        return ctx["conn"], None
    return None, ToolResult(tool_call_id=tool_call_id, output="",
                            error=_NEEDS_CONN[tool_name], exit_code=1)


async def worker_execute(ex, tool_name: str, tool_args: dict,
                         tool_call_id: str) -> ToolResult:
    """Execute a worker-side tool. The sandbox cannot reach these."""
    if tool_name == "story_commit":
        # A pod substrate keeps the repositories in the pod, so the
        # commit has to happen there; see `remote_commit`.
        host = getattr(ex._envelope, "workspace_host", "") or ""
        if host:
            return await remote_commit(ex, host, tool_args, tool_call_id)
        # The local path shells out to git four times per mount.
        return await asyncio.to_thread(
            tools_worker.story_commit, ex._envelope, tool_args,
            tool_call_id)
    if tool_name == "skill":
        return tools_worker.skill(ex._profile, tool_args, tool_call_id)
    if tool_name == "web_fetch":
        # Up to 20s per hop and up to four hops -- 80 seconds of the
        # conductor answering nothing, on a tool the model may call
        # whenever it likes.
        return await asyncio.to_thread(
            tools_worker.web_fetch, tool_args, tool_call_id,
            ex._allowed_hosts)
    if tool_name == "artifact_read":
        return tools_worker.artifact_read(tool_args, tool_call_id)

    conn = None
    if tool_name in _NEEDS_CONN:
        conn, refused = _conn(ex, tool_name, tool_call_id)
        if refused is not None:
            return refused

    if tool_name == "run_check":
        from kernel.harness.tools_worker.checks import run_check
        return await run_check(ex, conn, tool_args, tool_call_id)
    if tool_name == "goal_knowledge":
        return await goal_knowledge(ex, conn, tool_args, tool_call_id)
    if tool_name == "submit_story_review":
        return await tools_worker.submit_story_review(
            conn, ex._envelope, tool_args, tool_call_id)
    if tool_name == "sync_trunk":
        return await remote_sync(ex, conn, tool_call_id)
    if tool_name in ("publish_artifact", "emit_artifact"):
        if tool_name == "emit_artifact":
            from kernel.harness.emit import emit_artifact as _fn
        else:
            from kernel.harness.publish import publish_artifact as _fn
        return await _fn(conn, ex._envelope, ex._execution_id,
                         tool_args, tool_call_id)
    if tool_name in ("ask_human", "register_plan"):
        fn = (tools_worker.ask_human if tool_name == "ask_human"
              else tools_worker.register_plan)
        return await fn(conn, ex._execution_id, ex._envelope.generation,
                        tool_args, tool_call_id)
    if tool_name == "reply_to_human":
        return await tools_worker.reply_to_human(
            conn, ex._execution_id, tool_args, tool_call_id)
    if tool_name == "env_setup":
        return await env_setup(ex, conn, tool_args, tool_call_id)
    if tool_name == "env_install":
        return await env_install(ex, conn, tool_args, tool_call_id)
    if tool_name == "register_action_template":
        return await tools_worker.register_action_template(
            conn, ex._envelope, ex._execution_id, tool_args, tool_call_id)
    if tool_name == "subagent":
        return await dispatch_subagent(ex, tool_args, tool_call_id)
    return ToolResult(
        tool_call_id=tool_call_id,
        output="",
        error=f"unimplemented worker tool: {tool_name!r}",
        exit_code=1,
    )
