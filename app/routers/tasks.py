"""Task management routes -- Kanban board + CRUD.

Write path: event-first. Every mutation appends to the ledger, then updates
the projection table in the same transaction.
"""
import asyncio
from fastapi import Depends as _sec_Depends
from app.internal_auth import ownership_guard as _ownership_guard
import json
import logging
import uuid

_log = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role, get_agent_actor_id
from app.roles import SystemRole, GoalRole
from app.config import get_settings
from app.internal_auth import verify_internal_bearer
from app.database import get_pool, resolve_goal_id
from app.ledger.journal import append_event
from app.ledger.schema import (
    TASK_CREATED, TASK_ASSIGNED, TASK_STATUS_CHANGED, TASK_STATE_CHANGED,
    TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED, TASK_DEPENDENCY_ADDED,
    MESSAGE_POSTED, QUESTION_RAISED, QUESTION_ANSWERED, DECISION_RECORDED,
    ESCALATION_RESOLVED,
)
from app.models.task import TaskCreate, VALID_TRANSITIONS
from app.notifications import push_notification
from app.openclaw import push_task_reply, gateway_admin, make_task_session_key
from app.services.credential_brokers import release_credentials_safe
from app.task_controller import (
    task_controller, handle_task_completion, destroy_instance,
    emit_task_event, get_last_event_id,
    find_dependent_tasks, get_task_correlation_id,
    _actor_uuid, SYSTEM_ACTOR_ID, cancel_subtask,
)
from app.ledger.payloads import (
    actor_ref, artifact_ref,
    task_created_payload, task_assigned_payload, task_status_changed_payload,
    task_completed_payload, task_failed_payload, task_cancelled_payload,
    task_dependency_added_payload,
    decision_recorded_payload,
    question_raised_payload, question_answered_payload,
    extract_artifacts_from_report,
)
from app.cards import question_card, completion_card, failure_card, approval_card
from app.assistant_relay import post_card_to_assistant_thread as post_card_to_shca_thread, card_to_markdown

from app.ledger.schema import TASK_RESOURCE_GRANTED, TASK_RESOURCE_REVOKED, TASK_PROGRESS_UPDATED
from app.ledger.payloads import task_resource_granted_payload

router = APIRouter(dependencies=[_sec_Depends(_ownership_guard)])
templates = Jinja2Templates(directory="app/templates")


async def _auto_populate_task_resources(
    pool, task_id: str, goal_id: str, actor_id, correlation_id: str = "",
    requested_resources: list[dict] | None = None,
):
    """Populate the task's resource table.

    If requested_resources is provided (SHCA specified which resources the task
    needs), only those resources are granted. Otherwise all goal resources are
    granted with type-based access levels.

    Emits TASK_RESOURCE_GRANTED ledger events for each resource.
    """
    # Access level based on resource type
    TYPE_ACCESS = {
        "git_repo": "read-write",
        "gcs_bucket": "read",
        "bigquery_dataset": "read",
        "cloud_sql": "read",
    }

    if requested_resources:
        # SHCA specified resources — only grant those
        goal_resources = []
        for req in requested_resources:
            name = req.get("name", "")
            if not name:
                continue
            r = await pool.fetchrow(
                "SELECT * FROM resources WHERE goal_id = $1 AND name = $2 AND status = 'active'",
                goal_id, name,
            )
            if r:
                r = dict(r)
                # Use SHCA-specified access, or fall back to type-based default
                r["_requested_access"] = req.get("access", "")
                r["_requested_paths"] = req.get("paths", [])
                goal_resources.append(r)
            else:
                _log.warning("Requested resource '%s' not found in goal %s", name, goal_id)
    else:
        # No specific resources requested — grant all goal resources
        rows = await pool.fetch(
            "SELECT * FROM resources WHERE goal_id = $1 AND status = 'active'", goal_id,
        )
        goal_resources = [dict(r) for r in rows]

    for r in goal_resources:
        # Determine access level: type-based default, with SHCA override for non-git
        # Git repos are ALWAYS read-write (agents need to push branches)
        if r["type"] == "git_repo":
            access = "read-write"
        else:
            access = r.get("_requested_access", "") or TYPE_ACCESS.get(r["type"], "read")
        paths = r.get("_requested_paths", [])
        try:
            granted_by = "shca:specified" if requested_resources else "system:auto"
            await pool.execute(
                """INSERT INTO task_resources (task_id, resource_id, access_level, paths, granted_by)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (task_id, resource_id) DO NOTHING""",
                task_id, r["id"], access, paths or [], granted_by,
            )
            await emit_task_event(
                pool,
                event_type=TASK_RESOURCE_GRANTED,
                task_id=task_id, goal_id=goal_id,
                payload=task_resource_granted_payload(
                    resource_name=r["name"], resource_type=r["type"],
                    access_level=access,
                    gcp_project=r.get("gcp_project", ""),
                    dataset=r.get("dataset", ""),
                    bucket=r.get("bucket", ""),
                ),
                actor_id=actor_id, actor_type="system",
                correlation_id=correlation_id,
                summary=f"Auto-granted {r['type']} '{r['name']}' ({access})",
            )
        except Exception as exc:
            _log.warning("Failed to auto-grant resource %s to task %s: %s", r["name"], task_id, exc)
    if goal_resources:
        _log.info("Auto-populated %d resources for task %s", len(goal_resources), task_id)


async def _inherit_task_resources(
    pool, parent_task_id: str, child_task_id: str, goal_id: str,
    template_id: str, actor_id, correlation_id: str = "",
):
    """Inherit resources from parent task to child subtask.

    Writer templates get read-write, others get read.
    """
    WRITER_TEMPLATES = {"backend-dev-ia", "frontend-dev-ia", "planner-ia", "code-test-ia"}

    parent_resources = await pool.fetch(
        """SELECT tr.*, r.name, r.type, r.gcp_project, r.dataset, r.bucket
           FROM task_resources tr JOIN resources r ON r.id = tr.resource_id
           WHERE tr.task_id = $1""",
        parent_task_id,
    )
    for pr in parent_resources:
        pr = dict(pr)
        access = "read-write" if template_id in WRITER_TEMPLATES else "read"
        # Don't escalate beyond parent's access
        if pr["access_level"] == "read" and access == "read-write":
            access = "read"

        try:
            await pool.execute(
                """INSERT INTO task_resources
                       (task_id, resource_id, access_level, branch, paths, granted_by)
                   VALUES ($1, $2, $3, $4, $5, 'system:inherited')
                   ON CONFLICT (task_id, resource_id) DO NOTHING""",
                child_task_id, pr["resource_id"], access,
                pr.get("branch", ""), pr.get("paths", []),
            )
            await emit_task_event(
                pool,
                event_type=TASK_RESOURCE_GRANTED,
                task_id=child_task_id, goal_id=goal_id,
                payload=task_resource_granted_payload(
                    resource_name=pr["name"], resource_type=pr["type"],
                    access_level=access,
                    branch=pr.get("branch", ""),
                    paths=pr.get("paths", []),
                    gcp_project=pr.get("gcp_project", ""),
                    dataset=pr.get("dataset", ""),
                    bucket=pr.get("bucket", ""),
                    granted_by=f"inherited from {parent_task_id}",
                ),
                actor_id=actor_id, actor_type="system",
                correlation_id=correlation_id,
                summary=f"Inherited {pr['type']} '{pr['name']}' ({access}) from parent",
            )
        except Exception as exc:
            _log.warning("Failed to inherit resource %s to subtask %s: %s", pr["name"], child_task_id, exc)


async def _last_task_event_id(conn, task_id: str) -> str | None:
    """Return the event_id of the most recent ledger event for this task."""
    row = await conn.fetchval(
        """SELECT event_id FROM ledger_events
           WHERE context_task_id = $1
           ORDER BY sequence_number DESC LIMIT 1""",
        task_id,
    )
    return str(row) if row else None

TASK_STATES = ["pending", "ready", "dispatching", "in_progress", "blocked", "awaiting_approval", "approved", "rejected", "done", "failed", "cancelled"]
# Story routes moved to app/routers/stories.py



# -- Kanban board --------------------------------------------------------------

@router.get("/goals/{pid}/tasks", response_class=HTMLResponse)
async def task_board(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
    filter_agent: str = "",
    filter_priority: str = "",
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()

    # Hide human-review subtasks from the board — they're gates on the
    # parent task, not work items. Users complete them via the human-review
    # card on the parent's detail page, which POSTs to the dedicated
    # /human-review endpoint.
    query = (
        "SELECT * FROM tasks "
        "WHERE goal_id = $1 AND status = 'active' "
        "AND review_target_id IS NULL"
    )
    params: list = [pid]
    idx = 2
    if filter_agent:
        query += f" AND assigned_to = ${idx}"
        params.append(filter_agent)
        idx += 1
    if filter_priority:
        query += f" AND priority = ${idx}"
        params.append(filter_priority)
        idx += 1

    rows = await pool.fetch(query, *params)
    all_tasks = [dict(r) for r in rows]

    # Separate parent tasks from subtasks
    parent_tasks = [t for t in all_tasks if not t.get("parent_task_id")]
    subtasks_by_parent: dict[str, list] = {}
    for t in all_tasks:
        pid_ref = t.get("parent_task_id")
        if pid_ref:
            subtasks_by_parent.setdefault(pid_ref, []).append(t)

    # Also keep columns for backward compat
    columns: dict[str, list] = {s: [] for s in TASK_STATES}
    for t in all_tasks:
        state = t.get("state", "pending")
        if state in columns:
            columns[state].append(t)

    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row)

    # Member names for assignee display
    member_rows = await pool.fetch("""
        SELECT gm.uid, u.display_name, u.email FROM goal_members gm
        JOIN users u ON u.uid = gm.uid WHERE gm.goal_id = $1
    """, pid)
    member_names = {m["uid"]: m["display_name"] or m["email"] for m in member_rows}

    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "tasks-board-intro")

    return templates.TemplateResponse(
        "tasks/board.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "columns": columns,
            "states": TASK_STATES,
            "parent_tasks": parent_tasks,
            "subtasks_by_parent": subtasks_by_parent,
            "filter_agent": filter_agent,
            "filter_priority": filter_priority,
            "role": role,
            "member_names": member_names,
            "auto_fire_tour": auto_fire_tour,
        },
    )


# -- Delete task (soft-delete) -------------------------------------------------

