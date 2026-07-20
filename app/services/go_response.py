"""GO response parser — extract and execute json:go-action blocks.

When the GO agent responds, its message may contain structured action blocks
mixed with natural language. This module:
1. Extracts all ```json:go-action blocks from the response
2. Validates each action
3. Executes them against the database (create task, update state, etc.)
4. Returns clean text (action blocks stripped) for display to users
"""
import json
import logging
import re
import uuid

from app.database import get_pool
from app.notifications import push_notification
from app.ledger.journal import append_event
from app.ledger.actors import go_actor
from app.ledger.schema import (
    GO_TASK_CREATED, GO_TASK_REASSIGNED, GOAL_STATE_DOC_UPDATED,
    TASK_CREATED, TASK_STATUS_CHANGED,
)
from app.services import go_memory

_log = logging.getLogger(__name__)

# Regex to find ```json:go-action ... ``` blocks
_ACTION_BLOCK_RE = re.compile(
    r"```json:go-action\s*\n(.*?)\n```",
    re.DOTALL,
)

VALID_ACTIONS = {
    # Story-level coordination — GO's domain
    "create_story",
    "update_story",
    "cancel_story",
    # Goal-level state
    "update_goal_state",
    "update_completion",
    # Resource locking
    "lock_resource",
    "unlock_resource",
    # Communication
    "notify_member",
    "mark_milestone",
    # NOTE: create_task / update_task / reassign_task are intentionally NOT
    # listed. Tasks are owned by the human's SHCA — GO never creates them.
    # Even via go-action blocks. SHCAs use create_task instead.
}

# Actions that the GO must NEVER perform — explicitly rejected with a clear
# log line so we can detect attempted contract violations after the fact.
_FORBIDDEN_ACTIONS = {
    "create_task": "Tasks are created by the human's SHCA, not the GO. Use reply_assistant to ask the assistant to create the task instead.",
    "update_task": "Task updates belong to the SHCA. Use reply_assistant to coordinate.",
    "reassign_task": "Task reassignment belongs to the SHCA. Use reply_assistant to coordinate.",
}


async def process_go_response(goal_id: str, response: str) -> str:
    """Extract go-action blocks from GO's response, execute them, return clean text.

    Args:
        goal_id: The goal this response belongs to
        response: Raw response text from GO (may contain action blocks)

    Returns:
        Clean text with action blocks stripped (for display to users)
    """
    # Extract action blocks
    actions = []
    rejected = []
    for match in _ACTION_BLOCK_RE.finditer(response):
        try:
            action = json.loads(match.group(1))
            if not isinstance(action, dict):
                _log.warning("Invalid go-action block: %s", match.group(1)[:200])
                continue
            action_type = action.get("action", "")
            if action_type in _FORBIDDEN_ACTIONS:
                reason = _FORBIDDEN_ACTIONS[action_type]
                _log.warning(
                    "REJECTED forbidden GO action '%s' for goal %s: %s",
                    action_type, goal_id, reason,
                )
                rejected.append({"action": action_type, "reason": reason})
                continue
            if action_type in VALID_ACTIONS:
                actions.append(action)
            else:
                _log.warning(
                    "Unknown go-action '%s' (block: %s)",
                    action_type, match.group(1)[:200],
                )
        except json.JSONDecodeError as exc:
            _log.warning("Malformed go-action JSON: %s — %s", match.group(1)[:200], exc)

    # Execute each action
    results = []
    for action in actions:
        try:
            result = await _execute_action(goal_id, action)
            results.append(result)
            _log.info("GO action executed: %s in goal %s — %s",
                       action["action"], goal_id, result)
        except Exception as exc:
            _log.error("GO action failed: %s in goal %s — %s",
                        action["action"], goal_id, exc)
            results.append({"action": action["action"], "error": str(exc)})

    # Strip action blocks from display text
    clean_text = _ACTION_BLOCK_RE.sub("", response).strip()

    # Remove empty lines left by block removal
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)

    if actions:
        _log.info("Processed %d GO actions for goal %s: %s",
                   len(actions), goal_id,
                   [a["action"] for a in actions])

    return clean_text


