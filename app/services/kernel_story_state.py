"""The state block for a story the KERNEL is running.

WHAT WAS BEING INJECTED. `build_story_state_snapshot` describes the
legacy operator-mode story: a `current_phase`, a claude-code session, a
DAG of phase tasks, and a story workspace under the assistant's own
agent home. A kernel story has none of those, and the block was sent
before every human turn anyway. Read back from a real run mid-
verification, it said:

    phase: planning              the run was VERIFYING
    workspace_path: <agent home> the work was in the executor's volume
    branch: feat/…-cal_backend   the story wrote TWO branches
    plan_path: (not written yet) plan-01.md existed in both repos
    recent_progress: []          thirty-two steps had happened

Five fields, five wrong. Plus a closing directive telling the assistant
to reach story files through `custom_workspace_file` at a path that does
not exist for a kernel story. This is a large part of why "what's
happening" answers were thin: the model's most prominent context about
the story was mostly false, and it had no way to know which parts.

DELETING `phase` ALONE WOULD NOT HAVE HELPED. It was the visible one,
not the only one.

WHAT REPLACES IT. What is actually true, and nothing that isn't: the
run and its state, every write target with the branch it commits on and
where that lands, whether a person is being waited on, and the one
sentence saying where to look for detail. Fields the kernel does not
have are ABSENT rather than filled with a plausible default -- an
absent field is a question the model asks; a wrong one is an answer it
trusts.
"""

from __future__ import annotations

from app.database import get_pool

RUN_SQL = """
    SELECT e.id, e.state, e.generation, e.stall_reason, e.workspace_path,
           (SELECT count(*) FROM kernel.step_journal j
             WHERE j.execution_id = e.id) AS steps
      FROM kernel.executions e
     WHERE e.story_id = $1
     ORDER BY (e.container_id IS NOT NULL) DESC, e.id DESC
     LIMIT 1
"""

TARGETS_SQL = """
    SELECT gr.name, gr.branch_name AS lands_on, sr.relation, sr.notes,
           (SELECT gn.branch_name FROM git_node_stories gns
              JOIN git_nodes gn ON gn.id = gns.git_node_id
             WHERE gns.story_id = sr.story_id
               AND gn.trunk_branch = gr.branch_name
               AND gn.state NOT IN ('MERGED', 'ABANDONED')
             LIMIT 1) AS branch
      FROM story_resources sr
      JOIN goal_resources gr ON gr.id = sr.resource_id
     WHERE sr.story_id = $1
     ORDER BY sr.relation DESC, gr.name
"""


async def kernel_snapshot(story: dict) -> dict | None:
    """A kernel story's state block, or None if the kernel has no run.

    Returning None is how the caller knows to fall back: a story created
    in the control panel and never started has no kernel row, and
    describing it as a kernel story would be its own kind of wrong.
    """
    pool = get_pool()
    story_id = story["id"]
    run = await pool.fetchrow(RUN_SQL, story_id)
    if run is None:
        return None

    snapshot: dict = {
        "kernel": True,
        "story_id": story_id,
        "story_title": story["title"] or "",
        "run": run["id"],
        "state": run["state"],
        "leg": run["generation"],
        "steps": run["steps"] or 0,
    }
    if run["stall_reason"]:
        snapshot["stalled"] = run["stall_reason"]

    reads, writes = [], []
    for r in await pool.fetch(TARGETS_SQL, story_id):
        # The FEATURE branch when the engine has cut one, because that is
        # where commits go; the lane alone would send the executor at its
        # own merge target, which has happened.
        where = r["branch"] or r["lands_on"] or ""
        entry = r["name"] + (f" ({where})" if where else "")
        if r["relation"] in ("produces", "modifies"):
            if r["branch"] and r["lands_on"]:
                entry = f"{r['name']} — commit on {r['branch']}, lands on " \
                        f"{r['lands_on']}"
            writes.append(entry)
        else:
            reads.append(entry)
    if writes:
        snapshot["writes"] = writes
    if reads:
        snapshot["reads"] = reads

    question = await pool.fetchrow(
        "SELECT id, kind, question_text FROM kernel.questions "
        "WHERE execution_id = $1 AND status = 'OPEN' ORDER BY id DESC "
        "LIMIT 1", run["id"])
    if question:
        snapshot["waiting_on_you"] = (
            f"{question['kind']} (question {question['id']}): "
            f"{question['question_text']}")
    return snapshot


def render_kernel_block(snapshot: dict) -> list:
    """The kernel snapshot as the lines of the fenced block."""
    lines = ["```story_state_snapshot"]
    for key in ("story_id", "story_title", "run", "state", "leg", "steps",
                "stalled", "waiting_on_you"):
        if key in snapshot:
            lines.append(f"{key}: {snapshot[key]}")
    for key in ("writes", "reads"):
        if snapshot.get(key):
            lines.append(f"{key}:")
            for entry in snapshot[key]:
                lines.append(f"  - {entry}")
    lines.append("```")
    # NOT `custom_workspace_file`. That tool is rooted at the assistant's
    # agent home and a kernel story's files live in the executor's
    # volume, which the assistant has no path into and should not: its
    # job is to say what happened, not to read the source.
    lines.append("")
    lines.append(
        "> This story is run by the kernel. For what has changed — files, "
        "commits, checks, and why anything is stuck — call "
        "`story_progress`; it is the only tool that can see the "
        "executor's work. Do NOT use `custom_workspace_file` or "
        "`read`/`edit`/`write` for this story: there is no story "
        "workspace under your agent home, and those paths will ENOENT.")
    return lines
