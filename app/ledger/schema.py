"""Event type constants for the State Ledger.

Aligned with State Ledger — Data Model & Storage Architecture v0.1, Section 4.
Event types use dot-notation namespacing.
"""

# ── 4.1 Task Events ──────────────────────────────────────────────────────────

TASK_CREATED = "task.created"
TASK_ASSIGNED = "task.assigned"
TASK_STATUS_CHANGED = "task.status_changed"
TASK_PROGRESS_UPDATED = "task.progress_updated"
TASK_CONTEXT_UPDATED = "task.context_updated"
TASK_DEPENDENCY_ADDED = "task.dependency_added"
TASK_DEPENDENCY_RESOLVED = "task.dependency_resolved"
TASK_HANDOFF = "task.handoff"
TASK_HANDOFF_ACKNOWLEDGED = "task.handoff_acknowledged"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"
TASK_CANCELLED = "task.cancelled"

# Backward compat alias
TASK_STATE_CHANGED = TASK_STATUS_CHANGED

# ── 4.2 Goal Events ─────────────────────────────────────────────────────────

GOAL_CREATED = "goal.created"
GOAL_UPDATED = "goal.updated"
GOAL_STATUS_CHANGED = "goal.status_changed"
GOAL_MEMBER_ADDED = "goal.member_added"
GOAL_MEMBER_REMOVED = "goal.member_removed"
GOAL_MEMBER_ROLE_CHANGED = "goal.member_role_changed"
GOAL_MILESTONE_DEFINED = "goal.milestone_defined"
GOAL_MILESTONE_REACHED = "goal.milestone_reached"
GOAL_CONTEXT_UPDATED = "goal.context_updated"
GOAL_COMPLETED = "goal.completed"
GOAL_CANCELLED = "goal.cancelled"


# ── 4.3 Approval Events ──────────────────────────────────────────────────────

APPROVAL_REQUESTED = "approval.requested"
APPROVAL_GRANTED = "approval.granted"
APPROVAL_DENIED = "approval.denied"
APPROVAL_ESCALATED = "approval.escalated"
APPROVAL_TIMED_OUT = "approval.timed_out"

# ── 4.4 IAM Events ───────────────────────────────────────────────────────────

AGENT_PROVISIONED = "iam.agent.provisioned"
AGENT_PROVISIONING_FAILED = "iam.agent.provisioning_failed"
AGENT_SUSPENDED = "iam.agent.suspended"
AGENT_RESUMED = "iam.agent.resumed"
AGENT_DECOMMISSIONED = "iam.agent.decommissioned"
AGENT_DELETED = AGENT_DECOMMISSIONED  # alias
AGENT_UPDATED = "iam.agent.updated"  # field changes (model, name, tools, etc.)

AGENT_CONTEXT_BOUND = "iam.agent.context_bound"

PERMISSION_GRANTED = "iam.permission.granted"
PERMISSION_REVOKED = "iam.permission.revoked"
PERMISSION_ESCALATION_REQUESTED = "iam.permission.escalation_requested"

TRUST_SCORE_CHANGED = "iam.trust_score.changed"

USER_CREATED = "iam.user.created"
USER_SYSTEM_ROLE_CHANGED = "iam.user.role_changed"

SIGNUP_REQUESTED = "iam.signup.requested"
SIGNUP_APPROVED = "iam.signup.approved"
SIGNUP_REJECTED = "iam.signup.rejected"

# ── 4.5 Communication Events ─────────────────────────────────────────────────

MESSAGE_POSTED = "comms.message.posted"
QUESTION_RAISED = "comms.question.raised"
QUESTION_ANSWERED = "comms.question.answered"
EXTERNAL_SENT = "comms.external.sent"

THREAD_REFRESHED = "comms.thread.refreshed"
THREAD_DELETED = "comms.thread.deleted"