async def _execute_action(goal_id: str, action: dict) -> dict:
    """Execute a single GO action against the database."""
    action_type = action["action"]
    params = action.get("params", {})

    if action_type == "create_story":
        return await _action_create_story(goal_id, params)
    elif action_type == "update_story":
        return await _action_update_story(goal_id, params)
    elif action_type == "cancel_story":
        return await _action_cancel_story(goal_id, params)
    elif action_type == "create_task":
        return await _action_create_task(goal_id, params)
    elif action_type == "update_task":
        return await _action_update_task(goal_id, params)
    elif action_type == "reassign_task":
        return await _action_reassign_task(goal_id, params)
    elif action_type == "update_goal_state":
        return await _action_update_goal_state(goal_id, params)
    elif action_type == "update_completion":
        return await _action_update_completion(goal_id, params)
    elif action_type == "notify_member":
        return await _action_notify_member(goal_id, params)
    elif action_type == "lock_resource":
        return await _action_lock_resource(goal_id, params)
    elif action_type == "unlock_resource":
        return await _action_unlock_resource(goal_id, params)
    elif action_type == "mark_milestone":
        return await _action_mark_milestone(goal_id, params)
    else:
        return {"error": f"Unknown action: {action_type}"}


# ── Action implementations ──────────────────────────────────────────────────

