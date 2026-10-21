"""Local Execution Mode — the plan gate.

A locally-authored plan is SUBMITTED, recorded, and approved by the person
who wrote it, in their own editor. This module holds the one question the
control plane still answers: **is this a well-formed plan?**

STEPS AND CHECKS, NOT A TASK DAG. The DAG was the local lane's only plan
representation and its only work-dispatch mechanism; the kernel has neither
and never did. `app/services/local_plan.py` carries the validation, imported
from the kernel rather than rewritten, so a plan means the same thing
wherever it runs.

WHAT IS NO LONGER HERE. No DAG parse, no `ingest_approved_plan`, no child
tasks, no `claude_phases`. A local run is self-operated: nothing dispatches
work to it, so there is nothing to dispatch. The story is not "executing" a
graph -- a person is working, and the plan is the record of what they said
they would do.

THE APPROVAL IS STILL A HUMAN'S. The router enforces that (the agent token
is refused); this module records who. That is not governance of the run --
it is so the plan on the board says who agreed to it.
"""
from __future__ import annotations

import json
import logging

from app.database import get_pool
# The kernel owns the plan-folder name; restating it here is how the two
# lanes would drift apart one rename at a time.
from kernel.harness.planfiles import FOLDER as PLAN_FOLDER
from app.services import local_plan
from app.services.local_cycles import WORK

#: The other kind a plan can be: taking the trunk in, written by the
#: platform (or revised by the agent working it), and NOT a new intent --
#: so it consumes no cycle and lives in its own plan file.
INTEGRATION = "integration"
from app.services.local_report import report_message

_log = logging.getLogger(__name__)


async def _default_plan_path(pool, story_id: str) -> str:
    """`agentos_plans/<story_id>/plan-NN.md`, the kernel's own convention.

    THE PLAN FOLDER IS THE HISTORY. `kernel/harness/planfiles.py` writes each
    approved plan into every writable mount at this path so it rides the
    story's commits and shows up in the PR diff for whoever merges. The local
    lane defaulted to `stories/<id>/plans/plan.md` -- the operator lane's
    layout, from before a local plan was the same object a kernel plan is --
    which meant a story planned in an editor and the same story planned in a
    pod left their record in two different places.

    NN IS THE CYCLE, which `local_cycles` owns. Revising reuses the number
    -- ten rewordings of one intent overwrite one file rather than leaving
    ten -- and so does registering a replacement plan while the cycle is
    still open, because that is the same intent being adjusted and not a
    new one. A new number is only taken once the last cycle is SPENT.
    """
    from app.services.local_cycles import cycle_plan_path  # noqa: PLC0415
    return await cycle_plan_path(pool, story_id)


async def _next_cycle(pool, story_id: str) -> int:
    """Kept as the module's own name for the approval INSERT's `version`.

    `version` COUNTS REGISTRATIONS AND `cycle` COUNTS INTENTS, and locally
    they coincide because only an APPROVAL writes a row -- amending a
    parked plan rewrites `stories.pending_plan_*` and posts a new card
    without touching `plan_versions`. They stop coinciding in Phase 5,
    when a catch-up plan is a version and not a cycle, which is why the
    cycle number comes from `local_cycles` rather than from here.
    """
    return 1 + (await pool.fetchval(
        "SELECT COALESCE(MAX(version), 0) FROM plan_versions WHERE story_id = $1",
        story_id) or 0)


async def _cycle_for(pool, story_id: str, integration: bool) -> int:
    """The cycle number this approval belongs to.

    An integration plan joins the cycle already open rather than opening
    one: it exists to deliver the intent that cycle already agreed.
    """
    from app.services.local_cycles import current_cycle, next_cycle  # noqa: PLC0415
    return await (current_cycle if integration else next_cycle)(pool, story_id)


