"""Internal API endpoints for assistant operator tools.

These endpoints are called by the assistant's OpenClaw tools (story_phase,
story_state, plan_version, escalate_for_approval, etc.) using
Bearer auth — same OPENCLAW_AUTH_TOKEN pattern used by go_internal.

Tools in agent_os_openclaw/src/agents/tools/assistant-tools.ts are thin HTTP
wrappers around these endpoints. The actual state changes (DB writes,
ledger events, approval lifecycle) live here.

For ops that don't need control-panel state (workspace file reads, git
commands, OpenClaw session lifecycle) the TypeScript side handles them
directly on the VM — no endpoint here.
"""
import asyncio
from fastapi import Depends as _sec_Depends
from app.internal_auth import ownership_guard as _ownership_guard
import json
import logging
import uuid
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.schema import (
    STORY_PHASE_CHANGED,
    STORY_STATE_CHANGED as STORY_LIFECYCLE_STATE_CHANGED,
    PLAN_PROPOSED,
    CHECKPOINT_WRITTEN,
    ESCALATION_REQUESTED,
    ESCALATION_RESOLVED,
    ESCALATION_TIMED_OUT,
    BRANCH_CREATED,
)


_log = logging.getLogger(__name__)

# System actor used for SHCA-emitted ledger events — the one canonical,
# registered actor (SHCA emit sites are re-attributed to the instance in the
# provenance work; this remains the fallback).
from app.ledger.actors import SYSTEM_ACTOR_ID, request_actor, human_actor

router = APIRouter(prefix="/internal/assistant", dependencies=[_sec_Depends(_ownership_guard)])


# ── Auth ────────────────────────────────────────────────────────────────────

from app.internal_auth import verify_internal_bearer as _verify_auth  # SSOT: app/internal_auth.py


# ── Story state machine helpers ─────────────────────────────────────────────

# Mirrors the existing CHECK constraint on stories.state. Update here if
# the constraint is extended in a future migration.
_VALID_STORY_STATES = frozenset({
    "proposed", "accepted", "in_progress", "blocked",
    "done", "rejected", "cancelled",
})


async def _read_json(request: Request) -> dict:
    """Tolerant JSON body parser."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        return await request.json()
    except Exception:
        return {}


async def _story_exists(pool, story_id: str) -> Optional[asyncpg.Record]:
    return await pool.fetchrow(
        "SELECT id, goal_id, state, current_phase FROM stories WHERE id = $1",
        story_id,
    )


# ── 1. POST /stories/{id}/phase — update current_phase ──────────────────────

@router.post("/stories/{story_id}/phase")
async def story_phase(story_id: str, request: Request):
    """Update stories.current_phase + write STORY_PHASE_CHANGED ledger event.

    Body: { "new_phase": str, "reason": str (optional) }
    Returns: { ok, story_id, from_phase, to_phase, event_id }
    """
    _verify_auth(request)
    body = await _read_json(request)
    new_phase = (body.get("new_phase") or "").strip()
    reason = body.get("reason") or ""

    if not new_phase:
        return JSONResponse({"error": "new_phase required"}, status_code=400)

    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        story = await conn.fetchrow(
            "SELECT id, goal_id, current_phase, active_thread_id FROM stories WHERE id = $1",
            story_id,
        )
        if not story:
            return JSONResponse({"error": "story not found"}, status_code=404)

        from_phase = story["current_phase"]
        await conn.execute(
            # Entering a new planning cycle clears the previous cycle's plan-ready
            # pointer so "Approve plan" stays disabled until the new plan is written
            # (validate-plan hook re-sets current_plan_path). current_plan_id is KEPT —
            # the planning skill needs it to detect MODE C (new cycle) vs MODE A.
            "UPDATE stories SET current_phase = $1, "
            "current_plan_path = CASE WHEN $1 = 'planning' THEN NULL ELSE current_plan_path END, "
            "updated_at = NOW() WHERE id = $2",
            new_phase, story_id,
        )
        _lact, _ltype, _lobo = await request_actor(request)
        evt = await append_event(
            conn,
            actor_id=_lact, actor_type=_ltype, on_behalf_of=_lobo,
            event_type=STORY_PHASE_CHANGED,
            payload={
                "story_id": story_id,
                "from_phase": from_phase,
                "to_phase": new_phase,
                "reason": reason,
            },
            context_goal_id=story["goal_id"],
        )

    # Push the phase change to any open chat tab so the phase banner + its buttons
    # re-render immediately — WS-first, matching plan_ready / dag_recovery / diff_review.
    # (An assistant story_phase call previously had no live signal, so the banner stayed
    # stale until a manual refresh.) Best-effort: the client's post-turn poll is the
    # lost-frame fallback.
    if from_phase != new_phase and story["active_thread_id"]:
        try:
            from app.openclaw import session_manager
            thread_id = story["active_thread_id"]
            tr = await pool.fetchrow("SELECT agent_id FROM threads WHERE id = $1", thread_id)
            if tr:
                await session_manager.broadcast_to_thread(
                    story["goal_id"], tr["agent_id"], thread_id,
                    {"type": "phase_changed", "story_id": story_id,
                     "from_phase": from_phase, "to_phase": new_phase},
                )
        except Exception:
            _log.warning("story_phase: phase_changed broadcast failed (story=%s)", story_id)

    return JSONResponse({
        "ok": True,
        "story_id": story_id,
        "from_phase": from_phase,
        "to_phase": new_phase,
        "event_id": (evt or {}).get("event_id"),
    })


# ── 2. POST /stories/{id}/state — state machine transition ──────────────────

@router.post("/stories/{story_id}/state")
async def story_state(story_id: str, request: Request):
    """Update stories.state with CHECK whitelist validation + ledger event.

    Body: { "new_state": str, "reason": str (optional) }
    Returns: { ok, story_id, from_state, to_state, event_id }
    """
    _verify_auth(request)
    body = await _read_json(request)
    new_state = (body.get("new_state") or "").strip()
    reason = body.get("reason") or ""

    if new_state not in _VALID_STORY_STATES:
        return JSONResponse(
            {
                "error": f"invalid state '{new_state}'",
                "valid": sorted(_VALID_STORY_STATES),
            },
            status_code=400,
        )

    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        story = await conn.fetchrow(
            "SELECT id, goal_id, state, story_kind FROM stories WHERE id = $1",
            story_id,
        )
        if not story:
            return JSONResponse({"error": "story not found"}, status_code=404)

        # Engine merge stories are driven by the Git Engine + PR-merge + detection, not
        # by an SHCA tool — a merge story should never be attached to an SHCA (the import
        # path now refuses it), but guard the raw state writer too (defense in depth).
        if (story.get("story_kind") or "code") == "merge":
            return JSONResponse(
                {"error": "merge stories are managed by the Git Engine and cannot be "
                          "transitioned via the assistant"},
                status_code=409,
            )

        from_state = story["state"]
        if from_state == new_state:
            return JSONResponse({
                "ok": True, "no_change": True,
                "story_id": story_id, "state": new_state,
            })

        # Diff-review gate: a story with an undecided diff approval has a captured
        # diff waiting for the human's Approve & Push (which is what actually pushes
        # the branch). The assistant must NOT short-circuit to a terminal state here
        # — doing so strands the diff unpushed (observed live: an engine integrate
        # story was marked done by the assistant's story_state tool ~3s after its DAG
        # completed, so its merge commit never got pushed to the feature branch). The
        # human's diff decision drives completion. Defense in depth alongside the
        # merge-story guard above.
        if new_state in ("done", "reviewed"):
            pending_diff = await conn.fetchval(
                "SELECT 1 FROM approvals WHERE story_id = $1 AND kind = 'diff' "
                "AND decided_at IS NULL LIMIT 1",
                story_id,
            )
            if pending_diff:
                return JSONResponse(
                    {"error": "this story has a diff awaiting human review + push; it "
                              "completes when the human approves the diff — do not mark "
                              "it done"},
                    status_code=409,
                )

        # On terminal-state transitions (done/cancelled/rejected), also
        # clear active_thread_id so the SHCA cleanup watchdog can't
        # mis-match this finished story against the SHCA's persistent
        # thread when a new story comes in on the same thread later.
        await conn.execute(
            "UPDATE stories SET state = $1, "
            "  active_thread_id = CASE "
            "    WHEN $1 IN ('done', 'cancelled', 'rejected') THEN NULL "
            "    ELSE active_thread_id "
            "  END, "
            "  updated_at = NOW() "
            "WHERE id = $2",
            new_state, story_id,
        )
        _lact, _ltype, _lobo = await request_actor(request)
        evt = await append_event(
            conn,
            actor_id=_lact, actor_type=_ltype, on_behalf_of=_lobo,
            event_type=STORY_LIFECYCLE_STATE_CHANGED,
            payload={
                "story_id": story_id,
                "from_state": from_state,
                "to_state": new_state,
                "reason": reason,
            },
            context_goal_id=story["goal_id"],
        )

    return JSONResponse({
        "ok": True,
        "story_id": story_id,
        "from_state": from_state,
        "to_state": new_state,
        "event_id": (evt or {}).get("event_id"),
    })


# ── 3. POST /stories/{id}/branches — register a new branch ──────────────────

@router.post("/stories/{story_id}/branches")
async def story_branch_create(story_id: str, request: Request):
    """Insert a story_branches row (after TS-side `git branch` succeeds).

    Body: {
      "branch_name": str,
      "parent_branch": str (optional),
      "purpose": str (optional, default 'experiment'),
      "plan_version_id": uuid str (optional),
      "worktree_path": str (optional)
    }
    Returns: { ok, branch_id }
    """
    _verify_auth(request)
    body = await _read_json(request)
    branch_name = (body.get("branch_name") or "").strip()
    if not branch_name:
        return JSONResponse({"error": "branch_name required"}, status_code=400)

    pool = get_pool()
    story = await _story_exists(pool, story_id)
    if not story:
        return JSONResponse({"error": "story not found"}, status_code=404)

    branch_id = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO story_branches
              (id, story_id, branch_name, parent_branch, purpose,
               plan_version_id, worktree_path)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            branch_id, story_id, branch_name,
            body.get("parent_branch"),
            body.get("purpose") or "experiment",
            body.get("plan_version_id"),
            body.get("worktree_path"),
        )
        _lact, _ltype, _lobo = await request_actor(request)
        await append_event(
            conn,
            actor_id=_lact, actor_type=_ltype, on_behalf_of=_lobo,
            event_type=BRANCH_CREATED,
            payload={
                "story_id": story_id,
                "branch_id": str(branch_id),
                "branch_name": branch_name,
                "purpose": body.get("purpose") or "experiment",
                "parent_branch": body.get("parent_branch"),
            },
            context_goal_id=story["goal_id"],
        )

    return JSONResponse({"ok": True, "branch_id": str(branch_id)})


# ── 4. POST /stories/{id}/checkpoints — snapshot at a gate ─────────────────

@router.post("/stories/{story_id}/checkpoints")
async def story_checkpoint(story_id: str, request: Request):
    """Insert a checkpoints row (snapshot at named gate).

    Body: {
      "name": str,            # e.g. 'plan_v1', 'diff_v1', 'deploy_plan_v1'
      "payload": object,      # the thing being snapshotted
      "git_sha": str (optional),
      "approval_id": uuid str (optional, link to approvals row if for a gate)
    }
    Returns: { ok, checkpoint_id }
    """
    _verify_auth(request)
    body = await _read_json(request)
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)

    pool = get_pool()
    story = await _story_exists(pool, story_id)
    if not story:
        return JSONResponse({"error": "story not found"}, status_code=404)

    checkpoint_id = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO checkpoints (id, story_id, name, payload, git_sha, approval_id)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6)
            """,
            checkpoint_id, story_id, name,
            json.dumps(body.get("payload") or {}),
            body.get("git_sha"),
            body.get("approval_id"),
        )
        _lact, _ltype, _lobo = await request_actor(request)
        await append_event(
            conn,
            actor_id=_lact, actor_type=_ltype, on_behalf_of=_lobo,
            event_type=CHECKPOINT_WRITTEN,
            payload={
                "story_id": story_id,
                "checkpoint_id": str(checkpoint_id),
                "name": name,
                "git_sha": body.get("git_sha"),
            },
            context_goal_id=story["goal_id"],
        )

    return JSONResponse({"ok": True, "checkpoint_id": str(checkpoint_id)})


