"""Payload builders for ledger events — State Ledger spec v0.1 Section 4.

Every event type has a builder function that enforces the schema.
Callers use these instead of constructing raw dicts.

Usage:
    from app.ledger.payloads import task_created_payload, task_completed_payload

    payload = task_created_payload(
        title="Build auth", description="...", priority="high",
        requester=actor_ref(...), ...
    )
    await emit_task_event(..., payload=payload, ...)
"""
from __future__ import annotations
import uuid
from typing import Optional


# ── Shared Types ────────────────────────────────────────────────────────────

def actor_ref(
    actor_id: str | uuid.UUID,
    actor_type: str,
    display_name: str,
) -> dict:
    """Spec Section 5 — Actor Reference."""
    return {
        "actor_id": str(actor_id),
        "actor_type": actor_type,
        "display_name": display_name,
    }


def artifact_ref(
    artifact_type: str,
    storage_ref: str,
    version: int = 1,
    metadata: dict | None = None,
) -> dict:
    """Spec Section 5 — Artifact Reference."""
    ref = {
        "artifact_id": str(uuid.uuid4()),
        "artifact_type": artifact_type,
        "storage_ref": storage_ref,
        "version": version,
    }
    if metadata:
        ref["metadata"] = metadata
    return ref


# ── 4.1 Task Events ────────────────────────────────────────────────────────