async def cycle_kind(pool, story_id: str) -> str:
    """`integration` if the cycle being worked is a catch-up, else `work`.

    THE EXECUTOR MAY REVISE AN INTEGRATION PLAN, exactly as it may revise
    a work plan -- it is working the thing, and it is the one that finds
    out the plan is wrong. What it must NOT do is silently turn a
    catch-up into a feature cycle, which is what happened when a
    re-registration carried no kind: the card defaulted to `work`, so
    approving the revision consumed a work cycle, landed the story in
    `executing` rather than `integrating`, and replaced "take the trunk
    in" with whatever the new plan said. The story would then be judged
    against a contract nobody meant to adopt.

    READ FROM THE STORY'S STATE, not passed in by the caller. The model
    does not get to say which kind of cycle it is in.
    """
    phase = (await pool.fetchval(
        "SELECT current_phase FROM stories WHERE id = $1", story_id) or "").strip()
    if phase == "integrating":
        return INTEGRATION
    if phase == "awaiting_plan_approval":
        # Revising a catch-up that is parked and not yet approved. The
        # pending card is the only record of what kind it is.
        from app.services.local_report import latest_card  # noqa: PLC0415
        card = await latest_card(pool, story_id, "plan_review")
        if isinstance(card, str):
            import json  # noqa: PLC0415
            try:
                card = json.loads(card)
            except ValueError:
                card = None
        if ((card or {}).get("plan_kind") or WORK) == INTEGRATION:
            return INTEGRATION
    return WORK


async def submit_local_plan(goal_id: str, story_id: str, steps, rationale: str,
                            checks, plan_path: str = "") -> dict:
    """Validate and record a locally-authored plan.

    REFUSES IN PROSE, because the reader is the person who just wrote the
    plan and one edit fixes it. The same fault found later costs a run.
    """
    from app.services import execution_control  # noqa: PLC0415
    frozen = await execution_control.frozen_response(goal_id)
    if frozen is not None:
        return frozen

    pool = get_pool()
    if not await pool.fetchrow(
        "SELECT 1 FROM stories WHERE id = $1 AND goal_id = $2", story_id, goal_id,
    ):
        return {"ok": False, "reason": "story_not_found", "error": "story not found"}

    normalised = local_plan.normalise_checks(checks)
    refusal = local_plan.refusal_for(steps, rationale, normalised)
    if refusal:
        return {"ok": False, "reason": "invalid_plan", "error": refusal}

    plan_md = local_plan.as_markdown(steps, rationale, normalised)
    digest = local_plan.digest(steps, rationale, normalised)
    # THE KIND SURVIVES THE REVISION, and with it the plan FILE. A
    # catch-up lives at `plan-NN-trunk.md` beside the work plan of the
    # same cycle; writing a revision to `plan-NN.md` would overwrite the
    # record of what was agreed for the feature work -- which is on the
    # branch and in the pull request.
    kind = await cycle_kind(pool, story_id)
    if not plan_path and kind == INTEGRATION:
        from app.services.local_integration import (  # noqa: PLC0415
            integration_plan_path)
        resolved_path = await integration_plan_path(pool, story_id)
    else:
        resolved_path = plan_path or await _default_plan_path(pool, story_id)

    await pool.execute(
        "UPDATE stories SET pending_plan_md = $1, pending_plan_path = $2, "
        "current_phase = 'awaiting_plan_approval', updated_at = NOW() WHERE id = $3",
        plan_md, resolved_path, story_id,
    )
    await report_message(
        story_id, "agent",
        f"Plan submitted — {len(steps)} step(s), {len(normalised)} check(s).",
        card_data={"kind": "plan_review", "plan_path": resolved_path,
                   # Carried so approval keeps treating it as what it is.
                   "plan_kind": kind,
                   "steps": [str(x) for x in steps],
                   "checks": [{"run": c.get("run"), "label": c.get("label")}
                              for c in normalised],
                   "plan_hash": digest},
    )
    _log.info("local plan submitted story=%s steps=%d checks=%d hash=%s",
              story_id, len(steps), len(normalised), digest[:12])
    return {"ok": True, "plan_path": resolved_path, "plan_hash": digest,
            "step_count": len(steps), "check_count": len(normalised)}


