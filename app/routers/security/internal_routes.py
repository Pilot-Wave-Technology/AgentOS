"""Internal Security Council endpoint — aspect/convener agents submit here.

Called by the gateway's `submit_security_review` tool (Bearer
OPENCLAW_GATEWAY_TOKEN, same as other /internal/* routes).

POST /internal/security/review
  body: { run_id, agent_id, aspect, review_id, status, review_details, findings, reason }
  - status='failed'  → mark the stage failed with the reason (the agent did its
                       job by reporting; the convener treats it as a coverage gap).
  - status='success' → validate review_details per aspect + normalize findings;
                       invalid → 200 {ok:false, errors:[...]} (agent corrects);
                       valid   → replace this run+aspect's findings, store the
                       report in security_review_stages.output, mark 'done'.

The endpoint only records; the dispatcher observes completion and advances the DAG.
"""
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import get_pool
from app.services.security.validators import validate_review, normalize_findings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/security")


from app.internal_auth import verify_internal_bearer as _verify_auth  # SSOT: app/internal_auth.py


@router.post("/review")
async def submit_security_review(request: Request):
    _verify_auth(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "errors": ["Request body must be valid JSON."]})

    run_id = str(body.get("run_id") or "").strip()
    agent_id = str(body.get("agent_id") or "").strip()
    aspect = str(body.get("aspect") or "").strip()
    review_id = str(body.get("review_id") or "").strip()
    review_details = body.get("review_details")
    findings = body.get("findings")
    status = str(body.get("status") or "success").strip().lower()
    reason = str(body.get("reason") or "").strip()

    missing = [k for k in ("run_id", "aspect", "review_id") if not str(body.get(k) or "").strip()]
    if missing:
        return JSONResponse({"ok": False, "errors": [f"Missing required field(s): {', '.join(missing)}."]})
    if status not in ("success", "failed"):
        return JSONResponse({"ok": False, "errors": ["status must be 'success' or 'failed'."]})
    if status == "failed" and not reason:
        return JSONResponse({"ok": False, "errors": ["reason is required when status='failed'."]})

    pool = get_pool()
    stage = await pool.fetchrow(
        "SELECT id, run_id, aspect, status FROM security_review_stages WHERE id = $1", review_id,
    )
    if stage is None:
        return JSONResponse({"ok": False, "errors": [f"No review found for review_id {review_id!r}."]})
    if stage["run_id"] != run_id:
        return JSONResponse({"ok": False, "errors": ["run_id does not match this review."]})
    if aspect != stage["aspect"]:
        return JSONResponse({
            "ok": False,
            "errors": [f"aspect {aspect!r} does not match this review's stage {stage['aspect']!r}."],
        })

    if status == "failed":
        await pool.execute(
            "UPDATE security_review_stages SET status='failed', error=$1, "
            "instance_id=COALESCE(NULLIF($2,''), instance_id), finished_at=NOW() "
            "WHERE id=$3 AND status <> 'done'",
            f"agent-reported failure: {reason}"[:500], agent_id, review_id,
        )
        log.info("security: review reported FAILED run=%s aspect=%s reason=%s",
                 run_id, aspect, reason[:120])
        return JSONResponse({"ok": True})

    errors = validate_review(aspect, review_details)
    finding_rows, finding_errors = normalize_findings(aspect, findings)
    errors = errors + finding_errors
    if errors:
        log.info("security: review rejected run=%s aspect=%s errors=%d", run_id, aspect, len(errors))
        return JSONResponse({"ok": False, "errors": errors})

    # Valid. Replace this (run, aspect)'s findings so a resubmission is idempotent,
    # then store the report + mark done.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM security_findings WHERE run_id=$1 AND aspect=$2", run_id, aspect,
            )
            for f in finding_rows:
                await conn.execute(
                    "INSERT INTO security_findings "
                    "(id, run_id, aspect, severity, file, line, rule, tool, title, description) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                    uuid4().hex, run_id, aspect, f["severity"], f["file"], f["line"],
                    f["rule"], f["tool"], f["title"], f["description"],
                )
            await conn.execute(
                "UPDATE security_review_stages SET output=$1::jsonb, status='done', "
                "instance_id=COALESCE(NULLIF($2,''), instance_id), finished_at=NOW(), error=NULL "
                "WHERE id=$3",
                json.dumps(review_details), agent_id, review_id,
            )
    log.info("security: review accepted run=%s aspect=%s findings=%d",
             run_id, aspect, len(finding_rows))
    return JSONResponse({"ok": True})
