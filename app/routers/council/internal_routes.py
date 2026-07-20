"""Internal council endpoint — reviewer agents submit reviews here.

Called by the gateway's `submit_review` tool (Bearer OPENCLAW_GATEWAY_TOKEN,
same as other /internal/* routes).

POST /internal/council/review
  body: { goal_id, agent_id, agent_type, review_id, review_details }
  - validate review_details per agent_type
  - invalid → 200 {ok:false, errors:[...]}   (agent corrects + resubmits)
  - valid   → store review_details in council_review_stages.output, mark the
              stage 'done', record the submitting agent_id → 200 {ok:true}

The endpoint only records the finding + marks the stage done. The dispatcher
observes stage completion and advances the DAG / delivers to GO.
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import get_pool
from app.services.council.validators import validate_review

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/council")


from app.internal_auth import verify_internal_bearer as _verify_auth  # SSOT: app/internal_auth.py


@router.post("/review")
async def submit_review(request: Request):
    _verify_auth(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "errors": ["Request body must be valid JSON."]})

    goal_id = str(body.get("goal_id") or "").strip()
    agent_id = str(body.get("agent_id") or "").strip()
    agent_type = str(body.get("agent_type") or "").strip()
    review_id = str(body.get("review_id") or "").strip()
    review_details = body.get("review_details")
    status = str(body.get("status") or "success").strip().lower()
    reason = str(body.get("reason") or "").strip()

    missing = [k for k in ("goal_id", "agent_type", "review_id")
               if not str(body.get(k) or "").strip()]
    if missing:
        return JSONResponse({"ok": False, "errors": [f"Missing required field(s): {', '.join(missing)}."]})
    if status not in ("success", "failed"):
        return JSONResponse({"ok": False, "errors": ["status must be 'success' or 'failed'."]})
    if status == "failed" and not reason:
        return JSONResponse({"ok": False, "errors": ["reason is required when status='failed'."]})

    pool = get_pool()
    stage = await pool.fetchrow(
        "SELECT id, run_id, goal_id, stage, status FROM council_review_stages WHERE id = $1",
        review_id,
    )
    if stage is None:
        return JSONResponse({"ok": False, "errors": [f"No review found for review_id {review_id!r}."]})
    if stage["goal_id"] != goal_id:
        return JSONResponse({"ok": False, "errors": ["goal_id does not match this review."]})
    if agent_type != stage["stage"]:
        return JSONResponse({
            "ok": False,
            "errors": [f"agent_type {agent_type!r} does not match this review's stage {stage['stage']!r}."],
        })

    # Agent-reported failure — record it and accept (the agent did its job by
    # reporting; it should stop, not retry). The dispatcher / convener treat a
    # failed goal_health as a coverage gap.
    if status == "failed":
        await pool.execute(
            """UPDATE council_review_stages
                  SET status = 'failed',
                      error = $1,
                      instance_id = COALESCE(NULLIF($2, ''), instance_id),
                      finished_at = NOW()
                WHERE id = $3 AND status <> 'done'""",
            f"agent-reported failure: {reason}"[:500], agent_id, review_id,
        )
        log.info("council: review reported FAILED goal=%s stage=%s review_id=%s reason=%s",
                 goal_id, agent_type, review_id, reason[:120])
        return JSONResponse({"ok": True})

    errors = validate_review(agent_type, review_details)
    if errors:
        log.info("council: review rejected goal=%s stage=%s review_id=%s errors=%d",
                 goal_id, agent_type, review_id, len(errors))
        return JSONResponse({"ok": False, "errors": errors})

    # Valid. Idempotent: re-submission of an already-done stage is accepted.
    await pool.execute(
        """UPDATE council_review_stages
              SET output = $1::jsonb,
                  status = 'done',
                  instance_id = COALESCE(NULLIF($2, ''), instance_id),
                  finished_at = NOW(),
                  error = NULL
            WHERE id = $3""",
        json.dumps(review_details), agent_id, review_id,
    )
    log.info("council: review accepted goal=%s stage=%s review_id=%s", goal_id, agent_type, review_id)
    return JSONResponse({"ok": True})