# ── 5. POST /stories/{id}/plans — register a plan version ──────────────────

@router.post("/stories/{story_id}/plans")
async def plan_version_create(story_id: str, request: Request):
    """Insert a plan_versions row (after TS-side wrote docs/plans/v<N>.md).

    Body: {
      "version": int,                       # required; usually max(version)+1
      "plan_doc_path": str,                 # workspace-relative, e.g. 'docs/plans/v1.md'
      "task_dag": object,                   # structured plan from Claude
      "risk_level": "low" | "medium" | "high" (optional),
      "proposed_by": str,                   # 'shca' | 'ia:research-ia' | 'human:<uid>'
      "claude_session_id": str (optional),
      "branch_id": uuid str (optional),
      "parent_plan_id": uuid str (optional),
      "reason": str (optional)
    }
    Returns: { ok, plan_id, version }
    """
    _verify_auth(request)
    body = await _read_json(request)
    version = body.get("version")
    if not isinstance(version, int) or version < 1:
        return JSONResponse({"error": "version must be positive integer"}, status_code=400)

    pool = get_pool()
    story = await _story_exists(pool, story_id)
    if not story:
        return JSONResponse({"error": "story not found"}, status_code=404)

    plan_id = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        try:
            await conn.execute(
                """
                INSERT INTO plan_versions
                  (id, story_id, version, parent_plan_id, plan_doc_path,
                   task_dag, risk_level, proposed_by, claude_session_id,
                   branch_id, reason, status)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, 'proposed')
                """,
                plan_id, story_id, version,
                body.get("parent_plan_id"),
                body.get("plan_doc_path"),
                json.dumps(body.get("task_dag") or {}),
                body.get("risk_level"),
                body.get("proposed_by") or "shca",
                body.get("claude_session_id"),
                body.get("branch_id"),
                body.get("reason"),
            )
        except asyncpg.UniqueViolationError:
            return JSONResponse(
                {"error": f"plan version {version} already exists for story {story_id}"},
                status_code=409,
            )

        _lact, _ltype, _lobo = await request_actor(request)
        await append_event(
            conn,
            actor_id=_lact, actor_type=_ltype, on_behalf_of=_lobo,
            event_type=PLAN_PROPOSED,
            payload={
                "story_id": story_id,
                "plan_id": str(plan_id),
                "version": version,
                "risk_level": body.get("risk_level"),
                "proposed_by": body.get("proposed_by") or "shca",
                "claude_session_id": body.get("claude_session_id"),
            },
            context_goal_id=story["goal_id"],
        )

    return JSONResponse({"ok": True, "plan_id": str(plan_id), "version": version})


# ── 6. POST /stories/{id}/approvals — request human approval (non-blocking) ─

