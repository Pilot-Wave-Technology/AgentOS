"""The parked-executor surface: what is waiting, and a person's verdict.

Two endpoints and a page. The verdict endpoint is the only path in the
system that may approve a plan, because it is the only one that knows
which human is on the other end (H8a).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.database import get_pool

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def _require_member(uid: str, pid: str) -> None:
    """I5a — you must be in the goal to see or act on its questions.

    THREE ENDPOINTS SHARED ONE HOLE. `pid` was in every path and used by
    none of them for authorisation: `decide` looked a question up by id
    alone, `parked` listed a goal's open questions and their plan text to
    anybody signed in, and `retry_run` cleared a run's counters the same
    way. Any authenticated user could read or act on any goal.

    404, NOT 403, throughout. Confirming that a goal exists and has
    parked questions is itself a disclosure to somebody who cannot see
    it — the same reasoning the chat IDOR fix applied to threads.
    """
    from app.auth import get_goal_role
    from fastapi import HTTPException as _HTTPException

    if not await get_goal_role(uid or "", pid):
        raise _HTTPException(status_code=404, detail="No such goal")


@router.get("/goals/{pid}/kernel/parked")
async def parked(pid: str, user: dict = Depends(get_current_user)):
    """Open kernel questions for this goal, plan included."""
    from kernel.assistant.cards import as_text, plan_of
    from kernel.mailbox.schema import needs_a_person

    await _require_member(str(user.get("uid") or ""), pid)
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT q.id, q.kind, q.question_text, q.payload, q.created_at,
               q.execution_id, e.story_id, e.state,
               -- The third reader of one fact, asking it the same way.
               -- A plan on an AUTONOMOUS errand is the GO's to settle,
               -- so telling a person it needs them is telling them to
               -- act on something that is not theirs.
               (COALESCE(s.work_kind, 'story') = 'errand'
                AND EXISTS (SELECT 1 FROM kernel.errand_schedules g
                             WHERE g.goal_id = e.goal_id
                               AND g.approved_by IS NOT NULL
                               AND g.revoked_at IS NULL
                               AND (g.expires_at IS NULL
                                    OR g.expires_at > now())
                               AND (g.story_id IS NULL
                                    OR g.story_id = e.story_id))
               ) AS go_driven
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
          LEFT JOIN public.stories s ON s.id = e.story_id
         WHERE e.goal_id = $1 AND q.status = 'OPEN'
         ORDER BY q.created_at
        """, pid)
    out = []
    for r in rows:
        plan = plan_of(r["payload"]) if r["kind"] == "authorization" else None
        out.append({
            "question_id": r["id"], "kind": r["kind"],
            "asks": r["question_text"], "story_id": r["story_id"],
            "execution_id": r["execution_id"], "state": r["state"],
            # WAS `kind == "authorization"`, a fourth hand-written copy
            # of which kinds need a person — and it omitted acceptance,
            # escalation, direction and permission. A destructive-command
            # park listed here read as needing nobody, which is the same
            # bug the card builder had. One source now, and since
            # 2026-09-24 that source also knows an autonomous errand's
            # plan is the GO's.
            "needs_human": needs_a_person(r["kind"]),
            "plan": plan, "plan_text": as_text(plan),
            "created_at": r["created_at"].isoformat() if r["created_at"]
                          else None,
        })
    return JSONResponse({"goal": pid, "parked": out})


@router.post("/goals/{pid}/kernel/questions/{qid}/decide")
async def decide_question(pid: str, qid: int, request: Request,
                          user: dict = Depends(get_current_user)):
    """A person approves, revises, or declines a parked plan.

    I5a — THE GOAL IN THE PATH IS NOW LOAD-BEARING. It was decoration:
    `decide` looks the question up by id alone, and nothing checked that
    the question belonged to `pid` or that the caller belonged to the
    goal. Any authenticated user could approve any question in any goal —
    including a plan that lets an executor run `terraform apply` against
    an account they have never been given.

    Found while designing autonomous errands, whose rule is "any human IN
    THE GOAL may approve". That is NARROWER than what shipped, so the
    check had to exist before the approval it guards became a standing
    one.

    TWO CHECKS, NOT ONE. Membership alone would let a member of goal A
    approve goal B's question by passing their own goal in the path;
    ownership alone would let a stranger approve. Together they say: this
    question belongs to this goal, and you belong to it too.
    """
    from app.services.kernel_decide import DecideError, decide

    await _require_member(str(user.get("uid") or ""), pid)

    owner = await get_pool().fetchval(
        "SELECT goal_id FROM kernel.executions e "
        " JOIN kernel.questions q ON q.execution_id = e.id "
        " WHERE q.id = $1", qid)
    if owner is None or owner != pid:
        raise HTTPException(status_code=404, detail="No such question")

    body = {}
    try:
        body = await request.json()
    except Exception:                                       # noqa: BLE001
        form = await request.form()
        body = dict(form)

    # EDITED CHECKS RIDE WITH THE APPROVAL. Absent means "as proposed";
    # a list means the person rewrote the set and THAT is what they
    # approved. The distinction has to survive the wire, so a missing
    # key stays None rather than becoming an empty list -- which would
    # read as "approve this plan with no checks at all".
    checks = body.get("checks")
    if isinstance(checks, str):
        try:
            checks = json.loads(checks)                     # form-encoded
        except ValueError:
            checks = None
    if checks is not None and not isinstance(checks, list):
        checks = None

    try:
        result = await decide(
            question_id=qid,
            verdict=str(body.get("verdict") or ""),
            text=str(body.get("text") or ""),
            human_uid=str(user.get("uid") or ""),
            display_name=str(user.get("name") or user.get("email") or ""),
            checks=checks)
    except DecideError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:                                # noqa: BLE001
        log.exception("kernel decide failed (q=%s)", qid)
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(result)


