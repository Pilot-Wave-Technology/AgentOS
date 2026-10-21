"""The agent says the work is finished. The extension does the rest.

SEPARATE FROM `local_completion.py`, which is about a story being
marked DONE -- composing the result, recording it, finishing. This
is the step before any of that: the claim that the work is ready to
submit. Two different moments, and putting them in one file once
cost that module its contents.

WHY THIS EXISTS AT ALL. Nothing moved a local story out of `executing`.
`_park_for_review` set `awaiting_review` only AFTER a successful submit,
and `storyWatch` only submitted when the story was ALREADY
`awaiting_review` — a circular dependency with no entry point. The
handler that used to break it, `sentinel_parser._on_dag_tasks_complete`,
went with the task DAG. So an agent finished its work, said "All done"
in chat, and the story sat in `executing` for ever (seen on 66317ffc,
2026-10-14).

IT IS A CLAIM, NOT AN ACTION, and that is the same division the cloud
lane draws. There, `propose_completion` records that the executor
believes it is finished; the platform then decides. Here the agent
cannot push — the boundary hook refuses and it holds no token — so the
EXTENSION runs the checks, takes trunk in, pushes and opens the PR. The
agent declares and stops.

WHAT IT DOES NOT DO. It does not approve anything. Submitting IS the
approval in this lane (there is no diff-review card), so the thing this
unlocks is the push, and the human gate after it is the pull request.
"""

from __future__ import annotations

import logging

from app.database import get_pool
from app.services.local_report import report_message

_log = logging.getLogger(__name__)

#: The phases a claim may be made from. `planning` means no approved
#: plan; `submitting` means a claim is already in flight; `awaiting_merge`
#: means it has already been pushed.
#:
#: `integrating` IS ONE OF THEM, and that is the change that makes the
#: catch-up an ordinary cycle. It used to mean "the extension is taking
#: the trunk in, the agent waits" -- a second machine, with its own
#: merge, its own checks and its own push, none of which the agent could
#: see. Now the agent works the catch-up plan with `sync_trunk`,
#: `run_check` and `story_commit`, exactly as the pod's executor does,
#: and finishes it the way it finishes any other plan: by claiming
#: completion. One ending for both kinds of cycle.
_CLAIMABLE = ("executing", "integrating")


async def propose_local_completion(
    goal_id: str, story_id: str, summary: str = "",
) -> dict:
    """Record the agent's completion claim and hand the story to the extension.

    Refuses rather than guesses. Each refusal names a state the agent can
    act on, because an agent told only "no" will try again identically.
    """
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT id, goal_id, state, current_phase, execution_mode "
        "  FROM stories WHERE id = $1", story_id)
    if story is None or story["goal_id"] != goal_id:
        return {"ok": False, "reason": "story_not_found",
                "error": "no such story in this goal"}
    if (story["execution_mode"] or "") != "local":
        return {"ok": False, "reason": "not_local",
                "error": "this story is not running locally"}

    phase = (story["current_phase"] or "").strip()
    if phase not in _CLAIMABLE:
        return {"ok": False, "reason": "wrong_phase", "phase": phase,
                "error": (f"the story is in `{phase}`, not "
                          f"{' or '.join('`%s`' % p for p in _CLAIMABLE)}. A "
                          f"completion claim is only meaningful while the work "
                          f"is being done.")}

    # AN APPROVED PLAN FOR THE OPEN CYCLE, not merely any approved plan.
    # Claiming completion of work nobody authorised is the shape S1 was
    # written against, and it would reach a branch here rather than a card.
    approved = await pool.fetchval(
        "SELECT 1 FROM plan_versions "
        " WHERE story_id = $1 AND status = 'approved' LIMIT 1", story_id)
    if not approved:
        return {"ok": False, "reason": "no_approved_plan",
                "error": ("no approved plan for this story — register one and "
                          "wait for a person to approve it")}

    # GUARDED ON THE PHASE WE JUST READ, not on the whole claimable set:
    # the point of the guard is that nothing moved the story between the
    # check above and this write.
    await pool.execute(
        "UPDATE stories SET current_phase = 'submitting', updated_at = NOW() "
        " WHERE id = $1 AND current_phase = $2", story_id, phase)

    await report_message(
        story_id, "agent",
        (summary.strip() or "The work is finished.")
        + "\n\nSubmitting: running the checks, taking trunk in, and opening "
          "the pull request.",
        card_data={"kind": "completion_claimed"},
    )
    _log.info("local completion claimed story=%s", story_id)
    return {"ok": True, "phase": "submitting"}


async def working_phase(story_id: str, pool=None) -> str:
    """Which phase this story's OPEN cycle is worked in.

    The kind of the approved plan decides it: an integration plan is
    worked in `integrating`, anything else in `executing`. Read rather
    than remembered, because the alternative is a column that has to be
    kept in step with the plan and cannot be reconstructed when it is
    not.
    """
    pool = pool or get_pool()
    kind = await pool.fetchval(
        "SELECT COALESCE(kind, 'work') FROM plan_versions "
        " WHERE story_id = $1 AND status = 'approved' "
        " ORDER BY version DESC LIMIT 1", story_id)
    return "integrating" if kind == "integration" else "executing"


async def release_claim(story_id: str, why: str, pool=None) -> None:
    """Put a story back to work when the submit could not be made.

    FAILING CHECKS ARE NOT A FAILED STORY. The agent gets the output and
    carries on under the SAME approved plan — no new approval, which is
    the rule already agreed for the integration cycle. Leaving it in
    `submitting` would strand it exactly as `executing` used to strand it.

    BACK TO THE PHASE THE WORK WAS BEING DONE IN, which is not always
    `executing`. A catch-up is worked in `integrating`, and dropping it
    into `executing` would tell the agent it is doing ordinary feature
    work while the open plan is a trunk merge — and would hand it the
    `executing` hint, which says nothing about `sync_trunk`.
    """
    pool = pool or get_pool()
    back_to = await working_phase(story_id, pool)
    await pool.execute(
        "UPDATE stories SET current_phase = $2, updated_at = NOW() "
        " WHERE id = $1 AND current_phase = 'submitting'", story_id, back_to)
    await report_message(
        story_id, "agent",
        f"The submit did not go through: {why}\n\nBack to work on the same "
        f"approved plan — fix it and call `propose_completion` again.",
        card_data={"kind": "submit_failed"},
    )
    _log.info("local completion claim released story=%s why=%s", story_id, why)


async def release_refused_submit(story_id: str, why: str, pool=None) -> dict:
    """The extension refused a submit before asking us. Hand the story back.

    THE EXTENSION'S OWN REFUSAL, which is uncommitted changes after the
    checks: it will not commit on the agent's behalf (a commit it makes is
    one no check has seen -- c72e8263), so it stops before the push. Without
    this the story would stay `submitting`, where the boundary hook blocks
    the edits that would fix it, and the poll would retry the same refusal.

    ONLY FROM `submitting`, and silent otherwise: a story that already moved
    on needs no "back to work" message in its thread.
    """
    pool = pool or get_pool()
    phase = await pool.fetchval(
        "SELECT current_phase FROM stories WHERE id = $1 "
        "   AND execution_mode = 'local'", story_id)
    if phase is None:
        return {"ok": False, "reason": "story_not_found"}
    if phase != "submitting":
        return {"ok": True, "released": False, "phase": phase}
    await release_claim(story_id, why.strip() or "the submit was refused", pool=pool)
    return {"ok": True, "released": True}