@router.post("/stories/{story_id}/approvals")
async def approval_request(story_id: str, request: Request):
    """Create an approval request. Returns immediately with approval_id; the
    SHCA tool then polls GET /approvals/{id}/poll until decided.

    Body: {
      "kind": str,            # 'plan' | 'diff' | 'deploy_to_prod' | 'clarification' | ...
      "payload": object,
      "payload_ref": str (optional),
      "channels": [str],      # ['ui', 'slack', 'email']
      "timeout_hours": number (optional, default 4)
    }
    Returns: { ok, approval_id, timeout_at }
    """
    _verify_auth(request)
    body = await _read_json(request)
    kind = (body.get("kind") or "").strip()
    if not kind:
        return JSONResponse({"error": "kind required"}, status_code=400)

    pool = get_pool()
    story = await _story_exists(pool, story_id)
    if not story:
        return JSONResponse({"error": "story not found"}, status_code=404)

    approval_id = uuid.uuid4()
    timeout_hours = body.get("timeout_hours") or 4
    channels = body.get("channels") or ["ui"]
    if not isinstance(channels, list):
        channels = ["ui"]

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO approvals
              (id, story_id, kind, payload, payload_ref, channels, timeout_at)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6,
                    NOW() + ($7 || ' hours')::INTERVAL)
            """,
            approval_id, story_id, kind,
            json.dumps(body.get("payload") or {}),
            body.get("payload_ref"),
            channels,
            str(timeout_hours),
        )
        _lact, _ltype, _lobo = await request_actor(request)
        await append_event(
            conn,
            actor_id=_lact, actor_type=_ltype, on_behalf_of=_lobo,
            event_type=ESCALATION_REQUESTED,
            payload={
                "story_id": story_id,
                "approval_id": str(approval_id),
                "kind": kind,
                "channels": channels,
                "timeout_hours": timeout_hours,
            },
            context_goal_id=story["goal_id"],
        )

    # TODO Phase 2.5: trigger Slack/email push for channels other than 'ui'.
    # The UI surface (Phase 9 dashboard) will render pending approvals from
    # the table directly without needing a separate push.

    timeout_at = await pool.fetchval(
        "SELECT timeout_at FROM approvals WHERE id = $1", approval_id,
    )
    return JSONResponse({
        "ok": True,
        "approval_id": str(approval_id),
        "timeout_at": timeout_at.isoformat() if timeout_at else None,
    })


# ── 7. GET /approvals/{id}/poll — long-poll for decision ────────────────────

_POLL_INTERVAL_S = 2.0
_POLL_MAX_S = 300.0  # 5 min — caller (TS) will re-poll if still undecided.


@router.get("/approvals/{approval_id}/poll")
async def approval_poll(approval_id: str, request: Request):
    """Long-poll for an approval decision. Returns when decided, or after
    timeout (caller should re-poll).

    Query: ?timeout=<seconds>  (default 60, max 300)
    Returns when decided:
      { decided: true, decision, decided_by, decided_at, feedback }
    Returns on poll timeout (not approval timeout):
      { decided: false, timed_out_polling: true, approval_id }
    Returns on approval timeout (the timeout_at on the row):
      { decided: false, timed_out_approval: true, approval_id }
    """
    _verify_auth(request)

    timeout_s = float(request.query_params.get("timeout", "60"))
    timeout_s = max(1.0, min(timeout_s, _POLL_MAX_S))

    pool = get_pool()
    elapsed = 0.0
    while elapsed < timeout_s:
        row = await pool.fetchrow(
            """SELECT id, story_id, kind, decision, decided_by, decided_at,
                      feedback, timeout_at
                 FROM approvals WHERE id = $1""",
            approval_id,
        )
        if not row:
            return JSONResponse({"error": "approval not found"}, status_code=404)

        if row["decided_at"] is not None:
            # Decided — write resolution ledger event once (idempotent: only if
            # we haven't already written one for this approval).
            existing_resolved = await pool.fetchval(
                """SELECT 1 FROM ledger_events
                    WHERE event_type = $1
                      AND payload->>'approval_id' = $2
                    LIMIT 1""",
                ESCALATION_RESOLVED, approval_id,
            )
            if not existing_resolved:
                story = await pool.fetchrow(
                    "SELECT goal_id FROM stories WHERE id = $1", row["story_id"],
                )
                async with pool.acquire() as conn, conn.transaction():
                    # The human who decided the approval is the actor (system if
                    # it resolved via timeout with no decider).
                    _rby_actor, _rby_type = await human_actor(row["decided_by"])
                    await append_event(
                        conn,
                        actor_id=_rby_actor, actor_type=_rby_type,
                        event_type=ESCALATION_RESOLVED,
                        payload={
                            "story_id": row["story_id"],
                            "approval_id": approval_id,
                            "kind": row["kind"],
                            "decision": row["decision"],
                            "decided_by": row["decided_by"],
                            "feedback": row["feedback"],
                        },
                        context_goal_id=(story or {}).get("goal_id"),
                    )

            return JSONResponse({
                "decided": True,
                "decision": row["decision"],
                "decided_by": row["decided_by"],
                "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
                "feedback": row["feedback"],
            })

        # Check approval-level timeout (separate from poll timeout)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if row["timeout_at"] and row["timeout_at"] <= now:
            # Mark timed out + emit ledger event (idempotent)
            async with pool.acquire() as conn, conn.transaction():
                updated = await conn.fetchval(
                    """UPDATE approvals
                          SET decision = COALESCE(decision, 'timed_out'),
                              decided_at = COALESCE(decided_at, NOW())
                        WHERE id = $1 AND decided_at IS NULL
                        RETURNING id""",
                    approval_id,
                )
                if updated:
                    story = await conn.fetchrow(
                        "SELECT goal_id FROM stories WHERE id = $1", row["story_id"],
                    )
                    await append_event(
                        conn,
                        actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                        event_type=ESCALATION_TIMED_OUT,
                        payload={
                            "story_id": row["story_id"],
                            "approval_id": approval_id,
                            "kind": row["kind"],
                        },
                        context_goal_id=(story or {}).get("goal_id"),
                    )
            return JSONResponse({
                "decided": False,
                "timed_out_approval": True,
                "approval_id": approval_id,
            })

        await asyncio.sleep(_POLL_INTERVAL_S)
        elapsed += _POLL_INTERVAL_S

    return JSONResponse({
        "decided": False,
        "timed_out_polling": True,
        "approval_id": approval_id,
    })


# ── 8. POST /approvals/{id}/decide — UI/Slack action handler ────────────────
# Called by the human-facing UI when the user clicks Approve/Reject/etc.
# Separate from the SHCA-tool endpoints above (different auth surface) but
# kept in this file to keep the approval flow co-located.

@router.post("/approvals/{approval_id}/decide")
async def approval_decide(approval_id: str, request: Request):
    """Record a human decision on an approval.

    Body: {
      "decision": "approved" | "rejected" | "requested_changes" | "aborted",
      "decided_by": str,    # user uid
      "feedback": str (optional)
    }
    Returns: { ok, approval_id, decision, decided_at }
    """
    _verify_auth(request)
    # P0-C — human-approval integrity: a plan/diff/escalation decision must NEVER
    # originate from an agent, and `decided_by` must be a real human member of the
    # approval's goal. Without this, any caller on the internal-bearer surface
    # (incl. an agent) could POST a decision with an arbitrary `decided_by` and
    # self-approve. Legit human decisions flow through decide_diff_review (browser
    # /diff/decide via Firebase, Slack via a verified user) — not this endpoint.
    from app.internal_auth import get_agent_principal  # noqa: PLC0415
    from app.auth import get_goal_role  # noqa: PLC0415
    if get_agent_principal(request) is not None:
        return JSONResponse(
            {"error": "approvals may only be decided by a human, not an agent"},
            status_code=403,
        )
    body = await _read_json(request)
    decision = (body.get("decision") or "").strip()
    decided_by = (body.get("decided_by") or "").strip()

    valid_decisions = {"approved", "rejected", "requested_changes", "aborted"}
    if decision not in valid_decisions:
        return JSONResponse(
            {"error": f"invalid decision; must be one of {sorted(valid_decisions)}"},
            status_code=400,
        )
    if not decided_by:
        return JSONResponse({"error": "decided_by required"}, status_code=400)

    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        # decided_by must be a member of the approval's goal (resolve via story).
        appr = await conn.fetchrow(
            """SELECT a.story_id, s.goal_id
                 FROM approvals a
                 LEFT JOIN stories s ON s.id = a.story_id
                WHERE a.id = $1""",
            approval_id,
        )
        if not appr:
            return JSONResponse({"error": "approval not found"}, status_code=404)
        if not appr["goal_id"]:
            return JSONResponse(
                {"error": "approval has no goal to authorize the decider against"},
                status_code=409,
            )
        if await get_goal_role(decided_by, appr["goal_id"]) is None:
            return JSONResponse(
                {"error": "decided_by is not a member of this approval's goal"},
                status_code=403,
            )
        updated = await conn.fetchrow(
            """UPDATE approvals
                  SET decision = $1, decided_by = $2,
                      decided_at = NOW(), feedback = $3
                WHERE id = $4 AND decided_at IS NULL
                RETURNING id, decided_at, story_id, kind""",
            decision, decided_by, body.get("feedback"), approval_id,
        )
        if not updated:
            return JSONResponse(
                {"error": "approval not found or already decided"},
                status_code=409,
            )

    # Ledger ESCALATION_RESOLVED is written by the next /poll caller (SHCA
    # picks it up). This avoids double-writes if both the UI and SHCA poll
    # race the resolution. The poll endpoint guards with EXISTS check.

    return JSONResponse({
        "ok": True,
        "approval_id": approval_id,
        "decision": decision,
        "decided_at": updated["decided_at"].isoformat(),
    })


# ── 9. POST /ledger — generic SHCA ledger event writer ──────────────────────
# Catch-all for events SHCA emits that don't fit a specialized endpoint
# (CLAUDE_SESSION_SPAWNED, CLAUDE_SESSION_KILLED, COMMIT_CREATED,
#  PUSH_EXECUTED, BRANCH_CHECKED_OUT, LIVING_DOC_WRITTEN, etc.).
#
# Specialized endpoints above (phase, state, plans, checkpoints) write
# their own ledger events; this is the escape hatch for everything else
# without needing a new endpoint per event type.

@router.post("/ledger")
async def ledger_write(request: Request):
    """Write a generic ledger event from SHCA.

    Body: {
      "event_type": str,             # ledger/schema constant value
      "story_id": str (optional),
      "task_id": str (optional),
      "goal_id": str (optional, auto-resolved from story if missing),
      "payload": object
    }
    Returns: { ok, event_id }
    """
    _verify_auth(request)
    body = await _read_json(request)
    event_type = (body.get("event_type") or "").strip()
    if not event_type:
        return JSONResponse({"error": "event_type required"}, status_code=400)

    pool = get_pool()
    goal_id = body.get("goal_id")
    story_id = body.get("story_id")
    task_id = body.get("task_id")

    if not goal_id and story_id:
        row = await pool.fetchrow("SELECT goal_id FROM stories WHERE id = $1", story_id)
        if row:
            goal_id = row["goal_id"]

    async with pool.acquire() as conn, conn.transaction():
        _lact, _ltype, _lobo = await request_actor(request)
        evt = await append_event(
            conn,
            actor_id=_lact, actor_type=_ltype, on_behalf_of=_lobo,
            event_type=event_type,
            payload=body.get("payload") or {},
            context_goal_id=goal_id,
            context_task_id=task_id,
        )

    return JSONResponse({"ok": True, "event_id": (evt or {}).get("event_id")})


# ── Live tool-call progress from Claude's PostToolUse hook ──────────────────
#
# claude-cli's PostToolUse hook on the VM fires after every tool call and
# POSTs here with the tool name + truncated input. We persist a small
# progress card to the chat thread so the UI can show live activity
# ("🔍 Reading X", "✏️ Editing Y", "🐚 Running: pytest tests/").
#
# Mechanical writes: sender='task-controller', card_data populated. The
# SHCA LLM never sees these (so no token cost on summarization).

@router.get("/instances/{instance_id}/active-story")
async def get_active_story_for_instance(
    instance_id: str, request: Request,
) -> JSONResponse:
    """Resolve the active story for an SHCA instance.

    Called by the gateway's sessions_spawn tool (Phase 4 Stage 4
    Option B) so SHCA doesn't have to pass story_id / cwd / session_id
    explicitly — the gateway can fill them in from server-side state.

    Returns:
      { ok: True, story_id, workspace_path, claude_session_id }
      { ok: True, story_id: None } if no active story
    """
    _verify_auth(request)
    pool = get_pool()
    # Deterministic active story via the shared resolver (is_active_focus +
    # single-non-terminal fallback), keyed on shca_instance_id — the SAME resolver
    # the state snapshot uses (thread-keyed), so the two never disagree. LOWER()
    # match is preserved inside the resolver for the instance key.
    from app.services.story_state import resolve_active_focus_story
    row, ambiguous = await resolve_active_focus_story(pool, instance_id=instance_id)
    if not row:
        # ambiguous=True → >1 non-terminal, no focus: report it so the caller
        # (claude_code_communication) refuses rather than guessing a story.
        return JSONResponse({"ok": True, "story_id": None, "ambiguous": ambiguous})
    # Mask the prespawn-in-progress sentinel so the (legacy) gateway doesn't try
    # to use it as a real resumeSessionId; surface prespawn_in_flight so the new
    # tool can return `busy` instead of forking a second session.
    _csid = row["current_claude_session_id"] or ""
    prespawn_in_flight = _csid.startswith("__prespawn_")
    if prespawn_in_flight:
        _csid = ""
    # C2 gate input: is a claude-code-phase / system-gate task in flight for this
    # story? Reuse the dispatcher's busy_stories predicate EXACTLY (executor +
    # states, incl. 'blocked' phase = mid-Q&A, still owns the session) so the
    # tool's durable gate agrees with what actually blocks phase dispatch. The
    # gateway C3 lock also catches this at spawn time; this is the durable,
    # multi-gateway-safe check the tool consumes to return `busy` up front.
    phase_in_flight = bool(await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM tasks
             WHERE story_id = $1
               AND (
                   (executor = 'claude-code-phase'
                    AND state IN ('in_progress', 'dispatching', 'blocked'))
                   OR
                   (executor = 'system-gate'
                    AND state IN ('in_progress', 'dispatching', 'awaiting_approval'))
               )
        )
        """,
        row["id"],
    ))
    # Additive fields (current_phase, execution_paused, prespawn_in_flight,
    # phase_in_flight, ambiguous) — old gateway callers read only the first three
    # keys and ignore these; the new claude_code_communication gate consumes them.
    return JSONResponse({
        "ok": True,
        "story_id": row["id"],
        "workspace_path": row["workspace_path"] or "",
        "claude_session_id": _csid,
        "current_phase": row["current_phase"] or "",
        "execution_paused": bool(row["execution_paused"]),
        "prespawn_in_flight": prespawn_in_flight,
        "phase_in_flight": phase_in_flight,
        "ambiguous": ambiguous,
    })


