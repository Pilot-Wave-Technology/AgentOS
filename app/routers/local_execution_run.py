"""Local Execution Mode — execution + diff-landing endpoints (M3–M5).

  POST /internal/local/next-phase     — claim + describe the next phase to run
  POST /internal/local/report-phase   — the 8th governed action (done/blocked/failed)
  POST /internal/local/report-message — mirror a local conversation turn (record-only)
  POST /internal/local/push-token     — write token to push the feature branch (§8.2)
  POST /internal/local/diff-submit    — submit the pushed diff for human review

These decorate the SAME router as `local_execution.py` (see `local_common`).
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.internal_auth import get_agent_principal, verify_internal_bearer
from app.routers.local_common import (
    router, _enforce_local_scope, _field, _resolve_local_human, supports_multi_repo,
)
from app.services.local_claim import propose_local_completion, release_refused_submit
from app.services.local_diff import mint_push_token, submit_local_diff
from app.services.local_dispatch import local_state, story_done_blocker
from app.services.local_phase import report_phase
from app.services.local_plan_gate import approve_local_plan, reject_local_plan, submit_local_plan
from app.services.local_report import recent_local_messages, report_message

_PHASE_ERR_STATUS = {"bad_status": 400, "task_not_found": 404}
_MSG_ERR_STATUS = {"bad_role": 400, "empty": 400, "no_active_thread": 409}
_DIFF_ERR_STATUS = {"story_not_found": 404, "no_dag_awaiting_review": 409}
_CLAIM_ERR_STATUS = {"story_not_found": 404, "not_local": 400,
                     "wrong_phase": 409, "no_approved_plan": 409}


# ── THREE ENDPOINTS ARE GONE, AND THEY WENT TOGETHER ────────────────
#
#   /next-phase    claimed the next `claude-code-phase` task
#   /task-state    polled one for the observe-mode runner
#   /report-phase  took the sentinel back and advanced the graph
#
# They were the task DAG's dispatch loop. A local run dispatches nothing:
# the person is working in their own editor, and the control plane keeps
# the record rather than handing out work. Removing the loop without
# removing these would leave three endpoints that answer about a graph
# nothing builds.
#
# `/get-state` STAYS. It is not part of the loop -- the state-injection
# hook reads it every turn to tell the session where it is, which is as
# true of a self-operated story as of a dispatched one.

# `/plan-ingest` WAS REMOVED (S1). It took the story-scoped AGENT token and
# recorded the plan as approved by the assignee -- the agent could approve its
# own plan and build a DAG without a person ever seeing it. Nothing calls it: a
# local plan is submitted with `/plan-submit` and approved by a HUMAN at
# `/plan-approve`. test_the_agent_cannot_approve_its_own_plan keeps it gone.


@router.post("/plan-submit")
async def plan_submit(request: Request) -> JSONResponse:
    """Record a locally-authored plan: `{steps, rationale, checks}`.

    STEPS AND CHECKS, NOT MARKDOWN. The body used to be `plan_md`, parsed
    for a `## Task DAG` block. A plan is now the same object the kernel
    plans with, validated by the same rules, so it means one thing wherever
    the story runs — and the markdown becomes a rendering of it rather than
    its source.

    Agent or human-local token: submitting is not approving.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    if not (story_id and goal_id):
        return JSONResponse({"ok": False, "error": "story_id, goal_id required"}, status_code=400)
    _enforce_local_scope(request, story_id, goal_id)
    result = await submit_local_plan(
        goal_id, story_id,
        body.get("steps") or [],
        body.get("rationale") or "",
        body.get("checks") or [],
        _field(body, "plan_path"),
    )
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    status = 400 if result.get("reason") in ("invalid_plan", "story_not_found") else 409
    return JSONResponse(result, status_code=status)


