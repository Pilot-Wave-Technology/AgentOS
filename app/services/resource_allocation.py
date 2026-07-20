"""Resource allocation service — grant/revoke a resource to a story or task.

Single source of truth for allocation logic, shared by:
  - the GO/SHCA internal HTTP endpoints (app/routers/orchestrator.py) used by the
    grant_resource / revoke_resource agent tools, and
  - the Slack /resources grant|revoke commands.

Both surfaces enforce identical rules: resource must belong to the goal, the
repo TRUNK branch cannot be granted (it's the engine's merge target), a repo IS
grantable to a story (declares the engine's target repo) but a story maps to
exactly ONE repo, subject must belong to the goal.
Downstream agent workspaces (SHCA + IA persistent workers) are refreshed after
every change so RESOURCES.md reflects the new allocation immediately.

Functions return (ok: bool, message: str) so each caller formats the result for
its own platform (JSON response, Slack text, …). They never raise on expected
validation failures and never return HTTP objects.
"""
import logging
from typing import Optional

from app.database import get_pool, resolve_goal_id

_log = logging.getLogger(__name__)

_VALID_RELATIONS = ("needs", "produces", "modifies")


def _validate_subject(subject_type: str, subject_id: str) -> Optional[str]:
    """Return an error string if the (subject_type, subject_id) pair is invalid, else None."""
    if subject_type not in ("story", "task"):
        return "subject_type must be 'story' or 'task'"
    if not subject_id:
        return "subject_id required"
    return None