@router.get("/stories/{story_id}/execution-status")
async def get_execution_status(story_id: str, request: Request) -> JSONResponse:
    """Return whether autonomous execution is currently paused for this story's goal.

    Called by the pause-check PreToolUse hook before every tool call during
    a claude-code-phase turn. If paused, the hook exits 2 and Claude outputs
    PHASE_TASK_BLOCKED so the turn stops cleanly at the next tool boundary rather
    than continuing to make API calls while the operator's pause is active.

    Checks system → goal precedence via dispatch_block. Does NOT check
    stories.execution_paused (operator-mode replan) — that is a separate signal
    already gated at dispatch time; the hook only needs the kill-switch scopes.
    """
    _verify_auth(request)
    pool = get_pool()
    row = await pool.fetchrow("SELECT goal_id FROM stories WHERE id = $1", story_id)
    if not row:
        # Unknown story — fail open so a stale hook config doesn't block all work.
        return JSONResponse({"paused": False})

    from app.services.execution_control import dispatch_block  # noqa: PLC0415
    block = await dispatch_block(str(row["goal_id"]))
    if block:
        return JSONResponse({
            "paused": True,
            "scope": block.scope,
            "reason": block.reason or "",
        })
    return JSONResponse({"paused": False})


@router.post("/stories/{story_id}/claude-session")
async def record_claude_session(story_id: str, request: Request) -> JSONResponse:
    """Persist the claude-cli session id on the story row.

    Called by the gateway's cli-spawn after every successful spawn with
    a storyId. Idempotent: writes only if the value differs from what's
    already on the row.
    """
    _verify_auth(request)
    body = await _read_json(request)
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT current_claude_session_id FROM stories WHERE id = $1",
        story_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="story not found")
    if (row["current_claude_session_id"] or "") == session_id:
        return JSONResponse({"ok": True, "unchanged": True})

    await pool.execute(
        "UPDATE stories SET current_claude_session_id = $1, "
        "updated_at = NOW() WHERE id = $2",
        session_id, story_id,
    )
    _log.info(
        "claude_session: recorded session_id=%s for story=%s",
        session_id, story_id,
    )
    return JSONResponse({"ok": True, "session_id": session_id})


