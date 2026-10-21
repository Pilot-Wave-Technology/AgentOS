"""Which question a finished claim asks: merge it, or accept it.

ONE DECISION AT A TIME, IN THE ORDER THEY HAPPEN. A finished claim used
to ask one question for every kind of work -- "accept it, or say what
more it should do" -- and for a coding story that was asked too early.
The work was pushed but not merged, so accepting it meant accepting
something that was not yet anywhere, and the merge happened afterwards
with nobody looking at it.

So a coding story with work still to land is asked about the MERGE
first, and about the STORY afterwards:

    propose completion
      -> merge card      (here: the PR to merge, or more work)
      -> the merge happens
      -> mark-story-done (the same card an errand gets)
      -> review

AND AN ERRAND IS UNTOUCHED, which is the point of putting the choice
here rather than in the card. An errand's work never lands: there is no
merge to wait for, so it goes straight to the acceptance it always got,
with the same words on the same card. The two kinds differ in how many
questions they are asked, never in what "mark story done" means.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ACCEPTANCE = "acceptance"
LANDING = "landing"


async def kind_for(conn, execution_id: int) -> str:
    """`landing` if this claim owes a merge, else `acceptance`.

    Deliberately the same question `finish_execution` asks, through the
    same function, so the two cannot disagree about whether a story is
    going to land. They are asked at different moments -- this one
    before the person is asked anything, that one after -- and a story
    that answered yes here and no there would park twice.
    """
    from kernel.worker.landing import owes_a_landing

    try:
        if await owes_a_landing(conn, execution_id):
            return LANDING
    except Exception:                                       # noqa: BLE001
        # FALL BACK TO THE OLD QUESTION. A claim that cannot work out
        # whether it owes a merge must still be able to ask somebody
        # something; asking the acceptance is what every run did before
        # this existed, and it is never silently wrong -- only early.
        logger.exception("execution %s: could not tell whether it owes a "
                         "landing; asking for acceptance as before",
                         execution_id)
    return ACCEPTANCE


def payload_for(kind: str, *, work_kind: str, generation: int,
                summary: str, results) -> dict:
    """The question text and evidence for whichever is being asked."""
    if kind == LANDING:
        return {
            "question": ("the work is finished and pushed — merge its pull "
                         "request to land it, or say what more it should do"),
            # `summary` is what `validate_question` requires of a landing
            # ask, and it is what the person reads before merging.
            "summary": summary,
            "claim": generation,
            "checks": results,
            # Filled in by the card builder once the engine has opened
            # the PR. Absent here because at this moment it does not
            # exist: the PR is opened after the node reaches READY, and
            # the node reaches READY because this park moved the story
            # to `awaiting_merge`.
            "branches": [],
        }
    return {
        "question": (f"the {work_kind or 'story'} says it is finished and "
                     f"its own checks agree — accept it, or say what more "
                     f"it should do"),
        "claim": generation,
        "summary": summary,
        "checks": results,
    }