@router.post("/propose-completion")
async def propose_completion(request: Request) -> JSONResponse:
    """The agent says the work is finished. `{story_id, goal_id, summary?}`.

    THE LOCAL EQUIVALENT OF THE KERNEL'S `propose_completion`, and the same
    kind of thing: a CLAIM, not an action. The agent cannot push -- the
    boundary refuses it and it holds no token -- so this moves the story to
    `submitting` and the extension does the work: checks, trunk, push, PR.

    Agent or human-local token: claiming is not approving, and in this lane
    there is nothing left to approve. Submitting IS the approval; the pull
    request is the gate after it.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    if not (story_id and goal_id):
        return JSONResponse({"ok": False, "error": "story_id, goal_id required"},
                            status_code=400)
    _enforce_local_scope(request, story_id, goal_id)
    result = await propose_local_completion(goal_id, story_id,
                                            body.get("summary") or "")
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    return JSONResponse(result,
                        status_code=_CLAIM_ERR_STATUS.get(result.get("reason"), 409))


@router.post("/claim-release")
async def claim_release(request: Request) -> JSONResponse:
    """The extension refused a submit itself. `{story_id, reason, why}`.

    Story-token authed and scoped to the story: it can only hand back a
    claim on the story the token is for, and only from `submitting`.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    result = await release_refused_submit(story_id, _field(body, "why"))
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