@router.post("/goals/{pid}/tasks/{tid}/delete")
async def delete_task(
    pid: str,
    tid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Soft-delete a task and its incomplete subtasks.

    - Completed/approved subtasks keep their state (immutable outcomes).
    - Incomplete subtasks are cancelled with proper ledger attribution.
    - All tasks in the tree are marked status='deleted'.
    - No rows are removed from any table (ledger + data are immutable).

    Manager-or-assignee: matches the story-lifecycle model (the task's own
    assignee owns it, no manager babysitting required), while a manager can
    still act on any task.
    """
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE goal_id = $1 AND id = $2 AND status = 'active'",
        pid, tid,
    )
    if not row:
        raise HTTPException(status_code=404)

    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN and row.get("assigned_to") != user["uid"]:
        raise HTTPException(status_code=403)

    task = dict(row)
    user_actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", user["uid"],
    )
    user_actor_id = user_actor_id or SYSTEM_ACTOR_ID

    # Collect all subtasks (direct children; cancel_subtask cascades further)
    subtask_rows = await pool.fetch(
        "SELECT * FROM tasks WHERE parent_task_id = $1 AND status = 'active'",
        tid,
    )

    # States that represent completed work — don't change these
    COMPLETED_STATES = {"done", "approved"}

    # Cancel incomplete subtasks and tear down ALL gateway instances (including completed).
    # Each subtask is handled independently — one failure must not block the rest.
    cancelled_ids = []
    for sr in subtask_rows:
        try:
            if sr["state"] in COMPLETED_STATES:
                _log.info(
                    "Subtask %s already %s — preserving state, tearing down gateway agent (instance=%s)",
                    sr["id"], sr["state"], sr.get("instance_id") or "none",
                )
                # State stays, but gateway instance is cleaned up
                if sr["instance_id"]:
                    await destroy_instance(dict(sr))
            else:
                _log.info(
                    "Cancelling subtask %s (state=%s, instance=%s) — will remove gateway agent",
                    sr["id"], sr["state"], sr.get("instance_id") or "none",
                )
                ids = await cancel_subtask(
                    sr["id"],
                    reason=f"Parent task {tid} deleted by {user.get('email', user['uid'])}",
                    cancelled_by_actor=user_actor_id,
                    cancelled_by_type="human",
                    cancelled_by_name=user.get("email", user["uid"]),
                    cascade=True,
                )
                cancelled_ids.extend(ids)
        except Exception as exc:
            _log.warning("Failed to process subtask %s during deletion (non-fatal): %s", sr["id"], exc)

    # Cancel the parent task itself if not completed; tear down its instance either way
    try:
        if task["state"] not in COMPLETED_STATES:
            _log.info(
                "Cancelling parent task %s (state=%s, instance=%s)",
                tid, task["state"], task.get("instance_id") or "none",
            )
            await cancel_subtask(
                tid,
                reason=f"Deleted by {user.get('email', user['uid'])}",
                cancelled_by_actor=user_actor_id,
                cancelled_by_type="human",
                cancelled_by_name=user.get("email", user["uid"]),
                cascade=False,  # subtasks already handled above
            )
        else:
            _log.info("Parent task %s already %s — preserving state, recording deletion", tid, task["state"])
            # Record deletion in ledger even for completed tasks — this is a
            # significant business event (outcomes may be invalidated).
            corr_id = await get_task_correlation_id(pool, tid)
            last_evt = await get_last_event_id(pool, tid)
            await emit_task_event(
                pool,
                event_type=TASK_CANCELLED,
                task_id=tid, goal_id=pid,
                payload=task_cancelled_payload(
                    reason=f"Deleted by {user.get('email', user['uid'])} (was {task['state']})",
                    cancelled_by=actor_ref(user_actor_id, "human", user.get("email", user["uid"])),
                ),
                actor_id=user_actor_id, actor_type="human",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary=f"Task deleted (was {task['state']})",
            )
            if task.get("instance_id"):
                await destroy_instance(task)
    except Exception as exc:
        _log.warning("Failed to cancel parent task %s during deletion (non-fatal): %s", tid, exc)

    # Note: SHCA is goal-scoped, not task-scoped — it has no instance_id
    # on any task row, so destroy_instance never touches it.

    # Soft-delete: mark all tasks in the tree as status='deleted'
    all_task_ids = [tid] + [sr["id"] for sr in subtask_rows]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE tasks SET status = 'deleted' WHERE id = ANY($1::text[])",
                all_task_ids,
            )

    preserved = [sr["id"] for sr in subtask_rows if sr["state"] in COMPLETED_STATES]
    _log.info(
        "Soft-deleted task %s: %d subtasks (%d cancelled, %d preserved), goal %s, by %s",
        tid, len(subtask_rows), len(cancelled_ids), len(preserved),
        pid, user.get("email", user["uid"]),
    )

    return RedirectResponse(url=f"/goals/{pid}/tasks", status_code=303)


# -- Human task board (Kanban) -------------------------------------------------
# NOTE: must be registered before /goals/{pid}/tasks/{tid} — FastAPI matches
# routes in order, and "board" would otherwise be captured as {tid}.

@router.get("/goals/{pid}/tasks/board", response_class=HTMLResponse)
async def human_task_board(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Hierarchical task board — expandable main tasks with subtask trees."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1 AND status = 'active'", pid)
    if not goal:
        raise HTTPException(status_code=404)

    # Fetch ALL active tasks in the goal. Human-review subtasks are gates,
    # not work — surfaced on the parent's detail page via the review card,
    # not on the board.
    all_tasks = await pool.fetch("""
        SELECT t.* FROM tasks t
        WHERE t.goal_id = $1 AND t.status = 'active'
          AND t.review_target_id IS NULL
        ORDER BY
            CASE t.state
                WHEN 'in_progress' THEN 1 WHEN 'blocked' THEN 2
                WHEN 'pending' THEN 3 WHEN 'awaiting_approval' THEN 4
                WHEN 'done' THEN 5 WHEN 'failed' THEN 6 ELSE 7
            END,
            CASE t.priority
                WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5
            END,
            t.created_at
    """, pid)

    # Build hierarchy: main tasks (no parent) and subtasks grouped by parent
    main_tasks = []
    subtasks_by_parent = {}
    for t in all_tasks:
        task = dict(t)
        parent = task.get("parent_task_id")
        if parent:
            subtasks_by_parent.setdefault(parent, []).append(task)
        else:
            main_tasks.append(task)

    members = await pool.fetch("""
        SELECT gm.uid, u.display_name, u.email
        FROM goal_members gm
        JOIN users u ON u.uid = gm.uid
        WHERE gm.goal_id = $1
    """, pid)

    member_names = {m["uid"]: m["display_name"] or m["email"] for m in members}

    return templates.TemplateResponse(
        "tasks/human_board.html",
        {
            "request": request,
            "user": user,
            "goal": dict(goal),
            "pid": pid,
            "main_tasks": main_tasks,
            "subtasks_by_parent": subtasks_by_parent,
            "total_count": len(all_tasks),
            "members": [dict(m) for m in members],
            "member_names": member_names,
        },
    )


# -- Task detail ---------------------------------------------------------------

@router.get("/goals/{pid}/tasks/{tid}", response_class=HTMLResponse)
async def task_detail(
    pid: str,
    tid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE goal_id = $1 AND id = $2", pid, tid,
    )
    if not row:
        raise HTTPException(status_code=404)
    task = dict(row)

    event_rows = await pool.fetch(
        "SELECT * FROM task_events WHERE task_id = $1 ORDER BY created_at", tid,
    )
    events = [dict(r) for r in event_rows]

    thread_messages = []
    thread_id = task.get("thread_id")
    if thread_id:
        msg_rows = await pool.fetch(
            "SELECT * FROM messages WHERE thread_id = $1 ORDER BY created_at", thread_id,
        )
        thread_messages = [dict(r) for r in msg_rows]

    proj_row = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)
    goal = dict(proj_row)

    # If this task is awaiting an RA-driven human review, fetch the latest
    # task_reviews record + the active human-review subtask so the template
    # can render the review card with Approve/Reject buttons that POST to
    # the dedicated /human-review endpoint.
    human_review = None
    if task.get("state") == "awaiting_approval":
        hr_row = await pool.fetchrow(
            """SELECT id, state FROM tasks
                WHERE review_target_id = $1
                  AND template_id = 'human'
                  AND status = 'active'
                  AND state IN ('blocked', 'awaiting_approval')
                ORDER BY created_at DESC LIMIT 1""",
            tid,
        )
        if hr_row:
            tr_row = await pool.fetchrow(
                """SELECT verdict, quality_score, summary, findings, recommendations
                     FROM task_reviews
                    WHERE task_id = $1
                    ORDER BY reviewed_at DESC LIMIT 1""",
                tid,
            )
            human_review = {
                "subtask_id": hr_row["id"],
                "subtask_state": hr_row["state"],
                "ra_verdict": tr_row["verdict"] if tr_row else None,
                "ra_score": tr_row["quality_score"] if tr_row else None,
                "ra_summary": tr_row["summary"] if tr_row else None,
                "ra_findings": tr_row["findings"] if tr_row else [],
                "ra_recommendations": tr_row["recommendations"] if tr_row else [],
            }

    return templates.TemplateResponse(
        "tasks/detail.html",
        {
            "request": request,
            "user": user,
            "goal": goal,
            "pid": pid,
            "task": task,
            "events": events,
            "thread_messages": thread_messages,
            "role": role,
            "human_review": human_review,
        },
    )


# -- User reply to task thread -------------------------------------------------

@router.post("/goals/{pid}/tasks/{tid}/reply")
async def reply_to_task_thread(
    pid: str,
    tid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE goal_id = $1 AND id = $2", pid, tid,
    )
    if not row:
        raise HTTPException(status_code=404)

    task = dict(row)
    thread_id = task.get("thread_id")
    if not thread_id:
        raise HTTPException(status_code=400, detail="This task has no thread")

    form = await request.form()
    content = form.get("content", "").strip()
    reply_action = form.get("action", "reply")  # "reply" or "replan"
    if not content:
        raise HTTPException(status_code=400, detail="Reply cannot be empty")

    _log.info(
        "reply_to_task_thread: task=%s state=%s action=%s content_len=%d user=%s",
        tid, task.get("state"), reply_action, len(content), user.get("email", user["uid"]),
    )

    msg_id = str(uuid.uuid4())
    task_corr_id = task.get("correlation_id")

    async with pool.acquire() as conn:
        async with conn.transaction():
            last_event_id = await _last_task_event_id(conn, tid)

            await append_event(
                conn,
                event_type=MESSAGE_POSTED,
                payload={"msg_id": msg_id, "role": "human", "content": content, "sender": user["uid"]},
                actor_id=user["actor_id"],
                actor_type="human",
                correlation_id=task_corr_id,
                causation_ids=[last_event_id] if last_event_id else None,
                context_task_id=tid,
                context_goal_id=pid,
                context_conversation_id=thread_id,
            )
            await conn.execute(
                """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                   VALUES ($1, $2, $3, 'human', $4, 0)""",
                msg_id, thread_id, user["uid"], content,
            )

    # Send reply to the agent's session.
    # Persistent workers share a session across all subtask phases — the
    # session key uses the PARENT task id, not the subtask id. If we use
    # the subtask id, chat.send creates a new session and the running
    # agent never sees the answer.
    instance_id = task.get("instance_id", "")
    agent_id = task.get("assigned_to", "")
    parent_task_id = task.get("parent_task_id", "")
    session_task_id = parent_task_id or tid  # persistent worker → parent; standalone → self

    if instance_id and reply_action != "replan":
        session_key = make_task_session_key(instance_id, session_task_id)
        try:
            await gateway_admin.call("chat.send", {
                "sessionKey": session_key,
                "message": content,
                "idempotencyKey": f"reply-{msg_id}",
            })
            _log.info(
                "Sent reply to instance=%s session_key=%s for task=%s (parent=%s)",
                instance_id, session_key, tid, parent_task_id or "none",
            )
        except Exception as exc:
            _log.warning("Failed to send reply to instance %s: %s", instance_id, exc)
    elif agent_id:
        # Legacy: manually created task. pid is lowercased — gateway
        # canonicalises sessionKeys to lowercase.
        session_key = f"{pid.lower()}:{agent_id}"
        await push_task_reply(agent_id=agent_id, session_key=session_key, content=content)

    # Relay the human's answer to the SHCA thread so the assistant sees it
    # and can factor it into future task specs / GO updates.
    source_thread_id = task.get("source_thread_id")
    if source_thread_id:
        try:
            agent_name = task.get("template_id") or task.get("assigned_to") or "agent"
            relay_content = (
                f"**Human answered {agent_name}'s question on task {tid[:8]}**"
                f"{' (+ re-plan requested)' if reply_action == 'replan' else ''}:\n\n"
                f"> {content[:2000]}"
            )
            relay_msg_id = str(uuid.uuid4())
            await pool.execute(
                """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                   VALUES ($1, $2, 'system', 'agent', $3, 0)""",
                relay_msg_id, source_thread_id, relay_content,
            )
            _log.info(
                "Relayed human answer to SHCA thread=%s for task=%s action=%s",
                source_thread_id[:12], tid, reply_action,
            )
        except Exception as exc:
            _log.warning("Failed to relay answer to SHCA thread: %s", exc)

    # If task was blocked, handle re-plan or simple unblock
    if task.get("state") == "blocked":
        corr_id = await get_task_correlation_id(pool, tid)
        question_evt_id = await pool.fetchval(
            """SELECT event_id FROM ledger_events
               WHERE context_task_id = $1 AND event_type = $2
               ORDER BY sequence_number DESC LIMIT 1""",
            tid, QUESTION_RAISED,
        )
        question_evt_id = str(question_evt_id) if question_evt_id else None

        # Log the answer event regardless of path
        answer_evt = await emit_task_event(
            pool, event_type=QUESTION_ANSWERED, task_id=tid, goal_id=pid,
            payload=question_answered_payload(
                question_event_id=question_evt_id or "",
                answer=content[:32000],
                answerer=actor_ref(user["actor_id"], "human", user.get("email", "")),
            ),
            actor_id=user["actor_id"], actor_type="human",
            correlation_id=corr_id, causation_ids=[question_evt_id] if question_evt_id else None,
            summary=f"Human responded: {content[:200]}", content=content,
        )
        answer_id = answer_evt["event_id"] if answer_evt else None

        if reply_action == "replan":
            # ── Path B: Re-plan ──
            # Store the human's answer as feedback on the PARENT task (the
            # planner task), cancel unstarted sibling subtasks, and
            # re-dispatch the parent to the planner with revision context.
            _log.info(
                "REPLAN requested: task=%s parent=%s feedback_len=%d",
                tid, task.get("parent_task_id") or task.get("story_id") or "-",
                len(content),
            )

            # Find the parent (planner) task
            parent_task_id = task.get("parent_task_id", "")
            if not parent_task_id:
                # This IS the planner task — re-dispatch itself
                parent_task_id = tid

            # Store feedback on the parent for the planner to read
            await pool.execute(
                "UPDATE tasks SET blocked_reason = $1 WHERE id = $2",
                f"HUMAN FEEDBACK: {content}", parent_task_id,
            )

            # Cancel all unstarted sibling subtasks under the same parent
            # (in_progress ones keep running — their work is already on the branch).
            # RETURNING id so we can release credentials for any that had a
            # mint already (typical for tasks in the 'dispatching' state — the
            # broker provisioned but the RPC errored before the agent saw it).
            cancelled_rows = await pool.fetch("""
                UPDATE tasks SET state = 'cancelled', status = 'deleted'
                WHERE parent_task_id = $1
                  AND state IN ('pending', 'ready', 'dispatching')
                  AND id != $2
                RETURNING id
            """, parent_task_id, tid)
            _log.info(
                "REPLAN: cancelled unstarted siblings under parent=%s (affected=%d)",
                parent_task_id, len(cancelled_rows),
            )
            for r in cancelled_rows:
                await release_credentials_safe(
                    r["id"], "tasks_router:replan_sibling_cancel",
                )

            # Mark the blocked subtask as cancelled too (it asked the question,
            # the planner will decide whether to recreate it)
            if tid != parent_task_id:
                await pool.execute(
                    "UPDATE tasks SET state = 'cancelled' WHERE id = $1", tid,
                )
                await release_credentials_safe(tid, "tasks_router:replan_blocked_cancel")
                _log.info("REPLAN: cancelled blocked subtask %s", tid)

            # Reset the parent task to 'ready' so the controller picks it up
            # and re-dispatches it to the planner with the HUMAN FEEDBACK
            await pool.execute(
                "UPDATE tasks SET state = 'ready', result = '' WHERE id = $1",
                parent_task_id,
            )

            await emit_task_event(
                pool, event_type=TASK_STATUS_CHANGED, task_id=parent_task_id,
                goal_id=pid,
                payload=task_status_changed_payload(
                    previous_status="blocked" if parent_task_id == tid else "in_progress",
                    new_status="ready",
                    reason=f"Re-plan requested by human: {content[:200]}",
                ),
                actor_id=user["actor_id"], actor_type="human",
                correlation_id=corr_id,
                causation_ids=[answer_id] if answer_id else None,
                summary=f"Re-plan: {content[:100]}",
            )
            _log.info(
                "REPLAN: parent=%s reset to ready — controller will re-dispatch to planner",
                parent_task_id,
            )

            # Trigger the task controller to pick up the ready task
            from app.task_controller import task_controller
            await task_controller.trigger()
            _log.info("REPLAN: triggered task controller to pick up parent=%s", parent_task_id)

        else:
            # ── Path A: Simple reply — inject answer, resume agent ──
            await pool.execute(
                "UPDATE tasks SET state = 'in_progress', blocked_reason = NULL WHERE id = $1",
                tid,
            )
            await emit_task_event(
                pool, event_type=TASK_STATUS_CHANGED, task_id=tid, goal_id=pid,
                payload=task_status_changed_payload(
                    previous_status="blocked", new_status="in_progress",
                    reason="Human answered question",
                ),
                actor_id=user["actor_id"], actor_type="human",
                correlation_id=corr_id,
                causation_ids=[answer_id] if answer_id else None,
                summary="Unblocked by human reply",
            )
            _log.info("Task %s unblocked by human reply (simple)", tid)

    return RedirectResponse(url=f"/goals/{pid}/tasks/{tid}", status_code=303)


# -- Task assignment -----------------------------------------------------------

@router.post("/goals/{pid}/tasks/{tid}/assign")
async def assign_task(
    pid: str,
    tid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Assign or reassign a task to a human member."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    form = await request.form()
    assigned_to = form.get("assigned_to", "").strip()

    if not assigned_to:
        raise HTTPException(status_code=400, detail="assigned_to required")

    pool = get_pool()
    task = await pool.fetchrow(
        "SELECT * FROM tasks WHERE id = $1 AND goal_id = $2", tid, pid,
    )
    if not task:
        raise HTTPException(status_code=404)

    old_assigned = task.get("assigned_to", "")

    await pool.execute(
        "UPDATE tasks SET assigned_to = $1 WHERE id = $2 AND goal_id = $3",
        assigned_to, tid, pid,
    )

    # Ledger event
    await append_event(
        pool,
        event_type=TASK_ASSIGNED,
        payload=task_assigned_payload(
            assignee=actor_ref(
                await pool.fetchval("SELECT actor_id FROM users WHERE uid = $1", assigned_to) or SYSTEM_ACTOR_ID,
                "human",
                assigned_to,
            ),
            assignment_reason=f"Assigned by {user.get('display_name', user['uid'])}",
        ),
        actor_id=user["actor_id"],
        context_goal_id=pid,
        context_task_id=tid,
    )

    # Notify GO
    try:
        from app.services.go_events import on_task_state_changed
        await on_task_state_changed(pid, tid, task["state"], task["state"], user["uid"])
    except Exception:
        pass

    # Redirect back to wherever they came from
    referer = request.headers.get("referer", "")
    if "board" in referer:
        return RedirectResponse(url=f"/goals/{pid}/tasks/board", status_code=303)
    return RedirectResponse(url=f"/goals/{pid}/tasks/{tid}", status_code=303)


# -- Human task detail ---------------------------------------------------------

@router.get("/goals/{pid}/tasks/{tid}/human", response_class=HTMLResponse)
async def human_task_detail(
    pid: str,
    tid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Task detail page for human-owned tasks."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    task = await pool.fetchrow(
        "SELECT * FROM tasks WHERE id = $1 AND goal_id = $2", tid, pid,
    )
    if not task:
        raise HTTPException(status_code=404)
    task = dict(task)

    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)

    # Assignee name
    assignee_name = ""
    if task.get("assigned_to"):
        assignee_name = await pool.fetchval(
            "SELECT display_name FROM users WHERE uid = $1", task["assigned_to"],
        ) or task["assigned_to"]

    # Task events
    events = await pool.fetch("""
        SELECT * FROM task_events
        WHERE task_id = $1 ORDER BY created_at DESC LIMIT 20
    """, tid)

    # Resource dependencies
    resource_deps = await pool.fetch("""
        SELECT trd.*, gr.name AS resource_name, gr.kind AS resource_kind
        FROM task_resource_deps trd
        JOIN goal_resources gr ON gr.id = trd.resource_id
        WHERE trd.task_id = $1
    """, tid)

    # Dependent tasks (tasks that depend on this one)
    dependents = await pool.fetch("""
        SELECT id, title, state, assigned_to FROM tasks
        WHERE goal_id = $1 AND $2 = ANY(depends_on) AND status = 'active'
    """, pid, tid)

    # Upstream tasks (this task depends on)
    upstream = []
    if task.get("depends_on"):
        for dep_id in task["depends_on"]:
            dep = await pool.fetchrow(
                "SELECT id, title, state, result_summary FROM tasks WHERE id = $1", dep_id,
            )
            if dep:
                upstream.append(dict(dep))

    # Members for reassignment dropdown
    members = await pool.fetch("""
        SELECT gm.uid, u.display_name, u.email
        FROM goal_members gm JOIN users u ON u.uid = gm.uid
        WHERE gm.goal_id = $1
    """, pid)

    return templates.TemplateResponse(
        "tasks/human_detail.html",
        {
            "request": request,
            "user": user,
            "goal": dict(goal),
            "pid": pid,
            "task": task,
            "assignee_name": assignee_name,
            "events": [dict(e) for e in events],
            "resource_deps": [dict(r) for r in resource_deps],
            "dependents": [dict(d) for d in dependents],
            "upstream": upstream,
            "members": [dict(m) for m in members],
        },
    )


# -- State transitions ---------------------------------------------------------

@router.post("/goals/{pid}/tasks/{tid}/transition")
async def transition_task(
    pid: str,
    tid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Manually drive a task to a new state (cancel/fail/done/etc). Manager-only —
    this reaches the same terminal states as approve_task/delete_task, so it gets
    the same gate (bypass stripped: see ROLE_SYSTEM.md §4 participation routes)."""
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403, detail="Goal admin required")

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE goal_id = $1 AND id = $2", pid, tid,
    )
    if not row:
        raise HTTPException(status_code=404)
    current_state = row["state"]
    task_corr_id = row.get("correlation_id")

    form = await request.form()
    new_state = form.get("state", "")
    note = form.get("note", "")

    if new_state not in VALID_TRANSITIONS.get(current_state, []):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from '{current_state}' to '{new_state}'",
        )

    corr_id = await get_task_correlation_id(pool, tid) or str(task_corr_id or "")
    last_event_id = await get_last_event_id(pool, tid)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Route terminal states to their specific event types
            if new_state == "cancelled":
                cascade = await find_dependent_tasks(pool, pid, tid)
                await emit_task_event(
                    conn, event_type=TASK_CANCELLED, task_id=tid, goal_id=pid,
                    payload=task_cancelled_payload(
                        reason=note or "Manually cancelled",
                        cancelled_by=actor_ref(user["actor_id"], "human", user.get("email", "")),
                        cascade_impact=cascade,
                    ),
                    actor_id=user["actor_id"], actor_type="human",
                    correlation_id=corr_id, causation_ids=[last_event_id] if last_event_id else None,
                    summary=f"Cancelled: {note or 'manual'}",
                )
            elif new_state == "failed":
                cascade = await find_dependent_tasks(pool, pid, tid)
                await emit_task_event(
                    conn, event_type=TASK_FAILED, task_id=tid, goal_id=pid,
                    payload=task_failed_payload(
                        failure_reason=note or "Manually marked as failed",
                        cascade_impact=cascade,
                    ),
                    actor_id=user["actor_id"], actor_type="human",
                    correlation_id=corr_id, causation_ids=[last_event_id] if last_event_id else None,
                    summary=f"Failed: {note or 'manual'}",
                )
            elif new_state == "done":
                await emit_task_event(
                    conn, event_type=TASK_COMPLETED, task_id=tid, goal_id=pid,
                    payload=task_completed_payload(
                        outcome_summary=note or "Manually completed",
                    ),
                    actor_id=user["actor_id"], actor_type="human",
                    correlation_id=corr_id, causation_ids=[last_event_id] if last_event_id else None,
                    summary=f"Completed: {note or 'manual'}",
                )
            else:
                await emit_task_event(
                    conn, event_type=TASK_STATUS_CHANGED, task_id=tid, goal_id=pid,
                    payload=task_status_changed_payload(
                        previous_status=current_state, new_status=new_state, reason=note,
                    ),
                    actor_id=user["actor_id"], actor_type="human",
                    correlation_id=corr_id, causation_ids=[last_event_id] if last_event_id else None,
                    summary=f"{current_state} → {new_state}: {note}",
                )

            await conn.execute(
                # Clear blocked_reason when re-readying a blocked task so the re-dispatched
                # turn doesn't carry a stale block reason; otherwise just set the state.
                # Stamp started_at/completed_at for dashboard duration metrics — sentinel_parser
                # sets these for automated completions; this path covers manual transitions.
                "UPDATE tasks SET state = $1, "
                "  blocked_reason = CASE WHEN $1 = 'ready' THEN NULL ELSE blocked_reason END, "
                "  started_at   = CASE WHEN $1 = 'in_progress' THEN COALESCE(started_at, NOW()) ELSE started_at END, "
                "  completed_at = CASE WHEN $1 IN ('done', 'failed') THEN COALESCE(completed_at, NOW()) ELSE completed_at END, "
                "  updated_at = NOW() "
                "WHERE id = $2",
                new_state, tid,
            )
            await _append_task_event(conn, pid, tid, current_state, new_state, user["uid"], note)

    # Clean up agent instances when task reaches a terminal state
    if new_state in ("done", "approved", "cancelled", "failed"):
        from app.task_controller import cleanup_parent_task
        await cleanup_parent_task(tid)

    # Notify GO agent of task state change
    try:
        from app.services.go_events import on_task_state_changed
        await on_task_state_changed(pid, tid, current_state, new_state, user["uid"])
    except Exception as exc:
        _log.warning("GO event notification failed for task %s: %s", tid, exc)

    # Check if all subtasks of parent are now done → auto-complete the parent
    # AND notify the human.
    if new_state in ("done", "approved"):
        try:
            await _check_parent_completion(pool, pid, tid, user["uid"])
        except Exception as exc:
            _log.debug("Parent completion check failed: %s", exc)
        # Auto-complete the parent task (awaiting_approval for top-level,
        # done for nested). Uses the same logic the controller uses for
        # agent-side completions, so behavior is consistent.
        task_row = await pool.fetchrow(
            "SELECT parent_task_id FROM tasks WHERE id = $1", tid,
        )
        if task_row and task_row.get("parent_task_id"):
            try:
                from app.task_controller import check_parent_completion
                completed = await check_parent_completion(task_row["parent_task_id"])
                if completed:
                    _log.info(
                        "Auto-completed parent task %s after human marked %s as %s",
                        task_row["parent_task_id"], tid, new_state,
                    )
            except Exception as exc:
                _log.warning(
                    "Auto-completion of parent %s failed: %s",
                    task_row["parent_task_id"], exc,
                )

    # A claude-code-phase subtask manually marked FAILED → cascade-fail the following
    # subtasks + CANCEL the parent DAG (same as the sentinel FAILED path). Otherwise the
    # DAG just stalls because dependents never become ready.
    if (new_state == "failed"
            and (row.get("executor") or "") == "claude-code-phase"
            and row.get("parent_task_id")):
        # Release the failed subtask's per-task credentials (the sentinel FAILED path
        # does this; the board path must too, else the GCP SA key orphans).
        try:
            from app.services.credential_brokers import release_credentials_safe
            await release_credentials_safe(tid, "transition_task:FAILED")
        except Exception as exc:
            _log.warning("credential release failed for task %s: %s", tid, exc)
        try:
            from app.services.sentinel_parser import _fail_dag_cascade
            await _fail_dag_cascade(pid, row.get("story_id"), row["parent_task_id"], tid, note)
        except Exception as exc:
            _log.warning("DAG fail-cascade failed for task %s: %s", tid, exc)

    # Trigger RA review for standalone tasks manually completed (subtasks are
    # reviewed at the parent level once all siblings finish via check_parent_completion).
    # SKIP for claude-code tasks — they're reviewed via the diff-review card, never an RA.
    if (new_state == "done" and not row.get("parent_task_id")
            and "claude-code" not in (row.get("executor") or "")):
        try:
            from app.task_controller import _spawn_review_task
            await _spawn_review_task(pool, tid, dict(row))
        except Exception as exc:
            _log.warning("Review spawn failed for task %s: %s", tid, exc)

    # Wake the dispatch loop so work dispatches promptly (otherwise it waits for the
    # ~15s idle tick). done/approved → the NEXT subtask becomes ready + dispatches;
    # ready → THIS task (e.g. a blocked subtask manually re-readied) dispatches as the
    # next turn on the story's Claude session via phase_dispatcher.
    if new_state in ("done", "approved", "ready"):
        try:
            from app.task_controller import task_controller
            await task_controller.trigger()
        except Exception:
            pass

    # Release resource locks when task completes
    if new_state in ("done", "failed", "cancelled"):
        try:
            await pool.execute("""
                UPDATE goal_resources SET locked_by = NULL, locked_for = NULL, locked_at = NULL
                WHERE locked_for = $1
            """, tid)
        except Exception:
            pass

    return RedirectResponse(url=f"/goals/{pid}/tasks/{tid}", status_code=303)


