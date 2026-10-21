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

from app.config import get_settings
from app.database import get_pool, resolve_goal_id
from app.services.git_graph.repo_identity import canonical_repo_id, is_write_relation
from app.services.story_repo_scope import count_story_repos, has_live_local_checkout

_log = logging.getLogger(__name__)

_VALID_RELATIONS = ("needs", "produces", "modifies")


async def _check_no_new_write_repo_after_kickoff(
    pool, story_id: str, resource_id: str, res_row, relation: str,
) -> tuple[bool, str]:
    """Multi-repo fix, piece 2 (`research/MULTI_REPO_STORIES_PLAN.md` §12.5): refuse a
    NEW write repo once the story's kickoff has been sent.

    Makes the dangerous re-provisioning case structurally unreachable rather than
    merely unlikely: `provision_story_workspace`'s clone step is destructive on every
    call (the gateway nukes and re-clones every repo subdir to stay idempotent), which
    is only safe before any real work exists. Refusing the grant here means the
    "repo-set incomplete" case `dispatch_story_kickoff_to_assistant` may later redo
    (piece 1) can only ever be reached pre-kickoff, where redoing is provably safe.

    Only a genuinely NEW repo is refused — re-granting a repo the story already holds
    (a second `goal_resources` row for the SAME owner/name, e.g. adding the engine-cut
    branch alongside the bare repo grant) adds no new clone, so it is not the case this
    guards against and must stay allowed.

    Read-only ('needs') grants never provision a clone and are always allowed here —
    `_check_one_write_branch_per_repo` below has the same exemption for the same reason.
    """
    if not is_write_relation(relation):
        return True, ""

    this_repo = f"{(res_row['repo_owner'] or '').lower()}/{(res_row['repo_name'] or '').lower()}"
    already_granted = await pool.fetchval(
        """SELECT 1 FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1 AND sr.resource_id <> $2
              AND gr.status = 'active'
              AND gr.kind IN ('repo', 'branch')
              AND lower(coalesce(gr.repo_owner, '') || '/' || coalesce(gr.repo_name, '')) = $3
            LIMIT 1""",
        story_id, resource_id, this_repo,
    )
    if already_granted:
        return True, ""  # re-grant of a repo the story already holds — not new

    story = await pool.fetchrow(
        "SELECT execution_mode, active_thread_id FROM stories WHERE id = $1", story_id,
    )
    if not story or (story["execution_mode"] or "") != "operator":
        return True, ""  # not an operator-mode story — this fix's clone path doesn't apply

    from app.services.story_kickoff_state import story_has_had_kickoff  # noqa: PLC0415
    if await story_has_had_kickoff(pool, story_id, story["active_thread_id"] or ""):
        _log.warning(
            "resource_allocation: refusing NEW write repo %s to story=%s — kickoff "
            "already sent, re-provisioning now would risk the destructive clone step "
            "touching real work", resource_id, story_id,
        )
        return False, (
            f"This story's kickoff has already been sent, so it may already hold real, "
            f"uncommitted work. Adding repo '{res_row['repo_name']}' now would require "
            f"re-provisioning the workspace, which re-clones every repo in it — including "
            f"the one already in progress. Split this into a follow-up story instead."
        )
    return True, ""


