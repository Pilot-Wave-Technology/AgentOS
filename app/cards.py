"""Structured card builders for agent → SHCA thread communication.

Cards are JSON payloads stored in messages.card_data and pushed to the
browser via WebSocket. The frontend renders them as interactive UI
components (option buttons, expandable details, approval gates).

Card types:
  - question:   agent needs human input (options + custom answer)
  - progress:   agent status update (FYI, no action needed)
  - completion: agent finished work (artifacts summary)
  - failure:    agent hit an error (details + retry option)
  - approval:   parent task ready for human review (approve/reject)
  - review:     RA verdict on a completed task (verdict, score, findings)
"""

from typing import Optional


def question_card(
    *,
    task_id: str,
    agent_template: str,
    agent_name: str,
    title: str,
    body: str,
    context: str = "",
    options: list[dict] | None = None,
    allow_custom: bool = True,
    requests: list[dict] | None = None,
) -> dict:
    """Build a question card from an agent's blocked report.

    If `requests` is provided (structured request list from task-report),
    auto-generates options from request types.
    """
    card_options = options or []

    if not card_options and requests:
        for i, req in enumerate(requests):
            req_type = req.get("type", "question")
            req_msg = req.get("message", "")
            icon = {"question": "❓", "access": "🔑", "approval": "✅", "action": "⚡"}.get(req_type, "❓")
            card_options.append({
                "id": f"req-{i}",
                "label": f"{icon} {req_type.title()}: {req_msg[:60]}",
                "value": req_msg,
                "style": "secondary",
            })

    return {
        "type": "agent_card",
        "card_type": "question",
        "task_id": task_id,
        "agent": agent_template,
        "agent_name": agent_name,
        "title": title,
        "body": body,
        "context": context,
        "options": card_options,
        "allow_custom": allow_custom,
        "responded": False,
    }


def progress_card(
    *,
    task_id: str,
    agent_template: str,
    agent_name: str,
    title: str,
    body: str,
    context: str = "",
) -> dict:
    """Build a progress card (FYI, no action needed)."""
    return {
        "type": "agent_card",
        "card_type": "progress",
        "task_id": task_id,
        "agent": agent_template,
        "agent_name": agent_name,
        "title": title,
        "body": body,
        "context": context,
    }


def completion_card(
    *,
    task_id: str,
    agent_template: str,
    agent_name: str,
    title: str,
    body: str,
    artifacts: list[dict] | None = None,
    context: str = "",
) -> dict:
    """Build a completion card (agent finished work)."""
    return {
        "type": "agent_card",
        "card_type": "completion",
        "task_id": task_id,
        "agent": agent_template,
        "agent_name": agent_name,
        "title": title,
        "body": body,
        "artifacts": artifacts or [],
        "context": context,
    }


def failure_card(
    *,
    task_id: str,
    agent_template: str,
    agent_name: str,
    title: str,
    body: str,
    context: str = "",
    retryable: bool = True,
) -> dict:
    """Build a failure card (agent hit an error)."""
    options = []
    if retryable:
        options.append({
            "id": "retry",
            "label": "Retry Task",
            "value": "retry",
            "style": "primary",
        })
    options.append({
        "id": "cancel",
        "label": "Cancel Task",
        "value": "cancel",
        "style": "danger",
    })

    return {
        "type": "agent_card",
        "card_type": "failure",
        "task_id": task_id,
        "agent": agent_template,
        "agent_name": agent_name,
        "title": title,
        "body": body,
        "context": context,
        "options": options,
    }


def approval_card(
    *,
    task_id: str,
    title: str,
    body: str,
    subtask_summary: list[dict] | None = None,
    artifacts: list[dict] | None = None,
    context: str = "",
    show_action_buttons: bool = False,
) -> dict:
    """Build an approval card (parent task ready for human review).

    `show_action_buttons` controls whether the Approve / Request Changes
    buttons are rendered. Defaults to False — plans are auto-approved
    by the task controller for now, so the card is informational only
    (View Plan button via the `context` field still works). The
    underlying approve/request-changes handlers in card_responses.py
    are preserved for when manual review is re-enabled.
    """
    options: list[dict] = []
    if show_action_buttons:
        options = [
            {"id": "approve", "label": "Approve Plan", "value": "approve", "style": "primary"},
            {"id": "changes", "label": "Request Changes", "value": "request_changes", "style": "secondary"},
        ]
    return {
        "type": "agent_card",
        "card_type": "approval",
        "task_id": task_id,
        "agent": "system",
        "agent_name": "Task Controller",
        "title": title,
        "body": body,
        "subtask_summary": subtask_summary or [],
        "artifacts": artifacts or [],
        "context": context,
        "options": options,
        "allow_custom": show_action_buttons,
    }


def review_card(
    *,
    task_id: str,
    agent_template: str,
    agent_name: str,
    reviewed_task_title: str,
    verdict: str,
    quality_score: int,
    findings: list[str],
    recommendations: list[str],
    summary: str,
    updated_spec: str = "",
) -> dict:
    """Build a review card (RA verdict on a completed task)."""
    return {
        "type": "agent_card",
        "card_type": "review",
        "task_id": task_id,
        "agent": agent_template,
        "agent_name": agent_name,
        "reviewed_task_title": reviewed_task_title,
        "verdict": verdict,
        "quality_score": quality_score,
        "findings": findings,
        "recommendations": recommendations,
        "summary": summary,
        "updated_spec": updated_spec,
    }


def _resolve_agent_name(pool_or_name, template_id: str = "") -> str:
    """Helper to get agent display name. Accepts a name string or falls back to template_id."""
    if isinstance(pool_or_name, str) and pool_or_name:
        return pool_or_name
    return template_id or "Agent"