# ── 4.11 Task Controller Events (DEPRECATED) ──────────────────────────────
# These invented types are superseded by spec-compliant types above.
# Kept as aliases for backward compatibility with existing ledger_events rows.
#
# Migrate to:
#   TASK_SPEC_CREATED      → DECISION_RECORDED + individual TASK_CREATED
#   TASK_READY             → TASK_DEPENDENCY_RESOLVED + TASK_STATUS_CHANGED
#   TASK_DISPATCHED        → TASK_ASSIGNED + TASK_STATUS_CHANGED
#   TASK_BLOCKED           → TASK_STATUS_CHANGED (new_status="blocked")
#   TASK_RESUMED           → TASK_STATUS_CHANGED (new_status="in_progress")
#   TASK_QUESTION          → QUESTION_RAISED (comms.question.raised)
#   TASK_ANSWER            → QUESTION_ANSWERED (comms.question.answered)
#   TASK_SUBTASKS_CREATED  → individual TASK_CREATED per subtask
#   INSTANCE_CREATED       → AGENT_PROVISIONED (iam.agent.provisioned)
#   INSTANCE_DESTROYED     → AGENT_DECOMMISSIONED (iam.agent.decommissioned)

TASK_SPEC_CREATED = "task.spec_created"
TASK_READY = "task.ready"
TASK_DISPATCHED = "task.dispatched"
TASK_BLOCKED = "task.blocked"
TASK_RESUMED = "task.resumed"
TASK_QUESTION = "task.question"
TASK_ANSWER = "task.answer"
TASK_SUBTASKS_CREATED = "task.subtasks_created"
INSTANCE_CREATED = "task.instance_created"
INSTANCE_DESTROYED = "task.instance_destroyed"

# ── 4.6 Decision Events ──────────────────────────────────────────────────────

DECISION_RECORDED = "decision.recorded"

# ── 4.7 Security Events ──────────────────────────────────────────────────────

SECURITY_VIOLATION_DETECTED = "security.violation.detected"
SECURITY_AUDIT_COMPLETED = "security.audit.completed"
SECURITY_POLICY_ENFORCED = "security.policy.enforced"

# Per-capability event emitted by the OpenClaw plugin's runtime audit pipe
# every time a tool uses net.fetch / storage.read / storage.write / storage.list
# / storage.stat / secrets.get / mcp.call / tool.execute / console.{log,warn,error}.
# Payload shape:
#   {
#     "tool":        str | null,    — tool name (from tool definition)
#     "agent_id":    str | null,    — gateway agent id
#     "session_key": str | null,    — gateway sessionKey
#     "event":       "net.fetch" | "storage.read" | ...,
#     "outcome":     "ok" | "denied",
#     "reason":      str | null,    — when outcome=="denied"
#     "detail":      dict | null,   — event-specific (URL, path, name; never plaintext)
#     "ts":          str | null,    — runtime-emitted timestamp (ISO 8601)
#   }
TOOL_CAPABILITY_CALLED = "tool.capability_called"

# ── 4.8 Resource Events ──────────────────────────────────────────────────────

RESOURCE_CONSUMED = "resource.consumed"
RESOURCE_BUDGET_ADJUSTED = "resource.budget.adjusted"

# Artifact Registry (Control Panel specific)
REGISTRY_RESOURCE_ADDED = "registry.resource.added"
REGISTRY_RESOURCE_UPDATED = "registry.resource.updated"
REGISTRY_RESOURCE_REMOVED = "registry.resource.removed"

# Per-agent access control
REGISTRY_ACCESS_GRANTED = "registry.access.granted"
REGISTRY_ACCESS_UPDATED = "registry.access.updated"
REGISTRY_ACCESS_REVOKED = "registry.access.revoked"

# Task-scoped resource access
TASK_RESOURCE_GRANTED = "resource.task_access.granted"
TASK_RESOURCE_REVOKED = "resource.task_access.revoked"
TASK_RESOURCE_CREDENTIALS_PROVISIONED = "resource.task_credentials.provisioned"
TASK_RESOURCE_CREDENTIALS_REFRESHED = "resource.task_credentials.refreshed"

