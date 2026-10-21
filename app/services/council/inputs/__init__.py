"""The documents a council stage opens on.

Three builders, one per kind of input that is not a plain SELECT:

    story_state  — LEDGER.md (counts, in SQL) and STORY_STATE.md
                   (asked / delivered / verdict, per item)
    plans        — PLANS.md, the decision record, read from approved
                   plan parks rather than the empty `goal_decisions`
    stage_output — one stage's findings for the next, or a document
                   saying plainly why there are none

`provisioning._build_inputs` composes these with the goal-level SELECTs
and the findings ledger's OPEN_FINDINGS.md.
"""

from app.services.council.inputs import (
    plans, progress_delta, stage_output, story_state,
)

__all__ = ["plans", "progress_delta", "stage_output", "story_state"]