async def _record_plan_decision(pool, goal_id: str, story_id: str,
                                verdict: str, uid: str) -> None:
    """One ledger event per local plan decision, for the taximeter.

    A LOCAL PLAN DECISION LEFT NO COUNTABLE RECORD. In the cloud lane an
    approval is a `kernel.questions` row of kind `authorization` and the
    scorer counts it. Locally the plan is parked on a card and approved
    in the panel -- there is no question, no execution, and after this
    function the pending plan is cleared, so a REJECTION in particular
    left nothing behind at all. Approving a plan is the single most
    common decision a person makes in this lane and it was invisible.

    An event rather than a column: a story can be planned many times,
    and each decision is its own fact with its own time.

    NEVER FATAL. The decision has already been applied and committed by
    the time this runs; failing to record it must not undo it. A missing
    event costs somebody three points, which is a smaller harm than a
    plan approval that silently did not take.
    """
    try:
        from app.ledger.actors import human_actor
        from app.ledger.journal import append_event

        actor_id, actor_type = await human_actor(uid)
        async with pool.acquire() as conn:
            await append_event(
                conn,
                actor_id=actor_id, actor_type=actor_type,
                event_type="local_plan.decided",
                payload={"story_id": story_id, "verdict": verdict},
                context_goal_id=goal_id,
            )
    except Exception:                                       # noqa: BLE001
        _log.exception("local plan %s: decision event not recorded for %s",
                       verdict, story_id)


