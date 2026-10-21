"""One more thing, while the tree is still there.

THE WHOLE REASON THE STORY WAITS. A coding story holds its pod between
push and merge so a person who realises they forgot something can still
ask for it — in the same story, on the same branch, in the workspace
that already has the work in it. Phase 1 built the waiting. Without
this, the wait is just a longer wait: `kernel_run_control.resume`
un-parks only a `direction` hold, so a run parked on a `landing` had no
route back into work at all.

WHAT IT IS NOT. It is not a steer. A steer says HOW, and leaves the
definition of done alone; `resume` carries one for exactly that case.
This is new WORK — the thing the story delivers changes — and the
difference is the whole of why the plan has to be withdrawn.

WHY THE PLAN IS WITHDRAWN, in the words of the failure that taught it.
The plan gate is once per EXECUTION, not once per leg: `requires_plan`
blocks tools until *a* plan is approved, so an approval in leg 1 leaves
the gate open for the rest of the run. Asked for more work on errand
282, the executor walked straight past it — no `register_plan` anywhere
in leg 2 — and ran a test suite judged against a contract derived from
the plan for DOCUMENTING ENDPOINTS. New work, old definition of done,
and nobody asked to approve the difference. `park_resolve` learned this
on the acceptance park and submits a `replan`; this is the same act at
the other park, and it is deliberately the same shape rather than a
second answer to one question.

THE PUSH TAKES CARE OF ITSELF. The branch is already pushed and its PR
is already open, so the second push updates the same PR — there is no
new branch, no new PR, and nothing for a person to re-approve on the
forge. `publish_execution` is re-entrant and runs again at the end of
the new work, exactly as it did at the end of the first.
"""

from __future__ import annotations

import logging

from app.services.kernel_run_control import (ControlRefused, _kernel_conn,
                                             _live_run, _submit, _thread_story)

_log = logging.getLogger("app.kernel_more_work")

# THE LANDING PARK ONLY -- BEFORE THE MERGE.
#
# This used to cover the `closeout` park too, on the reasoning that a
# person "should not have to know which side of the merge they are on".
# Abhishek, 2026-10-14, decided the opposite and it is the better rule:
# "more work should always be done before merging -- that is how work
# happens everywhere." The merge is the commitment. Asking afterwards
# reopens work somebody has already accepted onto trunk.
#
# IT WAS ALSO THE ONLY PATH THAT NEEDED A MERGED BRANCH BROUGHT BACK TO
# LIFE, and that is where it went wrong: the node is reset, the create
# pass re-stamps `base_sha` with the live trunk tip, and the branch --
# never actually re-cut -- is behind by its own merge commit while the
# bookkeeping says it is level. No pull request opens, and no catch-up
# is offered to fix it.
#
# `CLOSEOUT_KIND` stays named because the refusal below distinguishes
# the two parks: "already merged, start a new story" and "not waiting
# for a merge at all" need different things from the person.
LANDING_KIND = "landing"
CLOSEOUT_KIND = "closeout"
WAITING_KINDS = (LANDING_KIND,)

# The run must still be holding its park. Anything else means the story
# moved while the page was open -- the merge landed, somebody cancelled
# it -- and the honest answer is to say so rather than to force it back.
RESUMABLE = ("PARKED_HOT", "PARKED_COLD")


async def available(conn, execution_id: int) -> int | None:
    """The open landing question, or None. Shared with `controls_for` so
    the button and the endpoint cannot disagree about when this is on."""
    return await conn.fetchval(
        "SELECT id FROM kernel.questions WHERE execution_id = $1 "
        "AND kind = ANY($2::text[]) AND status = 'OPEN' "
        "ORDER BY id DESC LIMIT 1",
        execution_id, list(WAITING_KINDS))


def _replan_reason(what: str, after_merge: bool = False) -> str:
    # `after_merge` IS NOW ALWAYS FALSE from `more_work` -- the closeout
    # park no longer offers it. Kept because `park_resolve` can still
    # drive a revise on a closeout by its own route, and because a
    # branch that answers only the reachable case is worse than one
    # that answers both and says which.
    """What the executor is told. The two cases differ in ONE fact that
    changes what it should do with git, so they are written out rather
    than hedged into a sentence that covers both and commits to neither."""
    if after_merge:
        where = (
            "\n\nThe work you already did has been MERGED to trunk and "
            "its PR is closed. You stay on the SAME branch — one feature "
            "branch per story — so do NOT cut a new one, do NOT start "
            "over, do NOT revert anything, and do not try to reopen the "
            "old PR. Bring your tree up to date with trunk first (what "
            "you already wrote is in there now), then build what was "
            "asked for on top. Your next push opens a fresh PR on the "
            "same branch, covering this round alone.")
    else:
        where = (
            "\n\nThe work you already did is pushed and its PR is open — do "
            "NOT start over, and do not cut a new branch. You are "
            "continuing on the same branch, and your next push updates the "
            "same PR.")
    return (
        "A person asked for more work on this story while it was waiting "
        "for its branch to be merged. What they asked for: " + what.strip()
        + where
        + "\n\nYour approved plan covered the work you have already done, "
        "and its checks are what your claim was measured against. This is "
        "new work, so it needs its own plan: call `register_plan` with "
        "steps and checks that cover what was just asked for, and wait "
        "for that to be approved before carrying on.")


