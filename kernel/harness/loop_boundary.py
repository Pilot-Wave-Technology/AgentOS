"""What a leg leaves behind for the next one. E6b/E6c.

THREE WRITES, IN THIS ORDER, AND EACH SURVIVES THE OTHERS FAILING.

  the leg_boundary checkpoint   what the leg cost and which predicate
                                ended it. Written first because it needs
                                nothing but arithmetic already done.
  the boundary TURN             one extra call to the model asking what
                                it was doing, journalled as a real step.
  the leg_brief checkpoint      layer 1 from the journal, layer 2 from
                                that turn, `progress_state` from the
                                workspace. This is what the next leg
                                reads.

THE BOUNDARY PROMPT IS ISSUED HERE AND NOT BY "THE CALLER". The previous
design left it to one, no caller ever did it, and layer 2 was therefore
always None -- every brief reached leg 2 as a bare file list. It is
harness-issued rather than a prompt instruction for the same reason:
three agents told explicitly to keep notes produced 1 commit, 0 and 0.

THE BOUNDARY TURN SITS OUTSIDE THE LEG BUDGET AND INSIDE THE ACCOUNTS.
Budget is our own bookkeeping and must not be able to prevent the brief
being written -- the leg that exhausts its budget is exactly the leg
whose brief matters most. But it is a real API call, measured at ~10,800
tokens, seven per run: it used to journal `{"spend": N}` and nothing
else, so about 6% of steps and 70,000 tokens were invisible to every
check the reconciler makes, and it skewed the cache-hit denominators it
did appear in. It now records the same cost shape as any other step.

DEGRADATION IS EXPLICIT, NEVER A SILENT None. A reader must be able to
tell "the model was never asked" from "the model had nothing to say", so
a failed boundary turn writes `degraded` into the brief with the reason.

EVERY TRY HERE IS LOAD-BEARING. A leg that finished its work must not be
lost because the note about it could not be written. Do not "simplify"
them into one block: they fail independently and the brief is still
worth writing when the journal row is not.
"""

from __future__ import annotations

import asyncio
import json
import logging

from kernel.harness.brief import (
    brief_to_dict,
    build_boundary_prompt,
    extract_journal_facts,
    generate_brief,
)
from kernel.harness.loop_accounting import _sum_step_spend
from kernel.harness.loop_progress import read_into_brief
from kernel.harness.models.types import CompletionRequest
from kernel.harness.snapshots import snapshot

logger = logging.getLogger(__name__)

# Boundary prompt (E6b): bounded retry so a transient provider error cannot
# cost us the brief, and bounded so a persistent one cannot hang the leg.
#
# The timeout is not optional. "Never fails" has to mean "never hangs" too — a
# boundary call that blocks forever stops the leg completing at all, which is
# strictly worse than a leg with a degraded brief. Observed live: leg 2 finished
# its work, entered the boundary turn, and the leg never returned.
BOUNDARY_MAX_ATTEMPTS = 3
BOUNDARY_RETRY_DELAY_S = 1.0
BOUNDARY_TIMEOUT_S = 90.0


