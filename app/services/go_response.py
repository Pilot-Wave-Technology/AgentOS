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
from app.services.story_priority import normalize_priority
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
    # AN AUTONOMOUS ERRAND ONLY, and the guard is in
    # `kernel.go.accept_errand` where it reads `work_kind` off the row.
    # A person types "approved" in the thread and GO carries it; it may
    # NOT start an ordinary story, which belongs to whoever it is
    # assigned to and is accepted from the board.
    "accept_errand",
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


# `(key: some-finding-key)` as `review_action_planning` asks for it. The
# em-dash placeholder `(key: --)` is the skill's "no specific finding" and
# is deliberately not matched.
_FINDING_KEY_RE = re.compile(r"\(\s*key\s*:\s*([a-z0-9][a-z0-9-]{0,78})\s*\)",
                             re.IGNORECASE)


async def _record_review_citations(goal_id: str, response: str) -> int:
    """Attach the GO's reply to each finding key it cited."""
    keys = {m.group(1).lower() for m in _FINDING_KEY_RE.finditer(response or "")}
    if not keys:
        return 0
    from app.services.council.findings import store

    # The WHOLE reply against each key, not just the citing line. Next
    # cycle a reviewer reads this to judge whether the answer was acted
    # on, and one line out of context is not enough to judge with.
    text = (response or "").strip()[:4000]
    recorded = 0
    for key in sorted(keys):
        if await store.record_go_response(goal_id, key, text):
            recorded += 1
    if recorded:
        _log.info("go_response: %s answered %d council finding(s)",
                  goal_id, recorded)
    return recorded