@router.post("/check-report")
async def check_report(request: Request) -> JSONResponse:
    """The extension reports what it ran. `{story_id, goal_id, head_sha, results[]}`.

    THE EXTENSION'S CALL, NOT THE AGENT'S -- and the token cannot tell
    them apart, which is why this endpoint RECORDS rather than decides.
    Nothing here says the work is good: it stores what was measured, at
    which commit, and `submit-diff` reads it. An agent that posted a
    fabricated pass would be claiming a commit's checks passed, which is
    the same claim `propose_completion` already lets it make and which
    the pull request is the answer to.

    `results` is one entry per check: `{run, label?, mount?, passed,
    detail?}`. `head_sha` is the commit they were measured at, and it is
    required -- a result with no commit on it cannot gate anything.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    head_sha = _field(body, "head_sha")
    if not (story_id and goal_id and head_sha):
        return JSONResponse(
            {"ok": False, "error": "story_id, goal_id, head_sha required"},
            status_code=400)
    _enforce_local_scope(request, story_id, goal_id)
    results = body.get("results")
    if not isinstance(results, list):
        return JSONResponse({"ok": False, "error": "results must be a list"},
                            status_code=400)
    from app.database import get_pool
    from app.services import local_checks
    pool = get_pool()
    written = await local_checks.record(pool, story_id, head_sha, results)
    outstanding = await local_checks.outstanding(pool, story_id, head_sha)
    return JSONResponse({"ok": True, "recorded": written,
                         "outstanding": outstanding,
                         "clear_to_submit": not outstanding}, status_code=200)


def _require_human(request: Request) -> None:
    """The plan gate's approve/reject are HUMAN decisions — the story-scoped local
    agent token must never self-approve (§14; human_local is 403'd at the gate)."""
    p = get_agent_principal(request)
    if p is not None and p.token_type == "human_local":
        raise HTTPException(status_code=403, detail="plan approval requires a human session")


async def _enforce_goal_membership(uid: str, goal_id: str, story_id: str):
    """IDOR guard for the human local endpoints (approve/reject/recover/done). The
    ext-session / Firebase token authenticates the USER but is GLOBAL across all their goals,
    so a body (goal_id, story_id) must be authorized: verify goal membership AND that the
    story belongs to the named goal (mirrors the web endpoints). Returns an error
    JSONResponse for the caller to return, or None when access is allowed."""
    from app.auth import get_goal_role  # noqa: PLC0415
    from app.database import get_pool  # noqa: PLC0415
    if await get_goal_role(uid, goal_id) is None:
        return JSONResponse({"ok": False, "error": "not a member of this goal"}, status_code=403)
    story_goal = await get_pool().fetchval("SELECT goal_id FROM stories WHERE id = $1", story_id)
    if story_goal is None or story_goal != goal_id:
        return JSONResponse({"ok": False, "error": "story not found in this goal"}, status_code=404)
    return None


@router.post("/plan-approve")
async def plan_approve(request: Request) -> JSONResponse:
    """Approve a parked plan → ingest DAG → executing (E.3). Requires a human
    ext-session (or browser login); rejects the local agent token."""
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    if not (story_id and goal_id):
        return JSONResponse({"ok": False, "error": "story_id, goal_id required"}, status_code=400)
    _require_human(request)
    uid = await _resolve_local_human(request)
    if (guard := await _enforce_goal_membership(uid, goal_id, story_id)) is not None:
        return guard
    result = await approve_local_plan(goal_id, story_id, uid)
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    status = 400 if result.get("reason") in ("invalid_plan", "story_not_found", "no_pending_plan") else 409
    return JSONResponse(result, status_code=status)


@router.post("/story-done")
async def story_done(request: Request) -> JSONResponse:
    """Mark a locally-executed story done (§14). Human ext-session only — the local
    agent token is 403'd, same as plan approval; the transition is the assignee's call."""
    from app.database import get_pool  # noqa: PLC0415
    from app.services.story_transitions import apply_story_transition  # noqa: PLC0415
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    if not (story_id and goal_id):
        return JSONResponse({"ok": False, "error": "story_id, goal_id required"}, status_code=400)
    _require_human(request)
    uid = await _resolve_local_human(request)
    if (guard := await _enforce_goal_membership(uid, goal_id, story_id)) is not None:
        return guard
    # An approved plan, authoritative — the extension pre-checks via
    # /done-check, but this is what enforces it.
    blocker = await story_done_blocker(story_id)
    if blocker:
        return JSONResponse({"ok": False, "error": blocker, "reason": "not_executed"}, status_code=409)
    # THE SUMMARY IS WRITTEN BEFORE THE TRANSITION, and the order is the whole
    # point. `apply_story_transition` is what wakes GO with the completion
    # notification, and that notification carries `stories.result_summary` as
    # it reads at that instant. Writing the summary afterwards -- which is
    # what `finish_local_story` did, and only did -- meant GO was woken with
    # "Result: No result summary" while the summary appeared a moment later.
    # Observed on story 81ee97ea: the record had the summary, the wake did
    # not, and GO had to go and find it in `goal_state` when asked.
    #
    # Composed from the approved plan and the diff approval, both of which
    # exist before the transition, so nothing here needs the story to be
    # `done` yet. Best-effort: a story must still be able to finish when its
    # summary cannot be composed.
    from app.services.local_completion import record_result  # noqa: PLC0415
    try:
        await record_result(get_pool(), story_id)
    except Exception:  # noqa: BLE001
        import logging  # noqa: PLC0415
        logging.getLogger(__name__).exception(
            "story-done: result summary failed for %s", story_id)

    # FROM `awaiting_merge`, WHICH THE SHARED TABLE DOES NOT PERMIT and must
    # not: `awaiting_merge -> done` in the table would let Slack's `/done`
    # force a CLOUD story done with its pull request still open. This caller
    # knows the story is local and that `story_done_blocker` above has
    # already confirmed every lane merged, so it opts itself in rather than
    # loosening the rule for everybody.
    ok, reason = await apply_story_transition(
        goal_id, story_id, "done", uid, get_pool(),
        extra_allowed_from=("awaiting_merge",))
    if not ok:
        return JSONResponse({"ok": False, "error": reason}, status_code=409)

    # ── AND THEN THE THREE THINGS A POD WOULD HAVE DONE ─────────────
    #
    # A local story has no `kernel.executions` row, so none of the kernel's
    # completion machinery fires: nothing writes `result_summary`, and
    # nothing takes the story from `done` to `reviewed` — the council
    # stands down while STORY_REVIEW_IN_POD is on and there is no pod
    # review to replace it. The story sat at `done` for ever, keeping its
    # thread bound and its workspace on the protect list, and the review
    # backstop could not even see it.
    #
    # AFTER the transition and OUTSIDE its success test: the story IS done
    # either way, and a failure to summarise it must not be reported to the
    # person as a failure to finish it.
    from app.services.local_completion import finish_local_story  # noqa: PLC0415

    finished = await finish_local_story(get_pool(), story_id, goal_id)
    return JSONResponse({"ok": True, **finished}, status_code=200)


@router.post("/done-check")
async def done_check(request: Request) -> JSONResponse:
    """C2 pre-check: is this local story eligible to be marked done (approved plan + fully-
    executed DAG)? The extension calls this before the mark-done flow to fail fast; the
    story-done endpoint enforces the same gate authoritatively. Story-token authed."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    blocker = await story_done_blocker(story_id)
    return JSONResponse({"ok": True, "ready": blocker is None, "blocker": blocker}, status_code=200)


@router.post("/plan-reject")
async def plan_reject(request: Request) -> JSONResponse:
    """Send a parked plan back to planning for a replan (E.3). Human-only, like approve."""
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    if not (story_id and goal_id):
        return JSONResponse({"ok": False, "error": "story_id, goal_id required"}, status_code=400)
    _require_human(request)
    uid = await _resolve_local_human(request)
    if (guard := await _enforce_goal_membership(uid, goal_id, story_id)) is not None:
        return guard
    result = await reject_local_plan(goal_id, story_id, uid, body.get("feedback") or "")
    status = 200 if result.get("ok") else (404 if result.get("reason") == "story_not_found" else 409)
    return JSONResponse(result, status_code=status)


@router.post("/story-review")
async def story_review(request: Request) -> JSONResponse:
    """Record the review a local session wrote, and move the story.

    THE AGENT'S TOKEN, not a human's. This is the local counterpart of the
    pod's `submit_story_review`, which the reviewer leg calls itself -- the
    verdict is the reviewer's to give. What a person decides is what happens
    NEXT (another cycle, or nothing), and that stays theirs.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"},
                            status_code=400)
    _enforce_local_scope(request, story_id)

    from app.database import get_pool
    from app.services.local_review import ReviewRefused, record_local_review

    pool = get_pool()
    goal_id = await pool.fetchval("SELECT goal_id FROM stories WHERE id = $1",
                                  story_id)
    if not goal_id:
        return JSONResponse({"ok": False, "reason": "story_not_found"},
                            status_code=404)
    try:
        result = await record_local_review(pool, story_id, goal_id, body)
    except ReviewRefused as refusal:
        # 400 and the sentence: the reader is the session that just called
        # this, and one correction costs a message where a bare rejection
        # costs the cycle.
        return JSONResponse({"ok": False, "error": str(refusal)},
                            status_code=400)
    return JSONResponse(result, status_code=200)


