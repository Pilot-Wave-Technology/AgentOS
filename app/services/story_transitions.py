"""Shared story state-transition service.

Validates and applies story state changes, enforcing the same rules
regardless of the calling surface (HTTP router, Notion inbound, future platforms).

Returns (True, "") on success or (False, human-readable reason) on rejection
so callers can surface the reason to the user via their platform's mechanism.
"""
import logging

from app.models.story import STORY_VALID_TRANSITIONS

_log = logging.getLogger(__name__)

_STATE_MAP = {
    "accept": "accepted",
    "start": "in_progress",
    "done": "done",
    "block": "blocked",
    "reject": "rejected",
    "unblock": "in_progress",
}

# Dependency satisfaction. A CODE dep is satisfied by any terminal state. A MERGE dep
# distinguishes WHY it ended (spec §0/§12):
#   done/reviewed → it MERGED to trunk → satisfied.
#   cancelled     → the producer WORKSTREAM was TORN DOWN (GC removed it from the plan)
#                   → the dep is obsolete → satisfied (don't strand the consumer).
#   rejected      → an EXPLICIT human merge-cancel (§12 cancel / a closed-unmerged PR)
#                   → the work was deliberately NOT landed → dependents STAY BLOCKED
#                   (§0 exit-2; the human must re-plan). So 'rejected' is NOT satisfying.
#   in-flight (proposed/accepted/in_progress/blocked) → PR not yet merged → blocks.
# branches._is_cut_ready uses the SAME merge rule (cut-gate / start-gate / import-gate consistent).
_DEP_SATISFIED_SQL = (
    "((story_kind <> 'merge' AND state IN ('done', 'reviewed', 'cancelled', 'rejected')) "
    " OR (story_kind = 'merge' AND state IN ('done', 'reviewed', 'cancelled')))"
)


def dep_state_satisfies(state: str | None, story_kind: str | None) -> bool:
    """Python mirror of `_DEP_SATISFIED_SQL` — is a dependency in this
    (state, story_kind) satisfied for the start/cut/import gate?

    KEEP IN LOCKSTEP with `_DEP_SATISFIED_SQL` above. The board-annotation code
    (`stories.py`) uses this so the disabled "Start (blocked)" button matches the
    server-side gate exactly — a `reviewed` dep (council done-and-then-some)
    satisfies just like `done`; without this the board falsely blocks Start.
    A `None` state (dep row absent — deleted story) is caller-handled as
    non-blocking, matching the SQL's `WHERE id = ANY(...) AND NOT (...)`.
    """
    if state is None:
        return False
    if story_kind == "merge":
        return state in ("done", "reviewed", "cancelled")
    return state in ("done", "reviewed", "cancelled", "rejected")


async def unmet_story_dependencies(pool, story_id: str) -> list[dict]:
    """Return a story's UNMET dependencies (the §5.9 dependent-import gate). A merge
    dep is unmet unless it actually MERGED ('done'/'reviewed'); a code dep is unmet
    unless terminal. Returns [{id, title, state, story_kind}] — empty ⇒ all satisfied."""
    deps = await pool.fetchval("SELECT depends_on FROM stories WHERE id = $1", story_id)
    if not deps:
        return []
    rows = await pool.fetch(
        f"SELECT id, title, state, story_kind FROM stories "
        f"WHERE id = ANY($1) AND NOT {_DEP_SATISFIED_SQL}",
        list(deps),
    )
    return [dict(r) for r in rows]