@router.post("/goals/{pid}/tasks/{tid}/approve")
async def approve_task(
    pid: str,
    tid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Manager-or-assignee: the task's own assignee can approve/reject their
    own awaiting-approval checkpoint, matching the story-lifecycle model."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE goal_id = $1 AND id = $2", pid, tid,
    )
    if not row:
        raise HTTPException(status_code=404)

    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN and row.get("assigned_to") != user["uid"]:
        raise HTTPException(status_code=403, detail="Admin approval required")

    form = await request.form()
    action = form.get("action", "approve")
    note = form.get("note", "")

    if row["state"] != "awaiting_approval":
        raise HTTPException(status_code=400, detail="Task is not awaiting approval")

    # If this awaiting-approval task has an active human-review subtask,
    # /approve isn't the right path — the request must go to
    # /human-review so the review subtask transitions in lock-step.
    # The detail.html template already routes the buttons there when
    # `human_review` is set; this guard is here in case any other caller
    # (or stale link) hits /approve directly.
    hr_check = await pool.fetchval(
        """SELECT 1 FROM tasks
            WHERE review_target_id = $1
              AND template_id = 'human'
              AND status = 'active'
              AND state IN ('blocked', 'awaiting_approval')
            LIMIT 1""",
        tid,
    )
    if hr_check:
        raise HTTPException(
            status_code=400,
            detail=(
                "This task has an active human-review subtask. "
                f"POST to /goals/{pid}/tasks/{tid}/human-review with "
                "{action: 'approve'|'reject', feedback: '...'} instead."
            ),
        )

    new_state = "approved" if action == "approve" else "rejected"

    corr_id = await get_task_correlation_id(pool, tid)
    last_event_id = await get_last_event_id(pool, tid)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await emit_task_event(
                conn, event_type=TASK_STATUS_CHANGED, task_id=tid, goal_id=pid,
                payload=task_status_changed_payload(
                    previous_status="awaiting_approval", new_status=new_state,
                    reason=note or (f"Human {action}d" if action != "approve" else "Human approved"),
                ),
                actor_id=user["actor_id"], actor_type="human",
                correlation_id=corr_id, causation_ids=[last_event_id] if last_event_id else None,
                summary=f"{'Approved' if new_state == 'approved' else 'Rejected'}: {note or ''}",
            )
            await conn.execute(
                "UPDATE tasks SET state = $1, "
                "completed_at = CASE WHEN $1 = 'approved' THEN COALESCE(completed_at, NOW()) ELSE completed_at END "
                "WHERE id = $2",
                new_state, tid,
            )
            await _append_task_event(conn, pid, tid, "awaiting_approval", new_state, user["uid"], note)

    # Escalation resolved — the human acted on the task.
    if new_state in ("approved", "rejected"):
        await emit_task_event(
            pool,
            event_type=ESCALATION_RESOLVED,
            task_id=tid, goal_id=pid,
            payload={
                "resolution": new_state,
                "note": note or "",
            },
            actor_id=user["actor_id"], actor_type="human",
            correlation_id=corr_id,
            causation_ids=[last_event_id] if last_event_id else None,
            summary=f"Escalation resolved: {new_state}",
        )

    # Clean up all agent instances when task is approved/done
    if new_state == "approved":
        from app.task_controller import cleanup_parent_task
        await cleanup_parent_task(tid)

    # Trigger controller: approval may unblock dependent tasks
    await task_controller.trigger()

    # Notify GO agent
    try:
        from app.services.go_events import on_task_state_changed
        await on_task_state_changed(pid, tid, "awaiting_approval", new_state, user["uid"])
    except Exception as exc:
        _log.warning("GO event notification failed for task %s: %s", tid, exc)

    return RedirectResponse(url=f"/goals/{pid}/tasks/{tid}", status_code=303)


