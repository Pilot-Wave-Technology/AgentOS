"""One ordinary tool call: dispatch it, measure it, and bound what comes back.

"ORDINARY" MEANS EVERYTHING EXCEPT THE CLAIM. `propose_completion` never
reaches here -- it is an assertion about the outcome, not work, and
kernel/harness/loop_claim.py holds it for that reason.

THREE THINGS HAPPEN AFTER THE DISPATCH AND ALL THREE ARE ABOUT EVIDENCE.

  the orientation floor   E5c. Tokens, steps and peak context from the
                          execution's first step to its FIRST
                          story_commit, written once. A later commit is
                          not the floor, so the UPDATE carries
                          `tokens_to_first_write IS NULL` and the
                          measurement cannot drift into "tokens to the
                          last write".
  the artifact            E7. This path used to truncate and DISCARD --
                          a suite failing at line 4000 could not be read
                          past the cut, and the only recourse was
                          re-running the command. The whole output is
                          stored now and the model is given head+tail
                          plus an id it can read ranges from.
  `result_ref`            has existed since the C1 migration and nothing
                          ever wrote it. The journal row is inserted
                          BEFORE the tool runs, so this is an UPDATE and
                          cannot be folded back into the insert.

`ParkRequested` IS NOT CAUGHT HERE, deliberately. A park ends the leg,
and the only statement that can raise one is the dispatch itself -- so
the handler belongs where the `break` is, in the loop. Wrapping it here
would mean this file deciding that a leg is over, which is not its job.

A MEASUREMENT MUST NEVER COST A LEG: the floor's own `except` is there
because a story that failed to complete over a statistic would be a bad
trade, and the same reasoning refuses an artifact rather than silently
truncating one.
"""

from __future__ import annotations

import logging

from kernel.artifacts.aio import aput
from kernel.artifacts.bounding import bound_output, should_store
from kernel.artifacts.store import ArtifactTooLarge
from kernel.harness.loop_accounting import (_execution_peak_context,
                                            _execution_spend)
from kernel.harness.models.types import MAX_TOOL_OUTPUT_SIZE, ToolResult

logger = logging.getLogger(__name__)


async def _default_tool_executor(tool_name: str, tool_args: dict, tool_call_id: str) -> ToolResult:
    """Default tool executor — returns an error saying no executor is configured.

    In production, the bridge provides the executor.  For tests with a
    MockAdapter, tools that are not propose_completion get this fallback.
    """
    return ToolResult(
        tool_call_id=tool_call_id,
        output="",
        error=f"No tool executor configured for {tool_name!r}",
        exit_code=1,
    )


async def run_tool(conn, tool_executor, tc_name: str, tc_args: dict,
                   tc_id: str, *, execution_id: int,
                   journal_id: int) -> ToolResult:
    """Run the call and return the result the model should see.

    Raises `ParkRequested` straight through: see the module docstring.
    """
    # ── Regular tool call: execute via tool_executor ──────────
    tool_result = await tool_executor(tc_name, tc_args, tc_id)

    # ── E5c: the orientation floor ───────────────────────────
    # Tokens (reasoning included) from the execution's first
    # step to its FIRST story_commit. Written once: a later
    # commit is not the floor, and updating would turn the
    # measurement into "tokens to the last write".
    if tc_name == "story_commit" and tool_result.exit_code == 0:
        try:
            await conn.execute(
                """
                UPDATE executions
                SET tokens_to_first_write = $2,
                    steps_to_first_write = $3,
                    context_at_first_write = $4
                WHERE id = $1 AND tokens_to_first_write IS NULL
                """,
                execution_id,
                # ACROSS THE EXECUTION, not this leg. step_records
                # restarts every leg, so summing it measured the
                # spend of whichever leg happened to commit — the
                # two-leg run reported 1,624 tokens at "step 1"
                # because leg 2 committed early, which made the
                # comparison against a single-leg control
                # meaningless. The floor is cumulative by
                # definition: first step of the execution to the
                # first write.
                int(await _execution_spend(conn, execution_id)),
                await conn.fetchval(
                    "SELECT count(*) FROM step_journal "
                    "WHERE execution_id = $1", execution_id) or 0,
                # What a leg had to HOLD to get here, as
                # against what getting here cost.
                await _execution_peak_context(
                    conn, execution_id),
            )
        except Exception as e:
            # A measurement must never cost a leg.
            logger.warning("orientation floor not recorded: %s", e)

    # ── E7: keep the output, bound the view ──────────────────
    # This used to truncate and discard. The bytes past the cut
    # were gone — a test suite failing at line 4000 could not be
    # read past the cut, and the only recourse was re-running
    # the command. Now the whole output is stored and the model
    # gets head+tail plus an id it can read ranges from.
    artifact_id = None
    # WHY the output was cut, in the words the model will read. "Too
    # large to store" tells it to run a smaller command, which is right
    # when the artifact was refused and WRONG when the store was simply
    # unreachable -- there it would shrink a command that was fine.
    cut_because = "too large to store"
    if should_store(tool_result.output, MAX_TOOL_OUTPUT_SIZE):
        raw = tool_result.output.encode("utf-8", errors="replace")
        try:
            # OFF THE LOOP — see kernel/artifacts/aio.py. This
            # stores oversized TOOL OUTPUT, so it is exactly
            # the large-payload case.
            artifact_id = await aput(raw)
        except ArtifactTooLarge as e:
            # Refused, and the model is told so plainly rather
            # than handed a silent truncation.
            logger.warning("artifact refused: %s", e)
        except Exception as e:                    # noqa: BLE001
            # STORING IS AN OPTIMISATION OVER TRUNCATING, so a
            # store that cannot be reached must cost the RANGE
            # READS, never the leg. Only `ArtifactTooLarge` was
            # caught here, and the `else` below -- which already
            # truncates and says so -- was unreachable for every
            # other failure.
            #
            # MEASURED, on execution 222. `ARTIFACT_STORE=gcs` is
            # set on the execution pod and the client cannot
            # resolve a project there, so `storage_client()`
            # raised OSError, the exception left `run_tool` and
            # `run_leg`, and the leg died -- on `git log -p`,
            # whose output crosses 32 KB on any repository with
            # history. The next leg rehydrated from the journal
            # with almost nothing, repeated the same opening
            # moves, reached another large output and died again.
            # Six generations, and it never got as far as
            # writing the file the story asked for.
            #
            # The rule is already stated twenty lines up, for the
            # orientation floor: a measurement must never cost a
            # leg. This is the same rule; it was written once and
            # applied once.
            cut_because = ("the artifact store is unreachable, which is "
                           "not about your command")
            logger.warning(
                "artifact store unreachable (%s); the output is truncated "
                "for this step and the leg carries on", e)
        if artifact_id:
            tool_result = ToolResult(
                tool_call_id=tool_result.tool_call_id,
                output=bound_output(tool_result.output,
                                    artifact_id, len(raw)),
                error=tool_result.error,
                exit_code=tool_result.exit_code,
            )
            # result_ref has existed since the C1 migration
            # and nothing has ever written it. The row was
            # inserted before the tool ran, so this is an
            # update, not part of the insert.
            await conn.execute(
                "UPDATE step_journal SET result_ref = $1 "
                "WHERE id = $2", artifact_id, journal_id)
        else:
            tool_result = ToolResult(
                tool_call_id=tool_result.tool_call_id,
                output=tool_result.output[:MAX_TOOL_OUTPUT_SIZE]
                       + "\n... [truncated; too large to store]",
                error=tool_result.error,
                exit_code=tool_result.exit_code,
            )
    return tool_result