async def _check_operator_workspace_ready(pool, goal_id: str, story_id: str) -> tuple[bool, str]:
    """Pre-flight check for operator-mode stories.

    Two hard requirements before Accept:
      1. A repo/branch resource with a real repo_name must be linked to the story.
      2. That resource must have branch_name set — workspace provisioning runs
         immediately at accept time and will fail without it.

    Under the Git Engine model the branch is cut by the engine (not the GO) and
    linked to the story via the branch-bridge (ensure_branch_resource), which sets
    branch_name. Both conditions must be met before the user can accept. (NOTE:
    when gitgraph auto-reconcile lands, this gate should allow accept on a PLANNED
    node + let provisioning lazy-wait — tracked with Components B/C.)

    Returns (True, "") when ready, or (False, human-readable reason) when not.
    """
    # Git Engine: the workstream branch is cut LAZILY by the engine (not pre-linked
    # by the GO), so accept is fine as long as the engine can resolve a repo for the
    # story — it will cut the branch and provisioning lazy-waits for it (Components
    # B/C). Uses the ENGINE resolver (single-repo fallback included). Gated: when
    # GITGRAPH is off this is skipped and the original branch_name gate runs.
    from app.config import get_settings as _get_settings
    if _get_settings().gitgraph_enabled:
        from app.services.git_graph.repo_identity import resolve_story_repo
        async with pool.acquire() as conn:
            eng_repo = await resolve_story_repo(conn, story_id, goal_id)
        if eng_repo:
            return True, ""
        # else: engine can't resolve a repo (no repo, or multi-repo unresolved) —
        # fall through to the original repo-missing messages below.

    linked = await pool.fetchrow(
        """SELECT gr.name, gr.branch_name
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND gr.goal_id = $2
              AND gr.status = 'active'
              AND gr.kind IN ('repo', 'branch')
              AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
              -- Never treat the repo's TRUNK designation as the story's working branch
              -- (would clone/commit on trunk). Work happens on the role='feature' branch.
              AND NOT (gr.kind = 'branch' AND gr.role = 'trunk')
            ORDER BY (gr.role = 'feature') DESC, (gr.kind = 'branch') DESC, gr.created_at ASC
            LIMIT 1""",
        story_id, goal_id,
    )
    if not linked:
        any_repo = await pool.fetchval(
            """SELECT id FROM goal_resources
               WHERE goal_id = $1 AND status = 'active'
               AND kind IN ('repo', 'branch') LIMIT 1""",
            goal_id,
        )
        if not any_repo:
            _log.info(
                "Operator accept blocked story=%s: goal %s has no repo resource",
                story_id, goal_id,
            )
            return False, (
                "Cannot accept: this goal has no GitHub repository resource. "
                "To add one: (1) install the GitHub App on the target repository, "
                "(2) then add it to this goal via the Resources panel."
            )
        _log.info(
            "Operator accept blocked story=%s: goal has repos but none linked to story",
            story_id,
        )
        return False, (
            "Cannot accept: no repo is allocated to this story, and the goal has "
            "more than one repo so the engine can't pick one automatically. A coding "
            "story must target at least one repo — ask the GO to grant the repo(s) "
            "this story works in (or link one via the Resources panel), then accept."
        )
    if not linked["branch_name"]:
        _log.info(
            "Operator accept blocked story=%s: linked repo '%s' has no branch_name set",
            story_id, linked["name"],
        )
        return False, (
            f"Cannot accept: the repo '{linked['name']}' has no working branch yet. "
            "The Git Engine cuts the branch from the trunk once the goal is reconciled "
            "— open the goal's Git Engine view and Reconcile (or wait for it), then accept."
        )
    return True, ""


# A user may have only ONE story actively in flight per goal. "In flight" =
# in_progress or blocked (blocked is reached only FROM in_progress and isn't
# terminal, so it still occupies the user). Keyed on assigned_to — NOT on
# active_thread_id — because the board "start" transition never binds a thread,
# so a thread-scoped check (routers.assistant.find_busy_story_on_thread) misses a
# board-started story. This is the single source of truth for the one-active-
# story rule; both the board start/unblock path and the local checkout path use it.
_ACTIVE_STORY_STATES = ("in_progress", "blocked")


async def find_conflicting_active_story(pool, goal_id: str, actor_uid: str, story_id: str):
    """Another story in this goal that `actor_uid` already has in flight
    (in_progress/blocked), or None. Excludes `story_id` itself so resuming a
    story never conflicts with itself."""
    return await pool.fetchrow(
        f"""SELECT id, title FROM stories
             WHERE goal_id = $1 AND id <> $2 AND assigned_to = $3
               AND state IN {_ACTIVE_STORY_STATES}
             LIMIT 1""",
        goal_id, story_id, actor_uid,
    )