# -- Human review (UI + Slack share this endpoint) ---------------------------

@router.post("/goals/{pid}/tasks/{main_task_id}/human-review")
async def post_human_review(
    pid: str,
    main_task_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Complete a human review for a reviewed work task.

    The URL identifies the **main task** being reviewed (the one users see
    on the kanban / in Slack cards). The endpoint finds the active
    human-review subtask under it and runs the unified
    `complete_human_review` cascade — subtask → done, main task → done or
    rejected, ledger events on both, IA cleanup, GO notification.

    Body:
        {"action": "approve" | "reject", "feedback": "<optional>"}

    Returns:
        302 redirect to the main task detail page on form posts,
        or JSON on Accept: application/json requests.

    Manager-or-assignee: the reviewed task's own assignee can post the
    review decision, matching the story-lifecycle model.
    """
    # Pull action/feedback from form or JSON body
    action = ""
    feedback = ""
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
            action = str(body.get("action", "")).strip().lower()
            feedback = str(body.get("feedback", "") or body.get("note", "")).strip()
        else:
            form = await request.form()
            action = (form.get("action", "") or "").strip().lower()
            feedback = (form.get("feedback", "") or form.get("note", "")).strip()
    except Exception:
        pass

    if action not in ("approve", "reject"):
        raise HTTPException(
            status_code=400,
            detail="action must be 'approve' or 'reject'",
        )

    pool = get_pool()

    # Verify the main task exists and is in this goal.
    main = await pool.fetchrow(
        "SELECT id, state, assigned_to FROM tasks WHERE id = $1 AND goal_id = $2",
        main_task_id, pid,
    )
    if not main:
        raise HTTPException(status_code=404, detail="Task not found")

    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN and main.get("assigned_to") != user["uid"]:
        raise HTTPException(status_code=403, detail="Admin approval required")

    # Find the active human-review subtask for this main task. Newest first
    # in case there's history.
    hr = await pool.fetchrow(
        """SELECT id FROM tasks
            WHERE review_target_id = $1
              AND template_id = 'human'
              AND status = 'active'
              AND state IN ('blocked', 'awaiting_approval')
            ORDER BY created_at DESC
            LIMIT 1""",
        main_task_id,
    )
    if not hr:
        raise HTTPException(
            status_code=404,
            detail="No active human-review subtask for this task",
        )

    from app.task_controller import complete_human_review
    res = await complete_human_review(
        review_task_id=hr["id"],
        action=action,
        feedback=feedback,
        actor_uid=user["uid"],
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "Review completion failed"))

    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse(res)
    return RedirectResponse(url=f"/goals/{pid}/tasks/{main_task_id}", status_code=303)


# -- Agent callback endpoints --------------------------------------------------

@router.post("/internal/tasks/{tid}/message")
async def agent_send_message(tid: str, request: Request):
    verify_internal_bearer(request)

    body = await request.json()
    pid = body.get("goal_id", "")
    content = body.get("content", "").strip()
    transition_to = body.get("transition_to")

    if not pid or not content:
        raise HTTPException(status_code=400, detail="goal_id and content are required")

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE goal_id = $1 AND id = $2", pid, tid,
    )
    if not row:
        raise HTTPException(status_code=404)

    task = dict(row)
    thread_id = task.get("thread_id")
    agent_id = task.get("assigned_to", "")
    task_corr_id = task.get("correlation_id")

    if not thread_id:
        raise HTTPException(status_code=400, detail="Task has no thread")

    msg_id = str(uuid.uuid4())
    agent_actor_id = await get_agent_actor_id(agent_id)
    agent_actor_id = agent_actor_id or SYSTEM_ACTOR_ID
    corr_id = await get_task_correlation_id(pool, tid) or str(task_corr_id or "")

    async with pool.acquire() as conn:
        async with conn.transaction():
            last_event_id = await _last_task_event_id(conn, tid)

            # MESSAGE_POSTED is already a spec type — keep using append_event directly
            msg_result = await append_event(
                conn,
                event_type=MESSAGE_POSTED,
                payload={"msg_id": msg_id, "role": "agent", "content": content, "sender": agent_id},
                actor_id=agent_actor_id,
                actor_type="independent_agent",
                correlation_id=corr_id,
                causation_ids=[last_event_id] if last_event_id else None,
                context_task_id=tid,
                context_goal_id=pid,
                context_conversation_id=thread_id,
            )
            await conn.execute(
                """INSERT INTO messages (id, thread_id, sender, role, content, token_count)
                   VALUES ($1, $2, $3, 'agent', $4, 0)""",
                msg_id, thread_id, agent_id, content,
            )

            # Store as task result so SHCA can see what this agent did
            await conn.execute(
                "UPDATE tasks SET result = $1 WHERE id = $2",
                content[:32000], tid,
            )

            if transition_to:
                current_state = task.get("state", "pending")
                if transition_to in VALID_TRANSITIONS.get(current_state, []):
                    msg_event_id = msg_result["event_id"] if msg_result else last_event_id
                    await emit_task_event(
                        conn, event_type=TASK_STATUS_CHANGED, task_id=tid, goal_id=pid,
                        payload=task_status_changed_payload(
                            previous_status=current_state, new_status=transition_to,
                            reason=f"Agent requested input: {content[:80]}",
                        ),
                        actor_id=agent_actor_id, actor_type="independent_agent",
                        correlation_id=corr_id, causation_ids=[msg_event_id] if msg_event_id else None,
                        summary=f"{current_state} → {transition_to}",
                    )
                    await conn.execute(
                        "UPDATE tasks SET state = $1 WHERE id = $2",
                        transition_to, tid,
                    )
                    await _append_task_event(conn, pid, tid, current_state, transition_to, agent_id,
                                             f"Agent requested input: {content[:80]}")

    creator_uid = task.get("created_by", "")
    if creator_uid:
        await push_notification(
            creator_uid,
            type="agent_question",
            title=f"{agent_id} needs your input",
            body=content[:120] + ("..." if len(content) > 120 else ""),
            goal_id=pid,
            agent_id=agent_id,
            task_id=tid,
            thread_id=thread_id,
            link=f"/goals/{pid}/tasks/{tid}",
        )

    # Trigger controller: state change may unblock dependents or need planner parsing
    if transition_to in ("done", "approved", "awaiting_approval"):
        asyncio.create_task(handle_task_completion(tid, content))

    return JSONResponse({"ok": True, "msg_id": msg_id, "thread_id": thread_id})


@router.post("/internal/tasks/{tid}/complete")
async def task_complete(tid: str, request: Request):
    """Called by the task_report tool when an agent finishes work.

    Body: { "status": "completed|failed|blocked", "result": "...", "metadata": {...} }
    """
    verify_internal_bearer(request)

    body = await request.json()
    status = body.get("status", "completed")
    result = body.get("result", "")
    report = body.get("report", {})  # Structured report from task_report tool
    # IA agents use "details"/"summary" fields, not "result". Populate result so
    # review tasks and sibling dispatch context have the actual work output.
    if not result and isinstance(report, dict):
        result = report.get("details", "") or report.get("summary", "")
    # RA agents send review/details at the top level (not under report).
    # Serialize the full body as result so _parse_review_verdict can read review.verdict.
    if not result and body.get("review"):
        result = json.dumps(body)

    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", tid)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task = dict(row)
    summary = report.get("summary", result[:200]) if report else result[:200]

    # Guard against re-entrant calls (agent retries on 500)
    if task["state"] in ("done", "approved", "awaiting_approval", "cancelled"):
        _log.info("Task %s already in terminal state %s — ignoring duplicate complete", tid, task["state"])
        return JSONResponse({
            "ok": True,
            "task_id": tid,
            "status": task["state"],
            "summary": "Already completed (duplicate call)",
        })

    goal_id = task["goal_id"]
    agent_actor = _actor_uuid(instance_id=task.get("instance_id", ""))
    corr_id = await get_task_correlation_id(pool, tid)
    last_evt = await get_last_event_id(pool, tid)

    # Resolve agent display name for cards
    template_id = task.get("template_id") or task.get("assigned_to") or "agent"
    agent_name_row = await pool.fetchrow(
        "SELECT name FROM agents WHERE id = $1", template_id,
    )
    agent_name = agent_name_row["name"] if agent_name_row else template_id

    if status == "completed":
        _log.info("Task %s completed: %s", tid, summary)

        # For planner tasks: fetch .plan/PLAN.md from the repo and store it
        if "planner" in template_id.lower() and isinstance(report, dict):
            repo_name = report.get("repo", "")
            branch = report.get("branch", "")
            files_changed = report.get("files_changed", [])
            _log.info(
                "Planner report for %s: repo=%s branch=%s files_changed=%s summary=%s",
                tid, repo_name or "(empty)", branch or "(empty)",
                files_changed, summary[:200],
            )

            # Candidate path order: whatever the planner self-reported in
            # files_changed first (most accurate), then the namespaced
            # PLAN.md resolved from the task chain (new convention), then
            # legacy flat paths for deep back-compat. See
            # app/plan_paths.py for the resolution logic.
            from app.plan_paths import candidate_plan_paths
            namespaced_candidates = await candidate_plan_paths(tid)
            candidate_paths: list[str] = []
            for f in (files_changed or []):
                if f.upper().endswith(".MD") and ("PLAN" in f.upper() or "CLAUDE" in f.upper()):
                    if f not in candidate_paths:
                        candidate_paths.append(f)
            for p in namespaced_candidates:
                if p not in candidate_paths:
                    candidate_paths.append(p)

            if repo_name and branch:
                try:
                    from app.github_files import fetch_file_from_repo
                    plan_content = None
                    tried_paths: list[str] = []
                    for path in candidate_paths:
                        tried_paths.append(path)
                        plan_content = await fetch_file_from_repo(repo_name, path, branch)
                        if plan_content and plan_content.strip():
                            break
                        plan_content = None

                    if plan_content:
                        await pool.execute(
                            "UPDATE tasks SET plan_content = $1 WHERE id = $2",
                            plan_content, tid,
                        )
                        _log.info("Stored plan (%d chars) for task %s from %s/%s (tried: %s)",
                                  len(plan_content), tid, repo_name, branch[:30], tried_paths)
                    else:
                        _log.warning(
                            "Could not fetch plan from %s/%s — tried %s — plan details will be missing from approval card",
                            repo_name, branch[:30], tried_paths,
                        )
                except Exception as exc:
                    _log.warning("Failed to fetch plan for task %s: %s", tid, exc)
            else:
                missing = []
                if not repo_name:
                    missing.append("repo")
                if not branch:
                    missing.append("branch")
                _log.warning(
                    "Planner %s report missing %s — cannot fetch plan file. Report keys: %s",
                    tid, "+".join(missing), list(report.keys()),
                )

        # RA review task: forward the full structured `report` so the verdict
        # parser sees `report["verdict"]` (approve / request_changes / block)
        # directly. Without this, only the summary text reaches
        # _parse_review_verdict, which then falls back to a fragile keyword
        # scan (e.g. "no blocking issues" matches BLOCK → wrong verdict →
        # parent flips to rejected). Scoped to review tasks owned by a
        # non-human assignee, so planner/IA/SHCA paths are untouched.
        completion_payload = result
        if task.get("review_target_id") and (task.get("template_id") or "") != "human" \
           and isinstance(report, dict) and report:
            completion_payload = json.dumps(report)

        try:
            await handle_task_completion(tid, completion_payload)
        except Exception as exc:
            _log.error("handle_task_completion failed for %s: %s", tid, exc, exc_info=True)
            return JSONResponse({
                "ok": False,
                "task_id": tid,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }, status_code=500)

        # Post completion card to SHCA thread (with token usage if available)
        artifacts = extract_artifacts_from_report(report) if isinstance(report, dict) else []

        # Look up token usage from the agent.completed lifecycle event.
        # task_activity stores structured event data in the `metadata` column
        # (see migrations/004_task_controller.sql).
        usage_line = ""
        try:
            usage_row = await pool.fetchrow("""
                SELECT metadata FROM task_activity
                WHERE task_id = $1 AND type = 'TASK_PROGRESS_UPDATED'
                  AND (metadata::jsonb->>'usage') IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
            """, tid)
            if usage_row:
                payload = json.loads(usage_row["metadata"]) if isinstance(usage_row["metadata"], str) else usage_row["metadata"]
                u = payload.get("usage", {})
                in_t = u.get("input_tokens", 0)
                out_t = u.get("output_tokens", 0)
                dur = payload.get("duration_ms", 0)
                if in_t or out_t:
                    total = in_t + out_t
                    dur_s = f"{dur / 1000:.0f}s" if dur else "?"
                    cost = u.get("cost_usd", 0)
                    cost_str = f" | Cost: ${cost:.4f}" if cost else ""
                    cache_r = u.get("cache_read_tokens", 0)
                    cache_str = f" | Cache: {cache_r:,}" if cache_r else ""
                    turns = u.get("num_turns", 0)
                    turns_str = f" | Turns: {turns}" if turns else ""
                    usage_line = f"\n\nTokens: {in_t:,} in + {out_t:,} out = {total:,}{cache_str}{cost_str}{turns_str} | Duration: {dur_s}"
        except Exception:
            pass

        card = completion_card(
            task_id=tid, agent_template=template_id, agent_name=agent_name,
            title=f"{agent_name} completed",
            body=summary + usage_line,
            artifacts=artifacts,
            context=(report.get("details") or "")[:32000] if isinstance(report, dict) else "",
        )
        await post_card_to_shca_thread(
            task=task, card=card, content_markdown=card_to_markdown(card),
            sender=template_id,
            notify_type="task_completed",
            notify_title=f"{agent_name} completed task {tid}",
        )

    elif status == "failed":
        _log.info("Task %s failed: %s", tid, summary)
        await pool.execute(
            "UPDATE tasks SET state = 'failed', result = $1 WHERE id = $2",
            result[:32000], tid,
        )
        cascade = await find_dependent_tasks(pool, goal_id, tid)
        artifacts = extract_artifacts_from_report(report) if isinstance(report, dict) else []
        await emit_task_event(
            pool, event_type=TASK_FAILED, task_id=tid, goal_id=goal_id,
            payload=task_failed_payload(
                failure_reason=summary, partial_output=artifacts,
                recovery_suggestion=report.get("recovery_suggestion", "") if isinstance(report, dict) else "",
                cascade_impact=cascade,
            ),
            actor_id=agent_actor, actor_type="independent_agent",
            correlation_id=corr_id, causation_ids=[last_evt] if last_evt else None,
            summary=summary, content=result,
            agent_id=template_id, instance_id=task.get("instance_id", ""),
        )

        # Post failure card to SHCA thread
        card = failure_card(
            task_id=tid, agent_template=template_id, agent_name=agent_name,
            title=f"{agent_name} failed",
            body=summary,
            context=(report.get("details") or "")[:32000] if isinstance(report, dict) else result[:32000],
        )
        await post_card_to_shca_thread(
            task=task, card=card, content_markdown=card_to_markdown(card),
            sender=template_id,
            notify_type="task_failed",
            notify_title=f"{agent_name} failed on task {tid}",
        )

    elif status == "blocked":
        requests_list = report.get("requests", []) if isinstance(report, dict) else []
        _log.info("Task %s blocked: %s (%d requests)", tid, summary, len(requests_list))

        await pool.execute(
            "UPDATE tasks SET state = 'blocked', blocked_reason = $1, result = $2 WHERE id = $3",
            summary, result[:32000], tid,
        )

        # Emit QUESTION_RAISED (spec: comms.question.raised)
        question_evt = await emit_task_event(
            pool, event_type=QUESTION_RAISED, task_id=tid, goal_id=goal_id,
            payload=question_raised_payload(
                question=summary, context=result[:32000],
                blocking_task=tid, requests=requests_list,
            ),
            actor_id=agent_actor, actor_type="independent_agent",
            correlation_id=corr_id, causation_ids=[last_evt] if last_evt else None,
            summary=summary, content=result,
            agent_id=template_id, instance_id=task.get("instance_id", ""),
        )
        # Emit TASK_STATUS_CHANGED in_progress → blocked
        q_evt_id = question_evt["event_id"] if question_evt else last_evt
        await emit_task_event(
            pool, event_type=TASK_STATUS_CHANGED, task_id=tid, goal_id=goal_id,
            payload=task_status_changed_payload(
                previous_status="in_progress", new_status="blocked",
                reason=f"Agent asked: {summary[:200]}",
            ),
            actor_id=agent_actor, actor_type="independent_agent",
            correlation_id=corr_id, causation_ids=[q_evt_id] if q_evt_id else None,
            summary=f"Blocked: {summary[:100]}",
        )

        # Post question card to SHCA thread
        details = report.get("details", "") if isinstance(report, dict) else ""
        card = question_card(
            task_id=tid, agent_template=template_id, agent_name=agent_name,
            title=f"Question from {agent_name}",
            body=summary,
            context=details[:32000],
            requests=requests_list,
        )
        await post_card_to_shca_thread(
            task=task, card=card, content_markdown=card_to_markdown(card),
            sender=template_id,
            notify_type="agent_question",
            notify_title=f"{agent_name} needs your input",
        )

    return JSONResponse({
        "ok": True,
        "task_id": tid,
        "status": status,
        "summary": summary,
    })


@router.post("/internal/tasks/{tid}/message-shca")
async def agent_message_shca(tid: str, request: Request):
    """Non-blocking message from agent to SHCA thread.

    Called by the pwt_message_shca tool for progress updates, FYI messages,
    or questions that don't block the task.

    Body: { "content": "...", "type": "progress|question|fyi", "gateway_agent_id": "..." }
    """
    verify_internal_bearer(request)

    body = await request.json()
    content = body.get("content", "").strip()
    msg_type = body.get("type", "progress")

    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", tid)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task = dict(row)
    template_id = task.get("template_id") or task.get("assigned_to") or "agent"
    agent_name_row = await pool.fetchrow("SELECT name FROM agents WHERE id = $1", template_id)
    agent_name = agent_name_row["name"] if agent_name_row else template_id

    from app.cards import progress_card

    card = progress_card(
        task_id=tid, agent_template=template_id, agent_name=agent_name,
        title=f"{agent_name} — {msg_type}",
        body=content,
    )

    await post_card_to_shca_thread(
        task=task, card=card, content_markdown=card_to_markdown(card),
        sender=template_id,
    )

    return JSONResponse({"ok": True, "task_id": tid, "type": msg_type})


@router.post("/internal/agents/lifecycle")
async def agent_lifecycle_webhook(request: Request):
    """Webhook from OpenClaw gateway: CLI agent started, completed, failed, or timed out.

    Body: { event, session_key, agent_id, run_id, provider, model, timestamp,
            duration_ms?, exit_code?, error?, reason? }
    """
    verify_internal_bearer(request)

    body = await request.json()
    event = body.get("event", "")
    session_key = body.get("session_key", "")
    agent_id_gw = body.get("agent_id", "")
    run_id = body.get("run_id", "")
    duration_ms = body.get("duration_ms")
    error_msg = body.get("error", "")
    reason = body.get("reason", "")

    _log.info("Agent lifecycle: %s for %s (run=%s, duration=%s)",
              event, agent_id_gw, run_id[:12] if run_id else "?", duration_ms)

    # Resolve task from session_key: "agent:{instance_id}:task-{task_id}"
    # Validate the extracted segment is a UUID before querying — session keys
    # from non-IA agents (GO, SHCA) may contain ":task-" as part of the goal
    # slug (e.g. "agent:go-vsdns:task-vsdns") which is not a valid task UUID.
    pool = get_pool()
    task_id = ""
    if ":task-" in session_key:
        candidate = session_key.split(":task-")[-1]
        try:
            uuid.UUID(candidate)
            task_id = candidate
        except ValueError:
            pass  # not a UUID — not a task reference

    task = None
    if task_id:
        row = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
        if row:
            task = dict(row)
            _log.info(
                "Agent lifecycle: resolved task=%s state=%s agent=%s event=%s",
                task_id, task.get("state"), agent_id_gw, event,
            )
        else:
            _log.warning(
                "Agent lifecycle: task_id=%s from session_key not found in DB agent=%s event=%s",
                task_id, agent_id_gw, event,
            )
    else:
        _log.debug(
            "Agent lifecycle: no task_id in session_key=%s agent=%s event=%s — non-IA agent (GO/SHCA)",
            session_key[:40] if session_key else "?", agent_id_gw, event,
        )

    # Emit ledger events based on lifecycle event type
    if task:
        goal_id = task["goal_id"]
        corr_id = await get_task_correlation_id(pool, task_id)
        last_evt = await get_last_event_id(pool, task_id)
        instance_id = task.get("instance_id", "")

        if event == "agent.started":
            await emit_task_event(
                pool,
                event_type=TASK_STATUS_CHANGED,
                task_id=task_id, goal_id=goal_id,
                payload=task_status_changed_payload(
                    previous_status=task.get("state", "dispatching"),
                    new_status="in_progress",
                    reason=f"CLI agent process started (run={run_id[:12]})",
                ),
                actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary=f"Agent process started",
                instance_id=instance_id,
            )
            # Post progress card to SHCA thread
            from app.cards import progress_card
            agent_name_row = await pool.fetchrow(
                "SELECT name FROM agents WHERE id = $1", task.get("template_id", ""),
            )
            agent_name = agent_name_row["name"] if agent_name_row else agent_id_gw
            card = progress_card(
                task_id=task_id, agent_template=task.get("template_id", ""),
                agent_name=agent_name,
                title=f"{agent_name} started working",
                body=f"CLI process spawned — provider: {body.get('provider', '?')}, model: {body.get('model', '?')}",
            )
            await post_card_to_shca_thread(
                task=task, card=card, content_markdown=card_to_markdown(card),
                sender=task.get("template_id", "system"),
            )

        elif event == "agent.failed" or event == "agent.timeout":
            event_label = "timed out" if event == "agent.timeout" else "crashed"
            await emit_task_event(
                pool,
                event_type=TASK_FAILED,
                task_id=task_id, goal_id=goal_id,
                payload=task_failed_payload(
                    failure_reason=f"Agent {event_label}: {error_msg or reason or 'unknown'}",
                    recovery_suggestion="Check gateway logs. Task may auto-retry if retries remain.",
                ),
                actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary=f"Agent {event_label} (exit={body.get('exit_code', '?')}, {duration_ms}ms)",
                instance_id=instance_id,
            )

            # Post failure card to SHCA thread
            from app.cards import failure_card
            agent_name_row = await pool.fetchrow(
                "SELECT name FROM agents WHERE id = $1", task.get("template_id", ""),
            )
            agent_name = agent_name_row["name"] if agent_name_row else agent_id_gw
            card = failure_card(
                task_id=task_id, agent_template=task.get("template_id", ""),
                agent_name=agent_name,
                title=f"{agent_name} {event_label}",
                body=error_msg[:500] if error_msg else f"Process {event_label} after {duration_ms}ms",
                context=f"Exit code: {body.get('exit_code', '?')}\nReason: {reason}\nRun ID: {run_id}",
            )
            await post_card_to_shca_thread(
                task=task, card=card, content_markdown=card_to_markdown(card),
                sender=task.get("template_id", "system"),
                notify_type="agent_failed",
                notify_title=f"{agent_name} {event_label}",
            )

        elif event == "agent.completed":
            usage = body.get("usage")
            usage_summary = ""
            if usage:
                input_t = usage.get("input_tokens", 0)
                output_t = usage.get("output_tokens", 0)
                cache_read = usage.get("cache_read_tokens", 0)
                cache_write = usage.get("cache_write_tokens", 0)
                cost_usd = usage.get("cost_usd", 0)
                num_turns = usage.get("num_turns", 0)
                total_t = input_t + output_t

                cost_str = f" | cost: ${cost_usd:.4f}" if cost_usd else ""
                turns_str = f" | turns: {num_turns}" if num_turns else ""
                usage_summary = f" | tokens: {input_t:,}in + {output_t:,}out = {total_t:,} (cache_read={cache_read:,}){cost_str}{turns_str}"
                _log.info(
                    "Agent completed for task %s (%dms)%s",
                    task_id, duration_ms or 0, usage_summary,
                )

                # Store token usage in task_activity
                await emit_task_event(
                    pool,
                    event_type=TASK_PROGRESS_UPDATED,
                    task_id=task_id, goal_id=goal_id,
                    payload={
                        "summary": f"CLI completed ({duration_ms}ms){usage_summary}",
                        "usage": usage,
                        "duration_ms": duration_ms,
                        "session_id": body.get("session_id", ""),
                    },
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                    correlation_id=corr_id,
                    causation_ids=[last_evt] if last_evt else None,
                    summary=f"Agent completed ({duration_ms}ms){usage_summary}",
                    instance_id=instance_id,
                )

                # Update agent_instances with cumulative token count
                try:
                    await pool.execute("""
                        UPDATE agent_instances
                        SET total_tokens = COALESCE(total_tokens, '{}'::jsonb) || $1::jsonb
                        WHERE gateway_agent_id = $2 AND status = 'active'
                    """, json.dumps({
                        f"phase_{task_id[:8]}": {
                            "input": input_t, "output": output_t,
                            "cache_read": cache_read, "cache_write": cache_write,
                            "duration_ms": duration_ms,
                        }
                    }), instance_id)
                except Exception as exc:
                    _log.warning("Failed to update token usage on instance: %s", exc)

                # Post a usage metrics card to the SHCA thread
                try:
                    from app.cards import progress_card
                    agent_name_row = await pool.fetchrow(
                        "SELECT name FROM agents WHERE id = $1", task.get("template_id", ""),
                    )
                    agent_name = agent_name_row["name"] if agent_name_row else task.get("template_id", "agent")
                    dur_s = f"{duration_ms / 1000:.0f}s" if duration_ms else "?"
                    cost_str = f"${cost_usd:.4f}" if cost_usd else "n/a"

                    metrics_body = (
                        f"**Input:** {input_t:,} tokens | **Output:** {output_t:,} tokens\n"
                        f"**Cache read:** {cache_read:,} tokens | **Cache write:** {cache_write:,} tokens\n"
                        f"**Cost:** {cost_str} | **Duration:** {dur_s}"
                    )
                    if num_turns:
                        metrics_body += f" | **Turns:** {num_turns}"

                    card = progress_card(
                        task_id=task_id,
                        agent_template=task.get("template_id", ""),
                        agent_name=agent_name,
                        title=f"Usage: {agent_name} — {cost_str}",
                        body=metrics_body,
                    )
                    await post_card_to_shca_thread(
                        task=task, card=card,
                        content_markdown=card_to_markdown(card),
                        sender=task.get("template_id", "system"),
                    )
                except Exception as exc:
                    _log.warning("Failed to post usage card: %s", exc)
            else:
                _log.info("Agent completed for task %s (%dms, no usage data)", task_id, duration_ms or 0)
            # Completion is handled by task-report, not the lifecycle webhook.
            # This event confirms the process exited cleanly + stores token usage.

    # Persist token usage to token_calls (fire-and-forget, never blocks)
    from app.routers.agent_lifecycle import record_token_usage
    await record_token_usage(body)

    return JSONResponse({"ok": True, "event": event})


@router.get("/internal/tasks/{tid}/replies")
async def agent_get_replies(tid: str, request: Request, after_msg_id: str = ""):
    verify_internal_bearer(request)

    goal_id = request.query_params.get("goal_id", "")
    if not goal_id:
        raise HTTPException(status_code=400, detail="goal_id query param required")

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE goal_id = $1 AND id = $2", goal_id, tid,
    )
    if not row:
        raise HTTPException(status_code=404)

    thread_id = row["thread_id"]
    if not thread_id:
        return JSONResponse({"replies": []})

    msg_rows = await pool.fetch(
        """SELECT * FROM messages
           WHERE thread_id = $1 AND role = 'human'
           ORDER BY created_at""",
        thread_id,
    )
    all_msgs = [dict(r) for r in msg_rows]

    if after_msg_id:
        ids = [m["id"] for m in all_msgs]
        if after_msg_id in ids:
            cut = ids.index(after_msg_id) + 1
            all_msgs = all_msgs[cut:]

    safe = []
    for m in all_msgs:
        safe.append({
            "id": m["id"],
            "content": m.get("content", ""),
            "role": m.get("role", "human"),
            "timestamp": str(m.get("created_at", "")),
        })

    return JSONResponse({"replies": safe})


@router.post("/internal/tasks/{tid}/agent-update")
async def agent_task_update(tid: str, request: Request):
    verify_internal_bearer(request)

    body = await request.json()
    pid = body.get("goal_id", "")
    new_state = body.get("state", "")
    note = body.get("note", "")
    result = body.get("result", "")

    if not pid or not new_state:
        raise HTTPException(status_code=400, detail="goal_id and state are required")

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM tasks WHERE goal_id = $1 AND id = $2", pid, tid,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")

    task = dict(row)
    current_state = task.get("state", "pending")
    agent_id = task.get("assigned_to", "")
    task_corr_id = task.get("correlation_id")

    if new_state not in VALID_TRANSITIONS.get(current_state, []):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition: {current_state} -> {new_state}",
        )

    agent_actor_id = await get_agent_actor_id(agent_id)
    agent_actor_id = agent_actor_id or SYSTEM_ACTOR_ID
    corr_id = await get_task_correlation_id(pool, tid) or str(task_corr_id or "")
    last_event_id = await get_last_event_id(pool, tid)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Route to specific event types for terminal states
            if new_state == "done":
                await emit_task_event(
                    conn, event_type=TASK_COMPLETED, task_id=tid, goal_id=pid,
                    payload=task_completed_payload(outcome_summary=note or result[:32000]),
                    actor_id=agent_actor_id, actor_type="independent_agent",
                    correlation_id=corr_id, causation_ids=[last_event_id] if last_event_id else None,
                    summary=f"Completed: {note or ''}", content=result,
                    agent_id=agent_id, instance_id=task.get("instance_id", ""),
                )
            elif new_state == "failed":
                cascade = await find_dependent_tasks(pool, pid, tid)
                await emit_task_event(
                    conn, event_type=TASK_FAILED, task_id=tid, goal_id=pid,
                    payload=task_failed_payload(
                        failure_reason=note or "Agent reported failure",
                        cascade_impact=cascade,
                    ),
                    actor_id=agent_actor_id, actor_type="independent_agent",
                    correlation_id=corr_id, causation_ids=[last_event_id] if last_event_id else None,
                    summary=f"Failed: {note or ''}", content=result,
                    agent_id=agent_id, instance_id=task.get("instance_id", ""),
                )
            else:
                await emit_task_event(
                    conn, event_type=TASK_STATUS_CHANGED, task_id=tid, goal_id=pid,
                    payload=task_status_changed_payload(
                        previous_status=current_state, new_status=new_state, reason=note,
                    ),
                    actor_id=agent_actor_id, actor_type="independent_agent",
                    correlation_id=corr_id, causation_ids=[last_event_id] if last_event_id else None,
                    summary=f"{current_state} → {new_state}: {note}",
                    agent_id=agent_id, instance_id=task.get("instance_id", ""),
                )

            update_fields = ["state = $1"]
            params: list = [new_state]
            idx = 2
            if result:
                update_fields.append(f"result = ${idx}")
                params.append(result)
                idx += 1

            params.append(tid)
            await conn.execute(
                f"UPDATE tasks SET {', '.join(update_fields)} WHERE id = ${idx}",
                *params,
            )
            await _append_task_event(conn, pid, tid, current_state, new_state, agent_id, note)

    creator_uid = task.get("created_by", "")
    if creator_uid:
        state_labels = {
            "done": "completed",
            "failed": "failed",
            "awaiting_approval": "is awaiting your approval",
            "in_progress": "started",
        }
        label = state_labels.get(new_state, f"moved to {new_state}")
        await push_notification(
            creator_uid,
            type="task_update",
            title=f"Task {label}: {task.get('title', tid)}",
            body=note or result or "",
            goal_id=pid,
            agent_id=agent_id,
            task_id=tid,
            thread_id=task.get("thread_id", ""),
            link=f"/goals/{pid}/tasks/{tid}",
        )

    # Notify GO agent
    try:
        from app.services.go_events import on_task_state_changed
        await on_task_state_changed(pid, tid, current_state, new_state, agent_id)
    except Exception as exc:
        _log.warning("GO event notification failed for task %s: %s", tid, exc)

    return JSONResponse({"ok": True, "task_id": tid, "state": new_state})


# -- helpers -------------------------------------------------------------------

async def _check_parent_completion(pool, goal_id: str, task_id: str, actor_uid: str):
    """Check if all subtasks of the parent task are done. If so, notify the human."""
    task = await pool.fetchrow("SELECT parent_task_id FROM tasks WHERE id = $1", task_id)
    if not task or not task.get("parent_task_id"):
        return  # Not a subtask

    parent_id = task["parent_task_id"]

    # Count incomplete subtasks
    incomplete = await pool.fetchval("""
        SELECT COUNT(*) FROM tasks
        WHERE parent_task_id = $1 AND status = 'active'
          AND state NOT IN ('done', 'approved', 'cancelled')
    """, parent_id)

    if incomplete == 0:
        # All subtasks done — notify via GO and push notification
        parent = await pool.fetchrow("SELECT title, assigned_to, goal_id FROM tasks WHERE id = $1", parent_id)
        if parent:
            _log.info("All subtasks of %s (%s) are complete", parent_id, parent["title"])

            # Notify the assignee
            if parent.get("assigned_to"):
                from app.notifications import push_notification
                await push_notification(
                    parent["assigned_to"],
                    type="task_update",
                    title=f"All subtasks complete: {parent['title']}",
                    body="All subtasks are done. You can now mark the main task as complete.",
                    goal_id=goal_id,
                    task_id=parent_id,
                    link=f"/goals/{goal_id}/tasks/{parent_id}/human",
                )

            # Notify GO
            try:
                from app.services.go_events import on_task_state_changed
                await on_task_state_changed(
                    goal_id, parent_id, "in_progress", "subtasks_complete", actor_uid,
                )
            except Exception:
                pass


async def _append_task_event(conn, pid: str, tid: str, from_state: str, to_state: str,
                              actor: str, note: str = ""):
    """Update the task_events projection table."""
    event_id = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO task_events (id, task_id, goal_id, from_state, to_state, actor, note)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        event_id, tid, pid, from_state, to_state, actor, note,
    )


# ── Internal API: Task spec creation (for SHCA / automation) ─────────────────

@router.post("/internal/goals/{pid}/task-spec")
async def create_task_spec(pid: str, request: Request):
    """Create a task. Used by SHCA.

    Simple mode (SHCA sends just a goal):
    {
        "title": "Build COGS calculator",
        "description": "..."
    }
    → Creates a single task, controller auto-assigns to planner-ia.

    Advanced mode (with explicit subtasks):
    {
        "title": "Build COGS calculator",
        "description": "...",
        "subtasks": [...]
    }
    → Creates parent task + subtasks.
    """
    verify_internal_bearer(request)

    body = await request.json()
    pid = await resolve_goal_id(pid)
    title = body.get("title", "")
    description = body.get("description", "")
    raw_source_thread_id = body.get("source_thread_id", "")
    gateway_agent_id = body.get("gateway_agent_id", "")
    subtasks = body.get("subtasks", [])
    requested_resources = body.get("resources", [])  # SHCA-specified resources
    explicit_parent_task_id = body.get("parent_task_id", "")

    # Full payload logging — every create_task invocation gets logged
    # here so we can see exactly what the SHCA sent when things go wrong.
    _log.info(
        "create_task IN: goal=%s gw_agent=%s source_thread=%s parent=%s "
        "title=%r description_len=%d resources=%s subtasks=%d",
        pid, gateway_agent_id or "-",
        (raw_source_thread_id or "-")[:12],
        explicit_parent_task_id or "-",
        title[:120],
        len(description),
        requested_resources,
        len(subtasks) if subtasks else 0,
    )
    # Full description at DEBUG level (can be huge)
    _log.debug("create_task IN description: %s", description)

    if not title:
        _log.warning(
            "create_task REJECTED (goal=%s gw_agent=%s): title missing; body keys=%s",
            pid, gateway_agent_id or "-", list(body.keys()),
        )
        raise HTTPException(status_code=400, detail="title is required")

    pool = get_pool()

    # Validate source_thread_id — must exist in threads table.
    # The tool extracts it from the session key, which may contain a gateway
    # session timestamp instead of a thread UUID.
    source_thread_id = ""
    if raw_source_thread_id:
        exists = await pool.fetchval(
            "SELECT id FROM threads WHERE id = $1", raw_source_thread_id,
        )
        if exists:
            source_thread_id = raw_source_thread_id
        else:
            _log.warning("source_thread_id %s not found in threads — resolving from SHCA agent", raw_source_thread_id[:20])

    # Fallback: find the SHCA agent's most recent direct chat thread for this goal
    if not source_thread_id and gateway_agent_id:
        source_thread_id = await pool.fetchval(
            """SELECT t.id FROM threads t
               JOIN agents a ON a.id = t.agent_id
               WHERE t.goal_id = $1 AND a.gateway_agent_id = $2
               AND t.task_id IS NULL
               ORDER BY t.created_at DESC LIMIT 1""",
            pid, gateway_agent_id,
        ) or ""
        if source_thread_id:
            _log.info("Resolved source_thread_id=%s from gateway agent %s", source_thread_id[:12], gateway_agent_id)

    # Resolve the SHCA agent's real actor_id.
    # Primary: gateway_agent_id sent directly by the tool → agents.gateway_agent_id
    # Fallback: source_thread_id → threads.agent_id → agents table
    shca_actor_id = SYSTEM_ACTOR_ID
    shca_display_name = "shca"
    shca_row = None
    if gateway_agent_id:
        shca_row = await pool.fetchrow(
            "SELECT actor_id, id, name FROM agents WHERE gateway_agent_id = $1 AND status = 'active'",
            gateway_agent_id,
        )
    if not shca_row and source_thread_id:
        shca_row = await pool.fetchrow(
            """SELECT a.actor_id, a.id, a.name FROM agents a
               JOIN threads t ON t.agent_id = a.id
               WHERE t.id = $1""",
            source_thread_id,
        )
    if shca_row:
        shca_actor_id = shca_row["actor_id"]
        shca_display_name = shca_row["name"] or shca_row["id"]

    # Resolve story_id and parent_task_id
    explicit_story_id = body.get("story_id", "")
    parent_task_id = explicit_parent_task_id
    story_id = explicit_story_id

    # If parent_task_id points to a story (not a task), move it to story_id
    if parent_task_id and not story_id:
        is_story = await pool.fetchval(
            "SELECT 1 FROM stories WHERE id = $1", parent_task_id)
        if is_story:
            story_id = parent_task_id
            parent_task_id = ""

    # Auto-detect story from source thread if not explicitly set
    if not story_id and source_thread_id:
        story_row = await pool.fetchrow("""
            SELECT s.id FROM stories s
            JOIN messages m ON m.thread_id = $1
            WHERE s.goal_id = $2
            AND s.state IN ('in_progress', 'accepted', 'awaiting_merge')
            AND m.content LIKE '%Story Mode Active%'
            LIMIT 1
        """, source_thread_id, pid)
        if story_row:
            story_id = story_row["id"]
            _log.info("Auto-linked task to story %s from thread %s", story_id, source_thread_id[:12])

    task_id = str(uuid.uuid4())[:8]
    thread_id = str(uuid.uuid4())

    # ── Step 1: Emit DECISION_RECORDED (anchors correlation_id for everything) ──
    decision_evt = await emit_task_event(
        pool, event_type=DECISION_RECORDED, task_id="", goal_id=pid,
        payload=decision_recorded_payload(
            description=f"Creating task: {title}",
            reasoning="Task spec received from SHCA",
        ),
        actor_id=shca_actor_id, actor_type="shca",
        summary=f"Decision: create task '{title}'",
    )
    decision_event_id = decision_evt["event_id"] if decision_evt else str(uuid.uuid4())

    # Simple mode: no subtasks → single task, controller auto-assigns to planner
    if not subtasks:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO threads (id, goal_id, agent_id, task_id, created_by, title)
                       VALUES ($1, $2, '', $3, 'system', $4)""",
                    thread_id, pid, task_id, title,
                )
                await conn.execute(
                    """INSERT INTO tasks
                           (id, goal_id, title, description, state, created_by,
                            thread_id, correlation_id, source_thread_id, decision_event_id,
                            parent_task_id, story_id)
                       VALUES ($1, $2, $3, $4, 'pending', 'system', $5, $6, $7, $8, $9, $10)""",
                    task_id, pid, title, description,
                    thread_id, decision_event_id, source_thread_id, decision_event_id,
                    parent_task_id or None, story_id or "",
                )

                await emit_task_event(
                    conn, event_type=TASK_CREATED, task_id=task_id, goal_id=pid,
                    payload=task_created_payload(
                        title=title, description=description,
                        requester=actor_ref(shca_actor_id, "shca", shca_display_name),
                    ),
                    actor_id=shca_actor_id, actor_type="shca",
                    correlation_id=decision_event_id, causation_ids=[decision_event_id],
                    summary=f"Task created: {title}",
                )

        # Auto-populate task resources from goal's active resources
        await _auto_populate_task_resources(
            pool, task_id, pid, shca_actor_id, decision_event_id,
            requested_resources=requested_resources or None,
        )

        await task_controller.trigger()

        _log.info(
            "create_task OUT (simple): goal=%s task_id=%s story_id=%s parent=%s",
            pid, task_id, story_id or "-", parent_task_id or "-",
        )
        return JSONResponse({
            "ok": True,
            "task_id": task_id,
            "decision_event_id": decision_event_id,
            "mode": "simple",
        })

    # Advanced mode: parent + subtasks
    parent_id = task_id
    parent_thread_id = thread_id

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO threads (id, goal_id, agent_id, task_id, created_by, title)
                   VALUES ($1, $2, '', $3, 'system', $4)""",
                parent_thread_id, pid, parent_id, title,
            )
            await conn.execute(
                """INSERT INTO tasks
                       (id, goal_id, title, description, state, created_by,
                        thread_id, correlation_id, source_thread_id, decision_event_id)
                   VALUES ($1, $2, $3, $4, 'pending', 'system', $5, $6, $7, $8)""",
                parent_id, pid, title, description,
                parent_thread_id, decision_event_id, source_thread_id, decision_event_id,
            )

            # Emit TASK_CREATED for parent
            parent_evt = await emit_task_event(
                conn, event_type=TASK_CREATED, task_id=parent_id, goal_id=pid,
                payload=task_created_payload(
                    title=title, description=description,
                    requester=actor_ref(shca_actor_id, "shca", shca_display_name),
                ),
                actor_id=shca_actor_id, actor_type="shca",
                correlation_id=decision_event_id, causation_ids=[decision_event_id],
                summary=f"Task created: {title}",
            )
            parent_evt_id = parent_evt["event_id"] if parent_evt else None

            # Create subtasks
            subtask_ids = []
            for i, st in enumerate(subtasks):
                st_id = f"{parent_id}-{i+1}"
                st_thread_id = str(uuid.uuid4())
                st_title = st.get("title", f"Subtask {i+1}")
                st_template = st.get("template_id", "")

                raw_deps = st.get("depends_on", [])
                resolved_deps = []
                for dep in raw_deps:
                    if isinstance(dep, int) and 0 <= dep < len(subtask_ids):
                        resolved_deps.append(subtask_ids[dep])
                    elif isinstance(dep, str):
                        resolved_deps.append(dep)

                await conn.execute(
                    """INSERT INTO threads (id, goal_id, agent_id, task_id, created_by, title)
                       VALUES ($1, $2, $3, $4, 'system', $5)""",
                    st_thread_id, pid, st_template, st_id, st_title,
                )

                await conn.execute(
                    """INSERT INTO tasks
                           (id, goal_id, title, description, assigned_to, template_id,
                            priority, depends_on, state, created_by, thread_id,
                            correlation_id, parent_task_id, acceptance_criteria,
                            source_thread_id, decision_event_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending','system',$9,$10,$11,$12,$13,$14)""",
                    st_id, pid, st_title, st.get("description", ""),
                    st_template, st_template,
                    st.get("priority", "medium"), resolved_deps,
                    st_thread_id, decision_event_id, parent_id,
                    st.get("acceptance_criteria", ""), source_thread_id,
                    decision_event_id,
                )

                assignee_ref = None
                if st_template:
                    assignee_ref = actor_ref(
                        _actor_uuid(agent_id=st_template),
                        "independent_agent" if st_template != "human" else "human",
                        st_template,
                    )

                st_created_evt = await emit_task_event(
                    conn, event_type=TASK_CREATED, task_id=st_id, goal_id=pid,
                    payload=task_created_payload(
                        title=st_title,
                        description=st.get("description", ""),
                        acceptance_criteria=st.get("acceptance_criteria", ""),
                        assignee=assignee_ref,
                        priority=st.get("priority", "medium"),
                        parent_task_id=parent_id,
                        dependencies=resolved_deps,
                    ),
                    actor_id=shca_actor_id, actor_type="shca",
                    correlation_id=decision_event_id,
                    causation_ids=[parent_evt_id] if parent_evt_id else [decision_event_id],
                    summary=f"Subtask created: {st_title}",
                    agent_id=st_template,
                )
                st_created_evt_id = st_created_evt["event_id"] if st_created_evt else None

                for dep_id in resolved_deps:
                    await emit_task_event(
                        conn, event_type=TASK_DEPENDENCY_ADDED, task_id=st_id, goal_id=pid,
                        payload=task_dependency_added_payload(depends_on=dep_id),
                        actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                        correlation_id=decision_event_id,
                        causation_ids=[st_created_evt_id] if st_created_evt_id else None,
                        summary=f"Dep: {st_id} blocked on {dep_id}",
                    )

                subtask_ids.append(st_id)

    # Auto-populate resources for parent + inherit to subtasks
    await _auto_populate_task_resources(
        pool, parent_id, pid, shca_actor_id, decision_event_id,
        requested_resources=requested_resources or None,
    )
    for st_id in subtask_ids:
        st_row = await pool.fetchrow("SELECT template_id FROM tasks WHERE id = $1", st_id)
        st_template = st_row["template_id"] if st_row else ""
        await _inherit_task_resources(
            pool, parent_id, st_id, pid, st_template, SYSTEM_ACTOR_ID, decision_event_id,
        )

    await task_controller.trigger()

    _log.info(
        "create_task OUT (advanced): goal=%s parent=%s subtasks=%d ids=%s",
        pid, parent_id, len(subtask_ids), subtask_ids,
    )
    return JSONResponse({
        "ok": True,
        "parent_task_id": parent_id,
        "subtask_ids": subtask_ids,
        "decision_event_id": decision_event_id,
    })


