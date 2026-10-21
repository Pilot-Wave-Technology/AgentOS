"""BridgeToolExecutor — routes tool calls to bridge or worker-side handlers.

Two dispatch paths (decision 7): sandbox-side via the bridge (read,
write, bash) and worker-side in-process (story_commit and the rest).
The interceptor chain (ported from TS to Python in cycle 1b) runs as a
direct function call — no subprocess, no IPC, no node dependency.

propose_completion is handled internally by the loop and never reaches this
executor. story_commit is worker-side because the sandbox holds no git
credential (rule 9).

The executor accepts an ExecutionEnvelope (P0) carrying writable_scope,
allowed_tools, workspace_path, and generation.

Named gaps (cycle 1):
  - Stage 4 (budget) is inert: budget_remaining is always 0 because
    run_leg calls tool_executor(name, args, id) with no budget channel.
  - bash has no target (None), so stage 1 cannot constrain it by path,
    and read's target is logged, never denied by writable_scope.

WHAT THIS FILE IS, after two splits. 415 lines, then 359, now the CALL
PATH and nothing else: where each tool runs (two sets read off the
catalog), what this executor may do (`__init__`), the error boundary
every call crosses (`__call__`), and the four gates in order
(`_call_inner` — chain, plan, the one recorded door into the
environment, route). One subject, read top to bottom.

Everything else is a module beside it:
  - `gate.py` — POLICY: the target a call names, the chain, a demand for
    approval becoming a park, the set of tools open before a plan.
  - `executor_tools.py` — BODIES that need the executor rather than just
    its arguments (`env_setup`, `goal_knowledge`, `remote_commit`,
    `dispatch_subagent`).
  - `executor_env_install.py` — the one install path that RECORDS, and
    the quoting, command shapes and replay honesty that go with it.
  - `executor_worker_dispatch.py` — `worker_execute`, the `if`-chain
    that was `_worker_execute` here. Grep the old name and land there.

WHAT HELD THE LAST TWO BACK, since the previous version of this text
argued at length that they could not move. Six tests read this file BY
PATH, so moving code out from under one leaves it passing against a file
the code has left — `test_every_worker_dispatch_has_a_function` would
have passed VACUOUSLY, on an empty set of dispatches. Correct about the
harm, wrong about the remedy: it kept the file at twice its ceiling, and
D3 has to walk 109 substrate branches through here. So the code moved
and THE GUARDS MOVED WITH IT. The three pinning relocated text now read
every `kernel/harness/executor*.py` — the generalisation
`test_tool_bindings` already made when `tools_worker` became a package,
where which file a name lives in is housekeeping and that the name
resolves is the contract.

WHAT IS STILL PINNED HERE, not to be moved without moving its guard in
the same commit: `SANDBOX_TOOLS`/`WORKER_TOOLS` as `tools_by_side(...)`
and not literals; `BridgeToolExecutor.__init__`'s signature, AST-walked
against three construction sites; `bridge_timeout(tool_name,
tool_args)`; `refusal(command, manager,`; and the E6h refusal, which
must name both `register_plan` and `_OPEN_BEFORE_PLAN` within 700
characters of where it starts — so THIS PROSE MUST NOT SPELL THAT ERROR
CODE, or the guard reads the sentence describing the refusal instead of
the refusal, the way `test_both_sides_read_one_file` was once defeated
by its own explanation.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Callable

from kernel.bridge.client import call_bridge
from kernel.bridge.limits import bridge_timeout
from kernel.envelope import ExecutionEnvelope
from kernel.harness.chain import run_chain as _default_run_chain
from kernel.harness.executor_tools import (
    dispatch_subagent, env_setup, goal_knowledge, remote_commit)
from kernel.harness.executor_worker_dispatch import worker_execute
from kernel.harness.gate import (
    _OPEN_BEFORE_PLAN, _WRITE_TOOLS, _ask_permission, _extract_target,
    needs_approved_plan, run_gate)
from kernel.harness.models.types import ToolResult
from kernel.harness.park import ParkRequested
from kernel.harness.propose_completion import InvalidReasonSetError
from kernel.tools.catalog import tools_by_side

logger = logging.getLogger(__name__)

# WHERE EACH TOOL RUNS — read off the catalog, not restated here.
#
# These were two hand-written literals, and `web_fetch` sat in the worker
# one for months with no function behind it: the name said routed, nothing
# answered. The catalog declares `side=` beside the schema now, so the
# only way to add a tool the model can see is to say where it runs, and
# these two sets follow from that rather than being kept level with it.
#
# The DIVISION is still a policy worth stating. Sandbox-side is anything
# that only touches the disposable container. Worker-side is everything
# the sandbox cannot satisfy and must not be given: `story_commit` needs
# git and the real workspace; `skill` reads the profile, which lives on
# the worker; `ask_human` and `register_plan` write to the database and
# park the run; `env_install` is worker-side because it RECORDS — an
# install the Verifier cannot see is worse than none; `goal_knowledge`
# reads the goal's record, and the point of it is that an executor can
# find out what the goal already settled, not that it can reach a
# database. The sandbox holds no credentials, and that is the line.
SANDBOX_TOOLS = tools_by_side("sandbox")

# WHAT AN UNFINISHED WAIT SLICE LOOKS LIKE. Spelled here and in
# `bridge/tools_jobs.py`, which is one spelling too many -- but the
# sandbox module is stdlib-only and loaded by bare name, so it cannot
# import this, and a shared constant would have to live in a third
# module both can reach. A test asserts the two agree.
_WAIT_INCOMPLETE = "[still running after the wait slice]"
WORKER_TOOLS = tools_by_side("worker")

# propose_completion is handled by the loop, never reaches the executor


# Exceptions that are CONTROL FLOW, not failure. A blanket handler
# around tool dispatch must let these through or the thing they signal
# silently stops happening -- and what stops happening here is a run
# pausing to ask a person.
_CONTROL_FLOW = (ParkRequested, InvalidReasonSetError)


class BridgeToolExecutor:
    """Route tool calls between sandbox (bridge) and worker (local).

    Used as the tool_executor callback for run_leg.

    Args:
        envelope: agent-level execution policy. See kernel/envelope.py.
        bridge_port: localhost port for the sandbox bridge shim.
        chain: interceptor chain function.
            Default: run_chain from kernel.harness.chain.
            None: explicit opt-out (adapter-seam tests).
            Any other callable: used directly.
            Anything else: TypeError (fail closed — no silent fallback).
        execution_id: for the InterceptorContext sent to the chain.
    """

    def __init__(
        self,
        envelope: Optional[ExecutionEnvelope] = None,
        bridge_port: int = 9100,
        bridge_host: str = "localhost",
        chain: Optional[Callable] = _default_run_chain,
        execution_id: int = 0,
        profile=None,
        subagent_context: Optional[dict] = None,
        plan_approved: Optional[bool] = None,
        plan_scope=None,
        approved_calls=(),
        allowed_hosts=None,
        mounts_env=None,
    ):
        # Validate chain parameter: must be None or callable. No magic strings,
        # no silent fallback. A requested chain that is not callable is a
        # programming error, not a graceful degradation.
        if chain is not None and not callable(chain):
            raise TypeError(
                f"chain must be None (opt-out) or a callable, got {type(chain).__name__}: {chain!r}"
            )

        self._envelope = envelope or ExecutionEnvelope()
        # Seconds this leg spent blocked in `bash_wait`. Read by
        # `leg_ending` so the lease clock measures the LEG rather than
        # the jobs it waited on. See `_wait_for_job`.
        self._blocked_s = 0.0
        self._port = bridge_port
        # WHERE the shim is, not just on which port. Defaulted to
        # localhost so Docker and E0 are unchanged; a pod substrate
        # resolves the execution's own address and passes it here,
        # because on Kubernetes the sandbox is in a different pod and
        # the caller's loopback answers nothing.
        self._host = bridge_host
        self._chain = chain
        self._execution_id = execution_id
        # The profile this execution runs under. Needed by `skill`, which
        # resolves names against the profile's own skill directories.
        self._profile = profile
        # What dispatching a child requires: the connection and adapter to
        # run it with, and — read at call time, not at construction — the
        # parent's depth and remaining budget. budget_remaining is a
        # callable because a value captured when the executor was built
        # would be the budget at step 0, not at the step that dispatches.
        self._subagent_context = subagent_context
        # None = unknown/not required; False = required and not yet given;
        # True = approved. Set by the worker, which is the only thing that
        # can see the approvals table.
        self._plan_approved = plan_approved
        # WHICH TOOLS THAT APPROVAL COVERS, when it covers only some.
        # None means an ordinary plan: a person read it and approved
        # the run, so the gate above is the whole gate. A frozenset is
        # the platform's canned trunk-integration plan, which nobody
        # read because it is the same every time -- and which is
        # therefore allowed to do only what it named.
        self._plan_scope = (None if plan_scope is None
                            else frozenset(plan_scope))
        # Ec3c — signatures a person has approved for this run, read per
        # leg by the worker exactly as `plan_approved` is. It has to be
        # loaded rather than remembered: the approval arrives BETWEEN
        # legs, which is the entire point of parking.
        self._approved_calls = frozenset(approved_calls or ())
        # Ec4a — which hosts this run may reach, from its grants. `None`
        # means the caller could not say and `web_fetch` falls back to the
        # SSRF guard alone; a worker-dispatched run always carries a set,
        # even when it is empty. Empty and None are different answers, so
        # `frozenset()` is never substituted for the unknown case.
        self._allowed_hosts = (None if allowed_hosts is None
                               else frozenset(allowed_hosts))
        # WHICH MOUNT'S EXECUTABLES A BARE TOOL NAME MEANS. See
        # `kernel/worker/leg_env.py`: the registry declares `bins` so a
        # check can say `pytest`, the Verifier honoured it and the
        # executor did not, and execution 219 died of the difference.
        self._mounts_env = mounts_env or {}
        # Published by run_leg after every step. A sub-agent's budget is
        # sliced from what the PARENT has left, so this has to be current
        # at the moment of dispatch, not at construction. Starts high so a
        # caller that never updates it does not accidentally starve a
        # child to zero and read that as 'no budget'.
        self.budget_remaining = 1_000_000

    async def __call__(
        self, tool_name: str, tool_args: dict, tool_call_id: str,
    ) -> ToolResult:
        """Execute a tool call. Runs the interceptor chain, then routes.

        EVERY TOOL CALL COMES FROM A MODEL, which is the least
        trustworthy source of input the harness has, and a tool that
        raises takes the leg down with it -- the run dies on an
        AttributeError rather than the model reading an error and trying
        again. So the arguments are coerced here and the routing is
        wrapped, at the one place every call passes through, rather than
        in each tool where the next tool added would miss it.
        """
        # ARGS MAY NOT BE AN OBJECT. A model that emits a JSON string
        # where a dict belongs produced `'str' object has no attribute
        # 'get'` from deep inside a tool. Refusing here says which call
        # was malformed; failing there says only that something was.
        if not isinstance(tool_args, dict):
            return ToolResult(
                tool_call_id=tool_call_id, output="",
                error=f"E_BAD_ARGUMENTS: {tool_name!r} was called with "
                      f"{type(tool_args).__name__}, not an object. Send the "
                      f"arguments as a JSON object and try again.",
                exit_code=1)

        try:
            return await self._call_inner(tool_name, tool_args, tool_call_id)
        except _CONTROL_FLOW:
            # NOT ERRORS. These are how a tool says something other than
            # "here is your result", and run_leg is what handles them --
            # ParkRequested is `ask_human` and `register_plan` stopping
            # the leg to reach a person.
            #
            # Swallowing them broke the entire human-in-the-loop path:
            # the run reported E_TOOL_RAISED and carried on instead of
            # parking, so a question was written that nothing was
            # waiting on. Caught by H6 and Fb5, both of which assert
            # that asking PARKS.
            raise
        except Exception as exc:                              # noqa: BLE001
            # NOT a blanket swallow: the leg continues and the model is
            # told, but the exception type and message go into the error
            # verbatim so the journal shows what actually broke rather
            # than a generic failure.
            logger.exception("tool %s raised", tool_name)
            return ToolResult(
                tool_call_id=tool_call_id, output="",
                error=f"E_TOOL_RAISED: {tool_name!r} raised "
                      f"{type(exc).__name__}: {exc}. This is a defect in the "
                      f"tool, not in your call; try a different approach.",
                exit_code=1)

    async def _mirror_job(self, extras: dict) -> None:
        """Tell the panel what a job is doing, so somebody can ask.

        BEST-EFFORT AND SILENT. A mirror that fails is a question the
        assistant answers less well; a mirror that raises is a leg that
        dies holding a job. Threaded because the POST is synchronous
        urllib -- the pod's image carries no async HTTP client -- and
        the leg loop is one event loop shared with everything else.
        """
        facts = (extras or {}).get("job")
        if not facts or self._execution_id is None:
            return
        await self._mirror_one(facts)

    async def _mirror_jobs(self, extras: dict) -> None:
        """Reconcile every job the sandbox knows about.

        `bash_jobs` carries the whole registry as data, so a job the
        model started and never waited on -- which gets a start write and
        no finish write, and reads as running for ever -- is corrected
        the next time anything asks for the list.
        """
        if self._execution_id is None:
            return
        for facts in (extras or {}).get("jobs") or ():
            await self._mirror_one(facts)

    async def _mirror_one(self, facts: dict) -> None:
        try:
            import asyncio

            from kernel.harness import claim
            from kernel.worker import job_mirror

            # A7 -- the tree this job ran against, taken once at its
            # start and once at its end. `stamp` is what keeps that from
            # becoming a `git add -A` per poll.
            facts = await claim.stamp(self, facts)
            await asyncio.to_thread(job_mirror.report,
                                    int(self._execution_id), facts)
        except Exception:                                   # noqa: BLE001
            logger.debug("job mirror failed; the job is unaffected",
                         exc_info=True)

    async def _interrupted(self) -> bool:
        """Is somebody waiting to say something to this run?

        FALSE ON ANY DOUBT. This buys a faster escape hatch; a wait that
        ended because the mailbox read hiccupped would turn a two-hour
        job into a leg boundary for no reason, which is worse than the
        delay it removes. No connection, no execution, any exception --
        all of them mean "carry on waiting".

        READ-ONLY. Nothing is drained here: `take_proposals` is the only
        thing that may apply an intent, and it runs where it always did,
        at the boundary this return creates. Draining in two places is
        how an instruction gets applied twice or lost.
        """
        conn = (self._subagent_context or {}).get("conn")
        if conn is None or not self._execution_id:
            return False
        try:
            from kernel.mailbox.api import pending_proposals

            return bool(await pending_proposals(conn,
                                                int(self._execution_id)))
        except Exception:                                   # noqa: BLE001
            logger.debug("mailbox peek failed; the wait continues",
                         exc_info=True)
            return False

    async def _wait_for_job(self, tool_args: dict, tool_call_id):
        """One model step, however long the job runs.

        THE SLICES ARE NOT STEPS. The sandbox blocks for a bounded slice
        because the worker is holding an HTTP read open, and a read held
        for an hour is a dead bridge. So the sandbox answers "still
        running" and this loop asks again -- and the model sees ONE tool
        call that returns when the job is done.

        WHY NOT LET THE MODEL POLL. A poll is a step, a journal entry and
        a round trip of tokens; and `bash_output` on one job signs the
        same stall signature unless the cursor moves, so the leg dies
        `stalled` on the eighth identical one. A forty-minute suite is
        not watchable that way at all. That is why this is a loop in the
        harness rather than an instruction in a prompt.

        THE CEILING IS THE JOB'S OWN. There is no separate wait budget to
        get wrong: a job with a declared timeout is stopped by its
        watchdog and this returns the result; a job without one is
        bounded by the leg and the lease, the clocks that already govern
        everything else.
        """
        # CANNOT SPIN. Every iteration is a bridge call that itself
        # blocks for a slice inside the sandbox, so this yields real
        # time rather than burning the event loop, and a bridge fault
        # breaks out instead of retrying for ever.
        # TIME BLOCKED HERE IS NOT THE LEG'S, and the lease clock has to
        # be told. `lease_clock_seconds` is 600 and `job_max_s` is 7200:
        # the platform offers a two-hour job and gives the leg that
        # waits for it ten minutes to live, so a forty-minute wait
        # returns its result and loses the leg in the same instant --
        # the model never gets to act on what it waited for.
        #
        # The clock exists to stop a WEDGED leg holding a pod. A leg
        # blocked on a job with its own watchdog, its own budget and a
        # mirror row updating every slice is the opposite of wedged, so
        # this time is credited back rather than charged. Everything the
        # lease clock actually guards against is still charged: the job
        # itself is bounded by `job_max_s`, which is the clock that
        # belongs to it.
        blocked_from = time.monotonic()
        while True:
            extras: dict = {}
            result = await call_bridge(
                "bash_wait", tool_args, tool_call_id, port=self._port,
                host=self._host,
                timeout=bridge_timeout("bash_wait", tool_args),
                extras=extras,
            )
            # EVERY SLICE IS A MIRROR WRITE, throttled on the other side.
            # This is what makes "how is it going?" answerable DURING a
            # two-hour wait rather than only at the end of it.
            await self._mirror_job(extras)
            if result.error:
                self._blocked_s += time.monotonic() - blocked_from
                return result
            if not str(result.output or "").startswith(_WAIT_INCOMPLETE):
                self._blocked_s += time.monotonic() - blocked_from
                return result
            # AND EVERY SLICE IS A CHANCE TO BE INTERRUPTED.
            #
            # A steer is injected AT A STEP BOUNDARY, and this whole call
            # is one step however long the job runs -- which is the point
            # of it, and is what makes a two-hour job affordable. It also
            # made the model unreachable for exactly as long as the job
            # was worth interrupting. Measured on run 262: a steer was
            # sent while a suite ran, the worker went quiet for eight
            # minutes, and it landed only when the job was killed from
            # outside, because that was the first step boundary since the
            # wait began. Before this loop existed the model polled, so a
            # boundary came every poll and a steer landed in seconds --
            # so this was a regression in the human's escape hatch that
            # arrived with an otherwise good change.
            #
            # THE JOB IS NOT TOUCHED. It keeps running, it survives the
            # leg boundary by design, and the brief names running jobs
            # (A2b) so the model can pick it back up with bash_wait. The
            # only thing that ends is this step.
            if await self._interrupted():
                self._blocked_s += time.monotonic() - blocked_from
                return ToolResult(
                    tool_call_id=tool_call_id,
                    output=(f"{result.output}\n\n[Returning early: somebody "
                            f"sent an instruction while you were waiting, "
                            f"and it can only be delivered between steps. "
                            f"The job was NOT stopped and is still running "
                            f"-- read what came in, then wait for it again "
                            f"with bash_wait if it still matters.]"),
                    exit_code=0)

    async def _call_inner(
        self, tool_name: str, tool_args: dict, tool_call_id: str,
    ) -> ToolResult:
        # ── Interceptor chain (if not opted out) ─────────────────────────
        denied = await run_gate(self, tool_name, tool_args, tool_call_id)
        if denied is not None:
            return denied

        # ── E6h: no work before the plan is approved ─────────────────────
        if (self._profile is not None
                and getattr(self._profile, "requires_plan", False)
                and needs_approved_plan(tool_name)
                and self._plan_approved is False):
            return ToolResult(
                tool_call_id=tool_call_id, output="",
                error=f"E_PLAN_NOT_APPROVED: {tool_name!r} acts, and this "
                      "profile needs an approved plan first. Call "
                      "register_plan with your steps — the run parks until a "
                      "human approves it, at no cost while it waits. Still "
                      "open until then: "
                      + ", ".join(sorted(_OPEN_BEFORE_PLAN)) + ".",
                exit_code=1)

        # ── AND A SCOPED PLAN APPROVES ONLY WHAT IT NAMED ────────────────
        #
        # The trunk-integration plan is WRITTEN by the platform and
        # approved by a person. (It was once answered by the platform
        # too; that was reversed -- the canned steps run the story's
        # existing checks, and integration is exactly when those can be
        # too few.) The scope survives that change because what a
        # person approves there is three steps they did not author, and
        # this refuses everything outside them.
        #
        # CONFLICTS ARE INSIDE IT NOW. Resolving one takes `write`/`edit`
        # and `story_commit`, and those are in the scope
        # (`integration_plan.TOOL_SCOPE`) -- one approval covers the whole
        # catch-up. What lands here is anything BEYOND that: `bash`, an
        # environment change, a new artifact. `ask_human` and
        # `register_plan` stay in `_OPEN_BEFORE_PLAN`, out of this gate's
        # reach, as the way back to a person.
        #
        # REVISING THE PLAN DROPS THE SCOPE, because a revised plan
        # carries no `tool_scope` and `scope_of` returns None. That is
        # the right answer -- the scope exists because nobody reads an
        # identical plan, and somebody who edited it read it -- but it
        # is worth knowing before reading a trace: on run 99 the person
        # revised, so gen 3 ran unscoped and wrote files freely.
        #
        # `is not None` rather than truthiness: an EMPTY scope would mean
        # "approved for nothing", which no caller should produce and
        # which must not silently read as "approved for everything".
        if (self._plan_scope is not None
                and needs_approved_plan(tool_name)
                and tool_name not in self._plan_scope):
            return ToolResult(
                tool_call_id=tool_call_id, output="",
                error=f"E_PLAN_NOT_APPROVED: the live plan is the "
                      f"platform's trunk-integration plan, which is "
                      f"approved for {', '.join(sorted(self._plan_scope))} "
                      f"and nothing else — {tool_name!r} is outside it. "
                      "Nobody read a plan covering this. Resolving a "
                      "merge conflict needs only `edit`/`write` and "
                      "`story_commit`, which it covers. If the "
                      "integration needs more than that -- a check that "
                      "broke in the merge, a dependency to install -- say "
                      "so with `register_plan` and a person will decide.",
                exit_code=1)

        # ── The environment has one recorded door ────────────────────────
        #
        # env_install records what it installs and the Verifier replays
        # exactly that; the shell installs the same packages and records
        # nothing, so the checks later run without them. The first live
        # story went that way -- `bash: pip install fastapi ...` -- and
        # its own suite was refused with ModuleNotFoundError, which tells
        # the author nothing about their work. env_install's description
        # already asks for this in as many words; being ignored is why it
        # is enforced here instead.
        if tool_name in ("bash", "bash_background"):
            from kernel.harness.env_guard import install_manager, refusal
            command = str(tool_args.get("command") or "")
            manager = install_manager(command)
            if manager:
                return ToolResult(
                    tool_call_id=tool_call_id, output="",
                    # THE CALLER'S OWN SURFACE. Without it the refusal used
                    # to name `env_setup` to a profile that does not hold
                    # it — a correction whose only route out is a tool
                    # chain stage 2 refuses by name.
                    error=refusal(command, manager,
                                  getattr(self._profile, "tools", None)),
                    exit_code=1)

        # ── The sandbox's PATH carries what the setups built ─────────────
        #
        # Worker-side, because the shim runs INSIDE the sandbox and has
        # no idea what a mount is: `_tool_bash` has one fixed
        # `_safe_env()`. Here the envelope knows, and
        # `criterion_env.environment_for` -- the Verifier's own scoping
        # rule, reused rather than copied -- says which mount the
        # command targets.
        if tool_name in ("bash", "bash_background"):
            from kernel.worker.leg_env import with_mount_path
            tool_args = dict(tool_args)
            tool_args["command"] = with_mount_path(
                str(tool_args.get("command") or ""), self._mounts_env)

        # ── Route to bridge or worker ────────────────────────────────────
        if tool_name in SANDBOX_TOOLS:
            # THE WORKER MUST OUT-WAIT THE SANDBOX. `call_bridge`'s own
            # HTTP read timeout was a flat 30s, so a `bash` asking for 90
            # would have come back `bridge unreachable: timed out` — the
            # model's argument apparently ignored, which is worse than not
            # offering it at all.
            if tool_name == "bash_wait":
                return await self._wait_for_job(tool_args, tool_call_id)
            extras: dict = {}
            result = await call_bridge(
                tool_name, tool_args, tool_call_id, port=self._port,
                host=self._host,
                timeout=bridge_timeout(tool_name, tool_args),
                extras=extras,
            )
            await self._mirror_job(extras)
            # `bash_jobs` carries the whole registry, so this is where an
            # abandoned row -- a job started and never waited on, which
            # got a start write and no finish write -- gets corrected.
            await self._mirror_jobs(extras)
            return result
        elif tool_name in WORKER_TOOLS:
            return await worker_execute(self, tool_name, tool_args,
                                        tool_call_id)
        else:
            return ToolResult(
                tool_call_id=tool_call_id,
                output="",
                error=f"unknown tool: {tool_name!r}. "
                      f"Sandbox tools: {sorted(SANDBOX_TOOLS)}. "
                      f"Worker tools: {sorted(WORKER_TOOLS)}.",
                exit_code=127,
            )



# RE-EXPORTED, NOT RE-IMPLEMENTED. `test_emit_and_plan_gate` imports
# `_OPEN_BEFORE_PLAN` and `needs_approved_plan` from this module by name,
# and the E6h refusal above reads the set from the same import. The split
# moved the code; it must not move the import path.
__all__ = ["BridgeToolExecutor", "SANDBOX_TOOLS", "WORKER_TOOLS",
           "_OPEN_BEFORE_PLAN", "_WRITE_TOOLS", "_ask_permission",
           "_extract_target", "dispatch_subagent", "env_setup",
           "goal_knowledge", "needs_approved_plan", "remote_commit",
           "run_gate"]