async def _action_create_task(goal_id: str, params: dict) -> dict:
    """Create a task assigned to a human. Supports parent_task_id for subtasks."""
    title = params.get("title", "")
    description = params.get("description", "")
    assigned_to = params.get("assigned_to", "")
    priority = params.get("priority", "medium")
    depends_on = params.get("depends_on", [])
    parent_task_id = params.get("parent_task_id", "")
    approval_required = params.get("approval_required", False)

    if not title:
        return {"error": "Task title required"}

    pool = get_pool()
    task_id = str(uuid.uuid4())[:8]
    thread_id = str(uuid.uuid4())
    corr_id = str(uuid.uuid4())

    # Resolve GO agent's actor_id for ledger (system-typed fallback if missing).
    go_actor_id, go_actor_type = await go_actor(goal_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Ledger event
            await append_event(
                conn,
                event_type=GO_TASK_CREATED,
                payload={
                    "task_id": task_id,
                    "title": title,
                    "assigned_to": assigned_to,
                    "priority": priority,
                    "created_by": f"go-{goal_id}",
                },
                actor_id=go_actor_id,
                actor_type=go_actor_type,
                correlation_id=corr_id,
                context_goal_id=goal_id,
                context_task_id=task_id,
            )

            # Create thread for the task
            await conn.execute(
                """INSERT INTO threads (id, goal_id, task_id, created_by, title, correlation_id)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                thread_id, goal_id, task_id, f"go-{goal_id}",
                f"Task: {title[:80]}", corr_id,
            )

            # Create task
            await conn.execute("""
                INSERT INTO tasks (id, goal_id, title, description, assigned_to,
                                   priority, depends_on, approval_required, state,
                                   correlation_id, created_by, thread_id, category,
                                   parent_task_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9, $10, $11, 'task', $12)
            """, task_id, goal_id, title, description, assigned_to,
                priority, depends_on, approval_required,
                corr_id, f"go-{goal_id}", thread_id,
                parent_task_id or None)

            # Task event
            await conn.execute("""
                INSERT INTO task_events (id, task_id, goal_id, from_state, to_state, actor, note)
                VALUES ($1, $2, $3, 'pending', 'pending', $4, 'Created by Goal Orchestrator')
            """, str(uuid.uuid4()), task_id, goal_id, f"go-{goal_id}")

    # Notify assignee + record to memory
    assignee_name = ""
    if assigned_to:
        try:
            assignee_name = await pool.fetchval(
                "SELECT display_name FROM users WHERE uid = $1", assigned_to,
            ) or assigned_to
            await push_notification(
                assigned_to,
                type="task_assigned",
                title=f"New task: {title[:60]}",
                body=f"Assigned by Goal Orchestrator. Priority: {priority}",
                goal_id=goal_id,
                task_id=task_id,
                link=f"/goals/{goal_id}/tasks/{task_id}",
            )
        except Exception as exc:
            _log.warning("Failed to notify assignee %s: %s", assigned_to, exc)

    # Record to GO memory
    await go_memory.on_task_created(
        goal_id, task_id, title, assignee_name or assigned_to,
        priority, f"go-{goal_id}")

    return {"task_id": task_id, "title": title, "assigned_to": assigned_to}


# ── Story actions ──────────────────────────────────────────────────────────

async def _action_create_story(goal_id: str, params: dict) -> dict:
    """Create a story in the stories table. State starts as 'proposed'."""
    title = params.get("title", "")
    description = params.get("description", "")
    assigned_to = params.get("assigned_to", "")
    priority = params.get("priority", "medium")
    raw_depends_on = params.get("depends_on", [])
    acceptance_criteria = params.get("acceptance_criteria", "")
    estimated_effort = params.get("estimated_effort", "")

    if not title:
        return {"error": "Story title required"}

    pool = get_pool()
    story_id = str(uuid.uuid4())[:8]

    # Resolve depends_on — GO may send story titles OR IDs
    depends_on = []
    for dep in raw_depends_on:
        if not dep:
            continue
        existing = await pool.fetchval(
            "SELECT id FROM stories WHERE id = $1 AND goal_id = $2", dep, goal_id,
        )
        if existing:
            depends_on.append(existing)
        else:
            by_title = await pool.fetchval(
                "SELECT id FROM stories WHERE title = $1 AND goal_id = $2 LIMIT 1", dep, goal_id,
            )
            if by_title:
                depends_on.append(by_title)
            else:
                _log.warning("Could not resolve dependency '%s' for story '%s'", dep, title)

    thread_id = str(uuid.uuid4())
    corr_id = str(uuid.uuid4())

    go_actor_id, go_actor_type = await go_actor(goal_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=GO_TASK_CREATED,
                payload={
                    "story_id": story_id, "title": title,
                    "assigned_to": assigned_to, "priority": priority,
                    "created_by": f"go-{goal_id}", "type": "story",
                },
                actor_id=go_actor_id, actor_type=go_actor_type,
                correlation_id=corr_id, context_goal_id=goal_id,
                context_task_id=story_id,
            )

            await conn.execute(
                """INSERT INTO threads (id, goal_id, created_by, title, correlation_id)
                   VALUES ($1, $2, $3, $4, $5)""",
                thread_id, goal_id, f"go-{goal_id}",
                f"Story: {title[:80]}", corr_id,
            )

            await conn.execute("""
                INSERT INTO stories (id, goal_id, title, description, assigned_to,
                                     priority, depends_on, state, thread_id,
                                     created_by, acceptance_criteria, estimated_effort)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'proposed', $8, $9, $10, $11)
            """, story_id, goal_id, title, description, assigned_to,
                priority, depends_on, thread_id, f"go-{goal_id}",
                acceptance_criteria, estimated_effort)

    # Notify assignee
    assignee_name = ""
    if assigned_to:
        try:
            assignee_name = await pool.fetchval(
                "SELECT display_name FROM users WHERE uid = $1", assigned_to,
            ) or assigned_to
            await push_notification(
                assigned_to, type="story_proposed",
                title=f"New story proposed: {title[:60]}",
                body="Proposed by Goal Orchestrator. Review and accept on the Story Board.",
                goal_id=goal_id, task_id=story_id,
                link=f"/goals/{goal_id}/stories/{story_id}",
            )
        except Exception as exc:
            _log.warning("Failed to notify assignee %s: %s", assigned_to, exc)

    await go_memory.on_task_created(
        goal_id, story_id, title, assignee_name or assigned_to,
        priority, f"go-{goal_id}")

    return {"story_id": story_id, "title": title, "assigned_to": assigned_to}


async def _action_update_story(goal_id: str, params: dict) -> dict:
    """Update a story's description, acceptance criteria, priority, deps, or assignee."""
    story_id = params.get("story_id", "")
    if not story_id:
        return {"error": "story_id required"}

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT * FROM stories WHERE id = $1 AND goal_id = $2", story_id, goal_id,
    )
    if not story:
        return {"error": f"Story {story_id} not found"}

    # Engine merge stories are owned by reconcile (depends_on/title/assignee) + the Git
    # Engine view — a GO edit here fights reconcile and corrupts the merge gate.
    if (story.get("story_kind") or "code") == "merge":
        return {"error": "Merge stories are managed by the Git Engine and can't be edited."}

    updates = ["updated_at = now()"]
    values = []
    idx = 1

    for field in ("description", "acceptance_criteria", "priority",
                  "estimated_effort", "assigned_to"):
        if field in params:
            updates.append(f"{field} = ${idx}")
            values.append(params[field])
            idx += 1

    if "depends_on" in params:
        updates.append(f"depends_on = ${idx}")
        values.append(params["depends_on"])
        idx += 1

    if len(updates) <= 1:  # only updated_at
        return {"error": "No fields to update"}

    values.append(story_id)
    values.append(goal_id)
    await pool.execute(
        f"UPDATE stories SET {', '.join(updates)} WHERE id = ${idx} AND goal_id = ${idx + 1}",
        *values,
    )

    changed_fields = [f for f in ("description", "acceptance_criteria", "priority",
                                   "depends_on", "assigned_to", "estimated_effort")
                      if f in params]
    await go_memory.record_memory(
        goal_id, category="plan_change",
        content=f"Updated story {story_id} ({story['title']}): changed {', '.join(changed_fields)}",
        actor=f"go-{goal_id}",
    )

    if "assigned_to" in params and params["assigned_to"] != story.get("assigned_to"):
        try:
            await push_notification(
                params["assigned_to"], type="story_reassigned",
                title=f"Story reassigned to you: {story['title'][:60]}",
                body="Review on the Story Board.",
                goal_id=goal_id, task_id=story_id,
                link=f"/goals/{goal_id}/stories/{story_id}",
            )
        except Exception:
            pass

    return {"story_id": story_id, "updated": changed_fields}


