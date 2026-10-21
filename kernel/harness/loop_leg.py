"""One leg, start to finish. E5, and everything Stage E through H added.

THE LEG IS THE UNIT OF WORK IN THIS SYSTEM. A run is a sequence of them;
each starts with an empty conversation, calls the model until a
predicate says stop, and leaves a brief for the next. ES2 built
`kernel/harness/` as pure functions with no loop -- this is the loop.

THE THREE THINGS IT PROMISES are stated in `kernel/harness/loop.py`, the
facade, in the words they were first written in: refuse to start on a
failed preflight, evaluate the predicates after every step, and let
`propose_completion` be idempotent without ending the run.

WHAT IS DELIBERATELY NOT HERE. `run_leg` and its helpers were 833 code
lines in one file against a 200 ceiling (plan 11.5c), and D2 is about to
move the leg runner into the worker container -- surgery on the leg is
not reviewable inside a file that size. The mechanisms moved out along
the seams the leg already had, one `loop_*` module each, and
`kernel/harness/loop.py` lists them; every one of them holds the
argument for its own existence, so the list is not repeated here.

WHAT STAYED: the order of the phases, the step loop itself, and every
`break`. `loop_intake` and `loop_endings` DECIDE that a leg is over --
they return the reason and touch no control flow -- and only this file
acts on it. So every way out of a leg is visible in the three `break`s
below, without following a call.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from kernel.harness.loop_boundary import close_leg
from kernel.harness.loop_claim import settle_claim
from kernel.harness.loop_endings import MODEL_DONE, Fired, leg_ending
from kernel.harness.loop_intake import drain_intake
from kernel.harness.loop_preamble import opening_messages
from kernel.harness.loop_progress import prompt_for_update, seed_if_absent
from kernel.harness.loop_result import completed_leg, preflight_refusal
from kernel.harness.loop_step import record_step
from kernel.harness.loop_terms import (resolve_basis, resolve_budget,
                                       resolve_surface)
from kernel.harness.loop_tool import _default_tool_executor, run_tool
from kernel.harness.loop_turn import take_turn
from kernel.harness.park import ParkRequested
from kernel.harness.predicates import peak_context
from kernel.harness.models.types import (
    ModelAdapter,
    ToolResult,
    make_assistant_tool_call_message,
    make_tool_result_message,
)

logger = logging.getLogger(__name__)


async def run_leg(
    conn,
    adapter: ModelAdapter,
    execution_id: int,
    generation: int,
    actor_id: str,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    profile=None,
    token_budget: Optional[int] = None,
    # NOT 200. A magic number in a signature is a number nobody can
    # change without editing code — the same shape as the model id that
    # was wrong for the whole of Stage G. None means "ask the config",
    # which reads the profile and then the model.
    step_budget: Optional[int] = None,
    # The whole budget at once, when a caller means to set both halves.
    # `token_budget` alone can only say "how much context", and since
    # the split there are two numbers a test may need to pin.
    leg_budget=None,
    lease_clock_seconds: float = 600.0,
    tool_executor=None,
    rehydrate: bool = True,
    progress_path: Optional[str] = None,
    progress_interval: int = 5,
    depth: int = 0,
) -> dict:
    """Run one leg of a model-driven execution.

    Returns one of the two shapes in `loop_result.py`: a refusal if the
    snapshot preflight fails, otherwise the completed leg.

    Args:
        conn: asyncpg connection (search_path must include kernel).
        adapter: ModelAdapter instance (anthropic or gemini).
        execution_id: the execution to run.
        generation: current generation for fencing.
        actor_id: identity for proposals.
        messages: initial conversation messages.
        tools: tool schemas available to the model.
        token_budget: leg token budget.  If None, uses adapter capabilities (LR7).
        step_budget: leg step budget.
        lease_clock_seconds: wall-clock limit for this leg.
        tool_executor: async callable (tool_name, tool_args, tool_call_id) -> ToolResult.
            If None, uses _default_tool_executor (returns error for unknown tools).
            propose_completion is handled internally and never reaches the executor.
        rehydrate: if True (default), inject prior leg's brief into messages.
            Set to False to disable rehydration (red-check, cold-start tests).
        progress_path: E6c. MUST resolve on BOTH sides — the model writes
            it inside the sandbox, the harness reads it back. That is a
            property of running the leg beside the sandbox and not of
            the path; kernel/harness/loop_progress.py holds all three
            touches, the measurement of what production passes here
            today (nothing), and the 11.5e question D2 settles.
        progress_interval: issue progress prompt every N steps (default 5).
    """
    if tool_executor is None:
        tool_executor = _default_tool_executor

    # The three precedence rules, resolved before anything is spent.
    tools = resolve_surface(profile, tools)
    leg, token_budget, step_budget = resolve_budget(
        profile, adapter, token_budget, leg_budget, step_budget)
    budget_basis = resolve_basis(adapter)

    # REFUSE TO START IF THE SNAPSHOT PREFLIGHT FAILS. No steps are
    # written and the CALLER makes the state transition -- see
    # loop_result.py for why this must not touch `executions` itself.
    refusal = await preflight_refusal(conn, execution_id, budget_basis)
    if refusal is not None:
        return refusal

    # Everything the model opens with, in the order it opens with.
    messages = await opening_messages(
        conn, execution_id, generation, messages, rehydrate=rehydrate,
        profile=profile, tool_executor=tool_executor)
    seed_if_absent(progress_path)

    # ── The loop ─────────────────────────────────────────────────────────
    start_time = time.monotonic()
    step_records: list[dict] = []
    proposals: list[dict] = []
    claim_count = 0
    # The last prose the model produced. A sub-agent's whole return value is
    # this: the child's answer is a paragraph, and without keeping it the
    # parent gets an empty string back from work it paid for.
    final_text = None
    parked = None
    current_messages = list(messages)
    firing_predicate = None

    for step_index in range(step_budget + 10):  # overshoot guard
        firing_predicate, parked = await drain_intake(
            conn, execution_id, generation, tool_executor, current_messages)
        if firing_predicate is not None:
            break

        response = await take_turn(
            adapter, current_messages, tools, execution_id=execution_id,
            generation=generation, step_index=step_index)

        if response.text and response.text.strip():
            final_text = response.text.strip()

        # JOURNALLED BEFORE THE TOOL RUNS, and that ordering is why
        # `run_tool` needs `journal_id` to write `result_ref` back as an
        # UPDATE. A step recorded only after its tool returned would be a
        # step a SIGKILL erases.
        step = await record_step(
            conn, response, execution_id=execution_id,
            generation=generation, depth=depth, step_index=step_index,
            current_messages=current_messages, adapter=adapter)
        step_records.append(step.record)

        # ── Handle tool calls ────────────────────────────────────────────
        if response.tool_call:
            tc = response.tool_call
            tc_id = tc.get("id", f"tc-{execution_id}-{step_index}")

            if step.tool_name == "propose_completion":
                # Handled internally and never reaching the executor: a
                # claim is an assertion about the outcome, not work.
                tool_result, proposal = await settle_claim(
                    conn, tool_executor, step.tool_args, tc_id,
                    execution_id=execution_id, actor_id=actor_id,
                    claim_count=claim_count)
                if proposal is not None:
                    # Only a claim that was actually recorded advances
                    # the counter -- a refusal must not burn an
                    # idempotency key. The run does NOT end either way.
                    proposals.append(proposal)
                    claim_count += 1
            else:
                try:
                    tool_result = await run_tool(
                        conn, tool_executor, step.tool_name, step.tool_args,
                        tc_id, execution_id=execution_id,
                        journal_id=step.journal_id)
                except ParkRequested as park:
                    # E6h/E6i: the run is waiting on a person. The leg ends
                    # here — a tool that blocked would hold the lease and
                    # burn the wall clock, then die on the lease predicate
                    # with the question still unanswered.
                    logger.info("leg parking: %s", park.summary)
                    firing_predicate = Fired(
                        "parked", {"request_id": park.request_id,
                                   "kind": park.kind,
                                   "summary": park.summary})
                    parked = park
                    break

            # Append assistant turn (with provider_state) + tool result
            current_messages.append(make_assistant_tool_call_message(
                tc, provider_state=response.provider_state,
                text=response.text,
            ))
            current_messages.append(make_tool_result_message(tool_result))

        # ── LR4: answer every tool call the turn contained ───────────────
        # We execute one call per step. The others are dropped, not queued —
        # but "dropped" cannot mean "ignored": LR9 replays the assistant turn
        # verbatim, so an unanswered tool_use stays in the history and the
        # provider rejects the next request. Anthropic:
        #   "tool_use ids were found without tool_result blocks immediately
        #    after" — which killed the leg outright.
        # A refusal is also honest to the model: it learns the call was not run
        # rather than silently receiving nothing.
        for extra in (response.extra_tool_calls or []):
            current_messages.append(make_tool_result_message(ToolResult(
                tool_call_id=extra.get("id", ""),
                output="",
                error="E_PARALLEL_DROPPED: the harness executes one tool call "
                      "per step. This call was not run — reissue it if still "
                      "needed.",
                exit_code=1,
            )))

        # Publish the remaining budget to the executor, so a `subagent`
        # call dispatched on a later step slices from what is actually
        # left rather than from the budget this leg started with.
        if hasattr(tool_executor, "budget_remaining"):
            # Same measure as the hint and the predicate: what ends
            # the leg is peak context, so that is what "remaining" means.
            tool_executor.budget_remaining = (
                -1 if token_budget == -1
                else max(0, token_budget - max(
                    (peak_context(r.get("cost")) for r in step_records),
                    default=0)))

        # ── Has this leg ended? The order decides WHICH cause is recorded ─
        ending = leg_ending(
            step_records, response, leg=leg, budget_basis=budget_basis,
            step_budget=step_budget, start_time=start_time,
            lease_clock_seconds=lease_clock_seconds, adapter=adapter,
            execution_id=execution_id, generation=generation,
            tool_executor=tool_executor)
        if ending is not None:
            # MODEL_DONE is not a predicate: the model stopped talking,
            # so the leg ends with `predicate: None` and `stopped: False`.
            if ending is not MODEL_DONE:
                firing_predicate = ending
            break

        # ── Progress prompt (E6c) — harness-issued, not model-volunteered ──
        nudge = prompt_for_update(progress_path, len(step_records),
                                  progress_interval)
        if nudge is not None:
            current_messages.append(nudge)

    total_spend = await close_leg(
        conn, adapter, execution_id, generation, step_records=step_records,
        current_messages=current_messages, firing_predicate=firing_predicate,
        budget_basis=budget_basis, token_budget=token_budget,
        step_budget=step_budget, depth=depth, progress_path=progress_path)

    return completed_leg(
        step_records=step_records, firing_predicate=firing_predicate,
        budget_basis=budget_basis, proposals=proposals,
        total_spend=total_spend, final_text=final_text, parked=parked)