async def apply_story_transition(
    goal_id: str,
    story_id: str,
    action: str,
    actor_uid: str,
    pool,
    blocked_reason: str = "",
    extra_allowed_from: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Validate and apply a story state transition.

    Enforces assignment ownership, dependency satisfaction, and resource
    availability — identical to the HTTP router's checks.  Side effects
    (timestamps, resource locks, GO notification, channel relay) are applied
    only on success.

    Returns:
        (True, "")               — transition applied
        (False, rejection_reason) — transition rejected; reason suitable for display
    """
    story = await pool.fetchrow(
        "SELECT id, goal_id, state, assigned_to, depends_on, title, execution_mode, "
        "       story_kind FROM stories WHERE id=$1 AND goal_id=$2",
        story_id, goal_id,
    )
    if not story:
        return False, "Story not found."

    # Engine-owned MERGE stories are driven by the Git Engine + detection + the human
    # PR-merge — NOT the board lifecycle. A board Accept/Start/Done/Reject/Block here
    # desyncs the merge story from its node: a manual 'done' falsely satisfies the
    # merge-dependency so consumers start against a trunk MISSING the producer's work,
    # and a board 'reject' leaves the PR open + engine helper stories running. The only
    # human controls are Approve/Cancel in the Git Engine view. (Engine diff/fix CODE
    # stories are deliberately NOT blocked — they're assistant-worked and need the
    # normal lifecycle.)
    if (story.get("story_kind") or "code") == "merge":
        return False, (
            "This is a Git Engine merge story — it lands automatically when the PR is "
            "merged. Use Approve/Cancel in the Git Engine view, not the story board."
        )

    old_state = story["state"]
    new_state = _STATE_MAP.get(action)
    if not new_state:
        return False, f"Unknown action '{action}'."

    # ALREADY THERE IS NOT A TRANSITION. A second Mark Done — a double
    # click, a retried request, a poll that raced — used to rewrite the
    # row and append another `story.state_changed` event saying a story
    # moved from `done` to `done`. The ledger is the record of what
    # happened, and that did not happen.
    #
    # It matters more now that completion is stamped: the stamp itself
    # is idempotent (COALESCE, and the trigger ignores a story already
    # in a terminal state), but a duplicate event would still be counted
    # as a second decision by anything that reads the ledger by kind.
    #
    # Reported as success, not as an error: the caller asked for the
    # story to be done and it is done.
    if old_state == new_state:
        return True, ""

    # `extra_allowed_from` IS AN OPT-IN, PER CALLER, AND NOT A WIDENING.
    #
    # The LOCAL lane finishes a story from `awaiting_merge`: the branch is
    # pushed, the pull request is merged, and Mark Done is the person's
    # last step. The shared table has no `awaiting_merge -> done` and must
    # not grow one -- adding it would let Slack's `/done` force a CLOUD
    # story to done with its pull request still open, which is the exact
    # thing `awaiting_merge` exists to prevent.
    #
    # So the caller that knows the story is local says so, and only that
    # caller's transition is allowed through. `test_the_story_transition_
    # table_is_unchanged` stays green because the table is unchanged.
    #
    # THIS IS A CORRECTION. Phase 4 dropped the transition entirely on the
    # claim that "the local Mark Done path touches neither that table nor
    # `apply_story_transition`". Line 238 of `local_execution_run.story_done`
    # calls exactly this function, and every local Mark Done from
    # `awaiting_merge` came back 409 -- which the extension then reported
    # as "its plan state changed", sending people to look at the plan.
    valid = list(STORY_VALID_TRANSITIONS.get(old_state, []))
    if new_state not in valid and old_state not in extra_allowed_from:
        return False, f"Transition {old_state} → {new_state} is not allowed."

    # Assignment: only the assigned user can drive a story's lifecycle.
    # Exception: unassigned story can be accepted by anyone (auto-assigns them).
    assigned = story.get("assigned_to") or ""
    if action == "accept":
        if assigned and assigned != actor_uid:
            _log.warning("BLOCKED: user %s tried to accept story %s assigned to %s", actor_uid, story_id, assigned)
            return False, "Only the assigned user can accept this story."
        execution_mode = story.get("execution_mode") or "operator"
        # Stage K: this gate exists because the operator's workspace IS a
        # checkout — accepting a story it cannot clone would strand it. A
        # kernel assistant clones nothing; its executor resolves its own
        # checkout from the story's grants at dispatch, and an errand
        # legitimately touches no repository at all. Applying a repo-and-branch
        # requirement to those refuses work that would have run fine.
        from app.services.assistant_kind import is_kernel_assistant_for_user  # noqa: PLC0415
        if execution_mode == "operator" and not await is_kernel_assistant_for_user(
            pool, assigned or actor_uid,
        ):
            ok, err = await _check_operator_workspace_ready(pool, goal_id, story_id)
            if not ok:
                _log.info("Accept blocked for operator story %s: %s", story_id, err)
                return False, err
    elif action in ("start", "done", "block", "reject", "unblock"):
        if not assigned:
            _log.warning("BLOCKED: story %s has no assignee, can't %s", story_id, action)
            return False, "Story has no assigned user. Accept it first to claim ownership."
        if assigned != actor_uid:
            _log.warning("BLOCKED: user %s tried to %s story %s assigned to %s", actor_uid, action, story_id, assigned)
            return False, f"Only the assigned user ({assigned}) can perform this action."

    # Concurrency guard: how many stories this user may have in flight at once
    # in this goal — one for the operator (whose single thread rotates between
    # stories, so a second would displace the first), four for the kernel
    # assistant (a thread each, so they coexist). Covers both "start"
    # (accepted → in_progress) and "unblock" (blocked → in_progress) since both
    # put a story in flight. The SAME function runs at the assistant-import
    # chokepoint — a limit enforced on one path and not the other is not a limit.
    if action in ("start", "unblock"):
        from app.services.thread_ceiling import refusal_for  # noqa: PLC0415
        refusal = await refusal_for(pool, goal_id, actor_uid, story_id)
        if refusal:
            _log.info(
                "BLOCKED: user %s tried to %s story %s — %s",
                actor_uid, action, story_id, refusal,
            )
            return False, refusal

    # Dependency and resource checks for "start"
    if action == "start":
        deps = story.get("depends_on") or []
        if deps:
            # Git-graph fix (§15 L8): a MERGE story (a git land) satisfies a
            # dependency only when 'done' — which means its whole group reached
            # trunk. A cancelled/rejected merge story never landed, so it must NOT
            # unblock dependents (else a consumer starts against trunk that lacks
            # the producer's work). Non-merge (code) stories keep the existing
            # convention: done/cancelled/rejected all release the chain. Scoped to
            # this git start-gate only; the engine's own lazy-cut
            # (git_graph.branches._is_cut_ready) enforces the same on branch cut,
            # and the cross-system cancelled/rejected-unblocks convention (council
            # _DEP_SATISFIED, Slack _unmet_dependencies) is deliberately unchanged.
            # 'reviewed' = a done story the council reviewed (done-and-then-some), so
            # it satisfies a dependency just like 'done' — for both code and merge deps.
            _SATISFIED = _DEP_SATISFIED_SQL
            dep_satisfied = await pool.fetchval(
                f"SELECT count(*) FROM stories WHERE id = ANY($1) AND {_SATISFIED}",
                deps,
            )
            if dep_satisfied < len(deps):
                # Collect titles of unresolved deps for a useful message
                blocking = await pool.fetch(
                    f"SELECT title FROM stories WHERE id = ANY($1) AND NOT {_SATISFIED}",
                    deps,
                )
                titles = ", ".join(r["title"] for r in blocking)
                _log.info("Start blocked for story %s — unresolved deps: %s", story_id, titles)
                return False, f"Blocked by unfinished stories: {titles}"

        unmet = await pool.fetch(
            """SELECT sr.resource_id FROM story_resources sr
               LEFT JOIN goal_resources gr ON gr.id = sr.resource_id
               WHERE sr.story_id = $1 AND sr.relation = 'needs'
                 AND (gr.id IS NULL OR (gr.locked_for IS NOT NULL AND gr.locked_for != $1))""",
            story_id,
        )
        if unmet:
            _log.info("Story %s has unmet resource deps: %s", story_id, [r["resource_id"] for r in unmet])
            return False, "Required resources are missing or locked by another story."

    # C2: a LOCAL story can be marked done only with an approved plan AND its DAG fully
    # executed. Enforced here (not only the local router) so the board / Slack / Notion
    # done-paths all inherit the gate. Gateway/operator stories are unaffected.
    if action == "done" and (story.get("execution_mode") or "") == "local":
        from app.services.local_dispatch import story_done_blocker  # noqa: PLC0415
        blocker = await story_done_blocker(story_id)
        if blocker:
            return False, blocker

    # Build and execute the UPDATE
    set_clauses = ["state = $1", "updated_at = now()"]
    vals: list = [new_state]
    idx = 2

    if action == "accept" and not story.get("assigned_to"):
        set_clauses.append(f"assigned_to = ${idx}")
        vals.append(actor_uid)
        idx += 1
    if action == "start":
        set_clauses.append("started_at = now()")
    if action == "done":
        set_clauses.append("completed_at = now()")
    if action == "block":
        set_clauses.append(f"blocked_reason = ${idx}")
        vals.append(blocked_reason)
        idx += 1
    if action == "unblock":
        set_clauses.append("blocked_reason = ''")

    vals.extend([story_id, goal_id])
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            f"UPDATE stories SET {', '.join(set_clauses)} WHERE id = ${idx} AND goal_id = ${idx + 1}",
            *vals,
        )
        from app.ledger.journal import append_event
        from app.ledger.schema import STORY_STATE_CHANGED
        from app.ledger.actors import human_actor
        # A story transition is a human/operator action (system if uid unknown).
        _tr_actor, _tr_type = await human_actor(actor_uid)
        await append_event(
            conn,
            actor_id=_tr_actor, actor_type=_tr_type,
            event_type=STORY_STATE_CHANGED,
            payload={
                "story_id": story_id,
                "title": story["title"],
                "from_state": old_state,
                "to_state": new_state,
                "action": action,
                "actor_uid": actor_uid,
            },
            context_goal_id=goal_id,
        )

    # Auto-lock the resources this story WRITES TO, on start.
    #
    # 'modifies' is included alongside 'produces': both are write grants (see
    # git_graph.repo_identity.WRITE_RELATIONS, which every multi-repo path resolves
    # through), and the GO may legitimately grant a repo either way. Locking only
    # 'produces' meant an identically-writable 'modifies' repo silently escaped the
    # lock — the lock and the write-set disagreed about what "this story writes to"
    # means. 'needs' is deliberately excluded: read-only context grants are not
    # written to and must never lock a repo out from under another story.
    #
    # Release is relation-agnostic (the 'done' branch below clears everything
    # locked_for this story), so widening what gets locked cannot strand a lock.
    if action == "start":
        await pool.execute(
            """UPDATE goal_resources SET locked_by=$1, locked_for=$2, locked_at=now()
               WHERE id IN (
                   SELECT resource_id FROM story_resources
                    WHERE story_id=$2 AND relation IN ('produces', 'modifies')
               ) AND (locked_for IS NULL OR locked_for = '')""",
            actor_uid, story_id,
        )

    # Release resource locks on done
    if action == "done":
        await pool.execute(
            "UPDATE goal_resources SET locked_by=NULL, locked_for=NULL, locked_at=NULL WHERE locked_for=$1",
            story_id,
        )

    # NOTE: the council's final story review is NOT enqueued here. A DB trigger
    # (council_story_done_notify) fires pg_notify('story_done') on ANY transition
    # into 'done' — this path included — and the council dispatcher's LISTENer
    # enqueues the review. That keeps the trigger the single source of truth
    # across every done-path (board, mark-done button, Slack, future paths).

    # Notify GO
    try:
        from app.services import go_events
        await go_events.on_task_state_changed(goal_id, story_id, old_state, new_state, actor_uid)
        if action == "block":
            await go_events.on_story_blocked(goal_id, story_id, blocked_reason or "No reason given")
    except Exception as exc:
        _log.warning("GO notification failed for story %s %s: %s", story_id, action, exc)

    # Notify all connected channels
    try:
        from app.channels.base import relay_lifecycle_event
        await relay_lifecycle_event(goal_id, "story.state_changed", {
            "story_id": story_id,
            "story_title": story["title"],
            "old_state": old_state,
            "new_state": new_state,
        })
    except Exception as exc:
        _log.warning("Channel lifecycle relay failed for story %s %s: %s", story_id, action, exc)

    _log.info("Story transition applied: story=%s %s→%s by %s", story_id, old_state, new_state, actor_uid)
    return True, ""


async def mark_story_reviewed(pool, goal_id: str, story_id: str) -> bool:
    """System transition done -> reviewed, applied by the council dispatcher when
    the final story review succeeds.

    This is NOT a user action — it bypasses the assignment-ownership checks of
    transition_story on purpose (the system, not the assignee, drives it). Once a
    story is 'reviewed', its SHCA workspace may be torn down on the next import
    (the import gate in operator_provisioning blocks until exactly this happens).
    Idempotent: only fires on a story still in 'done'.
    """
    row = await pool.fetchrow(
        "SELECT state, title FROM stories WHERE id=$1 AND goal_id=$2", story_id, goal_id,
    )
    if not row:
        _log.warning("mark_story_reviewed: story %s not found", story_id)
        return False
    if row["state"] != "done":
        # Already reviewed, or moved elsewhere — nothing to do.
        _log.info("mark_story_reviewed: story %s not in 'done' (state=%s); skipping",
                  story_id, row["state"])
        return False

    async with pool.acquire() as conn, conn.transaction():
        # Also clear the thread binding on this terminal transition. 'reviewed' is
        # terminal for the active-story resolver (_NON_TERMINAL_STATES excludes it),
        # so a reviewed story must not keep active_thread_id/is_active_focus — else the
        # chat workspace panel (which still keys off active_thread_id) renders the
        # finished story's cleaned-up workspace as a phantom "current story" while the
        # assistant correctly reports no active story. Mirrors the done + cancel clears;
        # completes the one-story-per-thread invariant across every terminal path.
        await conn.execute(
            "UPDATE stories SET state='reviewed', last_reviewed_state='done', "
            "active_thread_id=NULL, is_active_focus=false, updated_at=now() "
            "WHERE id=$1 AND goal_id=$2 AND state='done'",
            story_id, goal_id,
        )
        from app.ledger.journal import append_event
        from app.ledger.schema import STORY_STATE_CHANGED
        from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR_ID
        await append_event(
            conn,
            actor_id=_SYSTEM_ACTOR_ID, actor_type="system",
            event_type=STORY_STATE_CHANGED,
            payload={
                "story_id": story_id,
                "title": row["title"],
                "from_state": "done",
                "to_state": "reviewed",
                "action": "review_complete",
                "actor_uid": "council",
            },
            context_goal_id=goal_id,
        )

    try:
        from app.channels.base import relay_lifecycle_event
        await relay_lifecycle_event(goal_id, "story.state_changed", {
            "story_id": story_id,
            "story_title": row["title"],
            "old_state": "done",
            "new_state": "reviewed",
        })
    except Exception as exc:
        _log.warning("Channel lifecycle relay failed for story %s review_complete: %s", story_id, exc)

    _log.info("Story %s finalized: done → reviewed (council)", story_id)
    return True