async def _action_cancel_story(goal_id: str, params: dict) -> dict:
    """Cancel a story with a reason."""
    story_id = params.get("story_id", "")
    reason = params.get("reason", "Cancelled by Goal Orchestrator")
    if not story_id:
        return {"error": "story_id required"}

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT * FROM stories WHERE id = $1 AND goal_id = $2", story_id, goal_id,
    )
    if not story:
        return {"error": f"Story {story_id} not found"}

    old_state = story["state"]
    # Clear active_thread_id on terminal transition (cancelled). Prevents
    # the SHCA cleanup watchdog from mis-matching this finished story
    # against the SHCA's persistent thread once a new story arrives.
    # A MERGE story cancelled by a human is a merge REJECTION ('rejected' keeps
    # dependents blocked for re-plan), not a torn-down workstream ('cancelled' would
    # wrongly unblock them — spec §12 / git_graph dep rule). Code stories stay 'cancelled'.
    await pool.execute(
        "UPDATE stories SET "
        "  state = CASE WHEN story_kind = 'merge' THEN 'rejected' ELSE 'cancelled' END, "
        "  result_summary = $1, active_thread_id = NULL, updated_at = now() WHERE id = $2",
        reason, story_id,
    )

    # Ledger event
    go_actor_id, go_actor_type = await go_actor(goal_id)

    async with pool.acquire() as conn:
        await append_event(
            conn, event_type=TASK_STATUS_CHANGED,
            payload={
                "task_id": story_id, "previous_status": old_state,
                "new_status": "cancelled", "reason": reason,
                "category": "story",
            },
            actor_id=go_actor_id, actor_type=go_actor_type,
            context_goal_id=goal_id, context_task_id=story_id,
        )

    # Release resource locks if any
    await pool.execute(
        "UPDATE goal_resources SET locked_by = NULL, locked_for = NULL, locked_at = NULL "
        "WHERE goal_id = $1 AND locked_for = $2",
        goal_id, story_id,
    )

    # Notify assignee
    if story.get("assigned_to"):
        try:
            await push_notification(
                story["assigned_to"], type="story_cancelled",
                title=f"Story cancelled: {story['title'][:60]}",
                body=reason[:120],
                goal_id=goal_id, task_id=story_id,
                link=f"/goals/{goal_id}/stories",
            )
        except Exception:
            pass

    await go_memory.record_memory(
        goal_id, category="plan_change",
        content=f"Cancelled story {story_id} ({story['title']}): {reason}",
        actor=f"go-{goal_id}",
    )

    return {"story_id": story_id, "cancelled": True, "reason": reason}