@router.post("/continue-plan")
async def continue_plan(request: Request) -> JSONResponse:
    """Carry on with the plan that is already approved. Human-only.

    The third door out of `awaiting_next`: a person who landed a safe subset
    mid-plan wants to keep going, not to replan. Leaves the plan unspent.
    """
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    if not (story_id and goal_id):
        return JSONResponse({"ok": False, "error": "story_id, goal_id required"},
                            status_code=400)
    _require_human(request)
    uid = await _resolve_local_human(request)
    if (guard := await _enforce_goal_membership(uid, goal_id, story_id)) is not None:
        return guard
    from app.services.local_plan_gate import continue_this_plan
    result = await continue_this_plan(goal_id, story_id, uid)
    status = 200 if result.get("ok") else (
        404 if result.get("reason") == "story_not_found" else 409)
    return JSONResponse(result, status_code=status)


@router.post("/more-work")
async def more_work(request: Request) -> JSONResponse:
    """Ask for more work, at any step until the story is done. Human-only.

    THE REPLY SAYS WHICH KIND IT WAS. `mode` is `expand` (the instruction
    joined the plan already under discussion), `new` (the last cycle was
    delivered, so this starts `plan-NN+1.md`) or `queued` (held while the
    story takes the trunk in). The extension shows the difference --
    "added to plan-01.md" against "starting plan-02.md" -- because to a
    person those are not the same answer, and they cannot tell which one
    they got from a bare OK.
    """
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    if not (story_id and goal_id):
        return JSONResponse({"ok": False, "error": "story_id, goal_id required"},
                            status_code=400)
    _require_human(request)
    uid = await _resolve_local_human(request)
    if (guard := await _enforce_goal_membership(uid, goal_id, story_id)) is not None:
        return guard
    from app.services.local_more_work import request_more_work
    result = await request_more_work(goal_id, story_id, _field(body, "what"), uid)
    status = 200 if result.get("ok") else (
        404 if result.get("reason") == "story_not_found" else 409)
    return JSONResponse(result, status_code=status)


_RECOVER_ACTIONS = {"replan", "retry", "abandon"}  # review_keep is gateway-only, not for local