async def _check_one_write_branch_per_repo(
    pool, story_id: str, resource_id: str, res_row, relation: str,
) -> tuple[bool, str]:
    """Multi-repo guard (plan decision #2): a story may hold N repos, but at most ONE
    WRITE branch per repo.

    Why the narrower rule: a node is a (repo, branch) pair, and the DAG has no
    per-phase target-branch dimension — two write branches in one repo would give the
    story two nodes in the same repo with no way for a task to say which one it targets.
    Cross-branch work in a single repo stays "split into sibling stories".

    Read-only context grants ('needs') are always allowed: they are cloned for
    reference and never get a branch, node, or PR.
    """
    if not is_write_relation(relation):
        _log.debug(
            "grant_resource: story=%s resource=%s relation=%s is read-only context — "
            "multi-repo write check skipped", story_id[:8], resource_id[:8], relation,
        )
        return True, ""

    this_repo_id = canonical_repo_id(res_row["repo_owner"], res_row["repo_name"])
    existing = await pool.fetch(
        """
        SELECT gr.id, gr.kind, gr.role, gr.name, gr.branch_name,
               gr.repo_owner, gr.repo_name, sr.relation
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1
           AND sr.resource_id <> $2
           AND gr.status = 'active'
           AND gr.kind IN ('repo', 'branch')
           AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
           AND NOT (gr.kind = 'branch' AND gr.role = 'trunk')
        """,
        story_id, resource_id,
    )
    for row in existing:
        if canonical_repo_id(row["repo_owner"], row["repo_name"]) != this_repo_id:
            continue  # a DIFFERENT repo — allowed under multi-repo
        if not is_write_relation(row["relation"]):
            continue  # same repo, but held read-only — not a competing write branch
        # Same repo, both write. Only a genuine SECOND BRANCH is a conflict; the common
        # "bare repo grant + engine-cut feature branch for that same repo" pair is one
        # logical write target and must stay legal (the engine creates it itself).
        both_branches = (
            (row["kind"] or "").lower() == "branch"
            and (res_row["kind"] or "").lower() == "branch"
            and (row["branch_name"] or "") != (res_row["branch_name"] or "")
        )
        if both_branches:
            _log.info(
                "grant_resource: rejecting story=%s second WRITE branch '%s' in repo %s "
                "(already writes '%s')", story_id[:8], res_row["branch_name"],
                res_row["repo_name"], row["branch_name"],
            )
            return False, (
                f"This story already writes to branch '{row['branch_name']}' in repo "
                f"'{row['repo_name']}'. A story maps to at most ONE write branch per "
                f"repo (one git node per repo) — a second write branch in the same repo "
                f"would have no way for a task to say which branch it targets. Grant it "
                f"read-only ('needs') instead, or split the second branch's work into a "
                f"sibling story with a dependency."
            )
    _log.debug(
        "grant_resource: story=%s write grant for repo %s/%s accepted (multi-repo on)",
        story_id[:8], res_row["repo_owner"], res_row["repo_name"],
    )
    return True, ""


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

    # A TRUNK LANE IS GRANTABLE, AND GRANTING IT IS HOW A STORY SAYS WHERE IT LANDS.
    #
    # This refused it, and told the caller to "Grant the repo (kind='repo') instead".
    # The fear was real -- granting a trunk as a working ref would have the assistant
    # commit straight to it, bypassing branch/PR/merge -- and the conclusion has been
    # overtaken by the lane model:
    #
    #   `resolve_ws_trunk` (git_graph/repo_identity.py) reads the lane OFF
    #   `story_resources`: the trunk a workstream lands on is the trunk-lane resource
    #   its stories were granted. One repo can carry several lanes in one goal.
    #
    #   `kernel/workspace/mounts.py` is built around `relation IN ('produces',
    #   'modifies')` on a role='trunk' row -- "where the story has an explicit
    #   trunk-lane WRITE grant for a repo, the lane rows own that repo's checkouts
    #   outright". The engine still cuts a FEATURE branch from the lane; the grant
    #   names the target, it is not a checkout of trunk.
    #
    #   `kernel/go/AGENTS.md` instructs the GO in as many words: "Grant the trunk-lane
    #   branch resource with the correct relation -- the lane you grant is the lane
    #   the story lands on."
    #
    # So the kernel's granter (kernel/go/tools.py) has never refused this, and this one
    # did -- two granters disagreeing, with the legacy one served verbatim to agents
    # through the orchestrator and Slack. An agent obeying it grants the bare repo, the
    # story carries NO lane, and `resolve_ws_trunk` falls back to the oldest lane: work
    # lands on a branch nobody chose, silently, on exactly the multi-lane goals the
    # model exists for.
    #
    # NO REPLACEMENT GUARD IS ADDED HERE, deliberately. The checks that
    # survive are the ones below -- one write branch per repo per story,
    # and the multi-repo interlock -- and between them they already stop
    # a story acquiring two lanes of one repository, which is the case
    # the DAG cannot express. Refusing the lane grant on top of those
    # refused the only thing that tells the engine where to land.

    # Repos ARE grantable to a story under the Git Engine model. Granting a repo DECLARES
    # which repo the story works in (its target) — the signal the engine uses to know
    # where to cut the workstream branch. This does NOT expose the trunk: the engine cuts
    # the workstream branch from the repo's trunk and the provisioner collapses the bare
    # repo block into the cut branch, so the agent only sees/commits its feature branch.
    #
    # ONE REPO PER STORY (legacy rule, active while multi_repo_enabled is false): the
    # engine maps a story to a SINGLE repo (one git_node / one workstream branch). A
    # second, DIFFERENT repo would be SILENTLY DROPPED by plan derivation
    # (StoryInput.repo_id is scalar; resolve_story_repo LIMIT 1), so reject it here
    # rather than let work vanish. Cross-repo work = separate stories with a dependency.
    # (Re-granting the SAME repo, or the engine's same-repo branch, is fine.)
    #
    # MULTI-REPO (research/MULTI_REPO_STORIES_PLAN.md M1.3): with the flag on, N distinct
    # repos per story are allowed — each WRITE grant becomes its own node/branch/PR. The
    # rule that survives is decision #2: at most ONE WRITE BRANCH PER REPO, because the
    # DAG has no per-phase target-branch dimension. That narrower check lives below.
    if subject_type == "story" and get_settings().multi_repo_enabled \
            and (res_row["kind"] or "").lower() in ("repo", "branch") \
            and (res_row["repo_name"] or ""):
        # MULTI-REPO × LOCAL-EXECUTION INTERLOCK (audit decision D3). The flag-off
        # branch below cannot produce a multi-repo story at all, so this is the only
        # place the unsafe pairing can be created from the grant side. A story being
        # worked in someone's editor must not silently become multi-repo underneath
        # them: local execution resolves ONE repo for the clone and (independently)
        # one for the push token, and its landing path reports success without a
        # per-repo check. Checked BEFORE the write-branch rule because it is the
        # broader refusal — the pairing is unsafe regardless of branch topology.
        if await has_live_local_checkout(pool, subject_id):
            existing_repos = await count_story_repos(pool, subject_id)
            _log.warning(
                "resource_allocation: refusing repo grant %s to story=%s — story is "
                "checked out locally and already targets %d repo(s)",
                resource_id, subject_id, existing_repos,
            )
            return False, (
                "This story is currently checked out in a local editor, and local "
                "execution works one repository at a time. Granting it another "
                "repository would leave work in that repo unpushed while the story "
                "reported as landed. Release the local checkout first, or split the "
                "cross-repo work into a separate story."
            )
        ok, reason = await _check_no_new_write_repo_after_kickoff(
            pool, subject_id, resource_id, res_row, relation,
        )
        if not ok:
            return False, reason
        ok, reason = await _check_one_write_branch_per_repo(
            pool, subject_id, resource_id, res_row, relation,
        )
        if not ok:
            return False, reason
    elif subject_type == "story" and (res_row["kind"] or "").lower() in ("repo", "branch") \
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