async def _action_update_task(goal_id: str, params: dict) -> dict:
    """Update a task's description, priority, or dependencies."""
    task_id = params.get("task_id", "")
    if not task_id:
        return {"error": "task_id required"}

    pool = get_pool()
    updates = []
    values = []
    idx = 1

    for field in ["description", "priority", "acceptance_criteria"]:
        if field in params:
            updates.append(f"{field} = ${idx}")
            values.append(params[field])
            idx += 1

    if "depends_on" in params:
        updates.append(f"depends_on = ${idx}")
        values.append(params["depends_on"])
        idx += 1

    if not updates:
        return {"error": "No fields to update"}

    values.append(task_id)
    values.append(goal_id)

    await pool.execute(
        f"UPDATE tasks SET {', '.join(updates)} WHERE id = ${idx} AND goal_id = ${idx + 1}",
        *values,
    )

    return {"task_id": task_id, "updated_fields": list(params.keys())}


async def _action_reassign_task(goal_id: str, params: dict) -> dict:
    """Reassign a task to a different human."""
    task_id = params.get("task_id", "")
    new_assigned_to = params.get("new_assigned_to", "")
    reason = params.get("reason", "")

    if not task_id or not new_assigned_to:
        return {"error": "task_id and new_assigned_to required"}

    pool = get_pool()

    # Get current assignee
    task = await pool.fetchrow(
        "SELECT assigned_to, title FROM tasks WHERE id = $1 AND goal_id = $2",
        task_id, goal_id,
    )
    if not task:
        return {"error": f"Task {task_id} not found"}

    old_assigned = task["assigned_to"]

    # Update
    await pool.execute(
        "UPDATE tasks SET assigned_to = $1 WHERE id = $2 AND goal_id = $3",
        new_assigned_to, task_id, goal_id,
    )

    # Ledger
    go_actor_id, go_actor_type = await go_actor(goal_id)
    if go_actor_id:
        await append_event(
            pool,
            event_type=GO_TASK_REASSIGNED,
            payload={
                "task_id": task_id,
                "old_assigned_to": old_assigned,
                "new_assigned_to": new_assigned_to,
                "reason": reason,
            },
            actor_id=go_actor_id,
            actor_type=go_actor_type,
            context_goal_id=goal_id,
            context_task_id=task_id,
        )

    # Notify new assignee
    try:
        await push_notification(
            new_assigned_to,
            type="task_assigned",
            title=f"Task reassigned: {task['title'][:60]}",
            body=reason[:120] if reason else "Reassigned by Goal Orchestrator",
            goal_id=goal_id,
            task_id=task_id,
            link=f"/goals/{goal_id}/tasks/{task_id}",
        )
    except Exception:
        pass

    # Record to GO memory
    await go_memory.on_task_reassigned(
        goal_id, task_id, task["title"],
        old_assigned, new_assigned_to, reason)

    return {"task_id": task_id, "old": old_assigned, "new": new_assigned_to}