async def close_leg(conn, adapter, execution_id: int, generation: int, *,
                    step_records: list[dict], current_messages: list[dict],
                    firing_predicate, budget_basis: str, token_budget,
                    step_budget, depth: int, progress_path) -> float:
    """Write the boundary, ask for the brief, and return the leg's spend.

    `total_spend` goes back to the caller rather than being recomputed
    there: it is already summed for the checkpoint, and two sums over the
    same records is one place for them to disagree.
    """
    # ── Write leg-boundary checkpoint ────────────────────────────────────
    checkpoint_seq = await conn.fetchval(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM kernel.execution_checkpoints "
        "WHERE execution_id = $1",
        execution_id,
    )

    total_spend = sum(_sum_step_spend(s) for s in step_records)
    checkpoint_data = {
        "budget_basis": budget_basis,
        "total_spend": total_spend,
        "predicate": firing_predicate.predicate if firing_predicate else "completed",
        "steps": len(step_records),
        "token_budget": token_budget,
        "step_budget": step_budget,
    }

    await snapshot(
        conn, execution_id, generation, checkpoint_seq,
        "leg_boundary", checkpoint_data,
    )

    # ── Boundary prompt + leg brief checkpoint (E6b) ─────────────────────
    # Layer 1 (mechanical) always comes from the journal. Layer 2 needs the
    # model, so the prompt is issued HERE — inside run_leg, which holds the
    # adapter and the conversation. The previous design left it to "the
    # caller", and no caller has either, so layer 2 was always None and every
    # brief reached leg 2 as a bare file list.
    #
    # This is harness-issued, not a prompt instruction: three agents told
    # explicitly to keep notes produced 1 commit, 0 and 0 (E6c). The harness
    # has to ask, at the moment it matters.
    #
    # Budget: the boundary turn sits OUTSIDE the leg budget. Budget is our own
    # bookkeeping and must not be able to prevent the brief being written — the
    # leg that exhausts its budget is exactly the leg whose brief matters most.
    # Its spend is recorded separately so E5c's accounting stays honest.
    boundary_text = None
    boundary_degraded = None
    boundary_spend = 0.0
    boundary_cost: dict = {"spend": 0.0, "boundary": True}
    try:
        facts = await extract_journal_facts(conn, execution_id, generation)
        prompt = build_boundary_prompt(facts)
        last_err = None
        for attempt in range(BOUNDARY_MAX_ATTEMPTS):
            try:
                b_resp = await asyncio.wait_for(
                    adapter.complete(CompletionRequest(
                        messages=current_messages + [{"role": "user", "content": prompt}],
                        tools=[],                 # text answer, not a tool call
                    )),
                    timeout=BOUNDARY_TIMEOUT_S,
                )
                u = b_resp.usage
                boundary_spend = sum(
                    v or 0 for v in
                    (u.input, u.output, u.cacheRead, u.cacheWrite)
                ) if u else 0
                # THE SAME SHAPE AS EVERY OTHER STEP.
                #
                # This turn used to journal `{"spend": N}` and nothing
                # else — one opaque scalar, no breakdown, no model, no
                # provider total. It is a real API call: measured at
                # ~10,800 tokens each, seven per run, so about 6% of
                # steps and 70,000 tokens were invisible to every check
                # the reconciler makes. It also skewed the denominators
                # it did appear in, counting as a step that never hit
                # cache because there was no field to say otherwise.
                #
                # Outside the leg BUDGET, which is deliberate and stays
                # so, is not the same as outside the ACCOUNTS.
                if u:
                    boundary_cost = {
                        "input": u.input, "output": u.output,
                        "cacheRead": u.cacheRead, "cacheWrite": u.cacheWrite,
                        "spend": boundary_spend,
                        "boundary": True,
                        "model": getattr(adapter, "_model", None),
                    }
                    if u.total is not None:
                        boundary_cost["providerTotal"] = u.total
                    if u.thinking is not None:
                        boundary_cost["thinking"] = u.thinking
                text = (b_resp.text or "").strip()
                if text:
                    boundary_text = text
                    break
                last_err = "empty response"
            except Exception as e:                # provider error / rate limit
                last_err = f"{type(e).__name__}: {e}"
            if attempt + 1 < BOUNDARY_MAX_ATTEMPTS:
                await asyncio.sleep(BOUNDARY_RETRY_DELAY_S * (attempt + 1))
        if boundary_text is None:
            boundary_degraded = f"boundary prompt failed after "\
                                f"{BOUNDARY_MAX_ATTEMPTS} attempts: {last_err}"
    except Exception as e:
        boundary_degraded = f"boundary prompt not attempted: {type(e).__name__}: {e}"

    if boundary_degraded:
        logger.warning("leg brief degraded: %s", boundary_degraded)

    # Journal the boundary turn. E6c criterion 1 requires the journal to show
    # the harness prompt and the write that answered it — so this row IS the
    # evidence, not bookkeeping. Not a tool step: it does not advance the
    # progress clock and is not counted in `steps`.
    try:
        b_seq = await conn.fetchval(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM step_journal "
            "WHERE execution_id = $1", execution_id,
        )
        await conn.execute(
            """
            INSERT INTO step_journal
                (execution_id, seq, generation, idempotency_key, tool, args,
                 cost, depth)
            VALUES ($1, $2, $3, $4, 'boundary_prompt', $5, $6::jsonb, $7)
            ON CONFLICT (execution_id, idempotency_key) DO NOTHING
            """,
            execution_id, b_seq, generation,
            # depth belongs in BOTH the row and the key. Without it in the
            # row a child's boundary is attributed to its parent; without it
            # in the key the two collide at the same execution+generation and
            # ON CONFLICT drops one silently — and since the child finishes
            # inside the parent's step it writes first, so the row destroyed
            # is the PARENT's. Third instance of this shape: an idempotency
            # key missing a dimension that actually varies.
            f"loop-{execution_id}-gen-{generation}-d{depth}-boundary",
            json.dumps({"answered": boundary_text is not None,
                        "degraded": boundary_degraded}),
            json.dumps(boundary_cost),
            depth,
        )
    except Exception as e:
        logger.warning("boundary journal write failed: %s", e)

    try:
        brief = await generate_brief(
            conn, execution_id, generation, model_response=boundary_text,
        )
        payload = brief_to_dict(brief)
        payload["boundary_spend"] = boundary_spend

        read_into_brief(progress_path, payload)
        if boundary_degraded:
            # Explicit, not a silent None: a reader must be able to tell
            # "the model was never asked" from "the model had nothing to say".
            payload["degraded"] = boundary_degraded
        brief_seq = await conn.fetchval(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM kernel.execution_checkpoints "
            "WHERE execution_id = $1",
            execution_id,
        )
        await snapshot(
            conn, execution_id, generation, brief_seq, "leg_brief", payload,
        )
    except Exception as e:
        # Brief generation must not prevent the leg from completing.
        logger.warning("Brief generation failed: %s", e)

    return total_spend