async def approve_local_plan(goal_id: str, story_id: str, approved_by_uid: str) -> dict:
    """Record that a person approved the parked plan.

    NOTHING IS DISPATCHED. Approval used to ingest a DAG and move the story
    to `executing`, because the control plane was the thing that handed work
    out. It is not, for a local story: the person is already working in their
    editor. So this records the decision and the hash it binds to, and gets
    out of the way.

    `approved_by_uid` is a human's uid -- the router refuses the agent token.
    """
    from app.services import execution_control  # noqa: PLC0415
    frozen = await execution_control.frozen_response(goal_id)
    if frozen is not None:
        return frozen

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT pending_plan_md, pending_plan_path FROM stories "
        "WHERE id = $1 AND goal_id = $2", story_id, goal_id,
    )
    if not story:
        return {"ok": False, "reason": "story_not_found", "error": "story not found"}
    plan_md = story["pending_plan_md"]
    if not plan_md:
        return {"ok": False, "reason": "no_pending_plan",
                "error": "no plan is awaiting approval"}

    import json  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415

    version = 1 + (await pool.fetchval(
        "SELECT COALESCE(MAX(version), 0) FROM plan_versions WHERE story_id = $1",
        story_id) or 0)
    # THE HASH COMES FROM THE SUBMISSION, not from re-hashing the markdown.
    # The markdown is a rendering of the plan, and a rendering is not the
    # thing approved: re-deriving a hash from it would bind the approval to
    # whatever `as_markdown` happens to emit today, so a change to the
    # renderer would invalidate every past approval without touching a plan.
    # `messages` has no `story_id`; this queried one and would have raised
    # UndefinedColumn the first time anybody approved a plan.
    from app.services.local_report import latest_card  # noqa: PLC0415
    card = await latest_card(pool, story_id, "plan_review")
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except ValueError:
            card = None
    digest = (card or {}).get("plan_hash")

    # WHOSE PLAN THIS IS, and the two things that turn on it. An
    # INTEGRATION plan is written by the platform to take trunk in: it is
    # not a new intent, so it must not consume a work cycle (`kind`), and
    # approving it starts a merge rather than feature work, so the story
    # lands in `integrating` rather than `executing`. The card is where
    # that is recorded, because the card is what was approved.
    plan_kind = (card or {}).get("plan_kind") or WORK
    integration = plan_kind == INTEGRATION
    cycle = await _cycle_for(pool, story_id, integration)

    await pool.execute(
        """
        INSERT INTO plan_versions (id, story_id, version, cycle, kind,
                                   plan_doc_path, plan_json, plan_hash,
                                   proposed_by, status, approved_at, approved_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, 'local', 'approved',
                NOW(), $9)
        """,
        str(_uuid.uuid4()), story_id, version, cycle, plan_kind,
        story["pending_plan_path"] or f"{PLAN_FOLDER}/{story_id}/plan-{version:02d}.md",
        json.dumps({"markdown": plan_md}), digest, approved_by_uid,
    )
    await pool.execute(
        "UPDATE stories SET pending_plan_md = NULL, pending_plan_path = NULL, "
        "current_phase = $2, updated_at = NOW() WHERE id = $1",
        story_id, "integrating" if integration else "executing",
    )
    # AND THE STORY IS BEING WORKED AGAIN, whichever door the request
    # came through.
    #
    # "Ask for more work" moves the state off `awaiting_merge`; TALKING
    # to the session does not -- and the agent, finding its cycle spent,
    # quite correctly registers a new plan by itself rather than walking
    # into the submit gate. On ec6d5d1f that left the board reading
    # `awaiting_merge` while work was actively happening, which is the
    # exact failure `request_more_work` exists to prevent, reached
    # through the other door.
    #
    # So the APPROVAL settles it: approving a new WORK cycle means the
    # story is in progress. Not an integration plan -- a catch-up is
    # the platform taking the trunk in while the story waits for its
    # merge, and it is still waiting.
    if not integration:
        # The story is being worked again, whichever door the request
        # came through -- "Ask for more work" moves the state, typing in
        # the session does not, and the agent quite correctly registers
        # a new plan by itself when it finds its cycle spent.
        #
        # NOTHING IS REOPENED HERE ANY MORE. A merged story cannot take
        # another cycle at all now (`local_more_work._every_lane_merged`),
        # so the only stories reaching this are ones whose lanes are
        # still live -- and a live lane needs nothing done to it.
        await pool.execute(
            "UPDATE stories SET state = 'in_progress', updated_at = NOW() "
            " WHERE id = $1 AND state = 'awaiting_merge'", story_id)
    await report_message(
        story_id, "agent",
        f"Plan approved by {approved_by_uid}.",
        card_data={"kind": "plan_approved", "version": version},
    )
    # AND A MESSAGE THE SESSION ACTS ON, which the notice above is not.
    #
    # An `agent` message dispatches nothing -- it is a record. So approving
    # a plan wrote `executing`, posted that notice, and the session sat
    # there: on 2026-10-14 Abhishek approved the plan for 66317ffc, nothing
    # happened for 41 seconds, and the work only started when he typed
    # "approve the plan" into the session himself.
    #
    # `request_more_work` already had this right -- it posts a `human`
    # message, and a human message is what wakes a local session. Same
    # mechanism, same reason, and the person's uid is on it because they
    # are the one who approved.
    await report_message(
        story_id, "human",
        ("I have approved the catch-up. Take the trunk in exactly as the "
         "plan says with `sync_trunk`, resolve anything it reports and "
         "`story_commit` it, then run every check with `run_check` and "
         "call `propose_completion`." if integration else
         "I have approved the plan. Start the work. When every step is "
         # NAMED, because the submit is refused without it and nothing
         # else runs them: the extension stopped, deliberately. The first
         # story worked under the new rule (291c4901) did exactly what it
         # was told here, claimed completion, and was refused -- correctly,
         # and avoidably.
         "done and committed, run every check with `run_check` and fix "
         "what fails, THEN call `propose_completion`."),
        sender=approved_by_uid or "",
    )
    await _record_plan_decision(pool, goal_id, story_id, "approved",
                                approved_by_uid)
    _log.info("local plan approved story=%s version=%d by=%s",
              story_id, version, approved_by_uid)
    return {"ok": True, "version": version}