async def more_work(thread_id: str, goal_id: str, actor_uid: str,
                    what: str) -> dict:
    """Take a story out of `awaiting_merge` and back into work.

    THE ORDER IS THE SAME ONE `park_resolve` USES, and for the same
    reasons: the replan goes in BEFORE the un-park, because
    `take_proposals` drains the mailbox at the top of the step loop and
    a proposal submitted after would be read a step late — after the
    executor had already spent a step working to the old plan.
    """
    said = (what or "").strip()
    if not said:
        # REFUSED RATHER THAN DEFAULTED. "More work" with nothing said is
        # the input that produces a confident second attempt at the wrong
        # thing, and the person is right here to be asked.
        raise ControlRefused(
            "Say what else needs doing. The executor is resumed with your "
            "words as its new scope, so an empty request would put it back "
            "to work with nothing to go on.")

    conn = await _kernel_conn()
    try:
        story_id, title = await _thread_story(conn, thread_id, goal_id)
        run = await _live_run(conn, story_id)
        if run is None:
            raise ControlRefused(
                f'"{title}" has no run in flight — there is nothing to '
                f'continue. Ask for a new story instead.')
        qid = await available(conn, run["id"])
        if qid is None or run["state"] not in RESUMABLE:
            # WHICH "NO" IS THIS. A story that already merged and one
            # that was never waiting need different things from the
            # person, and "it is parked hot" tells them neither. The
            # landing park having been ANSWERED is what separates them:
            # it only ever gets answered by the merge or by the deadline.
            # EITHER PARK COUNTS AS "IT GOT THERE". An OPEN closeout
            # means the branch has merged and the person is being asked
            # whether the story is finished -- which is a `landed` for
            # the purpose of this message, and no longer a place more
            # work can be asked for.
            landed = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM kernel.questions "
                " WHERE execution_id = $1 AND kind = ANY($2::text[]) "
                "   AND (status = 'ANSWERED' OR kind = $3))",
                run["id"], [LANDING_KIND, CLOSEOUT_KIND], CLOSEOUT_KIND)
            if landed:
                raise ControlRefused(
                    f'"{title}" has already landed — its branch is merged '
                    f'and the story is finished. Anything more needs a new '
                    f'story. Ask for more work BEFORE merging the pull '
                    f'request and it joins this one.')
            raise ControlRefused(
                f'"{title}" is not waiting for a merge — it is '
                f'{run["state"].lower().replace("_", " ")}. If it is parked '
                f'on an acceptance, answer that with what you want instead; '
                f'that route asks for more work too.')

        kind = await conn.fetchval(
            "SELECT kind FROM kernel.questions WHERE id = $1", qid)
        after_merge = kind == CLOSEOUT_KIND

        async with conn.transaction():
            # ── AFTER A MERGE, THE NEXT ROUND NEEDS SOMEWHERE TO GO ──
            #
            # Before the merge there is nothing to do: the node is live,
            # the branch is open, and the next push updates the same PR.
            # After it, the branch is merged and its PR is closed, so
            # this round is a new landing and needs its own node to be
            # one -- its own base_sha against the trunk that has since
            # moved, its own PR, its own place in the queue.
            #
            # BEFORE the replan, so that if cutting the cycle fails the
            # whole thing rolls back and the person is told, rather than
            # the executor being set to work with nowhere to push.
            if after_merge:
                from app.services.git_graph.node_cycles import (
                    reopen_for_next_cycle)
                await reopen_for_next_cycle(conn, story_id)

            await _submit(conn, run["id"], "replan",
                          {"reason": _replan_reason(said, after_merge)},
                          actor_uid)

            # `status = 'OPEN'` is the CAS. The merge may have landed
            # between the page rendering and this call, and if it did the
            # merge wins -- it has already answered this question and
            # finished the run.
            answered = await conn.execute(
                "UPDATE kernel.questions "
                "   SET status = 'ANSWERED', answer_text = $2, "
                "       verdict = 'revise', answered_by = $3, "
                "       answered_at = now() "
                " WHERE id = $1 AND status = 'OPEN'",
                qid, said, f"human:{actor_uid}")
            if answered.split()[-1] == "0":
                raise ControlRefused(
                    f'"{title}" finished merging while you were asking. The '
                    f'work has landed, so this needs a new story.')

            # AND THE CONVERGENCE BUDGET RESTARTS, because a person
            # answering is somebody watching. `park_resolve` gates this on
            # `human_uid` and not on the kind, so that an agent cannot buy
            # itself a fresh ceiling; this route only ever has a person
            # behind it, which is why it passes one unconditionally.
            await conn.execute(
                "UPDATE kernel.executions "
                "   SET state = 'RUNNING', generation = generation + 1, "
                "       convergence_generation = generation + 1, "
                "       updated_at = now() "
                " WHERE id = $1 AND state = ANY($2::text[])",
                run["id"], list(RESUMABLE))
    finally:
        await conn.close()

    # THE STORY ROW FOLLOWS, through the ledger helper and outside the
    # kernel connection -- `story_landing` says why the projection is
    # written on this side. Not fatal if it lags: the sweep that moves a
    # story INTO `awaiting_merge` only matches runs parked on a landing,
    # and this one is no longer parked, so the next pass leaves it alone.
    from app.database import get_pool
    from app.services.story_state_ledger import record_state_pooled

    try:
        await record_state_pooled(
            get_pool(), story_id, goal_id, "in_progress",
            actor_uid=actor_uid, action="more_work_while_awaiting_merge",
            only_from=("awaiting_merge",))
    except Exception:                                       # noqa: BLE001
        _log.exception("story %s resumed for more work but its state did "
                       "not move", story_id)

    _log.info("story %s back to work from awaiting_merge (run %s)",
              story_id, run["id"])
    return {"ok": True, "story_id": story_id, "story_title": title,
            "execution_id": run["id"]}