# Backward compat aliases
RESOURCE_ADDED = REGISTRY_RESOURCE_ADDED
RESOURCE_UPDATED = REGISTRY_RESOURCE_UPDATED
RESOURCE_REMOVED = REGISTRY_RESOURCE_REMOVED

# ── 4.9 Assessment Events ────────────────────────────────────────────────────

ASSESSMENT_COMPLETED = "assessment.completed"

# ── 4.10 Policy Events ───────────────────────────────────────────────────────

POLICY_CREATED = "policy.created"
POLICY_UPDATED = "policy.updated"
POLICY_ENFORCEMENT_MODE_CHANGED = "policy.enforcement_mode_changed"
POLICY_RETIRED = "policy.retired"

# ── Delegation Events (system-specific) ──────────────────────────────────────

DELEGATION_SPAWNED = "delegation.spawned"
DELEGATION_COMPLETED = "delegation.completed"

# ── 4.12 Goal Orchestrator Events ───────────────────────────────────────────

# Goal state
GOAL_STATE_CHANGED = "goal.state_changed"
GOAL_STATE_DOC_UPDATED = "goal.state_doc_updated"
GOAL_STANDUP_POSTED = "goal.standup_posted"

# GO actions
GO_TASK_CREATED = "go.task_created"
GO_TASK_REASSIGNED = "go.task_reassigned"
GO_BLOCKER_DETECTED = "go.blocker_detected"
GO_BLOCKER_RESOLVED = "go.blocker_resolved"
GO_DECISION_REQUESTED = "go.decision_requested"

# Goal resources (flexible registry)
GOAL_RESOURCE_REGISTERED = "resource.goal.registered"
GOAL_RESOURCE_LOCKED = "resource.goal.locked"
GOAL_RESOURCE_UNLOCKED = "resource.goal.unlocked"
GOAL_RESOURCE_SCANNED = "resource.goal.scanned"
GOAL_RESOURCE_ARCHIVED = "resource.goal.archived"
GOAL_RESOURCE_DELETED = "resource.goal.deleted"

# Task enhancements
TASK_OUTPUT_REGISTERED = "task.output_registered"
TASK_UNBLOCKED = "task.unblocked"


# ── 4.13 SHCA Refactor Events ───────────────────────────────────────────────
#
# Added by migration 039 + the SHCA refactor (per SHCA_REFACTOR_PLAN.md).
# These are NEW event types — additive to the ledger vocabulary. No existing
# event types are modified. SHCA writes events through these constants when
# operating Claude Code on a story.

# Story lifecycle (SHCA-side; GO already owns story creation events)
STORY_PHASE_CHANGED             = "story.phase_changed"
STORY_STATE_CHANGED             = "story.state_changed"
STORY_PAUSED                    = "story.paused"
STORY_RESUMED                   = "story.resumed"

# Story assignment change (who works on it) — separate from state transitions.
# Emitted with actor_type="human" + payload.requested_by_uid when a human
# explicitly asked for the reassignment (GO must pass requested_by_uid on
# pwt_update_story for this to happen — not wired up yet, see
# research/TAXIMETER_DESIGN.md); actor_type="system" otherwise (GO's own
# planning decision). Feeds the Taximeter's story_assignment_requests metric,
# which reads zero rows until GO starts populating requested_by_uid.
STORY_ASSIGNMENT_CHANGED        = "story.assignment_changed"

# Phase transitions
PHASE_STARTED                   = "phase.started"
PHASE_COMPLETED                 = "phase.completed"
PHASE_TRANSITION_REQUESTED      = "phase.transition_requested"

# Claude Code session lifecycle (SHCA spawns + manages)
CLAUDE_SESSION_SPAWNED          = "claude.session_spawned"
CLAUDE_SESSION_PAUSED           = "claude.session_paused"
CLAUDE_SESSION_RESUMED          = "claude.session_resumed"
CLAUDE_SESSION_KILLED           = "claude.session_killed"
CLAUDE_SESSION_CRASHED          = "claude.session_crashed"
CLAUDE_SESSION_COMPACTED        = "claude.session_compacted"