async def reject_local_plan(goal_id: str, story_id: str, uid: str, feedback: str) -> dict:
    """Send the story back to planning so the assistant revises and re-submits."""
    pool = get_pool()
    if not await pool.fetchrow(
        "SELECT 1 FROM stories WHERE id = $1 AND goal_id = $2", story_id, goal_id,
    ):
        return {"ok": False, "reason": "story_not_found", "error": "story not found"}
    await pool.execute(
        "UPDATE stories SET pending_plan_md = NULL, pending_plan_path = NULL, "
        "current_phase = 'planning', updated_at = NOW() WHERE id = $1",
        story_id,
    )
    await report_message(
        story_id, "human",
        f"Plan sent back for a replan{': ' + feedback if feedback else ''}.",
        card_data={"kind": "plan_rejected", "feedback": feedback},
    )
    await _record_plan_decision(pool, goal_id, story_id, "rejected", uid)
    _log.info("local plan rejected story=%s by=%s", story_id, uid)
    return {"ok": True}


async def request_more_work(goal_id: str, story_id: str, what: str,
                            asked_by_uid: str) -> dict:
    """MOVED to `local_more_work`, and re-exported here so the routes and
    the three test modules that import it from the gate keep working.

    IT STOPPED BEING ONE DECISION. "More work" used to mean one thing --
    spend the plan, go back to planning, demand a new one -- and now
    means three (expand the open cycle, start a new one, hold during a
    catch-up) with a decision table behind them. That is a module, and
    leaving it here would have taken the plan gate past the 200-line
    rule with the half of itself that is not about the gate.
    """
    from app.services.local_more_work import request_more_work as _impl
    return await _impl(goal_id, story_id, what, asked_by_uid)


async def continue_this_plan(goal_id: str, story_id: str, by_uid: str) -> dict:
    """Go back to working the plan that is already approved.

    THE THIRD DOOR OUT OF `awaiting_next`, and without it the mid-plan case is
    still broken one layer up from where it was first fixed.

    Approving a diff sets `current_phase = 'awaiting_next'` -- truthfully, a
    cycle landed -- and that phase tells the session "the story is NOT done;
    acknowledge and wait". Correct when the plan is finished. Wrong when a
    person has just landed a SAFE SUBSET of a plan with steps left: the work
    was agreed, the plan is unspent and the diff gate would accept the next
    submission, but the agent has been told to stop and the only button out
    was "ask for more work", which spends the plan and demands a new one.

    So: three doors, and the person picks. Continue (here) leaves the plan
    alone and returns to `executing`. Ask for more work spends it and reopens
    planning. Mark done spends it and closes the story.
    """
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT current_phase, state FROM stories WHERE id = $1 AND goal_id = $2",
        story_id, goal_id)
    if story is None:
        return {"ok": False, "reason": "story_not_found", "error": "story not found"}
    if (story["state"] or "") in ("done", "reviewed", "cancelled", "rejected"):
        return {"ok": False, "reason": "story_closed",
                "error": f"this story is {story['state']}"}
    live = await pool.fetchval(
        "SELECT 1 FROM plan_versions WHERE story_id = $1 AND status = 'approved' "
        "LIMIT 1", story_id)
    if not live:
        # Nothing to continue. Saying so beats moving the story to `executing`
        # with no approved plan, where the next submit would refuse anyway.
        return {"ok": False, "reason": "no_approved_plan",
                "error": "there is no approved plan left to continue -- ask for "
                         "more work and the assistant will register a new one"}

    await pool.execute(
        "UPDATE stories SET current_phase = 'executing', updated_at = NOW() "
        " WHERE id = $1", story_id)
    await report_message(
        story_id, "human",
        "Carrying on with the approved plan - there is more of it to do.",
        sender=by_uid or "")
    _log.info("local continue-plan story=%s by=%s", story_id, by_uid)
    return {"ok": True, "phase": "executing"}