@router.get("/internal/goals/{pid}/task-activity/{tid}")
async def get_task_activity_api(pid: str, tid: str, request: Request):
    """Query task activity log. Used by SHCA to check what agents did."""
    verify_internal_bearer(request)

    from app.task_controller import get_task_activities
    activities = await get_task_activities(tid, limit=50)

    return JSONResponse({
        "task_id": tid,
        "activities": [
            {
                "type": a["type"],
                "summary": a["summary"],
                "content": a.get("content", ""),
                "agent_id": a.get("agent_id", ""),
                "instance_id": a.get("instance_id", ""),
                "metadata": a.get("metadata", {}),
                "created_at": str(a.get("created_at", "")),
            }
            for a in activities
        ],
    })


@router.get("/internal/goals/{pid}/task-dag")
async def get_task_dag(pid: str, request: Request):
    """Get the full task DAG for a goal. Used by SHCA for status reporting."""
    verify_internal_bearer(request)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT id, title, state, assigned_to, template_id, instance_id,
                  priority, depends_on, parent_task_id, result, blocked_reason,
                  acceptance_criteria, created_at
           FROM tasks
           WHERE goal_id = $1 AND status = 'active'
           ORDER BY created_at""",
        pid,
    )

    tasks = []
    for r in rows:
        t = dict(r)
        t["created_at"] = str(t.get("created_at", ""))
        tasks.append(t)

    return JSONResponse({"goal_id": pid, "tasks": tasks})