async def process_go_response(goal_id: str, response: str) -> str:
    """Extract go-action blocks from GO's response, execute them, return clean text.

    Args:
        goal_id: The goal this response belongs to
        response: Raw response text from GO (may contain action blocks)

    Returns:
        Clean text with action blocks stripped (for display to users)
    """
    # THE GO'S ANSWER TO A REVIEW, ATTACHED TO THE FINDING IT ANSWERS.
    # `review_action_planning` asks for `(key: <finding-key>)` on each
    # proposed action; capturing it is what lets the NEXT cycle say
    # "raised four times, answered once on cycle 16, and the story that
    # answer promised does not exist" -- which is a finding about the
    # orchestrator that no single reviewer can reach.
    #
    # Best-effort and never in the way: a reply with no keys is an
    # ordinary reply, and a failure here must not swallow the GO's turn.
    try:
        await _record_review_citations(goal_id, response)
    except Exception:
        _log.exception("go_response: recording review citations for %s", goal_id)

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
    elif action_type == "accept_errand":
        return await _action_accept_errand(goal_id, params)
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
    priority = normalize_priority(params.get("priority"))
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
    priority = normalize_priority(params.get("priority"))
    raw_depends_on = params.get("depends_on", [])
    acceptance_criteria = params.get("acceptance_criteria", "")
    estimated_effort = params.get("estimated_effort", "")
    # F8 — WHICH SHAPE OF WORK THIS IS, from the planner that decided.
    #
    # Phase 3 built `stories.work_kind` in four places -- the migration,
    # `kernel/assistant/write.py` which reads it so the judgement reaches
    # the thing that RUNS the work, `goal_state` which reports it, and
    # GO's prompt which says "say which it is" -- and gave GO nowhere to
    # say it. GO encoded the intent in the title instead (`[Errand] ...`),
    # which is what an agent does when the affordance is missing, and the
    # errand then resolved to the STORY profile: coding domain, wanting a
    # branch and a merge for work that produces a CSV.
    #
    # Validated rather than trusted: NULL means story, which is what every
    # row predating the column is, so an unknown value must become NULL
    # rather than reaching a CHECK constraint as a 500.
    from app.services.work_shape import work_kind_of

    work_kind = work_kind_of(params.get("work_kind"))

    # AN ERRAND IS NOT CREATED HERE, AND THIS IS WHY IT MATTERS.
    #
    # An errand's story is created by `errand_grants.approve()` -- the
    # act that also writes the GRANT, which is what makes the errand
    # autonomous. Created any other way it is an errand in name only: no
    # grant, so no check-ins, no plan self-approval, no person notices,
    # and it sits on the board waiting for somebody to press Start.
    #
    # Watched on ERR12. A person typed "Approved" in the thread; GO
    # reached for `accept_errand`, which needs a story id; no story
    # existed yet because the proposal had not been approved; so GO did
    # the only thing left to it and CREATED one. The result was two
    # stories for one errand -- `91996a9b` with no grant from the chat
    # path, `2e0a7c42` with a grant from the card -- and the chat one
    # was the worse of the two.
    #
    # Refusing here is what makes that impossible rather than merely
    # discouraged. The route back is named, because a refusal that does
    # not say what to do instead gets worked around exactly like this
    # one was.
    if work_kind == "errand":
        return {"error": (
            "an errand's story is created when somebody approves the "
            "proposal, and approving is what writes the grant that makes "
            "it autonomous. Creating one here produces an errand with no "
            "grant: no check-ins, no plan to settle, and a Start button "
            "waiting for a person. Use `propose_errand`, and when they "
            "say yes use `accept_errand`."),
            "code": "E_ERRAND_NOT_CREATED_HERE"}

    # WHICH MACHINE THIS NEEDS, from the planner that knows the work.
    #
    # Same shape as `work_kind` above and for the same reason: the ladder,
    # the resolver and the node pools all existed and nothing could ASK
    # for a rung, so every story in the system ran `small` -- including
    # the ones whose suite needs 8 GiB and dies for it.
    #
    # VALIDATED RATHER THAN TRUSTED, and an unknown word becomes NULL
    # rather than reaching dispatch: NULL means "nobody chose", which
    # falls through to the profile exactly as it does today. A person can
    # change it on the story page until the run picks it up.
    from kernel.conductor.size_choice import valid as _valid_size

    try:
        sandbox_size = _valid_size(params.get("sandbox_size"))
    except ValueError:
        _log.warning("create_story: %r is not a machine size; the "
                       "profile's applies", params.get("sandbox_size"))
        sandbox_size = None

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
                                     created_by, acceptance_criteria, estimated_effort,
                                     work_kind, sandbox_size)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'proposed', $8, $9, $10, $11, $12, $13)
            """, story_id, goal_id, title, description, assigned_to,
                priority, depends_on, thread_id, f"go-{goal_id}",
                acceptance_criteria, estimated_effort, work_kind, sandbox_size)

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

    out = {"story_id": story_id, "title": title,
           "assigned_to": assigned_to}

    # THE SAME GUIDANCE THE GATEWAY PATH RETURNS. It lived here only,
    # and the gateway's `create_story` posts somewhere else entirely --
    # so GO's prompt promised a catalogue that arrived on one of the two
    # paths. See app/services/story_catalogue.py.
    from app.services.story_catalogue import grant_guidance

    out.update(await grant_guidance(pool, goal_id))
    return out


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

    # THE MACHINE, REVISABLE UNTIL IT IS NOT. GO proposes a rung on
    # `create_story`, and often only learns the suite needs more after
    # the first run dies -- so being unable to change it meant cancelling
    # the story and writing a new one.
    #
    # GUARDED ON STATE, the same two the human endpoint uses
    # (`story_sandbox_size`). The rung is frozen onto the story at
    # dispatch under `WHERE sandbox_size IS NULL`, so an edit after that
    # writes nothing and would LOOK like it worked -- which is worse
    # than a refusal, because the next run still dies and the setting
    # says otherwise.
    if "sandbox_size" in params:
        # THE SAME RULE THE HUMAN FORM USES. A state whitelist refused a
        # change after a cancelled run, which is exactly when somebody
        # wants a bigger machine.
        from app.services.machine_lock import locked_reason
        why = await locked_reason(pool, story_id)
        if why:
            return {"error": f"Story {story_id}: {why}"}
        from kernel.conductor.size_choice import valid as _valid_size
        try:
            rung = _valid_size(params["sandbox_size"])
        except ValueError as exc:
            return {"error": str(exc)}
        updates.append(f"sandbox_size = ${idx}")
        values.append(rung)
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
                                   "depends_on", "assigned_to", "estimated_effort",
                                   "sandbox_size")
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


async def _action_accept_errand(goal_id: str, params: dict) -> dict:
    """Carry a person's approval onto an autonomous errand.

    THE GUARD IS THE KERNEL'S, reused rather than restated: it reads
    `work_kind` off the story row, so an ordinary story is refused with
    `E_NOT_AN_ERRAND` and there is nothing here for a prompt to talk its
    way past. The kernel cannot do the transition itself -- that lives
    in `app/services/story_transitions` and the kernel ships without
    `app/` -- so the check is there and the act is here.

    STRAIGHT THROUGH. An ordinary story is accepted and then started by
    a person who may do the second later. An errand has nobody waiting
    to click twice: approval IS the start, so this applies `accept` and
    then `start`, and the second failing is reported rather than left as
    a story sitting in `accepted` for a click that is not coming.
    """
    from kernel.go.accept_errand import accept_errand
    from app.services.story_transitions import apply_story_transition

    story_id = str(params.get("story_id") or params.get("story") or "").strip()
    human_uid = str(params.get("human_uid") or params.get("approved_by")
                    or "").strip()
    proposal_id = params.get("proposal_id") or params.get("proposal")
    all_pending = str(params.get("all_pending")
                      or params.get("all") or "").lower() in ("1", "true",
                                                              "yes", "all")
    pool = get_pool()

    async with pool.acquire() as conn:
        checked = await accept_errand(conn, goal_id, "go", story_id,
                                      human_uid, proposal_id=proposal_id,
                                      all_pending=all_pending)
    if not isinstance(checked, dict):
        # A `Refused` -- its code and message are written for GO to read.
        return {"error": getattr(checked, "message", str(checked)),
                "code": getattr(checked, "code", "E_REFUSED")}

    # A PROPOSAL, NOT A STORY: the normal case. Approving it is what
    # creates the story and the grant together and dispatches -- the
    # same call the card's Approve button makes, so chat and card are
    # one path producing one story instead of two.
    # ALL OF THEM, one decision. Each approval is its own row because
    # each errand is its own run with its own grant, but a failure part
    # way through is reported with what DID start -- silently approving
    # two of three and saying "ok" is the shape that loses work.
    if "proposals" in checked:
        from app.services.errand_grants import approve as approve_proposal
        started, failed = [], []
        for pid in checked["proposals"]:
            try:
                async with pool.acquire() as conn, conn.transaction():
                    done = await approve_proposal(conn, goal_id, pid,
                                                  human_uid)
                started.append({"proposal": pid, "story": done.get("story")})
            except Exception as exc:                        # noqa: BLE001
                _log.exception("accept_errand: proposal %s failed", pid)
                failed.append({"proposal": pid, "error": str(exc)})
        out = {"ok": not failed, "started": started,
               "approved_by": human_uid,
               "detail": f"{len(started)} errand(s) approved and started"}
        if failed:
            out["failed"] = failed
            out["detail"] += (f"; {len(failed)} could not be approved -- "
                              f"say which, they are still waiting")
        return out

    if "proposal" in checked:
        from app.services.errand_grants import approve as approve_proposal
        try:
            async with pool.acquire() as conn, conn.transaction():
                done = await approve_proposal(
                    conn, goal_id, checked["proposal"], human_uid)
        except Exception as exc:                            # noqa: BLE001
            _log.exception("accept_errand: approving proposal %s failed",
                           checked["proposal"])
            return {"error": f"could not approve that errand: {exc}"}
        return {"ok": True, "story": done.get("story"),
                "proposal": checked["proposal"], "approved_by": human_uid,
                "state": "in_progress",
                "detail": "approved and started; the plan parks next and "
                          "is yours to settle"}

    for action in ("accept", "start"):
        ok, why = await apply_story_transition(
            goal_id, story_id, action, human_uid, pool)
        if not ok and action == "accept" and "already" not in (why or "").lower():
            return {"error": f"could not accept {story_id}: {why}"}
        if not ok and action == "start":
            # ACCEPTED BUT NOT STARTED is a real outcome, not a failure
            # to hide: no slot, or a dependency that is not met. Say so,
            # because the errand will sit until it clears and GO should
            # tell the person that rather than report it as running.
            return {"ok": True, "story": story_id, "state": "accepted",
                    "detail": f"approved, but it cannot start yet: {why}"}
    return {"ok": True, "story": story_id, "state": "in_progress",
            "approved_by": human_uid,
            "detail": checked["detail"]}
