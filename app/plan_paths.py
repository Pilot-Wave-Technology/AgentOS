"""Resolve on-branch plan folder paths for a task chain.

A "task chain" is one SHCA-created top-level task and all its
planner-generated subtasks. The plan, progress log, and review live in
ONE folder shared by every agent in the chain:

    .plan/story-<story-id>/task-<root-task-id>/
        PLAN.md      — planner-ia writes
        PROGRESS.md  — append-only; every agent adds a timestamped entry
        REVIEW.md    — code-review-ia writes

For non-story task chains (ad-hoc SHCA tasks without a story), the
story level is dropped:

    .plan/task-<root-task-id>/
        ...

The single entry point is `resolve_plan_paths(task_id)` — given ANY
task in a chain (subtask or root), walks up `parent_task_id` to the
root, derives the plan folder, and returns all four paths (folder +
three file paths) so callers don't have to re-assemble them.

`list_prior_chains_on_branch(goal_id, task_id)` returns every OTHER
root task on the same working branch (regardless of story), used by
`build_dispatch_message` to surface cross-chain context so agents
read peer PROGRESS.md files before starting their own work.
"""
from app.database import get_pool


async def _walk_to_root(task_id: str) -> tuple[str, str]:
    """Walk up parent_task_id to find the root task.

    Returns (root_task_id, story_id). story_id is inherited from the
    first ancestor that has one (root task typically carries it, but
    some legacy subtasks carry their own copy).
    """
    pool = get_pool()
    current_id = task_id
    story_id = ""
    # Cap the walk at 20 hops — chains shouldn't go that deep; guard
    # against accidental cycles or bad parent pointers.
    for _ in range(20):
        row = await pool.fetchrow(
            "SELECT id, parent_task_id, story_id FROM tasks WHERE id = $1",
            current_id,
        )
        if not row:
            break
        if not story_id and row["story_id"]:
            story_id = (row["story_id"] or "").strip()
        parent = (row["parent_task_id"] or "").strip()
        if not parent:
            return row["id"], story_id
        current_id = parent
    return task_id, story_id


def _compose_plan_dir(story_id: str, root_task_id: str) -> str:
    if story_id:
        return f".plan/story-{story_id}/task-{root_task_id}"
    return f".plan/task-{root_task_id}"


async def resolve_plan_paths(task_id: str) -> dict:
    """Resolve every on-branch path for a task's chain.

    Given any task_id (subtask or root), returns:
      {
        "root_task_id": <str>,
        "story_id":     <str>  ("" for non-story tasks),
        "plan_dir":     ".plan/story-<sid>/task-<rid>"  or  ".plan/task-<rid>",
        "plan_md":      "<plan_dir>/PLAN.md",
        "progress_md":  "<plan_dir>/PROGRESS.md",
        "review_md":    "<plan_dir>/REVIEW.md",
      }

    Safe to call with a non-existent task_id — falls back to treating
    the id itself as the root and returns `.plan/task-<id>/…` so callers
    still get usable paths.
    """
    root_task_id, story_id = await _walk_to_root(task_id)
    plan_dir = _compose_plan_dir(story_id, root_task_id)
    return {
        "root_task_id": root_task_id,
        "story_id":     story_id,
        "plan_dir":     plan_dir,
        "plan_md":      f"{plan_dir}/PLAN.md",
        "progress_md":  f"{plan_dir}/PROGRESS.md",
        "review_md":    f"{plan_dir}/REVIEW.md",
    }


def legacy_plan_fallback_paths() -> list[str]:
    """Legacy paths to try AFTER the namespaced path fails.

    In-flight chains from before the namespacing landed keep their
    plans at `.plan/PLAN.md`. Deep-legacy planners wrote `CLAUDE.md`
    or `PLAN.md` at the repo root. Callers concatenate this list onto
    the namespaced path to build a fetch-candidate order.
    """
    return [".plan/PLAN.md", "CLAUDE.md", "PLAN.md"]


async def candidate_plan_paths(task_id: str) -> list[str]:
    """Return an ordered list of PLAN.md paths to try when fetching
    the plan from git: namespaced path first, then legacy fallbacks.
    Used by both routers/tasks.py (on planner completion) and
    slack/actions.py (on-demand refetch).
    """
    paths = await resolve_plan_paths(task_id)
    ordered = [paths["plan_md"]]
    for legacy in legacy_plan_fallback_paths():
        if legacy not in ordered:
            ordered.append(legacy)
    return ordered