# Messages (the four directions: Human ↔ SHCA ↔ Claude)
HUMAN_TO_SHCA_MESSAGE           = "msg.human_to_shca"
SHCA_TO_HUMAN_MESSAGE           = "msg.shca_to_human"
SHCA_TO_CLAUDE_MESSAGE          = "msg.shca_to_claude"
CLAUDE_TO_SHCA_MESSAGE          = "msg.claude_to_shca"

# Claude Code internals — meaningful tool calls, captured from the event stream.
# Streaming token deltas and tool input deltas are intentionally NOT events
# (they go to the raw transcript archive, not the ledger).
CLAUDE_TOOL_CALL                = "claude.tool_call"
CLAUDE_TOOL_RESULT              = "claude.tool_result"
SUBAGENT_INVOKED                = "claude.subagent_invoked"
SUBAGENT_RETURNED               = "claude.subagent_returned"
LIVING_DOC_WRITTEN              = "claude.living_doc_written"

# Plan + approval flow
PLAN_PROPOSED                   = "plan.proposed"
PLAN_APPROVAL_REQUESTED         = "plan.approval_requested"
PLAN_AUTO_APPROVED              = "plan.auto_approved"
PLAN_APPROVED                   = "plan.approved"
PLAN_REJECTED                   = "plan.rejected"
PLAN_OBSOLETED                  = "plan.obsoleted"

# Diff review
DIFF_READY                      = "diff.ready"
DIFF_APPROVAL_REQUESTED         = "diff.approval_requested"
DIFF_APPROVED                   = "diff.approved"
DIFF_REJECTED                   = "diff.rejected"

# Replan
REPLAN_REQUESTED                = "plan.replan_requested"
REPLAN_COMPLETED                = "plan.replan_completed"

# Iteration (verify-iterate loop)
ITERATION_STARTED               = "iter.started"
TEST_FAILED                     = "iter.test_failed"
ITERATION_EXHAUSTED             = "iter.exhausted"

# Workspace + git
WORKTREE_CREATED                = "ws.worktree_created"
WORKTREE_DELETED                = "ws.worktree_deleted"
COMMIT_CREATED                  = "ws.commit_created"
PUSH_EXECUTED                   = "ws.push_executed"
PR_OPENED                       = "ws.pr_opened"
BRANCH_CREATED                  = "ws.branch_created"
BRANCH_CHECKED_OUT              = "ws.branch_checked_out"
BRANCH_MERGED                   = "ws.branch_merged"
BRANCH_ABANDONED                = "ws.branch_abandoned"

# Escalation (richer than APPROVAL_* — for SHCA's blocking gates)
ESCALATION_REQUESTED            = "esc.requested"
ESCALATION_RESOLVED             = "esc.resolved"
ESCALATION_TIMED_OUT            = "esc.timed_out"
ESCALATION_RE_NOTIFIED          = "esc.re_notified"

# Deploy (used from Phase 11 onward)
DEPLOY_PROPOSED                 = "dep.proposed"
DEPLOY_APPROVAL_REQUESTED       = "dep.approval_requested"
DEPLOY_APPROVED                 = "dep.approved"
DEPLOY_EXECUTED                 = "dep.executed"
DEPLOY_ROLLED_BACK              = "dep.rolled_back"
SMOKE_TEST_PASSED               = "dep.smoke_test_passed"
SMOKE_TEST_FAILED               = "dep.smoke_test_failed"

# Checkpoint (named snapshots at gates)
CHECKPOINT_WRITTEN              = "checkpoint.written"

# ── 4.14 Goal Summary Events ─────────────────────────────────────────────────

GOAL_SUMMARY_GENERATED = "goal.summary_generated"
