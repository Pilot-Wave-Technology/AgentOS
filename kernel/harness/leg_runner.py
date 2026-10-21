"""The leg, in a process of its own. P6 of ES4 cycle 1, and D2's runner.

A CLI entry point for running a leg in its own process. The test harness
spawns this as a subprocess and can SIGKILL it mid-step to simulate a
mid-edit kill for E6b.

D2 GIVES IT A SECOND JOB, AND IT IS THE ONE THE PHASE IS NAMED FOR.
Plan §6: "the runner in the worker container; the conductor dispatches
and observes." This is that runner. In an execution pod it runs in the
WORKER container -- beside the sandbox, on the same workspace claim, on
the far side of the two-uid boundary from the model's `bash` -- and the
conductor reads its result rather than producing it.

Two things changed here to make that true, and neither is cosmetic:

  * IT TAKES `leg_lock` ITSELF. §10 chose option (a) -- the pod holds
    the connection -- for one reason: the lock is an advisory lock that
    dies with its connection, so a CONDUCTOR deploy must not be able to
    release a lock protecting a leg that is still running in a pod. D1
    built the connection and nothing used it. This is the use. A runner
    that could not get the lock does not run the leg and says so in its
    result, because somebody else already has it.

  * ITS RESULT IS A WIRE FORMAT. `print(json.dumps(result))` was fine
    while the only reader was a test script from the same afternoon. The
    reader is now a conductor that may be on a different deploy from
    this pod, so the line is versioned and self-identifying --
    `kernel/harness/loop_wire.py` argues each property against a failure
    this build has already had somewhere else.

WHAT IS STILL NOT WIRED, said plainly rather than implied, because a
seam that looks finished is how a half cutover ships. Nothing in the
pod's manifest STARTS this: the worker container's command is
`k8s_workspace.worker_command()`, which runs podfs alone. Turning
`AGENTOS_EXECUTION_SQL_INSTANCE` away from `off` stands the conductor's
leg claiming down (`worker/executor_pass.conductor_leg_slots`), so those
two belong in the same deploy -- and so do two more. The pod has no
model key: `conductor/sandbox_spec.POD_ENV_ALLOWLIST` is a closed list
and carries none, which is I10's premise and not yet its proof. And
`ensure_reachable` and `ensure_setup` live in `worker/advance.py` on the
conductor's side of the claim, so a deploy that stands the conductor
down without moving them is a fleet whose sandboxes are never set up.
Four things, one deploy; this file is one of them.

Usage (`--help` for the full argument list):
    python -m kernel.harness.leg_runner --execution-id 42 --generation 0
        --actor-id test-actor --dsn "$DATABASE_URL" --bridge-port 9100
        --envelope-json '{...}' --messages-json '[...]' --adapter mock

The runner takes a pooled connection, deserialises the envelope, messages
and tools, builds the adapter and executor, takes `leg_lock` on that
connection, asks `loop_gate` whether the leg may start at all, and calls
`run_leg`. The result leaves as ONE marked, versioned line on stdout
(`loop_wire`) so a reader can find it among log output; a failure leaves
a marked error line and exit 1, because a reader that gets nothing cannot
tell a crash from a process that is still working.

FROM THE POOL, THOUGH IT IS ONE PROCESS RUNNING ONE LEG. A leg holds its
connection for the whole leg -- `leg_lock` is a Postgres advisory lock and
release must happen on the connection that took it, and the sub-agent
context writes on that same connection -- so a connection to itself is
correct. Step 1 used to be a bare `asyncpg.connect`, and that is a
different claim: it put the leg outside `POOL_MAX`, gave it a second copy
of the `search_path` setting `make_pool` owns, and skipped `SET ROLE
agentos_worker`, so this CLI ran its legs as the superuser while the real
worker runs them under the role that cannot write `criterion_results`.
A harness that is more privileged than production cannot prove anything
about production.

`SET ROLE` IS CONDITIONAL SINCE D2-prep AND THAT IS NOT A WEAKENING. In
the execution pod the connection already IS the narrow role `agentos_leg`,
which was deliberately never made a member of `agentos_worker` (plan
§10a), so `loopbridge` asks the database first and adopts only where it
may. Run from the conductor this file still gets the role exactly as
before; run in the pod it gets a smaller one.

Adapters:
  "mock" — the busy adapter in `leg_runner_mock`, configurable steps
  "gemini" — GeminiAdapter (requires GEMINI_API_KEY)
  "anthropic" — AnthropicAdapter (requires ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from kernel.harness import loop_wire


async def run(args: argparse.Namespace) -> None:
    from kernel.envelope import ExecutionEnvelope
    from kernel.harness.executor import BridgeToolExecutor, _default_run_chain
    from kernel.harness.loop import run_leg

    # ── Parse JSON inputs ────────────────────────────────────────────────
    envelope_dict = json.loads(args.envelope_json)
    envelope = ExecutionEnvelope(
        writable_scope=envelope_dict.get("writable_scope", []),
        allowed_tools=envelope_dict.get("allowed_tools", []),
        workspace_path=envelope_dict.get("workspace_path", ""),
        generation=envelope_dict.get("generation", args.generation),
    )

    messages = json.loads(args.messages_json)

    # ── E6f: the profile decides the surface ─────────────────────────────
    # --kind/--domain resolve a profile; its tool list becomes BOTH the
    # envelope's allowed_tools (what chain stage 2 enforces) and the schemas
    # the model is shown. One object, so the two cannot disagree — offering
    # a tool the envelope refuses wastes a step and teaches nothing.
    #
    # --tools-json stays for the null-executor tests, which run a two-tool
    # surface with no profile behind it.
    profile = None
    tools = None
    if args.kind and args.domain:
        from kernel.profiles.registry import resolve
        profile = resolve(args.kind, args.domain)
        envelope = ExecutionEnvelope(
            writable_scope=envelope.writable_scope,
            allowed_tools=profile.tool_names(),
            workspace_path=envelope.workspace_path,
            generation=envelope.generation,
        )
    else:
        tools = json.loads(args.tools_json)

    # ── Build adapter ────────────────────────────────────────────────────
    if args.adapter == "mock":
        from kernel.harness.leg_runner_mock import build_mock_adapter

        adapter = build_mock_adapter(args.mock_steps)
    else:
        from kernel.harness.models import get_adapter
        adapter = get_adapter(args.adapter)

    # ── Build executor ───────────────────────────────────────────────────
    # ── The chain, and what a child needs to be dispatched ───────────────
    # The profile path ENFORCES. It carries a real allowed_tools, so chain
    # stage 2 has something to check; running it with chain=None would
    # derive a surface and then decline to apply it, which is how the
    # sub-agent dispatch shipped a hole an hour ago.
    #
    # The legacy --tools-json path keeps chain=None: its envelopes carry
    # allowed_tools=[] (deny-all), so enabling the chain there would refuse
    # every call in tests that predate profiles.
    subagent_context = None
    if profile is not None:
        # budget_remaining is read at CALL time, not captured here — a
        # value taken now is the budget at step 0, not at the step that
        # dispatches. run_leg updates the executor as it spends.
        subagent_context = {
            "conn": None,          # filled below, once connected
            "adapter": adapter,
            "actor_id": args.actor_id,
            "depth": 0,
            "budget_remaining": lambda: executor.budget_remaining,
        }

    executor = BridgeToolExecutor(
        envelope=envelope,
        bridge_port=args.bridge_port,
        chain=None if profile is None else _default_run_chain,
        execution_id=args.execution_id,
        profile=profile,
        subagent_context=subagent_context,
    )

    # ── Take a connection from the kernel pool ───────────────────────────
    # `make_pool` and not a second `create_pool` call: the role gate and
    # the search_path are configured in exactly one place, and a copy here
    # is a second thing to forget to change.
    #
    # THE COST IS NOW ZERO, where this used to argue it was worth paying.
    # D2-prep made `loopbridge` size its pool from the process it is in
    # (LEG_POOL_MIN, LEG_POOL_MAX = 1, 2), so in the execution pod this is
    # one socket — the same one a bare `asyncpg.connect` would have opened
    # — and it is counted and capped like every other.
    from kernel.harness.loop_gate import refuse_reason
    from kernel.harness.loop_result import refusal
    from kernel.harness.loop_wire import encode
    from kernel.loopbridge import make_pool
    from kernel.worker.lease import leg_lock

    pool = await make_pool(args.dsn)
    try:
        # ONE CONNECTION FOR THE WHOLE LEG, AND THE LOCK IS ON IT.
        #
        # `leg_lock` releases on the connection that took it, and the
        # sub-agent context writes on that same one, so acquiring twice
        # here would be two different claims about who owns this
        # execution. In the pod it is also one connection out of TWO
        # (`loopbridge.LEG_POOL_MAX`): a second acquire held across the
        # leg would leave the pod with no spare at all, which is the
        # arithmetic §11.5i asks D2 to watch.
        async with pool.acquire() as conn:
            async with leg_lock(conn, args.execution_id) as held:
                if not held:
                    # NOT AN ERROR, AND NOT A LEG. Somebody else is
                    # running this execution -- the other conductor
                    # replica, or a previous pod that has not finished
                    # dying. Reported in the refusal shape so the reader
                    # sees `stop_reason` and does not mistake it for a
                    # leg that ran and stopped talking (`loop_result`).
                    print(encode(refusal("busy")))
                    return
                # ── D4 · BETWEEN LEGS IS HERE ────────────────────
                #
                # The conductor's kill switch works by declining to
                # claim the next leg (`worker/loop.py`, `frozen`). A pod
                # that drives its own legs asks nobody, so without this
                # Freeze degrades to "stops new stories only" and an
                # operator watches a runaway story keep committing. Under
                # the lock, before the first model call, and refusing
                # rather than raising: nothing has been spent, so this is
                # the same not-a-leg the `busy` branch above reports.
                gate = await refuse_reason(conn, args.execution_id)
                if gate is not None:
                    print(encode(refusal(gate)))
                    return
                if subagent_context is not None:
                    subagent_context["conn"] = conn

                # ── THE PLAN GATES, WHICH THIS PATH NEVER ARMED ──────
                #
                # The executor is built above, before there is a
                # connection to read anything with, so it got neither
                # `plan_approved` nor `plan_scope` -- and BOTH GATES
                # FAIL OPEN on the default. `_plan_approved is False`
                # is never true when it is None, and the scope check is
                # `is not None`. The worker path passes real values
                # (`worker/executor_pass.py`); this one passed nothing,
                # and this one is what runs in the pod.
                #
                # So in production neither gate has ever fired. Run 100
                # ran the platform's integration plan -- scoped to
                # sync_trunk, run_check and emit -- and called `bash`,
                # `story_commit` and `propose_completion` with no
                # refusal logged anywhere.
                #
                # SET RATHER THAN PASSED, because the executor has to
                # exist before the pool does: `subagent_context` closes
                # over it. Same mechanism `loop_intake` already uses to
                # withdraw an approval mid-leg.
                #
                # `is False` rather than falsy is kept deliberately in
                # the gate: None means "nobody could say", which must
                # not read as "withdrawn" and brick a run that is
                # allowed to work.
                if profile is not None and getattr(
                        profile, "requires_plan", False):
                    from kernel.harness.park_approval import (
                        plan_is_approved, plan_scope)

                    executor._plan_approved = await plan_is_approved(
                        conn, args.execution_id)
                    executor._plan_scope = await plan_scope(
                        conn, args.execution_id)
                    # A PLATFORM-WRITTEN PLAN GETS ITS FILE HERE, where the
                    # tree is -- nothing in the card path can write it.
                    if executor._plan_scope is not None:
                        from kernel.harness.planfiles import (
                            mirror_canned_plan)
                        await mirror_canned_plan(conn, args.execution_id)

                # AND THE OTHER THREE PER-LEG FACTS, for the same
                # reason and found the same way: every argument
                # `worker/executor_pass.py` passes and this did not.
                #
                # `allowed_hosts` FAILS OPEN and is the one that
                # matters. `None` means "the caller could not say", and
                # `web_fetch` then skips the allowlist and keeps only
                # the SSRF guard -- so a story's egress grants have
                # never bounded anything in a pod. Loading it NARROWS
                # reach to what was actually granted, which is the
                # point and also the risk: a run reaching a host nobody
                # granted now gets refused where it used to succeed.
                #
                # `approved_calls` fails CLOSED, which is the bug
                # `worker/loop.py` already describes -- "an approval
                # could be demanded, given, and never seen. The run
                # refused the same call for twelve legs and ended
                # FAILED." That fix went to the worker path only, so
                # the pod kept the symptom it was written for.
                #
                # `mounts_env` is execution 219: `setups.REGISTRY`
                # declares `bins` so a check can say `pytest` rather
                # than `.venv/bin/pytest`. It buys a PATH entry and
                # returns `{}` on any doubt, so it can never end a leg.
                from kernel.harness.permission import (
                    approved_calls as _approved_calls)
                from kernel.worker.leg_env import (
                    for_execution as _sandbox_path)
                from kernel.workspace.egress import (
                    allowed_hosts as _allowed_hosts)

                executor._approved_calls = frozenset(
                    await _approved_calls(conn, args.execution_id) or ())
                _hosts = await _allowed_hosts(conn, args.execution_id)
                executor._allowed_hosts = (
                    None if _hosts is None else frozenset(_hosts))
                executor._mounts_env = await _sandbox_path(
                    conn, args.execution_id,
                    envelope.workspace_path or "", "") or {}

                # ── Run the leg ──────────────────────────────────────────
                result = await run_leg(
                    conn,
                    adapter=adapter,
                    execution_id=args.execution_id,
                    generation=args.generation,
                    actor_id=args.actor_id,
                    messages=messages,
                    tools=tools,
                    profile=profile,
                    token_budget=args.token_budget,
                    step_budget=args.step_budget,
                    lease_clock_seconds=args.lease_clock,
                    tool_executor=executor,
                    rehydrate=not args.no_rehydrate,
                    progress_path=args.progress_path,
                )

                # ── The result, on stdout, in the wire format ────────────
                print(encode(result))
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(
        description="Run a single leg in its own killable process.",
    )
    parser.add_argument("--execution-id", type=int, required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--actor-id", type=str, default="leg-runner")
    parser.add_argument("--dsn", type=str, required=True)
    parser.add_argument("--bridge-port", type=int, default=9100)
    parser.add_argument("--envelope-json", type=str, default="{}")
    parser.add_argument("--kind", type=str, default=None,
                        help="execution kind; with --domain, resolves the profile")
    parser.add_argument("--domain", type=str, default=None,
                        help="execution domain; with --kind, resolves the profile")
    parser.add_argument("--adapter", type=str, default="mock",
                        choices=["mock", "gemini", "anthropic"])
    parser.add_argument("--messages-json", type=str, default='[{"role": "user", "content": "work"}]')
    parser.add_argument("--tools-json", type=str, default='[]')
    parser.add_argument("--mock-steps", type=int, default=10,
                        help="Number of tool-call steps for the mock adapter.")
    parser.add_argument("--token-budget", type=int, default=None)
    parser.add_argument("--step-budget", type=int, default=200)
    parser.add_argument("--lease-clock", type=float, default=600.0)
    parser.add_argument("--no-rehydrate", action="store_true",
                        help="Disable brief rehydration (cold-start, red-check).")
    parser.add_argument("--progress-path", type=str, default=None,
                        help="Path to progress.md sibling file (E6c).")

    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except Exception as e:
        # ON STDOUT, WHERE THE READER IS LOOKING, and that is the change.
        # It went to stderr, so a reader parsing stdout for a result got
        # an EMPTY STREAM from a runner that had failed loudly -- which
        # is indistinguishable from a process killed before it could say
        # anything. `loop_wire.decode` turns this into a `WireError`
        # naming the fault, and the exit code still says it failed.
        # Repeated on stderr because that is where a human tailing the
        # pod's logs will be.
        print(loop_wire.encode_error(f"{type(e).__name__}: {e}"))
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