async def list_prior_chains_on_branch(
    goal_id: str, task_id: str,
) -> list[dict]:
    """List every OTHER root task on the same working branch as `task_id`.

    Returns rows grouped for rendering:
      [
        {"root_task_id": "...", "title": "...", "state": "...",
         "story_id": "...", "story_title": "...",
         "plan_dir": "..."},
        ...
      ]

    Used by build_dispatch_message to render the "Prior work on this
    branch" section. Includes in_progress AND done/approved chains;
    skips cancelled/rejected. Omits the task's own chain.
    """
    pool = get_pool()
    paths = await resolve_plan_paths(task_id)
    own_root = paths["root_task_id"]
    story_id = paths["story_id"]

    if not story_id:
        # Non-story task — no branch linkage to follow. Return [].
        return []

    # Branch resource linked to this story.
    branch_row = await pool.fetchrow(
        """SELECT sr.resource_id
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND gr.kind = 'branch'
              AND gr.status = 'active'
            LIMIT 1""",
        story_id,
    )
    if not branch_row:
        return []
    branch_resource_id = branch_row["resource_id"]

    # All stories sharing this branch.
    stories = await pool.fetch(
        """SELECT DISTINCT s.id AS story_id, s.title AS story_title,
                  s.state AS story_state, s.created_at
             FROM stories s
             JOIN story_resources sr ON sr.story_id = s.id
            WHERE s.goal_id = $1
              AND sr.resource_id = $2
              AND s.state IN ('done', 'approved', 'in_progress')
            ORDER BY s.state, s.created_at""",
        goal_id, branch_resource_id,
    )
    if not stories:
        return []

    # For every such story, list its active root tasks (parent_task_id
    # NULL/empty). Exclude our own root so the agent doesn't see itself
    # as "prior work".
    result: list[dict] = []
    for s in stories:
        roots = await pool.fetch(
            """SELECT id, title, state
                 FROM tasks
                WHERE goal_id = $1
                  AND story_id = $2
                  AND (parent_task_id IS NULL OR parent_task_id = '')
                  AND status = 'active'
                  AND state IN ('done', 'approved', 'in_progress')
                ORDER BY created_at""",
            goal_id, s["story_id"],
        )
        for r in roots:
            if r["id"] == own_root:
                continue
            result.append({
                "root_task_id": r["id"],
                "title":        r["title"],
                "state":        r["state"],
                "story_id":     s["story_id"],
                "story_title":  s["story_title"],
                "plan_dir":     _compose_plan_dir(s["story_id"], r["id"]),
            })
    return result


def render_prior_chains_section(prior: list[dict], own_plan_dir: str, own_title: str) -> str:
    """Render the 'Prior work on this branch' section for a dispatch
    message. Returns markdown; empty string if `prior` is empty.
    """
    if not prior:
        return ""
    lines = [
        "## Prior work on this branch",
        "",
        "Other task chains are working on the same branch. **Before starting your own work**,",
        "read each `done` / `approved` peer's `PROGRESS.md` top-to-bottom so you don't",
        "duplicate finished work or contradict established patterns. For `in_progress`",
        "chains, skim their latest PROGRESS.md entry — it may constrain what you can",
        "safely change.",
        "",
    ]
    # Group by story for readability.
    by_story: dict[tuple, list[dict]] = {}
    for p in prior:
        key = (p["story_id"], p["story_title"])
        by_story.setdefault(key, []).append(p)

    for (sid, stitle), chains in by_story.items():
        lines.append(f"### Story `{sid}` — \"{stitle}\"")
        for c in chains:
            tag = f"[{c['state']}]".ljust(14)
            lines.append(
                f"- {tag} task `{c['root_task_id']}` \"{c['title']}\" → `{c['plan_dir']}/`"
            )
        lines.append("")

    lines.append(f"### Your chain")
    lines.append(f"- task \"{own_title}\" → `{own_plan_dir}/`")
    lines.append("")
    return "\n".join(lines)