@router.post("/stories/{story_id}/token-usage")
async def record_phase_token_usage(story_id: str, request: Request) -> JSONResponse:
    """Persist incremental token usage from a claude-code-phase turn.

    Called by the gateway's cli-spawn after each assistant LLM response (parsed
    from the stream-json stdout). Each call writes one row to token_calls so the
    dashboard shows cost building up through a long session rather than only
    appearing at the end.

    Body: { task_id, session_key, provider, model, usage: {input_tokens,
            output_tokens, thinking_tokens, cache_read_input_tokens,
            cache_creation_input_tokens} }
    """
    _verify_auth(request)
    body = await _read_json(request)

    usage = body.get("usage") or {}
    provider = (body.get("provider") or "claude-cli").lower().strip()
    model = (body.get("model") or "").lower().strip()
    task_id = (body.get("task_id") or "").strip()
    session_key = (body.get("session_key") or "").strip()

    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    thinking = int(usage.get("thinking_tokens") or 0)
    # Claude stream-json uses different field names from the webhook
    cr = int(usage.get("cache_read_tokens") or usage.get("cache_read_input_tokens") or 0)
    cw = int(usage.get("cache_write_tokens") or usage.get("cache_creation_input_tokens") or 0)

    if inp == 0 and out == 0 and cr == 0 and cw == 0:
        return JSONResponse({"ok": True, "skipped": "no tokens"})

    pool = get_pool()
    # Resolve goal_id from the story so the dashboard per-goal breakdown works.
    goal_id = await pool.fetchval("SELECT goal_id FROM stories WHERE id = $1", story_id)

    # goal_id is TEXT after the token_calls_goal_id_text migration. If that
    # migration hasn't run yet, the column is still UUID and the INSERT would
    # fail with a type error. Retry without goal_id in that case so token
    # counts are at least recorded (unattributed) rather than silently dropped.
    goal_id_str = str(goal_id) if goal_id else None
    try:
        await pool.execute(
            """INSERT INTO token_calls
                   (provider, model, goal_id, agent_id, run_id, session_key,
                    input_tokens, output_tokens, thinking_tokens,
                    cache_read_tokens, cache_write_tokens, record_type, story_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'call',$12)""",
            provider, model, goal_id_str,
            None,       # agent_id not applicable for claude-code-phase
            task_id or None,
            session_key or None,
            inp, out, thinking, cr, cw,
            story_id,
        )
        _log.info(
            "phase token usage: story=%s task=%s provider=%s model=%s "
            "in=%d out=%d thinking=%d cr=%d cw=%d goal=%s",
            story_id, task_id, provider, model, inp, out, thinking, cr, cw, goal_id_str,
        )
    except Exception as exc:
        if "token_calls" in str(exc) and ("uuid" in str(exc).lower() or "type" in str(exc).lower()):
            # token_calls_goal_id_text migration hasn't run yet — retry without goal_id.
            _log.warning(
                "phase token_calls: goal_id type mismatch (run token_calls_goal_id_text migration) — "
                "retrying without goal_id for story=%s: %s",
                story_id, exc,
            )
            try:
                await pool.execute(
                    """INSERT INTO token_calls
                           (provider, model, agent_id, run_id, session_key,
                            input_tokens, output_tokens, thinking_tokens,
                            cache_read_tokens, cache_write_tokens, record_type, story_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'call',$11)""",
                    provider, model, None, task_id or None, session_key or None,
                    inp, out, thinking, cr, cw,
                    story_id,
                )
            except Exception as exc2:
                _log.warning("phase token_calls retry also failed story=%s: %s", story_id, exc2)
                return JSONResponse({"ok": False, "error": str(exc2)}, status_code=500)
        else:
            _log.warning("phase token_calls insert failed story=%s: %s", story_id, exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True})


@router.post("/stories/{story_id}/tool-progress")
async def tool_progress(story_id: str, request: Request) -> JSONResponse:
    """Live tool-call progress from Claude's PostToolUse hook.

    Broadcasts a JSON frame to every WebSocket subscriber of the active
    chat thread; does NOT persist to the messages table. Frames are
    live-only (visible while user is watching, gone on refresh).
    """
    _verify_auth(request)
    body = await _read_json(request)
    tool = (body.get("tool") or "").strip() or "unknown"
    task_id = (body.get("task_id") or "").strip()
    tool_input = body.get("input") or {}
    if tool == "unknown":
        # Either the hook's jq extraction failed (missing/empty tool_name in
        # Claude's PostToolUse stdin) or claude-tool-progress.sh fell back to
        # its no-jq literal. Logging the raw body is the only way to tell
        # which without reproducing live — the uvicorn access log only shows
        # method/status, never the payload.
        _log.warning(
            "tool_progress: got tool='unknown' for story=%s — raw body=%r",
            story_id, body,
        )

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT active_thread_id, goal_id FROM stories WHERE id = $1",
        story_id,
    )
    if not story or not story["active_thread_id"]:
        return JSONResponse({"ok": True, "skipped": "no active thread"})
    thread_id = story["active_thread_id"]

    icon, summary = _summarize_tool_call(tool, tool_input)

    # Broadcast to chat WebSocket subscribers (no DB write). The agent
    # for this thread is whoever owns the active_thread_id — for SHCA
    # threads that's the user's SHCA agent.
    thread_row = await pool.fetchrow(
        "SELECT agent_id FROM threads WHERE id = $1", thread_id,
    )
    if not thread_row:
        return JSONResponse({"ok": True, "skipped": "thread not found"})

    from app.openclaw import session_manager
    frame = {
        "type": "tool_progress",
        "kind": "claude_tool_progress",
        "story_id": story_id,
        "thread_id": thread_id,
        "task_id": task_id,
        "tool": tool,
        "icon": icon,
        "summary": summary,
        "ts": __import__("time").time(),
    }
    delivered = await session_manager.broadcast_to_thread(
        story["goal_id"], thread_row["agent_id"], thread_id, frame,
    )
    return JSONResponse({"ok": True, "subscribers": delivered})


# ── Plan-file validation (PreToolUse hook gate) ─────────────────────────────
#
# Called by the validate-plan-dag.sh hook on every Write/Edit Claude does in
# an operator story workspace. Three checks:
#   1. Planning-phase write gate — if the story is in planning (or pre-
#      planning), reject any write outside the story's own
#      repo/stories/<story_id>/ subtree. Stops Claude from implementing
#      source code while it's supposed to be planning.
#   2. Path discipline — if the content looks like a plan (contains the
#      "## Task DAG" marker) but the path isn't under
#      repo/stories/<story_id>/plans/plan_*.md, reject with the correct path.
#   3. Schema validation — if the path IS a plan file, run dag_parser and
#      reject with the parser error + canonical schema example.
#
# Anything else (source files during execution phase, etc.) passes through.
# The hook is scoped to Claude Code CLI sessions only — other openclaw
# agents don't touch this code path.

# Plan path convention (CANONICAL, one everywhere):
#   repo/stories/<story_id>/plans/cycle-NN-<slug>.md   (cycle-based, in the story workspace)
#   repo/stories/<story_id>/scope.md                   (per-story scope, not a plan file)
#
# The matcher below ALSO accepts, for backward/transition safety only (older
# workspaces / interim `plan_` writes), the legacy `plan_<slug>` filename in the
# same stories/<id>/plans/ dir and the pre-059 `docs/plans/<id>/cycle-NN` path.
# All INSTRUCTIONS (skills, CLAUDE.md) now direct the canonical cycle-NN form.
_PLAN_PATH_RE = __import__("re").compile(
    r"(?:^|/)(?:"
    r"stories/(?P<story_id>[^/]+)/plans/(?:cycle-\d+-[a-zA-Z0-9_-]+|plan_[a-zA-Z0-9_-]+)\.md"  # canonical cycle-NN (+ legacy plan_)
    r"|docs/plans/(?P<story_id_old>[^/]+)/cycle-\d+-[a-zA-Z0-9_-]+\.md"     # pre-059 legacy
    r")$"
)
# NOTE: the old convention deliberately matches ONLY cycle-NN-slug.md filenames,
# not SCOPE.md or any other file at docs/plans/<story_id>/. SCOPE.md at the old
# path passes CHECK 1 (allowed location) but has plan_match=False so it skips
# DAG parsing (CHECK 3) and is allowed through as a non-plan write.

# Story-subdir gate: anything under the story's own subtree (either
# convention) is allowed during planning; everything else is rejected.
def _is_within_story_subtree(norm_path: str, story_id: str) -> bool:
    if not story_id:
        return False
    # New convention: repo/stories/<story_id>/
    new_needle = f"stories/{story_id}/"
    if f"/{new_needle}" in f"/{norm_path}" or norm_path.startswith(new_needle):
        return True
    # Old convention: repo/docs/plans/<story_id>/
    old_needle = f"docs/plans/{story_id}/"
    return f"/{old_needle}" in f"/{norm_path}" or norm_path.startswith(old_needle)


# Phases where Claude is meant to plan, not implement. Source-code writes
# in these phases are rejected. `executing` is the only phase where
# Claude is authorized to modify source.
_PLANNING_PHASES = frozenset({
    "discovery",
    "planning",
    "awaiting_plan_approval",
    "awaiting_next",  # interim between push and next planning turn
    "awaiting_review",  # diff is being reviewed; no further writes
    # THE WORK IS SUBMITTED. `submitting` means the claim is in and the
    # extension is pushing; `awaiting_merge` means it is on a branch with
    # a pull request open. A source edit in either would be a change the
    # person merging never saw, so the boundary refuses it -- the way
    # back is "ask for more work", which starts a new plan.
    "submitting",
    "awaiting_merge",
    # AND AFTER THE MERGE. `awaiting_closeout` means every lane is in and
    # a person is deciding whether the story is finished. An edit there
    # would be a change to code already on trunk, outside any plan and
    # outside the pull request that carried it.
    "awaiting_closeout",
    # `integrating` is the platform taking the trunk into the branch. A
    # source edit mid-merge is the one way to lose work that was already
    # agreed to.
    "integrating",
})