@router.post("/story-recover")
async def story_recover(request: Request) -> JSONResponse:
    """Human-driven block/fail recovery for a local story (C6): Replan / Retry / Abandon /
    Review-keep. Mirrors the web recover endpoint but on the internal local router so the
    VS Code extension can offer the SAME actions the web/Slack recovery cards do — all
    delegate to the shared `story_recovery` engine. Human-only (like plan approval)."""
    from app.services import story_recovery as _sr  # noqa: PLC0415
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    action = (_field(body, "action") or "").lower()
    if not (story_id and goal_id and action):
        return JSONResponse({"ok": False, "error": "story_id, goal_id, action required"}, status_code=400)
    if action not in _RECOVER_ACTIONS:
        return JSONResponse({"ok": False, "error": f"unknown action '{action}'"}, status_code=400)
    _require_human(request)
    uid = await _resolve_local_human(request)
    if (guard := await _enforce_goal_membership(uid, goal_id, story_id)) is not None:
        return guard
    feedback = body.get("feedback") or ""
    if action == "replan":
        result = await _sr.replan_dag(story_id, feedback, uid)
    elif action == "retry":
        result = await _sr.retry_dag(story_id, uid)
    else:  # abandon — the only remaining valid action
        result = await _sr.abandon_dag(story_id, uid, bool(body.get("force")))
    # On a completed action, stamp the recovery card decided (parity with the web/Slack path)
    # and wake the controller so a Retry re-dispatches promptly.
    if result.get("ok") and result.get("dag_task_id"):
        try:
            await _sr.mark_recovery_cards_decided(result["dag_task_id"], action)
        except Exception:  # noqa: BLE001 — card stamping is cosmetic
            pass
        try:
            from app.task_controller import task_controller  # noqa: PLC0415
            await task_controller.trigger()  # async — must be awaited or it never runs
        except Exception:  # noqa: BLE001
            pass
    # needs_confirm (abandon with uncommitted work) is a 200 with a confirm payload, not an error.
    if result.get("ok") or result.get("needs_confirm"):
        return JSONResponse(result, status_code=200)
    return JSONResponse(result, status_code=409)