async def grant_resource(
    goal_id: str,
    subject_type: str,
    subject_id: str,
    resource_id: str,
    relation: str = "needs",
    notes: str = "",
    actor_id: str = "",
) -> tuple[bool, str]:
    """Grant a resource to a story or task.

    For subject_type='task', any prior row for the (task, resource) pair is
    replaced so there is exactly one effective allocation — this also turns an
    'excluded' row back into an active one (re-include).

    Returns (True, human-readable success) or (False, rejection reason).
    """
    subject_type = (subject_type or "").strip()
    subject_id = (subject_id or "").strip()
    resource_id = (resource_id or "").strip()
    relation = (relation or "needs").strip()

    err = _validate_subject(subject_type, subject_id)
    if err:
        return False, err
    if not resource_id:
        return False, "resource_id required"
    if relation not in _VALID_RELATIONS:
        return False, "relation must be needs|produces|modifies"

    goal_id = await resolve_goal_id(goal_id)
    pool = get_pool()

    # Security boundary: the resource must belong to THIS goal.
    res_row = await pool.fetchrow(
        "SELECT kind, role, name, repo_owner, repo_name "
        "FROM goal_resources WHERE id = $1 AND goal_id = $2",
        resource_id, goal_id,
    )
    if not res_row:
        return False, "resource_id not found in this goal"

    # NEVER grant the repo's TRUNK designation (a kind='branch' role='trunk' resource,
    # e.g. `dev`) to a story/task. It exists only so the engine knows the trunk
    # (resolve_repo_trunk); granting it makes the trunk a WRITABLE working ref, and the
    # story's workspace then clones the trunk and the assistant commits straight to it —
    # bypassing the branch/PR/merge model entirely. The GO grants the kind='repo'
    # resource; the Git Engine cuts + links the feature branch ('produces').
    if (res_row["kind"] or "").lower() == "branch" and (res_row["role"] or "").lower() == "trunk":
        return False, (
            f"Refusing to grant the TRUNK branch '{res_row['name']}' to {subject_type} "
            f"'{subject_id[:8]}'. The trunk is the engine's merge target, not a working "
            "ref — granting it would make the assistant commit straight to trunk. Grant "
            "the repo (kind='repo') instead; the Git Engine cuts and links the feature "
            "branch automatically."
        )

    # Repos ARE grantable to a story under the Git Engine model. Granting a repo DECLARES
    # which repo the story works in (its target) — the signal the engine uses to know
    # where to cut the workstream branch. This does NOT expose the trunk: the engine cuts
    # the workstream branch from the repo's trunk and the provisioner collapses the bare
    # repo block into the cut branch, so the agent only sees/commits its feature branch.
    #
    # ONE REPO PER STORY: the engine maps a story to a SINGLE repo (one git_node / one
    # workstream branch). A second, DIFFERENT repo would be SILENTLY DROPPED by plan
    # derivation (StoryInput.repo_id is scalar; resolve_story_repo LIMIT 1), so reject it
    # here rather than let work vanish. Cross-repo work = separate stories with a
    # dependency. (Re-granting the SAME repo, or the engine's same-repo branch, is fine.)
    if subject_type == "story" and (res_row["kind"] or "").lower() in ("repo", "branch") \
            and (res_row["repo_name"] or ""):
        this_repo = f"{(res_row['repo_owner'] or '').lower()}/{(res_row['repo_name'] or '').lower()}"
        other = await pool.fetchrow(
            "SELECT gr.repo_name FROM story_resources sr "
            "JOIN goal_resources gr ON gr.id = sr.resource_id "
            "WHERE sr.story_id = $1 AND gr.status = 'active' "
            "  AND gr.kind IN ('repo', 'branch') AND gr.repo_name IS NOT NULL AND gr.repo_name <> '' "
            "  AND lower(coalesce(gr.repo_owner,'') || '/' || gr.repo_name) <> $2 LIMIT 1",
            subject_id, this_repo,
        )
        if other:
            return False, (
                f"This story already targets repo '{other['repo_name']}'. A story maps to "
                f"exactly ONE repo (one workstream branch) — granting a second repo would "
                f"be silently dropped by the engine. To work across repos, split into "
                f"separate stories with a dependency; revoke the other repo first if "
                f"you're moving this story to a different repo."
            )

    if subject_type == "story":
        story_owned = await pool.fetchval(
            "SELECT 1 FROM stories WHERE id = $1 AND goal_id = $2", subject_id, goal_id,
        )
        if not story_owned:
            return False, "story not found in this goal"
        await pool.execute(
            """INSERT INTO story_resources (story_id, resource_id, relation, notes)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (story_id, resource_id, relation)
               DO UPDATE SET notes = EXCLUDED.notes""",
            subject_id, resource_id, relation, notes,
        )
    else:
        task_owned = await pool.fetchval(
            "SELECT 1 FROM tasks WHERE id = $1 AND goal_id = $2", subject_id, goal_id,
        )
        if not task_owned:
            return False, "task not found in this goal"
        # task_resource_deps PK is (task_id, resource_id, dep_type); keep ONE
        # effective allocation per pair by wiping prior rows first.
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM task_resource_deps WHERE task_id = $1 AND resource_id = $2",
                subject_id, resource_id,
            )
            await conn.execute(
                """INSERT INTO task_resource_deps
                     (task_id, resource_id, dep_type, notes, created_by)
                   VALUES ($1, $2, $3, $4, $5)""",
                subject_id, resource_id, relation, notes, actor_id,
            )

    _log.info(
        "allocation.grant: goal=%s subject=%s:%s resource=%s relation=%s actor=%s",
        goal_id, subject_type, subject_id, resource_id, relation, actor_id or "-",
    )
    await refresh_after_allocation_change(goal_id, subject_type, subject_id, resource_id, "grant")
    return True, (
        f"Granted *{res_row['name']}* ({relation}) to {subject_type} `{subject_id[:8]}`."
    )