@router.post("/validate-plan")
async def validate_plan(request: Request) -> JSONResponse:
    """Validate a Claude-proposed file write before it hits disk.

    Body:
      {
        "tool":       "Write" | "Edit",
        "file_path":  string (relative or absolute),
        "content":    string (the file body Claude is about to write),
        "cwd":        string (Claude's working directory — used to derive
                              the expected story_id)
      }

    Responses:
      200 OK  → write is allowed
      400     → write is rejected; body has {error, schema_example?}
      401     → bad auth token

    Every call emits a single structured log line tagged `validate_plan:`
    with the inputs, derived paths, decision, and which check fired (if any).
    Grep `validate_plan:` in the control-panel logs to trace what the hook
    did for any given Claude write.
    """
    _verify_auth(request)
    body = await _read_json(request)

    tool = (body.get("tool") or "").strip()
    file_path = (body.get("file_path") or "").strip()
    content = body.get("content") or ""
    cwd = (body.get("cwd") or "").strip()

    trace: list[str] = [
        f"tool={tool}",
        f"file_path={file_path!r}",
        f"cwd={cwd!r}",
        f"content_len={len(content)}",
    ]

    def _log_and_return(response: JSONResponse, decision: str, reason: str) -> JSONResponse:
        trace.append(f"decision={decision}")
        trace.append(f"reason={reason}")
        _log.info("validate_plan: %s", " | ".join(trace))
        return response

    if tool not in ("Write", "Edit"):
        return _log_and_return(
            JSONResponse({"ok": True, "skipped": "non-write tool"}),
            "allow", "non-write tool",
        )

    # Normalize file_path to a workspace-relative POSIX form for the
    # validation checks. Used by regex-based gates; tolerant of partial
    # paths (e.g. `stories/X/plans/Y.md` vs `repo/stories/X/plans/Y.md`).
    norm_path = file_path.replace("\\", "/")
    if cwd:
        cwd_norm = cwd.replace("\\", "/").rstrip("/")
        if norm_path.startswith(cwd_norm + "/"):
            norm_path = norm_path[len(cwd_norm) + 1:]
    trace.append(f"norm_path={norm_path!r}")

    # CLAUDE.md at the workspace root is a managed operator file —
    # control panel writes it at provisioning, it's never updated by
    # Claude. Reject any attempt to write/edit it.
    norm_lower = norm_path.lower()
    if norm_lower == "claude.md" or norm_lower.endswith("/claude.md"):
        # Allow writes to CLAUDE.md inside repo/ (the user's project
        # may have its own CLAUDE.md; that's not ours).
        if "/repo/" in f"/{norm_path}/" and not norm_lower.startswith(
            ("claude.md",)
        ):
            pass  # writes under repo/ are project's own; allowed
        elif norm_lower == "claude.md":
            return _log_and_return(
                JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": (
                            "CLAUDE.md at the workspace root is a managed "
                            "operator file. It's written once at story "
                            "provisioning and must not be edited by Claude. "
                            "If the operator rules need to change, that's a "
                            "control-panel-side update — not a Claude write."
                        ),
                    },
                ),
                "reject", "claude_md_workspace_root",
            )

    # Derive expected story_id from cwd. Workspace layout is
    # <agent-workspace>/stories/<story_id>/<...>, so the segment right
    # after "stories" is the story id.
    expected_story_id: Optional[str] = None
    if cwd:
        parts = [p for p in cwd.replace("\\", "/").split("/") if p]
        for i, seg in enumerate(parts):
            if seg == "stories" and i + 1 < len(parts):
                expected_story_id = parts[i + 1]
                break
    trace.append(f"story_id={expected_story_id}")

    # Compute the canonical story-root-relative path. This is what gets
    # stored in stories.current_plan_path and what plan_approval will pass
    # to the gateway's shca.workspace.readFile RPC (which resolves paths
    # relative to <agent-workspace>/stories/<story_id>/).
    #
    # Cases this handles correctly:
    #   1. cwd=<ws>/stories/X/, file_path=repo/stories/X/plans/Y.md
    #      → abs=<ws>/stories/X/repo/stories/X/plans/Y.md
    #      → canonical=repo/stories/X/plans/Y.md ✓
    #   2. cwd=<ws>/stories/X/repo/, file_path=stories/X/plans/Y.md
    #      → abs=<ws>/stories/X/repo/stories/X/plans/Y.md (same file)
    #      → canonical=repo/stories/X/plans/Y.md ✓
    #   3. file_path absolute → abs=file_path, then strip story root ✓
    #
    # The bug before this fix: in case 2, the validator stored the cwd-
    # relative path "stories/X/plans/Y.md", which gateway readFile then
    # resolved to <ws>/stories/X/stories/X/plans/Y.md — not found.
    import os.path as _osp
    import re as _re_abscheck
    abs_path = file_path.replace("\\", "/")
    # An absolute path needs no cwd-prepending -- but "absolute" isn't just
    # POSIX's leading "/". Claude Code running natively on Windows (our local
    # dev setup; the deployed gateway VM is always Ubuntu, so this never
    # matters in prod) sends drive-letter paths like "D:/Projects/...".
    # Missing that case here made this treat an already-absolute Windows path
    # as relative and prepend cwd anyway, producing a doubled, garbage path
    # (".../stories/X/D:/Projects/.../stories/X/...") that could never match
    # the "repo/stories/<id>/plans/" check below -- silently rejecting every
    # real plan write validated from a Windows Claude Code session.
    _is_absolute = abs_path.startswith("/") or bool(_re_abscheck.match(r"^[A-Za-z]:/", abs_path))
    if not _is_absolute and cwd:
        abs_path = _osp.normpath(f"{cwd.rstrip('/')}/{abs_path}").replace("\\", "/")
    canonical_plan_path: Optional[str] = None
    if expected_story_id:
        needle = f"/stories/{expected_story_id}/"
        idx = abs_path.find(needle)
        if idx >= 0:
            canonical_plan_path = abs_path[idx + len(needle):].lstrip("/")
    trace.append(f"abs_path={abs_path!r}")
    trace.append(f"canonical={canonical_plan_path!r}")

    # Look up the story's current phase. If we can't determine it
    # (no story_id, story missing, DB error), fail open — phase
    # gating only applies when we're confident about state.
    story_phase: Optional[str] = None
    if expected_story_id:
        try:
            pool = get_pool()
            story_phase = await pool.fetchval(
                "SELECT current_phase FROM stories WHERE id = $1",
                expected_story_id,
            )
        except Exception:
            story_phase = None  # fail open on DB issues
    trace.append(f"phase={story_phase}")

    plan_match = _PLAN_PATH_RE.search(norm_path)
    # story_id group captures new convention; story_id_old captures old convention.
    path_story_id = (
        (plan_match.group("story_id") or plan_match.group("story_id_old"))
        if plan_match else None
    )

    has_dag_marker = bool(
        __import__("re").search(r"^##\s+Task\s+DAG\s*$", content, __import__("re").MULTILINE | __import__("re").IGNORECASE)
    )
    trace.append(f"plan_match={bool(plan_match)}")
    trace.append(f"has_dag_marker={has_dag_marker}")

    from app.services.dag_parser import (
        CANONICAL_DAG_EXAMPLE,
        DagParseError,
        parse_dag_from_markdown,
    )
    from app.services import claude_phases as cp_svc

    # CHECK 1: Planning-phase write gate.
    # If the story is in a planning-adjacent phase, Claude can ONLY write
    # into its own story subtree. Source-code writes during planning are
    # the most common regression — this is the structural enforcement.
    if (
        story_phase in _PLANNING_PHASES
        and expected_story_id
        and not _is_within_story_subtree(norm_path, expected_story_id)
    ):
        # Give the ABSOLUTE, cwd-anchored path ($CLAUDE_PROJECT_DIR is set by the
        # gateway to the story-workspace root) so a drifted shell cwd can't misplace
        # the write — matches the absolute-path rule in the planning skill.
        target = f"$CLAUDE_PROJECT_DIR/repo/stories/{expected_story_id}/plans/cycle-NN-<slug>.md"
        return _log_and_return(
            JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": (
                        f"this story is in phase '{story_phase}'. During planning, "
                        f"you can only write files under the story's own subtree. "
                        f"You tried to write '{file_path}', which is outside it. "
                        f"Write the plan to this exact absolute path:\n"
                        f"  {target}\n"
                        f"Implementation happens AFTER plan approval via [DISPATCH]."
                    ),
                },
            ),
            "reject", "check1_planning_phase_outside_subtree",
        )

    # CHECK 2: Path discipline — content looks like a plan but path is wrong.
    if has_dag_marker and not plan_match:
        # Absolute, cwd-anchored path (see CHECK 1) so the write can't drift.
        target = (
            f"$CLAUDE_PROJECT_DIR/repo/stories/{expected_story_id}/plans/cycle-NN-<slug>.md"
            if expected_story_id
            else "$CLAUDE_PROJECT_DIR/repo/stories/<story_id>/plans/cycle-NN-<slug>.md"
        )
        return _log_and_return(
            JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": (
                        f"plan written to wrong location: '{file_path}'. "
                        f"Write the plan to this exact absolute path: {target}"
                    ),
                    "schema_example": CANONICAL_DAG_EXAMPLE,
                },
            ),
            "reject", "check2_dag_marker_wrong_path",
        )

    # Path discipline: nested-repo guard.
    if plan_match:
        if "/repo/repo/" in abs_path:
            target = (
                f"stories/{expected_story_id}/plans/cycle-NN-<slug>.md"
                if expected_story_id
                else "stories/<story_id>/plans/cycle-NN-<slug>.md"
            )
            return _log_and_return(
                JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": (
                            f"path '{file_path}' would create a nested 'repo/repo/' "
                            f"directory. Your shell cwd has drifted into the repo "
                            f"directory but the file_path still starts with 'repo/'. "
                            f"Use an absolute path "
                            f"($CLAUDE_PROJECT_DIR/repo/{target}) or drop the "
                            f"'repo/' prefix."
                        ),
                        "schema_example": CANONICAL_DAG_EXAMPLE,
                    },
                ),
                "reject", "nested_repo_guard",
            )

    # Path discipline: plan path but wrong story id.
    if plan_match and expected_story_id and path_story_id != expected_story_id:
        return _log_and_return(
            JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": (
                        f"plan path uses story_id '{path_story_id}' but this "
                        f"workspace is for story '{expected_story_id}'. Write to this "
                        f"exact absolute path: "
                        f"$CLAUDE_PROJECT_DIR/repo/stories/{expected_story_id}/plans/cycle-NN-<slug>.md."
                    ),
                    "schema_example": CANONICAL_DAG_EXAMPLE,
                },
            ),
            "reject", "wrong_story_id_in_path",
        )

    # Path discipline: the plan must land in THIS story's own repo checkout —
    # i.e. its canonical (story-root-relative) path must be
    # repo/stories/<id>/plans/... . The failure this catches: an absolute path
    # anchored at the AGENT-workspace root (<ws>/repo/stories/<id>/plans/...)
    # instead of the story workspace ($CLAUDE_PROJECT_DIR = <ws>/stories/<id>).
    # That "phantom" repo/ is outside the story's git checkout, so the diff/
    # commit-review gate and custom_workspace_file (which read the story repo)
    # never see it — and canonical comes out as 'plans/...' (missing the
    # repo/stories/<id>/ prefix). Reject with the exact absolute path so Claude
    # corrects on the FIRST write instead of a silent phantom + later retry.
    # The clone dir is NOT always literally `repo/` — a multi-repo workspace has
    # `repos/<owner>__<name>/` instead, and hardcoding `repo/` here rejected the exact
    # path provisioning's own CLAUDE.md tells the agent to write to. Resolve it.
    # Resolved from the RECORDED plan path where the story already has one, so the
    # directory the agent is told to use cannot move under it. Deriving this from the
    # repository grants meant granting a repo that sorts earlier alphabetically changed
    # the expected location mid-story, and this gate would then reject the exact path
    # provisioning's own CLAUDE.md had told the agent to write to. `plan_approval` reads
    # it the same way, which is what keeps this gate and the commit in agreement.
    _plan_root = "repo"
    # 2026-08-20: every WRITE repo now gets its own copy of the plan (not just
    # the canonical root above), so a sibling write repo's own
    # stories/<id>/plans/ must also pass this gate — otherwise the copy this
    # feature asks the agent to make gets rejected by the very validator that's
    # supposed to enforce where plans belong. `_plan_root` stays the single
    # frozen root `plan_approval`/`resolve_plan_root` agree on for the
    # CANONICAL submitted plan; `_other_write_roots` only widens WHERE a write
    # is allowed to land, never which copy is authoritative.
    _other_write_roots: list[str] = []
    if expected_story_id:
        try:
            from app.services.story_plan_root import resolve_plan_root
            _plan_root = await resolve_plan_root(get_pool(), expected_story_id)
        except Exception as _exc:
            _log.debug("validate_plan: plan-root lookup failed (%s)", _exc)
        try:
            from app.services.story_workspace_layout import resolve_workspace_clones, write_clones
            _clones = await resolve_workspace_clones(get_pool(), expected_story_id)
            _other_write_roots = [
                c["subdir"] for c in write_clones(_clones) if c["subdir"] != _plan_root
            ]
        except Exception as _exc:
            _log.debug("validate_plan: write-repo lookup failed (%s)", _exc)
    _allowed_plan_roots = [_plan_root, *_other_write_roots]
    if plan_match and expected_story_id and (
        not canonical_plan_path
        or not any(
            canonical_plan_path.startswith(f"{root}/stories/{expected_story_id}/plans/")
            for root in _allowed_plan_roots
        )
    ):
        target = (
            f"$CLAUDE_PROJECT_DIR/{_plan_root}/stories/{expected_story_id}"
            f"/plans/cycle-NN-<slug>.md"
        )
        return _log_and_return(
            JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": (
                        f"plan written outside this story's repo: '{file_path}' resolved "
                        f"to '{canonical_plan_path or norm_path}', not under "
                        f"{_plan_root}/stories/{expected_story_id}/plans/. This usually means the "
                        f"path was anchored at the agent-workspace root instead of the "
                        f"story workspace. Write to this EXACT absolute path:\n  {target}"
                    ),
                    "schema_example": CANONICAL_DAG_EXAMPLE,
                },
            ),
            "reject", "canonical_outside_story_repo",
        )

    # Not a plan write → allow without parsing (already passed the planning
    # gate above, so this is a non-plan write in execution phase).
    if not plan_match:
        return _log_and_return(
            JSONResponse({"ok": True, "skipped": "not a plan file"}),
            "allow", "not_a_plan_file",
        )

    # CHECK 3: Schema validation on plan content.
    phases = await cp_svc.list_phases(only_active=True)
    allowed = {p["name"] for p in phases}

    # Multi-repo (plan M4.3): validate `repo:` against the story's actual write repos,
    # so the planner is told at WRITE time that a task is missing or misnames its repo
    # — rather than the plan passing the hook and failing later at DAG ingest, after
    # the human has already approved it.
    repo_aliases: dict = {}
    if expected_story_id:
        try:
            from app.services.dag_ingest import _resolve_story_repo_aliases
            _, repo_aliases = await _resolve_story_repo_aliases(
                get_pool(), expected_story_id,
            )
        except Exception as exc:
            # Fail OPEN, consistent with the phase-gate above: a lookup problem must
            # not block a legitimate plan write.
            _log.debug(
                "validate_plan: repo alias lookup failed for story=%s (%s) — "
                "skipping repo validation", expected_story_id, exc,
            )

    try:
        parse_dag_from_markdown(
            content, allowed_phases=allowed, repo_aliases=repo_aliases,
        )
    except DagParseError as exc:
        return _log_and_return(
            JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": str(exc),
                    "allowed_phases": sorted(allowed),
                    "schema_example": CANONICAL_DAG_EXAMPLE,
                },
            ),
            "reject", f"check3_dag_schema:{type(exc).__name__}",
        )

    # Plan validated successfully — record the canonical (story-root-relative)
    # path as the story's current plan. plan_approval reads
    # stories.current_plan_path and passes it to the gateway's
    # shca.workspace.readFile RPC, which resolves it relative to the story
    # workspace root — so the stored path MUST be relative to that root,
    # including any `repo/` prefix.
    #
    # If we couldn't compute a canonical path (cwd didn't contain the
    # story segment), fall back to norm_path so behavior degrades but
    # doesn't crash; plan_approval's workspace-scan fallback can recover.
    stored_path = canonical_plan_path or norm_path
    db_updated = False
    if expected_story_id:
        try:
            pool = get_pool()
            await pool.execute(
                "UPDATE stories SET current_plan_path = $1, updated_at = NOW() "
                "WHERE id = $2",
                stored_path, expected_story_id,
            )
            db_updated = True
        except Exception:
            _log.exception(
                "validate-plan: failed to update stories.current_plan_path "
                "(story=%s path=%s) — validation still succeeded",
                expected_story_id, stored_path,
            )

        # Push plan_ready to the browser WS only when current_plan_path was
        # actually persisted — so the Approve button appears without polling.
        # Non-fatal if the push fails; checkPlanBannerAfterTurn fallback covers it.
        if db_updated:
            try:
                from app.openclaw import session_manager
                pool = get_pool()
                story_ws = await pool.fetchrow(
                    "SELECT goal_id, active_thread_id FROM stories WHERE id = $1",
                    expected_story_id,
                )
                if story_ws and story_ws["active_thread_id"]:
                    thread_row = await pool.fetchrow(
                        "SELECT agent_id FROM threads WHERE id = $1",
                        story_ws["active_thread_id"],
                    )
                    if thread_row:
                        await session_manager.broadcast_to_thread(
                            story_ws["goal_id"],
                            thread_row["agent_id"],
                            story_ws["active_thread_id"],
                            {"type": "plan_ready", "story_id": expected_story_id},
                        )
                        _log.info(
                            "validate-plan: plan_ready pushed to WS story=%s",
                            expected_story_id,
                        )
            except Exception:
                _log.debug(
                    "validate-plan: plan_ready WS push failed (non-fatal) story=%s",
                    expected_story_id,
                    exc_info=True,
                )

        # Post an "Approve Plan / Request Changes" card to the story's Slack
        # channel so the human can approve without switching to the web UI.
        # Non-fatal — WS push above already unblocks the UI path.
        try:
            pool = get_pool()
            story_row = await pool.fetchrow(
                "SELECT goal_id, active_thread_id, title FROM stories WHERE id = $1",
                expected_story_id,
            )
            if story_row and story_row["active_thread_id"]:
                from app.channels.slack.outbound import relay_plan_ready
                await relay_plan_ready(
                    goal_id=str(story_row["goal_id"]),
                    thread_id=str(story_row["active_thread_id"]),
                    story_id=expected_story_id,
                    story_title=story_row["title"] or expected_story_id[:8],
                    plan_path=stored_path,
                )
        except Exception:
            _log.debug(
                "validate-plan: Slack plan_ready card failed (non-fatal) story=%s",
                expected_story_id,
                exc_info=True,
            )

    return _log_and_return(
        JSONResponse({"ok": True}),
        "allow", f"plan_stored:{stored_path}",
    )