def task_created_payload(
    *,
    title: str,
    description: str = "",
    acceptance_criteria: str = "",
    requester: dict | None = None,
    assignee: dict | None = None,
    priority: str = "medium",
    parent_task_id: str | None = None,
    dependencies: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Spec 4.1 — task.created"""
    return {
        "title": title,
        "description": description,
        "acceptance_criteria": acceptance_criteria,
        "requester": requester,
        "assignee": assignee,
        "priority": priority,
        "parent_task_id": parent_task_id,
        "dependencies": dependencies or [],
        "tags": tags or [],
    }


def task_assigned_payload(
    *,
    assignee: dict,
    assignment_reason: str = "",
) -> dict:
    """Spec 4.1 — task.assigned"""
    return {
        "assignee": assignee,
        "assignment_reason": assignment_reason,
    }


def task_status_changed_payload(
    *,
    previous_status: str,
    new_status: str,
    reason: str = "",
) -> dict:
    """Spec 4.1 — task.status_changed"""
    return {
        "previous_status": previous_status,
        "new_status": new_status,
        "reason": reason,
    }


def task_completed_payload(
    *,
    outcome_summary: str,
    artifacts_produced: list[dict] | None = None,
    resources_consumed: dict | None = None,
    lessons_learned: list[str] | None = None,
) -> dict:
    """Spec 4.1 — task.completed"""
    return {
        "outcome_summary": outcome_summary,
        "artifacts_produced": artifacts_produced or [],
        "resources_consumed": resources_consumed or {},
        "lessons_learned": lessons_learned or [],
    }


def task_failed_payload(
    *,
    failure_reason: str,
    partial_output: list[dict] | None = None,
    recovery_suggestion: str = "",
    cascade_impact: list[str] | None = None,
) -> dict:
    """Spec 4.1 — task.failed"""
    return {
        "failure_reason": failure_reason,
        "partial_output": partial_output or [],
        "recovery_suggestion": recovery_suggestion,
        "cascade_impact": cascade_impact or [],
    }


def task_cancelled_payload(
    *,
    reason: str,
    cancelled_by: dict,
    cascade_impact: list[str] | None = None,
) -> dict:
    """Spec 4.1 — task.cancelled"""
    return {
        "reason": reason,
        "cancelled_by": cancelled_by,
        "cascade_impact": cascade_impact or [],
    }


def task_dependency_added_payload(
    *,
    dependency_type: str = "finish_to_start",
    depends_on: str,
    description: str = "",
) -> dict:
    """Spec 4.1 — task.dependency_added"""
    return {
        "dependency_type": dependency_type,
        "depends_on": depends_on,
        "description": description,
    }


def task_dependency_resolved_payload(
    *,
    dependency_id: str,
    resolution: str = "completed",
) -> dict:
    """Spec 4.1 — task.dependency_resolved"""
    return {
        "dependency_id": dependency_id,
        "resolution": resolution,
    }


def task_progress_updated_payload(
    *,
    summary: str,
    progress_pct: int | None = None,
    artifacts_produced: list[dict] | None = None,
    open_questions: list[str] | None = None,
) -> dict:
    """Spec 4.1 — task.progress_updated"""
    return {
        "progress_pct": progress_pct,
        "summary": summary,
        "artifacts_produced": artifacts_produced or [],
        "open_questions": open_questions or [],
    }


def task_context_updated_payload(
    *,
    update_type: str,
    content: str,
    source: str = "",
    affected_assumptions: list[str] | None = None,
) -> dict:
    """Spec 4.1 — task.context_updated"""
    return {
        "update_type": update_type,
        "content": content,
        "source": source,
        "affected_assumptions": affected_assumptions or [],
    }


def task_handoff_payload(
    *,
    from_actor: dict,
    to_actor: dict,
    handoff_package: dict,
    acknowledgement_required: bool = False,
) -> dict:
    """Spec 4.1 — task.handoff"""
    return {
        "from_actor": from_actor,
        "to_actor": to_actor,
        "handoff_package": handoff_package,
        "acknowledgement_required": acknowledgement_required,
    }


# ── 4.3 Approval Events ────────────────────────────────────────────────────

def decision_recorded_payload(
    *,
    decision_type: str = "approach_selection",
    description: str,
    reasoning: str = "",
    options_considered: list[dict] | None = None,
    option_selected: int = 0,
    confidence: float | None = None,
    reversible: bool = True,
) -> dict:
    """Spec 4.6 — decision.recorded"""
    return {
        "decision_type": decision_type,
        "description": description,
        "options_considered": options_considered or [],
        "option_selected": option_selected,
        "reasoning": reasoning,
        "confidence": confidence,
        "reversible": reversible,
    }


# ── 4.4 IAM Events ─────────────────────────────────────────────────────────

def agent_provisioned_payload(
    *,
    agent_type: str = "independent_agent",
    instance_id: str,
    template_id: str,
    workspace: str = "",
    model: str = "",
    expected_lifespan: str = "task_scoped",
    spawned_by: dict | None = None,
) -> dict:
    """Spec 4.4 — iam.agent.provisioned"""
    return {
        "agent_type": agent_type,
        "instance_id": instance_id,
        "template_id": template_id,
        "workspace": workspace,
        "model": model,
        "expected_lifespan": expected_lifespan,
        "spawned_by": spawned_by,
    }


def agent_decommissioned_payload(
    *,
    instance_id: str,
    reason: str = "",
    tasks_reassigned: dict | None = None,
) -> dict:
    """Spec 4.4 — iam.agent.decommissioned"""
    return {
        "reason": reason,
        "instance_id": instance_id,
        "tasks_reassigned": tasks_reassigned or {},
    }


# ── 4.5 Communication Events ───────────────────────────────────────────────

def question_raised_payload(
    *,
    question: str,
    context: str = "",
    directed_to: list[dict] | None = None,
    blocking_task: str = "",
    urgency: str = "routine",
    requests: list[dict] | None = None,
) -> dict:
    """Spec 4.5 — comms.question.raised"""
    return {
        "question": question,
        "context": context,
        "directed_to": directed_to,
        "blocking_task": blocking_task,
        "urgency": urgency,
        "requests": requests or [],
    }


def question_answered_payload(
    *,
    question_event_id: str,
    answer: str,
    answerer: dict,
) -> dict:
    """Spec 4.5 — comms.question.answered"""
    return {
        "question_event_id": question_event_id,
        "answer": answer,
        "answerer": answerer,
    }


# ── Artifact extraction helper ──────────────────────────────────────────────

def extract_artifacts_from_report(report: dict) -> list[dict]:
    """Parse task-report JSON into Artifact References.

    Maps: branch → branch artifact, files_changed → branch metadata,
    test_results → test_results artifact, review → review artifact.
    """
    if not isinstance(report, dict):
        return []
    artifacts = []
    branch = report.get("branch")
    if branch:
        artifacts.append(artifact_ref(
            artifact_type="branch",
            storage_ref=branch,
            metadata={
                "branch_name": branch,
                "files_changed": report.get("files_changed", []),
            },
        ))
    test_results = report.get("test_results")
    if isinstance(test_results, dict):
        artifacts.append(artifact_ref(
            artifact_type="test_results",
            storage_ref="inline",
            metadata=test_results,
        ))
    review = report.get("review")
    if isinstance(review, dict):
        artifacts.append(artifact_ref(
            artifact_type="review",
            storage_ref="inline",
            metadata=review,
        ))
    return artifacts


# ── Task Resource Events ─────────────────────────────────────────────────────

def task_resource_granted_payload(
    *,
    resource_name: str,
    resource_type: str,
    access_level: str,
    branch: str = "",
    paths: list[str] | None = None,
    gcp_project: str = "",
    dataset: str = "",
    bucket: str = "",
    granted_by: str = "system",
) -> dict:
    """resource.task_access.granted"""
    return {
        "resource_name": resource_name,
        "resource_type": resource_type,
        "access_level": access_level,
        "branch": branch,
        "paths": paths or [],
        "gcp_project": gcp_project,
        "dataset": dataset,
        "bucket": bucket,
        "granted_by": granted_by,
    }


def task_resource_revoked_payload(
    *,
    resource_name: str,
    resource_type: str,
    reason: str = "",
) -> dict:
    """resource.task_access.revoked"""
    return {
        "resource_name": resource_name,
        "resource_type": resource_type,
        "reason": reason,
    }


def assessment_completed_payload(
    *,
    assessed_task_id: str,
    verdict: str,
    quality_score: int,
    findings: list[str],
    recommendations: list[str],
    summary: str,
) -> dict:
    """assessment.completed — emitted when an RA finishes reviewing a task."""
    return {
        "assessment_type": "task_quality_review",
        "assessed_task_id": assessed_task_id,
        "verdict": verdict,
        "quality_score": quality_score,
        "findings": findings,
        "recommendations": recommendations,
        "summary": summary,
    }


def task_credentials_provisioned_payload(
    *,
    resource_name: str,
    resource_type: str,
    credential_type: str,
    expires_at: str = "",
    scope: str = "",
) -> dict:
    """resource.task_credentials.provisioned"""
    return {
        "resource_name": resource_name,
        "resource_type": resource_type,
        "credential_type": credential_type,
        "expires_at": expires_at,
        "scope": scope,
    }
