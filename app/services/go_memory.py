"""GO Memory — auto-records major events for persistent goal awareness.

Records key events (task changes, decisions, blockers, member changes) to the
go_memory table. These are injected into GO's context on every interaction so
it always knows what has happened in the goal.
"""
import logging

from app.database import get_pool

_log = logging.getLogger(__name__)


async def record_memory(
    goal_id: str,
    category: str,
    title: str,
    content: str,
    related_task: str = "",
    related_resource: str = "",
    actor: str = "",
):
    """Record a memory entry for the GO agent."""
    pool = get_pool()
    try:
        await pool.execute("""
            INSERT INTO go_memory (goal_id, category, title, content,
                                   related_task, related_resource, actor)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, goal_id, category, title, content,
            related_task, related_resource, actor)
    except Exception as exc:
        _log.warning("Failed to record GO memory for %s: %s", goal_id, exc)


# ── Convenience recorders for specific event types ──────────────────────────

async def on_task_created(goal_id: str, task_id: str, title: str,
                          assigned_to_name: str, priority: str, actor: str):
    await record_memory(goal_id, "assignment",
        title=f"Task created: {title}",
        content=f"Assigned to {assigned_to_name}. Priority: {priority}.",
        related_task=task_id, actor=actor)


async def on_task_completed(goal_id: str, task_id: str, title: str,
                            assigned_to_name: str, result_summary: str, actor: str):
    await record_memory(goal_id, "completion",
        title=f"Task done: {title}",
        content=f"Completed by {assigned_to_name}. Result: {result_summary[:300]}",
        related_task=task_id, actor=actor)


async def on_task_failed(goal_id: str, task_id: str, title: str, reason: str, actor: str):
    await record_memory(goal_id, "completion",
        title=f"Task failed: {title}",
        content=f"Reason: {reason[:300]}",
        related_task=task_id, actor=actor)


async def on_task_reassigned(goal_id: str, task_id: str, title: str,
                              old_assignee: str, new_assignee: str, reason: str):
    await record_memory(goal_id, "assignment",
        title=f"Reassigned: {title}",
        content=f"From {old_assignee} to {new_assignee}. Reason: {reason[:200]}",
        related_task=task_id, actor="go")


async def on_decision(goal_id: str, decision: str, decided_by: str):
    await record_memory(goal_id, "decision",
        title=decision[:100],
        content=decision,
        actor=decided_by)


async def on_blocker(goal_id: str, task_id: str, reason: str, is_resolved: bool = False):
    prefix = "Resolved" if is_resolved else "Blocked"
    await record_memory(goal_id, "blocker",
        title=f"{prefix}: {reason[:80]}",
        content=reason,
        related_task=task_id)


async def on_member_change(goal_id: str, user_name: str, action: str, detail: str = ""):
    await record_memory(goal_id, "member_change",
        title=f"{user_name} {action}",
        content=detail or f"{user_name} {action}",
        actor="system")


async def on_resource_change(goal_id: str, resource_name: str, resource_id: str,
                              action: str, actor: str):
    await record_memory(goal_id, "resource_change",
        title=f"Resource {action}: {resource_name}",
        content=f"Resource '{resource_name}' {action}.",
        related_resource=resource_id, actor=actor)


async def on_milestone(goal_id: str, milestone_name: str, related_tasks: list[str]):
    await record_memory(goal_id, "milestone",
        title=milestone_name,
        content=f"Milestone reached: {milestone_name}. Tasks: {related_tasks}",
        related_task=",".join(related_tasks) if related_tasks else "")


async def on_plan_change(goal_id: str, description: str, actor: str):
    await record_memory(goal_id, "plan_change",
        title=description[:100],
        content=description,
        actor=actor)


# ── Memory retrieval for context injection ──────────────────────────────────

async def get_recent_memories(goal_id: str, limit: int = 30) -> list[dict]:
    """Get recent GO memories for context injection."""
    pool = get_pool()
    rows = await pool.fetch("""
        SELECT category, title, content, related_task, actor, created_at
        FROM go_memory
        WHERE goal_id = $1 AND tier = 'recent'
        ORDER BY created_at DESC
        LIMIT $2
    """, goal_id, limit)
    return [dict(r) for r in rows]


def format_memory_block(memories: list[dict]) -> str:
    """Format memories into a markdown block for GO context."""
    if not memories:
        return ""

    # Group by category
    by_category: dict[str, list] = {}
    for m in memories:
        by_category.setdefault(m["category"], []).append(m)

    lines = ["## GO Memory (Recent Events)\n"]

    category_order = ["decision", "completion", "assignment", "blocker",
                       "member_change", "resource_change", "milestone", "plan_change", "context"]

    for cat in category_order:
        entries = by_category.get(cat, [])
        if not entries:
            continue

        cat_title = cat.replace("_", " ").title()
        lines.append(f"### {cat_title}")
        for m in entries[:10]:  # max 10 per category
            date = m["created_at"].strftime("%Y-%m-%d %H:%M") if m.get("created_at") else ""
            lines.append(f"- [{date}] {m['title']}")
        lines.append("")

    return "\n".join(lines)