import re as _re

# Matches the full OpenClaw workspace prefix up to and including /repo/.
# Pattern: /home/<user>/.openclaw/workspace-<name>/stories/<uuid>/repo/
_WS_PATH_RE = _re.compile(
    r'/home/[^/\s]+/\.openclaw/workspace-[^/\s]+/stories/[a-f0-9-]+/repo/'
)


def _strip_ws_path(s: str) -> str:
    """Strip the long workspace prefix, leaving only the repo-relative path."""
    return _WS_PATH_RE.sub('', str(s or ''))


def _summarize_tool_call(tool: str, tool_input: dict) -> tuple[str, str]:
    """Return (emoji_icon, one-line summary) for a tool call."""
    def truncate(s: str, n: int = 120) -> str:
        s = str(s or "")
        return s if len(s) <= n else s[: n - 1] + "…"

    def short_path(key: str) -> str:
        """Get a file path from tool_input, strip workspace prefix, truncate."""
        return truncate(_strip_ws_path(tool_input.get(key) or ""))

    if tool == "Read":
        return ("🔍", f"Reading `{short_path('file_path')}`")
    if tool == "Write":
        return ("📝", f"Writing `{short_path('file_path')}`")
    if tool in ("Edit", "NotebookEdit"):
        return ("✏️", f"Editing `{short_path('file_path')}`")
    if tool == "Glob":
        return ("🔎", f"Glob `{truncate(tool_input.get('pattern'))}`")
    if tool == "Grep":
        return ("🔎", f"Grep `{truncate(tool_input.get('pattern'))}`")
    if tool == "Bash":
        raw = str(tool_input.get("command") or "")
        # Strip workspace paths embedded in the command so it stays readable.
        cmd = truncate(_strip_ws_path(raw), 200)
        return ("🐚", f"`{cmd}`")
    if tool == "Task":
        return ("🤖", f"Subagent: {truncate(tool_input.get('subagent_type') or tool_input.get('description'))}")
    if tool == "WebFetch":
        return ("🌐", f"Fetching `{truncate(tool_input.get('url'))}`")
    if tool == "WebSearch":
        return ("🌐", f"Web search: `{truncate(tool_input.get('query'))}`")
    if tool == "TodoWrite":
        return ("📋", "Updating todo list")
    return ("⚙️", f"{tool}")