async def revoke_resource(
    goal_id: str,
    subject_type: str,
    subject_id: str,
    resource_id: str,
    actor_id: str = "",
) -> tuple[bool, str]:
    """Revoke a resource from a story or task.

    story: deletes the story_resources row (removes from the story and all its tasks).
    task : deletes a task-level addition, or writes an 'excluded' row to hide a
           story-inherited resource from THIS task only.

    Returns (True, human-readable success) or (False, rejection reason).
    """
    subject_type = (subject_type or "").strip()
    subject_id = (subject_id or "").strip()
    resource_id = (resource_id or "").strip()

    err = _validate_subject(subject_type, subject_id)
    if err:
        return False, err
    if not resource_id:
        return False, "resource_id required"

    goal_id = await resolve_goal_id(goal_id)
    pool = get_pool()

    res_name = await pool.fetchval(
        "SELECT name FROM goal_resources WHERE id = $1 AND goal_id = $2",
        resource_id, goal_id,
    )
    if not res_name:
        return False, "resource_id not found in this goal"

    if subject_type == "story":
        await pool.execute(
            "DELETE FROM story_resources WHERE story_id = $1 AND resource_id = $2",
            subject_id, resource_id,
        )
        outcome = "removed"
    else:
        # Distinguish a task-level addition (just delete) from a story-inherited
        # resource (write an 'excluded' row to hide it from this task only).
        existing = await pool.fetchrow(
            """SELECT dep_type FROM task_resource_deps
                WHERE task_id = $1 AND resource_id = $2
                ORDER BY created_at DESC LIMIT 1""",
            subject_id, resource_id,
        )
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM task_resource_deps WHERE task_id = $1 AND resource_id = $2",
                subject_id, resource_id,
            )
            if existing and existing["dep_type"] != "excluded":
                outcome = "removed (task-level addition)"
            else:
                await conn.execute(
                    """INSERT INTO task_resource_deps
                         (task_id, resource_id, dep_type, notes, created_by)
                       VALUES ($1, $2, 'excluded', '', $3)""",
                    subject_id, resource_id, actor_id,
                )
                outcome = "excluded (inherited from story)"

    _log.info(
        "allocation.revoke: goal=%s subject=%s:%s resource=%s actor=%s outcome=%s",
        goal_id, subject_type, subject_id, resource_id, actor_id or "-", outcome,
    )
    await refresh_after_allocation_change(goal_id, subject_type, subject_id, resource_id, "revoke")
    return True, f"{res_name} {outcome} for {subject_type} `{subject_id[:8]}`."


async def refresh_after_allocation_change(
    goal_id: str, subject_type: str, subject_id: str, resource_id: str, action: str,
) -> None:
    """Refresh downstream agent workspaces after a grant/revoke.

    - story-level change: refresh the SHCA for that story + every IA persistent
      worker on a task of that story.
    - task-level change: refresh the IA worker bound to that task, plus the SHCA
      for the task's story (its task-spec view should reflect the change).
    """
    try:
        from app.resource_provisioning import (
            refresh_story_resources,
            refresh_task_workers_for_story,
            refresh_task_worker,
        )
        if subject_type == "story":
            shca_ok = await refresh_story_resources(goal_id, subject_id)
            ia_count = await refresh_task_workers_for_story(goal_id, subject_id)
            _log.info(
                "allocation-refresh story: goal=%s story=%s resource=%s action=%s shca=%s ia=%d",
                goal_id, subject_id, resource_id, action, shca_ok, ia_count,
            )
        else:
            ia_ok = await refresh_task_worker(goal_id, subject_id)
            pool = get_pool()
            story_id = await pool.fetchval(
                "SELECT story_id FROM tasks WHERE id = $1", subject_id,
            )
            shca_ok = False
            if story_id:
                shca_ok = await refresh_story_resources(goal_id, story_id)
            _log.info(
                "allocation-refresh task: goal=%s task=%s story=%s resource=%s action=%s ia=%s shca=%s",
                goal_id, subject_id, story_id or "-", resource_id, action, ia_ok, shca_ok,
            )
    except Exception as exc:
        _log.warning(
            "allocation-refresh failed: subject=%s:%s action=%s: %s",
            subject_type, subject_id, action, exc,
        )
