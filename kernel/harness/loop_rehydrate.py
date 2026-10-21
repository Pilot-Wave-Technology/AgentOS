"""What the previous leg left, found two ways, because one of them fails.

E6/E6b. A leg starts with no conversation: the window was the point of
ending the last one. Everything the model knew mechanically has to be
put back in front of it, and there are exactly two places it can come
from.

  THE CHECKPOINT   the `leg_brief` written at the last boundary. Layer 1
                   is journal-derived fact, layer 2 is the model's own
                   prose about what it was doing, and `progress_state`
                   rides alongside carrying its own [UNVERIFIED -- tree
                   is authoritative] header. It is NOT part of LegBrief,
                   so `dict_to_brief` drops it and it is read out of the
                   raw payload by hand. That is deliberate: a model's
                   claim about what is finished must not become a typed
                   field that reads like a measurement.

  THE JOURNAL      E6b's headline case, and the reason this file is not
                   three lines. A leg killed mid-edit never reaches the
                   boundary, so it writes no checkpoint at all -- and
                   that is precisely the leg whose brief matters most.
                   `extract_journal_facts` rebuilds layer 1 from rows
                   that were committed as the work happened, so the
                   read-set survives a SIGKILL. Layer 2 stays None,
                   which is what LegBrief already documents for a leg
                   killed before the boundary prompt; it is not a
                   degraded special case. Measured: 11 steps against 4
                   on the same task, without it.

AN ALL-EMPTY BRIEF IS WORSE THAN NONE. It spends prompt on a heading
that says nothing and reads as "the prior leg did nothing" rather than
"there was no prior leg" -- so `has_content` gates the injection and the
absence is logged instead.

NON-FATAL BY CONSTRUCTION. A leg that cannot remember must still run.
Do not "simplify" the outer `except` away to surface errors: the trade
is a degraded leg against no leg at all.
"""

from __future__ import annotations

import json
import logging

from kernel.harness.brief import (
    dict_to_brief,
    extract_journal_facts,
    format_brief_for_prompt,
)

logger = logging.getLogger(__name__)


async def rehydrated(conn, execution_id: int, generation: int,
                     messages: list[dict], rehydrate: bool) -> list[dict]:
    """`messages` with the prior leg's brief in front of it, if there is one."""
    # ── Rehydration: inject prior leg's brief (E6) ─────────────────────
    if rehydrate and generation > 0:
        try:
            prior_brief_row = await conn.fetchrow(
                """
                SELECT data::text FROM kernel.execution_checkpoints
                WHERE execution_id = $1
                  AND generation = $2
                  AND kind = 'leg_brief'
                ORDER BY seq DESC LIMIT 1
                """,
                execution_id,
                generation - 1,
            )
            prior_brief = None
            progress_state = None
            source = None

            if prior_brief_row and prior_brief_row["data"]:
                prior_raw = json.loads(prior_brief_row["data"])
                prior_brief = dict_to_brief(prior_raw)
                # E6c criterion 2: completion state travels with the brief,
                # already carrying its [UNVERIFIED — tree is authoritative]
                # header. It is not part of LegBrief, so dict_to_brief drops
                # it — read it from the raw payload and append.
                progress_state = prior_raw.get("progress_state")
                source = "checkpoint"
            else:
                # E6b's headline case: a leg killed mid-edit never reaches
                # the boundary, so it writes no leg_brief checkpoint. Without
                # this branch leg 2 resumed blind after exactly the failure
                # the brief exists to survive — measured at 11 steps against
                # 4 on the same task.
                #
                # Layer 1 is journal-derived by definition, so the read-set
                # survives a SIGKILL. Layer 2 stays None — which is what
                # LegBrief already documents for a leg killed before the
                # boundary prompt, not a degraded special case.
                prior_brief = await extract_journal_facts(
                    conn, execution_id, generation - 1
                )
                source = "journal"

            # An all-empty brief is worse than none: it spends prompt on a
            # heading that says nothing, and reads as "the prior leg did
            # nothing" rather than "there was no prior leg".
            has_content = prior_brief is not None and (
                prior_brief.files_read
                or prior_brief.search_targets
                or prior_brief.files_written
                or prior_brief.commits
                or prior_brief.refusals
            )

            if has_content:
                brief_text = format_brief_for_prompt(prior_brief)
                if progress_state:
                    brief_text = f"{brief_text}\n\n{progress_state}"
                # Inject as a system message at the start of the conversation
                messages = [
                    {"role": "system", "content": brief_text},
                ] + list(messages)
                logger.info(
                    "Rehydrated from prior brief via %s (generation %d): "
                    "%d files_read, %d search_targets",
                    source,
                    generation - 1,
                    len(prior_brief.files_read),
                    len(prior_brief.search_targets),
                )
            else:
                logger.info(
                    "No prior-leg facts to rehydrate from (generation %d)",
                    generation - 1,
                )
        except Exception as e:
            # Rehydration failure must not prevent the leg from starting.
            logger.warning("Rehydration failed (non-fatal): %s", e)

    return messages