# ── Backward-compat aliases: /internal/shca/* → same handlers as /internal/assistant/* ──
#
# WHY THIS EXISTS
# Agents provisioned before the SHCA → Assistant rename (shca-tools.ts, cli-spawn.ts,
# shell hooks) have workspace files and session state that call /internal/shca/* paths.
# OpenClaw and the control panel deploy independently, so there is a window — potentially
# hours, while existing goals are still running — where old agents call /internal/shca/*
# and new agents call /internal/assistant/*. Both must work simultaneously.
#
# HOW IT WORKS
# Every route is re-registered on legacy_router with the /internal/shca prefix pointing
# at the exact same handler function. No logic is duplicated — a change to any handler
# above is automatically reflected here.
#
# WHEN TO REMOVE
# Safe to remove once ALL of the following are true:
#   1. The DB migration `rename_pwt_message_shca_to_assistant` has been applied to prod.
#   2. Cloud Run access logs show zero requests to /internal/shca/* for at least 24 h.
#      Query: filter httpRequest.requestUrl=~"/internal/shca/" in Cloud Logging.
#   3. No active goals have agent_instances with workspace files that reference
#      /internal/shca/ (check GCS workspace files if unsure).
#
# HOW TO REMOVE
#   1. Delete this entire comment block + legacy_router + _LEGACY_ROUTES + the for-loop.
#   2. In app/main.py: remove the `from app.routers.internal_assistant import legacy_router
#      as _internal_shca_legacy_router` import and the `app.include_router(...)` call.
#   3. Deploy control panel.
#   See docs/SHCA_TO_ASSISTANT_RENAME.md § "Step 4 — Remove the backward-compat aliases".
legacy_router = APIRouter(prefix="/internal/shca", dependencies=[_sec_Depends(_ownership_guard)])

_LEGACY_ROUTES = [
    ("POST", "/stories/{story_id}/phase",             story_phase),
    ("POST", "/stories/{story_id}/state",             story_state),
    ("POST", "/stories/{story_id}/branches",          story_branch_create),
    ("POST", "/stories/{story_id}/checkpoints",       story_checkpoint),
    ("POST", "/stories/{story_id}/plans",             plan_version_create),
    ("POST", "/stories/{story_id}/approvals",         approval_request),
    ("GET",  "/approvals/{approval_id}/poll",         approval_poll),
    ("POST", "/approvals/{approval_id}/decide",       approval_decide),
    ("POST", "/ledger",                               ledger_write),
    ("GET",  "/instances/{instance_id}/active-story", get_active_story_for_instance),
    ("GET",  "/stories/{story_id}/execution-status",  get_execution_status),
    ("POST", "/stories/{story_id}/claude-session",    record_claude_session),
    ("POST", "/stories/{story_id}/token-usage",       record_phase_token_usage),
    ("POST", "/stories/{story_id}/tool-progress",     tool_progress),
    ("POST", "/validate-plan",                        validate_plan),
]

for _method, _path, _handler in _LEGACY_ROUTES:
    legacy_router.add_api_route(_path, _handler, methods=[_method])