async def _action_update_goal_state(goal_id: str, params: dict) -> dict:
    """Update the goal state document with optimistic locking."""
    state_doc = params.get("state_doc", {})
    if not state_doc:
        return {"error": "state_doc required"}

    pool = get_pool()

    # Optimistic lock: read current version, write with check
    row = await pool.fetchrow(
        "SELECT state_version FROM goals WHERE id = $1", goal_id,
    )
    if not row:
        return {"error": "Goal not found"}

    current_version = row["state_version"]

    result = await pool.execute("""
        UPDATE goals
        SET state_doc = $1, state_version = state_version + 1,
            completion_pct = COALESCE($2, completion_pct)
        WHERE id = $3 AND state_version = $4
    """, json.dumps(state_doc), state_doc.get("completion_pct"),
        goal_id, current_version)

    if result == "UPDATE 0":
        return {"error": "Stale state — concurrent update detected, retry needed"}

    # Ledger
    go_actor_id, go_actor_type = await go_actor(goal_id)
    if go_actor_id:
        await append_event(
            pool,
            event_type=GOAL_STATE_DOC_UPDATED,
            payload={"state_doc_keys": list(state_doc.keys())},
            actor_id=go_actor_id,
            actor_type=go_actor_type,
            context_goal_id=goal_id,
        )

    return {"updated": True, "new_version": current_version + 1}


async def _action_update_completion(goal_id: str, params: dict) -> dict:
    """Update goal completion percentage."""
    pct = params.get("completion_pct", 0)
    if not isinstance(pct, int) or pct < 0 or pct > 100:
        return {"error": "completion_pct must be 0-100"}

    pool = get_pool()
    await pool.execute(
        "UPDATE goals SET completion_pct = $1 WHERE id = $2",
        pct, goal_id,
    )

    return {"completion_pct": pct}


async def _action_notify_member(goal_id: str, params: dict) -> dict:
    """Send a notification to a specific goal member."""
    uid = params.get("uid", "")
    message = params.get("message", "")

    if not uid or not message:
        return {"error": "uid and message required"}

    await push_notification(
        uid,
        type="go_notification",
        title="Goal Orchestrator",
        body=message[:200],
        goal_id=goal_id,
        link=f"/goals/{goal_id}/thread",
    )

    return {"notified": uid}


async def _action_lock_resource(goal_id: str, params: dict) -> dict:
    """Lock a resource for exclusive write access."""
    resource_id = params.get("resource_id", "")
    for_task = params.get("for_task", "")

    if not resource_id:
        return {"error": "resource_id required"}

    pool = get_pool()
    result = await pool.execute("""
        UPDATE goal_resources
        SET locked_by = 'go', locked_for = $1, locked_at = now()
        WHERE id = $2 AND goal_id = $3 AND locked_by IS NULL
    """, for_task, resource_id, goal_id)

    if result == "UPDATE 0":
        return {"error": f"Resource {resource_id} already locked or not found"}

    return {"locked": resource_id, "for_task": for_task}


async def _action_unlock_resource(goal_id: str, params: dict) -> dict:
    """Release a resource lock."""
    resource_id = params.get("resource_id", "")
    if not resource_id:
        return {"error": "resource_id required"}

    pool = get_pool()
    await pool.execute("""
        UPDATE goal_resources
        SET locked_by = NULL, locked_for = NULL, locked_at = NULL
        WHERE id = $1 AND goal_id = $2
    """, resource_id, goal_id)

    return {"unlocked": resource_id}


async def _action_mark_milestone(goal_id: str, params: dict) -> dict:
    """Record a milestone in GO memory."""
    milestone_name = params.get("milestone_name", "")
    related_tasks = params.get("related_tasks", [])

    if not milestone_name:
        return {"error": "milestone_name required"}

    pool = get_pool()

    # Record in go_memory
    await pool.execute("""
        INSERT INTO go_memory (goal_id, category, title, content, related_task, actor)
        VALUES ($1, 'milestone', $2, $3, $4, $5)
    """, goal_id, milestone_name,
        f"Milestone reached: {milestone_name}. Tasks: {related_tasks}",
        ",".join(related_tasks) if related_tasks else "",
        f"go-{goal_id}")

    return {"milestone": milestone_name, "tasks": related_tasks}