@router.post("/goals/{pid}/kernel/executions/{eid}/retry")
async def retry_run(pid: str, eid: int, request: Request,
                    user: dict = Depends(get_current_user)):
    """A person says the cause is fixed; clear what is holding this run.

    THE SAME AUTHORITY AS AN APPROVAL, and here for the same reason
    `decide` is: this endpoint is the only one that knows which human is
    on the other end. `retry_story` refuses without that identity, so
    the gate is in the function and this is merely the one caller that
    can honestly satisfy it.

    It clears counters and recorded errors. It does not change state,
    approve anything, or move a run through a gate -- the loops already
    know how to pick it up; what they needed was permission to stop
    giving up on it.
    """
    from kernel.harness.retry import retry_story

    await _require_member(str(user.get("uid") or ""), pid)

    body = {}
    try:
        body = await request.json()
    except Exception:                                       # noqa: BLE001
        body = {}

    # The kernel's own search_path, and no outer transaction -- the same
    # two rules `kernel_decide` documents at length. `retry_story` takes
    # no role, but keeping the connection shape identical means the next
    # kernel call added here inherits a correct one.
    from app.services.kernel_conn import kernel_conn

    conn = await kernel_conn()
    try:
        out = await retry_story(conn, pid, eid,
                                human_uid=str(user.get("uid") or ""),
                                note=str(body.get("note") or ""))
    finally:
        await conn.close()

    if hasattr(out, "code"):
        # A typed refusal is a result, not a failure -- but this one is
        # read by a button, and a 200 would light up as success. The
        # message is written for the person, so it is what they see.
        raise HTTPException(status_code=400, detail=out.message)
    log.info("kernel retry: run %s by %s cleared %s",
             eid, user.get("uid"), out.get("cleared"))
    return JSONResponse(out)


@router.get("/kernel/executions", response_class=HTMLResponse)
async def executions_page(request: Request,
                          user: dict = Depends(get_current_user)):
    """System-wide: what is running, what is parked, what is queued."""
    return templates.TemplateResponse(
        "kernel/executions.html",
        {"request": request, "user": user, "active_page": "executions"})


@router.get("/api/kernel/executions")
async def executions_data(user: dict = Depends(get_current_user)):
    """The board, every goal. Ordered so the waiting come first."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT e.id, e.goal_id, e.story_id, e.kind, e.domain, e.state,
               e.generation, e.created_at, e.updated_at,
               e.container_id IS NOT NULL AS has_sandbox,
               e.stall_reason,
               g.name AS goal_name, s.title AS story_title,
               (SELECT count(*) FROM kernel.questions q
                 WHERE q.execution_id = e.id AND q.status = 'OPEN')
                 AS open_questions,
               (m.terminal_states @> to_jsonb(e.state)) AS terminal
          FROM kernel.executions e
          JOIN kernel.run_state_machine m ON m.version = e.machine_version
          LEFT JOIN public.goals g ON g.id = e.goal_id
          LEFT JOIN public.stories s ON s.id = e.story_id
         WHERE e.goal_id IS NOT NULL
           -- Goal-less rows are fixtures from the null-executor tests.
           -- They sit in RUNNING for ever and belong to nobody, so a
           -- board that counts them tells you eight things are running
           -- when one is. admit_pending refuses them for the same
           -- reason; this is the same rule, read side.
           AND (NOT (m.terminal_states @> to_jsonb(e.state))
                OR e.updated_at > now() - interval '24 hours')
         ORDER BY (m.terminal_states @> to_jsonb(e.state)),
                  e.updated_at DESC
         LIMIT 300
        """)
    # asyncpg hands back datetimes and the JSON encoder will not guess.
    out = []
    for r in rows:
        d = dict(r)
        for k in ("created_at", "updated_at"):
            d[k] = d[k].isoformat() if d[k] else None
        out.append(d)
    return JSONResponse({"executions": out})