@router.post("/diff-decide")
async def diff_decide(request: Request) -> JSONResponse:
    """C8: a human approves / requests-changes on the submitted diff for a LOCAL story from
    the VS Code extension (browser-free). Resolves the story's pending diff approval and runs
    the SAME diff-review engine the web card uses. Human-only + IDOR-guarded; the agent token
    can't self-approve. Approve moves the DAG to done (story stays in_progress → Mark Done is
    still a separate step)."""
    from app.database import get_pool  # noqa: PLC0415
    from app.services.diff_review import decide_diff_review  # noqa: PLC0415
    body = await request.json()
    story_id, goal_id = _field(body, "story_id"), _field(body, "goal_id")
    decision = (_field(body, "decision") or "").lower()
    if not (story_id and goal_id and decision in ("approve", "reject")):
        return JSONResponse(
            {"ok": False, "error": "story_id, goal_id, decision (approve|reject) required"},
            status_code=400,
        )
    _require_human(request)
    uid = await _resolve_local_human(request)
    if (guard := await _enforce_goal_membership(uid, goal_id, story_id)) is not None:
        return guard
    approval_id = await get_pool().fetchval(
        "SELECT id FROM approvals WHERE story_id = $1 AND kind = 'diff' "
        "AND decided_at IS NULL ORDER BY proposed_at DESC LIMIT 1",
        story_id,
    )
    if not approval_id:
        return JSONResponse({"ok": False, "error": "no diff is awaiting review"}, status_code=409)
    result = await decide_diff_review(
        approval_id=str(approval_id), decision=decision,
        feedback=body.get("feedback") or "", decided_by_uid=uid,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/recent-messages")
async def recent_messages(request: Request) -> JSONResponse:
    """Recent conversation turns on the story's thread — seeds a rebuilt local session
    (lost transcript) so it recovers the dialogue. Read-only, story-token authed."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    return JSONResponse(await recent_local_messages(story_id), status_code=200)


@router.post("/get-state")
async def get_state(request: Request) -> JSONResponse:
    """Read-only story-state snapshot (the `get_state` MCP tool + the
    `UserPromptSubmit` state-injection hook call this)."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    return JSONResponse(await local_state(story_id), status_code=200)


@router.post("/report-message")
async def report_message_turn(request: Request) -> JSONResponse:
    """Mirror a local conversation turn into the story thread (record-only, no
    gateway wake) so memory/continuity survives a local story (§7.4/§14)."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id, role = _field(body, "story_id"), _field(body, "role")
    if not (story_id and role):
        return JSONResponse({"ok": False, "error": "story_id, role required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    sender, card_data = _field(body, "sender"), body.get("card_data")
    principal = get_agent_principal(request)
    if principal is not None and principal.token_type == "human_local":
        # THE STORY TOKEN MIRRORS TEXT, NOTHING MORE. The hooks send a role and the
        # words; they never send a card or a sender. Anything holding this token --
        # including an agent that read it off disk -- could otherwise post a
        # `plan_review` card with a hash of its choosing (and `/plan-approve` bound to
        # the newest such card), or a message signed with somebody else's name. Cards
        # come from the control plane's own services; the sender is resolved here.
        if card_data:
            return JSONResponse(
                {"ok": False, "reason": "cards_not_allowed",
                 "error": "a story token cannot post cards"}, status_code=403)
        sender = ""
    result = await report_message(story_id, role, body.get("content") or "", sender, card_data)
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    return JSONResponse(result, status_code=_MSG_ERR_STATUS.get(result.get("reason"), 409))


@router.post("/push-token")
async def push_token(request: Request) -> JSONResponse:
    """Short-lived WRITE token so the local clone can push the feature branch at
    landing (§8.2). Landing authority stays CP-side (git engine opens the PR)."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    result = await mint_push_token(story_id, multi_repo=supports_multi_repo(request))
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.post("/diff-submit")
async def diff_submit(request: Request) -> JSONResponse:
    """Submit the pushed feature-branch diff for human review (§14.2). Reuses the
    shared diff-review card/approval; a human approves via the normal browser path.

    A multi-repo-capable client (`repos` in the body — D-5, C4/C5) sends one entry
    per repo (`{name, diff, stat, truncated, commit_sha}`) instead of the scalar
    `diff`/`stat`/`truncated`/`commit_sha` fields; the scalar fields are then ignored
    (see `submit_local_diff`'s doc comment). Without a `repos` array, the scalar
    fields are required exactly as before this capability existed.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    repos = body.get("repos") if supports_multi_repo(request) else None
    if not isinstance(repos, list):
        repos = None
    if not story_id or (not repos and not (body.get("diff") or "")):
        return JSONResponse(
            {"ok": False, "error": "story_id and (diff or repos) required"}, status_code=400,
        )
    _enforce_local_scope(request, story_id)
    result = await submit_local_diff(
        story_id, body.get("diff") or "", _field(body, "stat"), bool(body.get("truncated")),
        _field(body, "commit_sha"), repos=repos,
    )
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    return JSONResponse(result, status_code=_DIFF_ERR_STATUS.get(result.get("reason"), 409))


@router.post("/artifact-emit")
async def artifact_emit(request: Request) -> JSONResponse:
    """Store an artifact a LOCAL run produced, and catalogue it.

    NOT `/internal/artifact`. That one authorises with the EXECUTION's own
    token -- "the worker has the answer and not the key" -- and a local run
    has no execution and no such token. Adding a second arm there would mean
    an endpoint whose auth depends on which field the caller supplied, which
    is the shape mistakes hide in. This route takes the same story-scoped
    local token every other `/internal/local/*` route takes, and
    `_enforce_local_scope` binds it to the story in the body.

    The BYTES go to the same place either way: `aput` is content-addressed and
    the bucket comes from this service's own env, so a pod's artifact and a
    laptop's land in one store and dedup against each other.
    """
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)

    import base64
    from app.services.local_artifacts import ArtifactRefused, emit_local_artifact

    raw = body.get("content_b64")
    if not isinstance(raw, str) or not raw:
        return JSONResponse(
            {"ok": False, "error": "content_b64 required"}, status_code=400)
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": "content_b64 is not valid base64"}, status_code=400)

    try:
        result = await emit_local_artifact(
            story_id, _field(body, "role") or "report", _field(body, "name"),
            data, _field(body, "summary"), _field(body, "media_type"),
            _field(body, "supersedes"),
        )
    except ArtifactRefused as refusal:
        # 400 and the sentence: the reader is the agent that just called it,
        # and a refusal it can act on costs one retry instead of a leg.
        return JSONResponse({"ok": False, "error": str(refusal)}, status_code=400)
    if result.get("ok"):
        return JSONResponse(result, status_code=200)
    return JSONResponse(result, status_code=404 if result.get("reason") ==
                        "story_not_found" else 409)


@router.post("/artifact-list")
async def artifact_list(request: Request) -> JSONResponse:
    """What this local story has emitted so far — heads of each chain."""
    verify_internal_bearer(request)
    body = await request.json()
    story_id = _field(body, "story_id")
    if not story_id:
        return JSONResponse({"ok": False, "error": "story_id required"}, status_code=400)
    _enforce_local_scope(request, story_id)
    from app.database import get_pool
    from app.services.errand_result import artifacts_for_story
    rows = await artifacts_for_story(get_pool(), story_id)
    return JSONResponse({"ok": True, "artifacts": [
        {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
        for r in rows]}, status_code=200)
