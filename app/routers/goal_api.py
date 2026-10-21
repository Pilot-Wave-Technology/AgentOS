"""External Goal Orchestration API for MARL and External Systems.

Provides:
- POST /api/v1/goals: Atomic platform goal provisioning with Merkle provenance
- GET /api/v1/goals/{id}/questions: Bi-directional active question card querying
- POST /api/v1/goals/{id}/questions/{qid}/answer: Question resolution and pod unparking
- GET /api/v1/goals/{id}/actions: Query discovered action templates produced in-goal
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import string
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.database import get_pool
from app.ledger.journal import append_event, generate_event_id
from agentos_ledger_client.schema import (
    ACTION_TEMPLATE_DISCOVERED,
    DECISION_RECORDED,
    GOAL_CREATED,
    GOAL_MEMBER_ADDED,
)
from app.roles import GoalRole
from app.services.api_keys import ApiPrincipal, require_api_key

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/goals", tags=["api_v1_goals"])

_GOAL_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{2,4}$")


async def _require_goal_access(pool, principal, pid: str) -> None:
    """Refuse a goal this principal is not a member of.

    SCOPES ARE VERBS, NOT GOALS. `goals:read` says what a key may do,
    never which goal it may do it to, so without this any valid key
    reached every goal on the platform -- listing and ANSWERING the
    human-approval questions of goals it has nothing to do with. That
    is the cross-tenant shape `chat.py` carries a dedicated guard
    against, and it was already fixed there once as a P0.

    MEMBERSHIP, BECAUSE THAT IS WHERE THE ANSWER ALREADY LIVES. The
    creator is inserted as `admin` on the way out of `create_goal_api`,
    so a principal has exactly the goals it created plus any it has
    since been added to -- which is also how a MARL principal is meant
    to be given one.

    404 AND NOT 403, matching `_guard_thread_in_goal`: a 403 confirms
    the goal exists, and which goal ids are real is not something an
    unauthorised caller should be able to enumerate.
    """
    ok = await pool.fetchval(
        "SELECT 1 FROM goal_members WHERE goal_id = $1 AND uid = $2",
        pid, principal.owner_uid)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such goal")


def _generate_goal_id() -> str:
    """Generate a random 4-character uppercase alphanumeric ID starting with a letter."""
    first = secrets.choice(string.ascii_uppercase)
    rest = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(3))
    return f"{first}{rest}"


class ProvenanceInput(BaseModel):
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None


class ResourceInput(BaseModel):
    kind: str = "repo"
    name: str
    uri: Optional[str] = ""
    repo_owner: Optional[str] = None
    repo_name: Optional[str] = None
    default_branch: Optional[str] = "main"
    clone_url: Optional[str] = None
    branch_name: Optional[str] = None
    role: Optional[str] = "feature"
    metadata: Optional[dict[str, Any]] = None


class CreateGoalRequest(BaseModel):
    name: str
    objective: str = ""
    description: str = ""
    priority: str = "medium"
    provenance: Optional[ProvenanceInput] = None
    resources: Optional[list[dict[str, Any]]] = None
    id: Optional[str] = None
    color_tag: str = "#6366f1"
    go_model: Optional[str] = None


class AnswerQuestionRequest(BaseModel):
    verdict: str = "approve"
    text: str = ""
    checks: Optional[list[dict[str, Any]]] = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_goal_api(
    data: CreateGoalRequest,
    principal: ApiPrincipal = Depends(require_api_key(["goals:create"])),
):
    """Atomic platform provisioning for a new goal."""
    pool = get_pool()

    # Validate or generate Goal ID
    if data.id:
        pid = data.id.strip().upper()
        if not _GOAL_ID_RE.match(pid):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Goal ID must be 3-5 uppercase alphanumeric chars starting with a letter",
            )
        existing = await pool.fetchval("SELECT id FROM goals WHERE id = $1", pid)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Goal ID `{pid}` is already taken",
            )
    else:
        for _ in range(10):
            pid = _generate_goal_id()
            if not await pool.fetchval("SELECT id FROM goals WHERE id = $1", pid):
                break
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate unique goal ID",
            )

    go_thread_id = str(uuid.uuid4())
    orch_thread_id = str(uuid.uuid4())
    council_thread_id = str(uuid.uuid4())
    security_thread_id = str(uuid.uuid4())

    root_event_id = generate_event_id()
    correlation_id = (
        data.provenance.correlation_id
        if data.provenance and data.provenance.correlation_id
        else str(root_event_id)
    )
    causation_ids = (
        [data.provenance.causation_id]
        if data.provenance and data.provenance.causation_id
        else []
    )

    actor_uuid = uuid.UUID(principal.actor_id)

    # Resolved here; provisioning itself happens after the write, for the
    # reasons given at step 6.
    from app.llm_models import default_for
    from app.services.go_agent import create_go_for_goal

    model = data.go_model or default_for("go")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Event first in state ledger
            await append_event(
                conn,
                event_id=root_event_id,
                event_type=GOAL_CREATED,
                payload={
                    "goal_id": pid,
                    "name": data.name,
                    "description": data.description,
                    "objective": data.objective,
                    "priority": data.priority,
                    "color_tag": data.color_tag,
                    "created_by": principal.owner_uid,
                },
                actor_id=actor_uuid,
                correlation_id=correlation_id,
                causation_ids=causation_ids,
                context_goal_id=pid,
            )

            # 2. Insert into goals
            await conn.execute(
                """
                INSERT INTO goals (
                    id, name, description, objective, priority, state,
                    color_tag, created_by, go_thread_id,
                    orchestrator_thread_id, council_thread_id,
                    security_thread_id
                ) VALUES ($1, $2, $3, $4, $5, 'planning', $6, $7, $8, $9,
                          $10, $11)
                """,
                pid,
                data.name,
                data.description,
                data.objective,
                data.priority,
                data.color_tag,
                principal.owner_uid,
                go_thread_id,
                orch_thread_id,
                council_thread_id,
                security_thread_id,
            )

            # 3. Creator becomes admin in goal_members
            member_event_id = generate_event_id()
            await append_event(
                conn,
                event_id=member_event_id,
                event_type=GOAL_MEMBER_ADDED,
                payload={
                    "goal_id": pid,
                    "uid": principal.owner_uid,
                    "role": GoalRole.GOAL_ADMIN,
                    "added_by": principal.owner_uid,
                },
                actor_id=actor_uuid,
                correlation_id=correlation_id,
                causation_ids=[str(root_event_id)],
                context_goal_id=pid,
            )
            await conn.execute(
                """
                INSERT INTO goal_members (goal_id, uid, role, added_by)
                VALUES ($1, $2, 'admin', $3)
                ON CONFLICT (goal_id, uid) DO NOTHING
                """,
                pid,
                principal.owner_uid,
                principal.owner_uid,
            )

            # 4. Create 4 threads
            await conn.execute(
                """
                INSERT INTO threads (id, goal_id, agent_id, created_by, title, status)
                VALUES ($1, $2, $3, $4, $5, 'active')
                """,
                go_thread_id,
                pid,
                f"go-{pid.lower()}",
                principal.owner_uid,
                f"Goal: {data.name}",
            )
            await conn.execute(
                """
                INSERT INTO threads (id, goal_id, created_by, title, status)
                VALUES ($1, $2, $3, 'Orchestrator Channel', 'active')
                """,
                orch_thread_id,
                pid,
                principal.owner_uid,
            )
            await conn.execute(
                """
                INSERT INTO threads (id, goal_id, created_by, title, status)
                VALUES ($1, $2, $3, 'Goal Review Council', 'active')
                """,
                council_thread_id,
                pid,
                principal.owner_uid,
            )
            await conn.execute(
                """
                INSERT INTO threads (id, goal_id, created_by, title, status)
                VALUES ($1, $2, $3, 'Security Council', 'active')
                """,
                security_thread_id,
                pid,
                principal.owner_uid,
            )

            # 5. Grant resources if provided
            if data.resources:
                for res in data.resources:
                    res_id = str(uuid.uuid4())[:12]
                    res_kind = res.get("kind", "repo")
                    res_name = res.get("name", f"res-{res_id}")
                    res_uri = res.get("uri", "")
                    res_owner = res.get("repo_owner")
                    res_repo = res.get("repo_name")
                    res_branch = res.get("branch_name") or res.get("default_branch", "main")
                    res_clone = res.get("clone_url", "")
                    res_role = res.get("role", "feature")
                    res_meta = res.get("metadata")

                    await conn.execute(
                        """
                        INSERT INTO goal_resources (
                            id, goal_id, kind, name, uri, created_by,
                            repo_owner, repo_name, default_branch, clone_url,
                            role, branch_name, metadata, summary_stale, default_writable
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, true, true)
                        ON CONFLICT (goal_id, kind, name) DO NOTHING
                        """,
                        res_id,
                        pid,
                        res_kind,
                        res_name,
                        res_uri,
                        principal.owner_uid,
                        res_owner,
                        res_repo,
                        res_branch,
                        res_clone,
                        res_role,
                        res_branch,
                        json.dumps(res_meta) if res_meta else None,
                    )

    # ── 6. The GO, and a failure undoes the goal ─────────────────────
    #
    # AFTER THE COMMIT: `agent_contexts` has a foreign key to `goals`,
    # so provisioning cannot precede the row, and putting it inside the
    # transaction would hold the global ledger advisory lock that
    # `append_event` takes across seconds of gateway I/O.
    #
    # A FAILURE UNDOES THE GOAL. It used to return 201 with a
    # `go_agent_id` naming an agent that did not exist -- the worst
    # answer for a caller with nobody watching a log.
    try:
        go_res = await create_go_for_goal(
            goal_id=pid,
            goal_name=data.name,
            objective=data.objective,
            created_by_uid=principal.owner_uid,
            model=model,
        )
        go_agent_id = (go_res or {}).get("agent_id") or ""
        if not go_agent_id:
            raise RuntimeError("provisioning returned no agent id")
        # An agent id is not evidence of an agent: the dead-goal guard
        # returns one even when it skips.
        if (go_res or {}).get("skipped"):
            raise RuntimeError(f"provisioning skipped ({go_res['skipped']})")
        await pool.execute(
            "UPDATE goals SET go_agent_id = $1 WHERE id = $2", go_agent_id, pid)
    except Exception as exc:                                # noqa: BLE001
        log.exception("goal %s: GO provisioning failed — undoing it", pid)
        from app.services.goal_rollback import undo_goal_creation
        undone = await undo_goal_creation(pool, pid)
        if undone:
            detail = (f"Goal not created: provisioning the Goal Orchestrator "
                      f"failed ({type(exc).__name__}: {exc}). Nothing was "
                      f"kept; retry once the cause is cleared.")
        else:
            detail = (f"Provisioning the Goal Orchestrator failed "
                      f"({type(exc).__name__}: {exc}) AND goal {pid} could "
                      f"not be rolled back; it is still present and needs "
                      f"an admin.")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)

    return {
        "goal_id": pid,
        "name": data.name,
        "objective": data.objective,
        "description": data.description,
        "priority": data.priority,
        "state": "planning",
        "go_agent_id": go_agent_id,
        "threads": {
            "go_thread_id": go_thread_id,
            "orchestrator_thread_id": orch_thread_id,
            "council_thread_id": council_thread_id,
            "security_thread_id": security_thread_id,
        },
        "created_by": principal.owner_uid,
        "resources": data.resources or [],
    }


@router.get("/{id}/questions")
async def get_goal_questions(
    id: str,
    principal: ApiPrincipal = Depends(require_api_key(["goals:read"])),
):
    """Retrieve active question cards parked inside the goal (needs_human: true)."""
    pid = id.strip().upper()
    pool = get_pool()
    await _require_goal_access(pool, principal, pid)

    rows = await pool.fetch(
        """
        SELECT q.id, q.kind, q.question_text, q.payload, q.created_at,
               q.execution_id, e.story_id, e.state
          FROM kernel.questions q
          JOIN kernel.executions e ON e.id = q.execution_id
         WHERE e.goal_id = $1 AND q.status = 'OPEN'
         ORDER BY q.created_at ASC
        """,
        pid,
    )

    from kernel.assistant.cards import as_text, plan_of
    from kernel.mailbox.schema import needs_a_person

    active_questions = []
    for r in rows:
        if needs_a_person(r["kind"]):
            plan = plan_of(r["payload"]) if r["kind"] == "authorization" else None
            active_questions.append({
                "question_id": r["id"],
                "kind": r["kind"],
                "question_text": r["question_text"],
                "story_id": r["story_id"],
                "execution_id": r["execution_id"],
                "state": r["state"],
                "needs_human": True,
                "plan": plan,
                "plan_text": as_text(plan),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })

    return {"goal_id": pid, "questions": active_questions}


@router.post("/{id}/questions/{qid}/answer")
async def answer_goal_question(
    id: str,
    qid: int,
    body: AnswerQuestionRequest,
    principal: ApiPrincipal = Depends(require_api_key(["goals:write"])),
):
    """Record human/MARL decision, settle the question card, and unpark the pod."""
    pid = id.strip().upper()
    pool = get_pool()
    await _require_goal_access(pool, principal, pid)

    owner = await pool.fetchval(
        """
        SELECT e.goal_id
          FROM kernel.executions e
          JOIN kernel.questions q ON q.execution_id = e.id
         WHERE q.id = $1
        """,
        qid,
    )
    if owner is None or owner != pid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such question")

    actor_uuid = uuid.UUID(principal.actor_id)

    # ── SETTLE FIRST, RECORD SECOND ──────────────────────────────────
    #
    # `DECISION_RECORDED` used to be appended and COMMITTED before
    # `decide()` ran, in its own connection. A decide that then refused
    # -- a declined plan, a question already answered, an approval the
    # kernel would not take -- left the hash-chained ledger permanently
    # asserting a decision that never took effect, and the ledger is
    # the thing other systems are meant to trust.
    #
    # The two cannot share a transaction: `decide` opens and closes its
    # own connection. So they are ORDERED instead, and the order is
    # chosen by which failure is survivable. Settling without a ledger
    # row is a gap in the audit trail that the question row itself can
    # still evidence. A ledger row without a settlement is a false
    # statement of fact. Take the gap.
    from app.services.kernel_decide import DecideError, decide

    try:
        result = await decide(
            question_id=qid,
            verdict=body.verdict,
            text=body.text,
            human_uid=principal.owner_uid,
            display_name=principal.name or "API Principal",
            checks=body.checks,
        )
    except DecideError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        log.exception("Kernel decide failed (q=%s)", qid)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    # It happened. Now say so -- and never fail the request over the
    # saying, because the decision is already taken and reporting it as
    # an error would invite a retry that double-answers.
    try:
        async with pool.acquire() as conn:
            await append_event(
                conn,
                event_type=DECISION_RECORDED,
                payload={
                    "goal_id": pid,
                    "question_id": qid,
                    "verdict": body.verdict,
                    "text": body.text,
                    "answered_by": principal.owner_uid,
                },
                actor_id=actor_uuid,
                context_goal_id=pid,
            )
    except Exception:                                       # noqa: BLE001
        log.exception("question %s settled but DECISION_RECORDED could not "
                      "be appended; the answer stands and the audit trail "
                      "is short one event", qid)

    return result


@router.get("/{id}/actions")
async def get_registered_actions(
    id: str,
    principal: ApiPrincipal = Depends(require_api_key(["goals:read"])),
):
    """Retrieve action templates discovered and registered in-goal."""
    pid = id.strip().upper()
    pool = get_pool()
    await _require_goal_access(pool, principal, pid)

    rows = await pool.fetch(
        """
        -- `timestamp`, not `created_at`: ledger_events has carried that
        -- name since 001_initial_schema and this endpoint has been
        -- raising UndefinedColumn on every call. Aliased so the caller
        -- keeps reading row["created_at"].
        SELECT event_id, event_type, actor_id, actor_type,
               timestamp AS created_at, payload
          FROM ledger_events
         WHERE (context_goal_id = $1 OR payload->>'goal_id' = $1)
           AND event_type = $2
         ORDER BY timestamp ASC
        """,
        pid,
        ACTION_TEMPLATE_DISCOVERED,
    )

    actions = []
    for r in rows:
        payload = (
            json.loads(r["payload"])
            if isinstance(r["payload"], str)
            else (r["payload"] or {})
        )
        actions.append({
            "event_id": str(r["event_id"]),
            "template": payload.get("template"),
            "params": payload.get("params", {}),
            "cohort_scope": payload.get("cohort_scope"),
            "description": payload.get("description", ""),
            "execution_id": payload.get("execution_id"),
            "story_id": payload.get("story_id"),
            "goal_id": payload.get("goal_id", pid),
            "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {"goal_id": pid, "actions": actions}
