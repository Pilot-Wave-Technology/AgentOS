# Services — Canonical Operations Catalogue

This directory is the **operation layer**. Every state-mutating action in the
system that has invariants beyond a raw DB write lives here. Channel adapters
(Slack, Notion, …), routers, task controllers, and agent tools all call these
functions — they never reimplement the logic themselves.

## The rule

> **Channels are input/output adapters. Business logic belongs here.**
>
> If a Slack command, Notion webhook handler, or any other channel adapter needs
> to change system state, it must call a service function. If the service
> function doesn't exist yet, add it here — not in the adapter.

This prevents the same logic appearing in multiple surfaces and invariants
(ledger events, transition guards, session refreshes, notifications) being
silently skipped when one path is used instead of another.

---

## Domain catalogue

### Story state

**`story_transitions.apply_story_transition(goal_id, story_id, action, actor_uid, pool, blocked_reason="")`**
→ `(ok: bool, error: str)`

The single gate for all story state changes (`accept`, `start`, `block`,
`unblock`, `reject`, `done`). Enforces: assignment ownership, dependency
satisfaction (no blocking stories still open), resource availability. Emits the
`story.state_changed` ledger event and triggers lifecycle relay to all connected
platforms.

Don't bypass: raw `UPDATE stories SET state=...` skips transition guards and
produces no ledger event, so audit logs and Notion/Slack lifecycle messages are
silently lost.

---

**`story_transitions.mark_story_reviewed(pool, goal_id, story_id)`** → `bool`

Clear the `execution_paused` replan flag on a story after an operator review.
Called by the RA approval path.

---

### Execution control (kill switch)

**`execution_control.conversation_block(goal_id)`** → `ExecutionBlock | None`

Returns the highest-precedence block that prevents a conversational reply (GO or
Assistant) for this goal. Always check this before routing an inbound message to the
gateway. Returns `None` if execution is not frozen.

**`execution_control.dispatch_block(goal_id, instance_id=None)`** → `ExecutionBlock | None`

Returns a block forbidding autonomous task dispatch. Check this in the task
controller before spinning up a new IA.

**`execution_control.pause(scope, scope_id, reason, paused_by)`** → `None`

Record an execution pause at `system`, `goal`, or `agent_instance` scope.
Idempotent. Write to `execution_pauses` only through this function.

**`execution_control.resume(scope, scope_id)`** → `bool`

Remove an execution pause. Returns `True` if a row was deleted.

Don't bypass: writing directly to `execution_pauses` skips the ledger event and
leaves the admin control panel out of sync.

---

### Task kill

**`agent_kill.force_kill_task(task_id, reason, by_uid)`** → `dict`

Force-kill the running agent instance for a task and park the task in `halted`
(terminal — no outbound transitions). Cancels the gateway turn, destroys the
agent instance, emits `task.killed` ledger event, and notifies subscribers.

Don't bypass: `halted` is a one-way door with specific cleanup steps. A raw
status update leaves the agent instance running and the session subscribed.

---

### Goal membership

**`goal_membership.add_goal_member(goal_id, target_uid, role, added_by_uid, added_by_actor_id)`**
→ `(ok: bool, error: str)`

Add a user to a goal and run the full provisioning chain: insert `goal_members`,
provision Assistant template, create Slack/Notion member channels via registered
provisioners, emit `member.joined` lifecycle event to all platforms.

Don't bypass: a raw `INSERT INTO goal_members` skips Assistant provisioning and
channel setup — the new member has no agent and no platform channel.

---

### Resource allocation

**`resource_allocation.grant_resource(goal_id, subject_type, subject_id, resource_id, relation, notes, actor_id)`**
→ `(ok: bool, error: str)`

Allocate a `goal_resource` to a `story` or `task` via `story_resources` /
`task_resource_deps`. Validates that the resource belongs to the goal, inserts
the allocation row, refreshes all goal sessions so RESOURCES.md is updated, and
emits the ledger event.

**`resource_allocation.revoke_resource(goal_id, subject_type, subject_id, resource_id, actor_id)`**
→ `(ok: bool, error: str)`

Remove an allocation and refresh sessions.

Don't bypass: skipping `refresh_after_allocation_change` means agents see a
stale RESOURCES.md and plan against resources they no longer have access to.

---

### Resource upload

**`resource_upload.upload_and_register_resource(*, goal_id, filename, file_bytes, uploader_uid, ...)`**
→ `UploadResult`

Upload a file to GCS and register it as a `goal_resource`. Idempotent on
`(goal_id, kind, name)`. Used by HTTP upload endpoints, Slack file ingestion
(`base.ingest_attachments`), and agent tools. All file intake goes through this
single function.

Don't bypass: calling GCS and `INSERT INTO goal_resources` separately skips
deduplication, kind validation, and the resource index update.

---

### Resource scanning

**`resource_scanner.scan_resource(goal_id, resource_id)`** → `dict`

Dispatch the right scanner (repo, document, spreadsheet) for a resource and
store the summary in `resource_summaries`. Called after upload and on operator
request (`/resources scan`).

---

### Agent sessions

**`agent_driver.ensure_go_session(goal_id)`** → `_AgentSession | None`

Return the live GO session for a goal, creating it if it doesn't exist. Use this
from any inbound handler that needs to send a message to the Goal Orchestrator —
never reach into `session_manager` directly.

**`agent_driver.ensure_shca_session(goal_id, agent_id, thread_id)`** → `_AgentSession | None`

Return the live Assistant session for a `(goal, agent, thread)`, creating it if
needed. Use this from any inbound handler routing a message to a human agent
channel.

Don't bypass: creating sessions directly circumvents workspace provisioning,
RESOURCES.md generation, and the reconnect/backoff logic in `openclaw.py`.

---

### Plan approval

**`plan_approval.approve_plan(goal_id, story_id, instance_id, feedback="")`**
→ `(ok: bool, error: str)`

Approve an Assistant-submitted plan: read the plan file from the workspace, validate
and ingest the DAG, push the task tree to the DB, trigger task dispatch via
`task_controller`, and emit the `plan.approved` ledger event.

---

## Adding a new service

If a channel adapter or router needs to mutate state and no service function
covers it:

1. Add a function to the relevant existing service file, or create a new one
   under `app/services/`.
2. Document it in this catalogue — include what invariants it enforces and why
   callers must not bypass it.
3. The channel adapter or router calls the new function. No business logic in
   the adapter.
