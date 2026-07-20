"""Story management routes — board, detail, state transitions, internal API.

Stories are human-level work items under a goal, created by the GO agent.
They live in the `stories` table (separate from tasks).
"""
import json
from fastapi import Depends as _sec_Depends
from app.internal_auth import ownership_guard as _ownership_guard
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role
from app.config import get_settings
from app.internal_auth import verify_internal_bearer
from app.database import get_pool, resolve_goal_id
from app.ledger.journal import append_event
from app.ledger.schema import STORY_STATE_CHANGED, STORY_ASSIGNMENT_CHANGED
from app.models.story import story_weight_effort_label
from app.notifications import push_notification

# System actor for ledger events emitted by GO-driven story operations
# (the create_story / cancel_story / update_story tools all
# call these endpoints; the GO is the logical actor but we record under
# system since there's no per-GO uuid actor).
from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR_ID
from app.ledger.actors import human_actor, go_actor

_log = logging.getLogger(__name__)

router = APIRouter(dependencies=[_sec_Depends(_ownership_guard)])
templates = Jinja2Templates(directory="app/templates")

STORY_STATES_DISPLAY = ["proposed", "accepted", "in_progress", "blocked", "done", "reviewed"]


async def _goal_member_rows(pool, pid: str):
    """Members for a goal. The Platform Improvement goal (__platform__) is
    membership-free and admins-only (see auth.get_goal_role) — it has no
    goal_members rows, so every system admin is surfaced as a member there so
    the board/detail assignee lists aren't empty."""
    if pid == "__platform__":
        return await pool.fetch(
            "SELECT uid, display_name, email FROM users WHERE system_role = 'admin'")
    return await pool.fetch("""
        SELECT gm.uid, u.display_name, u.email FROM goal_members gm
        JOIN users u ON u.uid = gm.uid WHERE gm.goal_id = $1
    """, pid)


# ── Story Board ───────────────────────────────────────────────────────────────

@router.get("/goals/{pid}/stories", response_class=HTMLResponse)
async def story_board(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    pool = get_pool()
    goal = dict(await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid))

    stories = await pool.fetch("""
        SELECT * FROM stories
        WHERE goal_id = $1 AND state != 'cancelled'
        ORDER BY
            CASE state
                WHEN 'in_progress' THEN 1 WHEN 'blocked' THEN 2
                WHEN 'proposed' THEN 3 WHEN 'accepted' THEN 4
                WHEN 'done' THEN 5 WHEN 'reviewed' THEN 6 ELSE 7
            END, created_at
    """, pid)
    stories = [dict(s) for s in stories]

    # Annotate each story with start-readiness so the board can render Start
    # disabled when deps aren't met. Mirrors the check enforced in
    # story_state_change(action='start').
    if stories:
        all_ids = [s["id"] for s in stories]
        # Resource needs lookup once.
        unmet_res_rows = await pool.fetch("""
            SELECT sr.story_id, sr.resource_id, gr.name, gr.locked_for
              FROM story_resources sr
              LEFT JOIN goal_resources gr ON gr.id = sr.resource_id
             WHERE sr.story_id = ANY($1) AND sr.relation = 'needs'
               AND (gr.id IS NULL
                    OR (gr.locked_for IS NOT NULL AND gr.locked_for != sr.story_id))
        """, all_ids)
        unmet_by_story: dict[str, list] = {}
        for r in unmet_res_rows:
            unmet_by_story.setdefault(r["story_id"], []).append(
                {"name": r["name"] or r["resource_id"], "locked_for": r["locked_for"]}
            )
        # Operator proposed stories: classify whether a repo/branch resource is
        # linked and whether it has a branch_name set.
        # needs_resource=True  → no repo linked at all → Accept hard-disabled
        # needs_branch_name=True → repo linked but no branch set → soft advisory only
        operator_proposed_ids = [
            s["id"] for s in stories
            if s["state"] == "proposed"
            and (s.get("execution_mode") or "operator") == "operator"
        ]
        # story_id → True if branch_name set, False if only repo_name set
        repo_by_story: dict[str, bool] = {}
        if operator_proposed_ids:
            rows = await pool.fetch(
                """SELECT DISTINCT ON (sr.story_id)
                       sr.story_id,
                       (gr.branch_name IS NOT NULL AND gr.branch_name <> '') AS has_branch_name
                     FROM story_resources sr
                     JOIN goal_resources gr ON gr.id = sr.resource_id
                    WHERE sr.story_id = ANY($1)
                      AND gr.status = 'active'
                      AND gr.kind IN ('repo', 'branch')
                      AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
                    ORDER BY sr.story_id,
                             (gr.branch_name IS NOT NULL AND gr.branch_name <> '') DESC""",
                operator_proposed_ids,
            )
            repo_by_story = {r["story_id"]: bool(r["has_branch_name"]) for r in rows}
            needs_resource_count = sum(
                1 for sid in operator_proposed_ids if sid not in repo_by_story
            )
            _log.debug(
                "story_board: goal=%s operator-proposed=%d needs_resource=%d needs_branch_name=%d",
                pid, len(operator_proposed_ids),
                needs_resource_count,
                sum(1 for v in repo_by_story.values() if not v),
            )

        # Under the Git Engine, the workstream branch is cut LAZILY by the engine
        # AFTER accept (not pre-created by the GO), so "repo linked, no branch yet"
        # is the normal pre-accept state and must NOT block Accept. And a single-repo
        # goal resolves its one repo automatically (engine fallback), so a story with
        # no explicit repo link is still acceptable there. needs_resource only blocks
        # when the engine genuinely can't resolve a repo: no link AND the goal has
        # more than one repo (multi-repo → the GO must grant one).
        gitgraph_on = get_settings().gitgraph_enabled
        goal_repo_count = await pool.fetchval(
            "SELECT count(*) FROM goal_resources WHERE goal_id = $1 AND kind = 'repo' "
            "AND status = 'active' AND repo_name IS NOT NULL AND repo_name <> ''",
            pid,
        ) if gitgraph_on else 0

        # Dep metadata for the start-gate mirror. `stories` excludes cancelled,
        # but a cancelled dep IS satisfying (server rule), so fetch every
        # referenced dep's (state, story_kind) explicitly — including cancelled.
        from app.services.story_transitions import dep_state_satisfies  # noqa: PLC0415
        dep_ids = {d for s in stories for d in (s.get("depends_on") or [])}
        dep_meta: dict[str, tuple] = {}
        if dep_ids:
            dep_rows = await pool.fetch(
                "SELECT id, state, story_kind FROM stories WHERE id = ANY($1)",
                list(dep_ids),
            )
            dep_meta = {r["id"]: (r["state"], r["story_kind"]) for r in dep_rows}

        for s in stories:
            deps = s.get("depends_on") or []
            # A dep blocks only if its row exists AND it is not satisfied. A
            # missing row (deleted story) is non-blocking — mirrors the server's
            # `WHERE id = ANY(...) AND NOT _DEP_SATISFIED_SQL`.
            blocked_deps = [
                d for d in deps
                if d in dep_meta and not dep_state_satisfies(*dep_meta[d])
            ]
            s["blocking_deps"] = blocked_deps
            s["unmet_resources"] = unmet_by_story.get(s["id"], [])
            s["deps_met"] = not blocked_deps and not s["unmet_resources"]
            if (
                s["state"] == "proposed"
                and (s.get("execution_mode") or "operator") == "operator"
            ):
                has_branch_name = repo_by_story.get(s["id"])  # None = not linked
                if gitgraph_on:
                    resolvable = has_branch_name is not None or goal_repo_count == 1
                    s["needs_resource"] = not resolvable
                    s["needs_branch_name"] = False  # engine cuts the branch lazily on accept
                else:
                    s["needs_resource"] = has_branch_name is None
                    s["needs_branch_name"] = has_branch_name is False
            else:
                s["needs_resource"] = False
                s["needs_branch_name"] = False

    # Task progress per story
    task_counts = {}
    if stories:
        story_ids = [s["id"] for s in stories]
        rows = await pool.fetch("""
            SELECT story_id,
                   count(*) AS total,
                   count(*) FILTER (WHERE state IN ('done', 'approved')) AS done
            FROM tasks
            WHERE story_id = ANY($1) AND status = 'active'
              AND review_target_id IS NULL
            GROUP BY story_id
        """, story_ids)
        task_counts = {r["story_id"]: {"total": r["total"], "done": r["done"]} for r in rows}

    # Group by state. Cosmetic-only: git-engine stories (created_by='engine' —
    # the merge/diff/fix git ops) that are 'done' render under the 'reviewed'
    # column, so 'reviewed' reads as the terminal column for every story. Their
    # real DB state stays 'done' — this is display bucketing only, NOT a state
    # transition, so it can't trigger the mid-grind workspace teardown that a
    # real done→reviewed move would (see council/runs.enqueue_story_review).
    columns = {s: [] for s in STORY_STATES_DISPLAY}
    for s in stories:
        state = s["state"]
        if state == "done" and s.get("created_by") == "engine":
            state = "reviewed"
        if state in columns:
            columns[state].append(s)

    # Members
    member_rows = await _goal_member_rows(pool, pid)
    member_names = {m["uid"]: m["display_name"] or m["email"] for m in member_rows}
    members = [{"uid": m["uid"], "name": m["display_name"] or m["email"]} for m in member_rows]

    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "story-board-intro")

    return templates.TemplateResponse(
        "stories/board.html",
        {
            "request": request, "user": user, "goal": goal, "pid": pid,
            "stories": stories, "columns": columns,
            "story_states": STORY_STATES_DISPLAY,
            "task_counts": task_counts, "member_names": member_names,
            "members": members, "role": role,
            "auto_fire_tour": auto_fire_tour,
        },
    )


# ── Story Detail ──────────────────────────────────────────────────────────────

@router.get("/goals/{pid}/stories/{sid}", response_class=HTMLResponse)
async def story_detail(
    pid: str, sid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT * FROM stories WHERE id = $1 AND goal_id = $2", sid, pid,
    )
    if not story:
        raise HTTPException(status_code=404)
    story = dict(story)
    story["story_weight_label"] = story_weight_effort_label(story.get("story_weight"))

    goal = dict(await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid))

    # Child tasks — exclude review tasks (internal pipeline, not story work)
    child_tasks = await pool.fetch("""
        SELECT * FROM tasks
        WHERE story_id = $1 AND status = 'active'
          AND review_target_id IS NULL
        ORDER BY created_at
    """, sid)
    child_tasks = [dict(t) for t in child_tasks]
    done_count = sum(1 for t in child_tasks if t["state"] in ("done", "approved"))

    # Dependency stories
    dep_stories = []
    if story.get("depends_on"):
        dep_stories = await pool.fetch(
            "SELECT id, title, state, story_kind FROM stories WHERE id = ANY($1)", story["depends_on"],
        )
        dep_stories = [dict(d) for d in dep_stories]

    # Downstream stories
    downstream = await pool.fetch("""
        SELECT id, title, state FROM stories
        WHERE goal_id = $1 AND $2 = ANY(depends_on) AND state != 'cancelled'
    """, pid, sid)
    downstream = [dict(d) for d in downstream]

    # Mirror the server-side start-gate (story_transitions._DEP_SATISFIED_SQL):
    # 'reviewed'/'cancelled' (+ 'rejected' for code deps) satisfy, not just 'done'.
    from app.services.story_transitions import dep_state_satisfies  # noqa: PLC0415
    deps_met = all(
        dep_state_satisfies(d["state"], d.get("story_kind")) for d in dep_stories
    ) if dep_stories else True

    # Resources allocated to this story specifically (NOT all goal
    # resources). Joins story_resources → goal_resources so we surface
    # the GO's per-story tactical note (sr.notes) and the relation
    # (needs/produces/modifies). DISTINCT ON collapses duplicate
    # (story, resource) rows that arise when a single resource is
    # linked with multiple relations (e.g. produces + needs).
    resources = await pool.fetch("""
        SELECT * FROM (
            SELECT DISTINCT ON (gr.id)
                   gr.id, gr.kind, gr.name, gr.uri,
                   gr.repo_name, gr.branch_name, gr.human_context, gr.locked_by,
                   sr.relation, sr.notes AS go_note
              FROM story_resources sr
              JOIN goal_resources gr ON gr.id = sr.resource_id
             WHERE sr.story_id = $1 AND gr.status = 'active'
             ORDER BY gr.id,
                      (COALESCE(sr.notes, '') <> '') DESC,
                      sr.relation
        ) dedup
        ORDER BY kind, name
    """, sid)
    resources = [dict(r) for r in resources]

    # Member names
    member_rows = await _goal_member_rows(pool, pid)
    member_names = {m["uid"]: m["display_name"] or m["email"] for m in member_rows}

    # needs_resource=True  → no repo/branch resource linked at all → Accept hard-disabled
    # needs_branch_name=True → repo linked but no branch_name set → soft advisory, Accept enabled
    needs_resource = False
    needs_branch_name = False
    if story["state"] == "proposed" and (story.get("execution_mode") or "operator") == "operator":
        has_repo = any(
            r["kind"] in ("repo", "branch") and r.get("repo_name")
            for r in resources
        )
        has_branch = any(
            r["kind"] in ("repo", "branch") and r.get("branch_name")
            for r in resources
        )
        gitgraph_on = get_settings().gitgraph_enabled
        if gitgraph_on:
            # Engine cuts the branch lazily on accept, so a missing branch never
            # blocks. A single-repo goal resolves its one repo automatically; only
            # a multi-repo goal with no repo linked needs the GO to grant one.
            goal_repo_count = await pool.fetchval(
                "SELECT count(*) FROM goal_resources WHERE goal_id = $1 AND kind = 'repo' "
                "AND status = 'active' AND repo_name IS NOT NULL AND repo_name <> ''",
                pid,
            )
            needs_resource = (not has_repo) and goal_repo_count != 1
            needs_branch_name = False
            if needs_resource:
                _log.debug("story_detail: story=%s (multi-repo goal) needs a repo allocated", sid)
        else:
            if not has_repo:
                needs_resource = True
                _log.debug("story_detail: story=%s needs a repo/branch resource linked", sid)
            elif not has_branch:
                needs_branch_name = True
                _log.debug("story_detail: story=%s has repo linked but no branch_name set", sid)

    return templates.TemplateResponse(
        "stories/detail.html",
        {
            "request": request, "user": user, "goal": goal, "pid": pid,
            "story": story, "child_tasks": child_tasks,
            "done_count": done_count, "total_count": len(child_tasks),
            "dep_stories": dep_stories, "downstream": downstream,
            "deps_met": deps_met, "member_names": member_names,
            "resources": resources, "role": role,
            "needs_resource": needs_resource,
            "needs_branch_name": needs_branch_name,
        },
    )


# ── Story Execution Mode Toggle (Phase 4) ───────────────────────────────────

# Must be declared BEFORE the catch-all /{action} handler below — FastAPI
# matches by registration order. The catch-all would otherwise swallow this
# path with action='execution-mode' and fall through to a redirect.

@router.post("/goals/{pid}/stories/{sid}/execution-mode", response_class=HTMLResponse)
async def story_execution_mode(
    pid: str, sid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Toggle a story's execution_mode between 'ia_dispatch' and 'operator'.

    Operator mode means SHCA will drive a Claude Code subprocess directly
    when the story is picked up, instead of writing a task spec and
    dispatching to the planner-ia / dev-ia pipeline.

    Restrictions:
      - Only modifiable on stories in 'proposed' or 'accepted' state
        (before pickup). Stories already in_progress have a SHCA session
        attached and a Claude Code session may already be spawned;
        switching modes mid-flight would conflict.
      - Only the story's assignee (or unassigned + the toggler) can change it.
        Admins do NOT override here — execution_mode is the assignee's
        operational choice for their own work.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    form = await request.form()
    new_mode = (form.get("mode") or "").strip()
    if new_mode not in ("ia_dispatch", "operator"):
        return RedirectResponse(f"/goals/{pid}/stories/{sid}", status_code=303)

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT state, assigned_to, execution_mode FROM stories WHERE id = $1 AND goal_id = $2",
        sid, pid,
    )
    if not story:
        raise HTTPException(status_code=404)

    # Lock execution_mode once the story has left the pre-pickup states.
    # accepted = SHCA hasn't picked up yet; safe to toggle.
    if story["state"] not in ("proposed", "accepted"):
        _log.warning(
            "BLOCKED: cannot change execution_mode on story %s in state %s "
            "(user=%s, requested mode=%s)",
            sid, story["state"], user["uid"], new_mode,
        )
        return RedirectResponse(f"/goals/{pid}/stories/{sid}", status_code=303)

    # Assignee check.
    assigned = story.get("assigned_to") or ""
    if assigned and assigned != user["uid"]:
        _log.warning(
            "BLOCKED: user %s tried to change execution_mode on story %s "
            "assigned to %s",
            user["uid"], sid, assigned,
        )
        return RedirectResponse(f"/goals/{pid}/stories/{sid}", status_code=303)

    if story["execution_mode"] == new_mode:
        return RedirectResponse(f"/goals/{pid}/stories/{sid}", status_code=303)

    await pool.execute(
        "UPDATE stories SET execution_mode = $1, updated_at = NOW() WHERE id = $2",
        new_mode, sid,
    )
    _log.info(
        "Story %s execution_mode: %s → %s (changed by %s)",
        sid, story["execution_mode"], new_mode, user["uid"],
    )

    return RedirectResponse(f"/goals/{pid}/stories/{sid}", status_code=303)


# ── Story State Transitions ──────────────────────────────────────────────────

@router.post("/goals/{pid}/stories/{sid}/{action}", response_class=HTMLResponse)
async def story_state_change(
    pid: str, sid: str, action: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT * FROM stories WHERE id = $1 AND goal_id = $2", sid, pid,
    )
    if not story:
        raise HTTPException(status_code=404)

    # Only 'block' carries a form field (blocked_reason); accept/start/done/
    # reject/unblock post an empty body, so don't parse a (possibly empty or
    # malformed) multipart body for them — that raised MultipartParseError.
    blocked_reason = ""
    if action == "block":
        try:
            form = await request.form()
            blocked_reason = form.get("blocked_reason", "")
        except Exception:
            blocked_reason = ""

    from app.services.story_transitions import apply_story_transition
    ok, reason = await apply_story_transition(pid, sid, action, user["uid"], pool, blocked_reason=blocked_reason)
    if not ok:
        _log.warning("Story transition rejected: story=%s action=%s reason=%s", sid, action, reason)
        raise HTTPException(status_code=409, detail=reason)

    new_state = {
        "accept": "accepted", "start": "in_progress", "done": "done",
        "block": "blocked", "reject": "rejected", "unblock": "in_progress",
    }.get(action, "")

    # Operator-mode workspace cleanup on terminal state transitions.
    # Story workspace lives at <agent-ws>/stories/<sid>/ on the openclaw VM.
    # On done/cancelled/rejected we tell the gateway to delete it. Best-
    # effort — gateway failures don't block the state transition.
    if new_state in ("done", "cancelled", "rejected"):
        active_thread_id = story.get("active_thread_id") or ""
        if active_thread_id:
            try:
                template_agent_id = await pool.fetchval(
                    "SELECT agent_id FROM threads WHERE id = $1",
                    active_thread_id,
                )
                if template_agent_id:
                    from app.services.operator_provisioning import delete_story_workspace
                    result = await delete_story_workspace(
                        goal_id=pid, story_id=sid,
                        agent_id=template_agent_id, thread_id=active_thread_id,
                    )
                    if not result.get("ok"):
                        _log.warning(
                            "Story %s entered %s but workspace cleanup failed: %s",
                            sid, new_state, result.get("error"),
                        )

                    # Snapshot token/cost usage into agent_metrics for operator
                    # stories. ia_dispatch stories are covered by the gateway's
                    # fireLifecycleWebhook on IA task completion; operator stories
                    # bypass that path entirely so we snapshot here instead.
                    if (story.get("execution_mode") or "operator") == "operator":
                        import asyncio as _asyncio
                        from app.services.story_metrics import record_story_session_metrics
                        _asyncio.create_task(record_story_session_metrics(
                            goal_id=pid,
                            story_id=sid,
                            template_agent_id=template_agent_id,
                            thread_id=active_thread_id,
                        ))
            except Exception as exc:
                _log.warning(
                    "Failed to clean up workspace for story %s: %s", sid, exc,
                )

    # NOTE: accepting a story now ONLY marks it accepted (the transition above).
    # It does NOT provision the SHCA workspace — that is the IMPORT path's job
    # (pick-shca?import_story=, which opens the story in the assistant). This
    # removes the accept+import double-provision race: a single "open in
    # assistant" used to both accept (firing this auto-kickoff) and import,
    # cloning the repo twice into the same dir. Slack /work does accept+import
    # itself (see app/slack/inbound.py). The user accepts on the board, then
    # opens the story in the assistant to start it.

    return RedirectResponse(f"/goals/{pid}/stories", status_code=303)


# ── Create Story (from board) ────────────────────────────────────────────────

@router.post("/goals/{pid}/stories", response_class=HTMLResponse)
async def create_story_from_board(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    form = await request.form()
    title = form.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title required")

    story_weight_raw = form.get("story_weight", "")
    story_weight = int(story_weight_raw) if story_weight_raw else None

    pool = get_pool()
    story_id = str(uuid.uuid4())[:8]

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("""
            INSERT INTO stories (id, goal_id, title, description, acceptance_criteria,
                                 assigned_to, priority, estimated_effort, story_weight,
                                 state, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'proposed', $10)
        """, story_id, pid, title, form.get("description", ""),
            form.get("acceptance_criteria", ""), form.get("assigned_to", ""),
            form.get("priority", "medium"), form.get("estimated_effort", ""),
            story_weight, user["uid"])

        # Ledger emit so audit trail + memory_v2 bridge see this creation.
        _h_actor, _h_type = await human_actor(user["uid"])
        await append_event(
            conn,
            actor_id=_h_actor, actor_type=_h_type,
            event_type="story.created",
            payload={
                "story_id": story_id,
                "title": title,
                "assigned_user_id": form.get("assigned_to", "") or "",
                "priority": form.get("priority", "medium"),
                "actor_uid": user["uid"],
            },
            context_goal_id=pid,
        )

    try:
        from app.channels.base import relay_lifecycle_event
        await relay_lifecycle_event(pid, "story.created", {
            "story_id": story_id,
            "story_title": title,
            "assigned_to": form.get("assigned_to", ""),
            "priority": form.get("priority", "medium"),
        })
    except Exception as exc:
        _log.warning("Channel lifecycle relay failed for story-created %s: %s", story_id, exc)

    return RedirectResponse(f"/goals/{pid}/stories", status_code=303)


# ── Story phase polling (browser) ────────────────────────────────────────────

@router.get("/goals/{pid}/stories/{sid}/phase")
async def story_phase_poll(pid: str, sid: str, request: Request):
    """Lightweight endpoint for browser polling during planning phase.

    Returns current_phase and current_plan_path so the chat UI can
    auto-enable the Approve Plan button without a full page reload.
    """
    user = await get_current_user(request)
    pool = get_pool()
    pid = await resolve_goal_id(pid)
    # get_goal_role returns None for a non-member rather than raising, so the
    # gate must be explicit here.
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)
    row = await pool.fetchrow(
        "SELECT current_phase, current_plan_path FROM stories WHERE id = $1 AND goal_id = $2",
        sid, pid,
    )
    if not row:
        _log.info("story_phase_poll: story=%s not found in goal=%s", sid, pid)
        raise HTTPException(status_code=404)
    _log.debug("story_phase_poll story=%s phase=%s plan_path=%s", sid, row["current_phase"], row["current_plan_path"])
    return JSONResponse({
        "current_phase": row["current_phase"] or "",
        "current_plan_path": row["current_plan_path"] or "",
    })


# ── Internal API (for OpenClaw tools) ─────────────────────────────────────────

@router.get("/internal/goals/{pid}/stories")
async def internal_list_stories(pid: str, request: Request):
    """List all stories for a goal with task progress. Used by story_list."""
    verify_internal_bearer(request)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    stories = await pool.fetch("""
        SELECT id, title, state, assigned_to, priority, depends_on,
               acceptance_criteria, progress_summary, estimated_effort,
               blocked_reason, created_at
        FROM stories WHERE goal_id = $1 AND state != 'cancelled'
        ORDER BY created_at
    """, pid)

    result = []
    for s in stories:
        s = dict(s)
        tc = await pool.fetchrow("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE state IN ('done', 'approved')) AS done
            FROM tasks WHERE story_id = $1 AND status = 'active'
              AND review_target_id IS NULL
        """, s["id"])
        s["task_count"] = tc["total"] if tc else 0
        s["task_done"] = tc["done"] if tc else 0
        s["progress_preview"] = (s.get("progress_summary") or "")[:200]
        s["created_at"] = str(s["created_at"]) if s.get("created_at") else ""
        result.append(s)

    return JSONResponse({"stories": result, "goal_id": pid})


@router.get("/internal/goals/{pid}/stories/{sid}")
async def internal_story_detail(pid: str, sid: str, request: Request):
    """Get story detail with tasks and deps. Used by story_detail."""
    verify_internal_bearer(request)

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT * FROM stories WHERE id = $1 AND goal_id = $2", sid, pid,
    )
    if not story:
        raise HTTPException(status_code=404)

    story = dict(story)
    # Convert datetimes
    for k in ("started_at", "completed_at", "created_at", "updated_at"):
        if story.get(k):
            story[k] = str(story[k])
    # Convert UUID fields (asyncpg returns these as uuid.UUID objects)
    if story.get("current_plan_id") is not None:
        story["current_plan_id"] = str(story["current_plan_id"])

    # Tasks
    tasks = await pool.fetch("""
        SELECT id, title, state, result_summary, parent_task_id
        FROM tasks WHERE story_id = $1 AND status = 'active'
        ORDER BY created_at
    """, sid)
    story["tasks"] = [dict(t) for t in tasks]

    # Dep stories
    if story.get("depends_on"):
        deps = await pool.fetch(
            "SELECT id, title, state FROM stories WHERE id = ANY($1)",
            story["depends_on"],
        )
        story["dependency_stories"] = [dict(d) for d in deps]
    else:
        story["dependency_stories"] = []

    return JSONResponse(story)


@router.post("/internal/goals/{pid}/stories/{sid}/update")
async def internal_story_update(pid: str, sid: str, request: Request):
    """Post a progress update. Merges with existing summary via Gemini.
    Used by story_update."""
    verify_internal_bearer(request)

    body = await request.json()
    update_text = body.get("update", "").strip()
    if not update_text:
        raise HTTPException(status_code=400, detail="update text required")

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT progress_summary, title FROM stories WHERE id = $1 AND goal_id = $2",
        sid, pid,
    )
    if not story:
        raise HTTPException(status_code=404)

    current = story["progress_summary"] or ""

    # Merge via Gemini
    merged = await _merge_progress_summary(current, update_text)

    await pool.execute(
        "UPDATE stories SET progress_summary = $1, updated_at = now() WHERE id = $2",
        merged, sid,
    )

    _log.info("Story %s progress updated (%d chars → %d chars)", sid, len(current), len(merged))
    return JSONResponse({"ok": True, "story_id": sid, "summary_length": len(merged)})


async def _resolve_assignee_to_uid(value: str, pool) -> tuple[str, str]:
    """Resolve a free-form assignee identifier to a canonical uid.

    Display names are rejected on purpose — multiple users can share the
    same name (we just hit this with two "Abhishek Chaudhary" rows). The
    uid is the only stable identifier; email is a usable proxy because
    it's enforced unique by Firebase.

    Returns (uid, error). On success error is "". On failure uid is "".
    Empty input is treated as "unassigned" — returns ("", "").
    """
    value = (value or "").strip()
    if not value:
        return "", ""

    # Email path — has '@'.
    if "@" in value:
        row = await pool.fetchrow(
            "SELECT uid FROM users WHERE LOWER(email) = LOWER($1) LIMIT 1",
            value,
        )
        if row:
            return row["uid"], ""
        return "", f"No user with email '{value}'"

    # Direct uid path. Firebase uids are typically 28 alphanumeric chars,
    # but we don't shape-check — just see if it matches a row.
    row = await pool.fetchrow(
        "SELECT uid FROM users WHERE uid = $1 LIMIT 1", value,
    )
    if row:
        return row["uid"], ""

    # Reject anything else. Display-name lookups would silently pick one
    # of multiple matches and corrupt downstream assignments.
    return "", (
        f"assigned_to '{value[:40]}' is not a recognised uid or email. "
        "Use the user's uid or email — display names are ambiguous."
    )


@router.post("/internal/goals/{pid}/stories/create")
async def internal_create_story(pid: str, request: Request):
    """Create a story. Used by create_story tool (GO agent)."""
    verify_internal_bearer(request)

    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "title required"}, status_code=400)

    story_weight = body.get("story_weight")
    story_weight = int(story_weight) if story_weight not in (None, "") else None

    pid = await resolve_goal_id(pid)
    pool = get_pool()
    story_id = str(uuid.uuid4())[:8]
    thread_id = str(uuid.uuid4())

    # Canonicalise assigned_to. Caller may pass uid OR email; we always
    # store the uid. Display names are rejected with a clear error so
    # the GO surfaces the problem to the human instead of corrupting
    # the assignment with a duplicate-name match.
    raw_assignee = body.get("assigned_to", "") or ""
    assignee_uid, assignee_err = await _resolve_assignee_to_uid(raw_assignee, pool)
    if assignee_err:
        return JSONResponse(
            {"ok": False, "error": assignee_err}, status_code=400,
        )

    # Resolve depends_on (GO may send titles or IDs)
    raw_deps = body.get("depends_on", [])
    depends_on = []
    for dep in raw_deps:
        if not dep:
            continue
        existing = await pool.fetchval(
            "SELECT id FROM stories WHERE id = $1 AND goal_id = $2", dep, pid)
        if existing:
            depends_on.append(existing)
        else:
            by_title = await pool.fetchval(
                "SELECT id FROM stories WHERE title = $1 AND goal_id = $2 LIMIT 1", dep, pid)
            if by_title:
                depends_on.append(by_title)
            else:
                _log.warning("Could not resolve story dep '%s'", dep)

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("""
            INSERT INTO stories (id, goal_id, title, description, acceptance_criteria,
                                 assigned_to, priority, estimated_effort, story_weight,
                                 state, depends_on, thread_id, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'proposed', $10, $11, $12)
        """, story_id, pid, title, body.get("description", ""),
            body.get("acceptance_criteria", ""), assignee_uid,
            body.get("priority", "medium"), body.get("estimated_effort", ""),
            story_weight, depends_on, thread_id, f"go-{pid}")

        await conn.execute(
            "INSERT INTO threads (id, goal_id, created_by, title) VALUES ($1, $2, $3, $4)",
            thread_id, pid, f"go-{pid}", f"Story: {title[:80]}")

        # Emit ledger event so the audit trail + memory_v2 event bridge see
        # the story creation. The same payload shape is consumed by
        # app/memory_v2/event_bridge.py to write an EVENT line to GO's
        # daily memory log.
        _go_actor, _go_type = await go_actor(pid)
        await append_event(
            conn,
            actor_id=_go_actor, actor_type=_go_type,
            event_type="story.created",
            payload={
                "story_id": story_id,
                "title": title,
                "assigned_user_id": assignee_uid or "",
                "priority": body.get("priority", "medium"),
                "thread_id": thread_id,
            },
            context_goal_id=pid,
        )

    # Notify assignee (uses the resolved canonical uid)
    if assignee_uid:
        try:
            from app.notifications import push_notification
            await push_notification(
                assignee_uid, type="story_proposed",
                title=f"New story proposed: {title[:60]}",
                body="Review and accept on the Story Board.",
                goal_id=pid, task_id=story_id,
                link=f"/goals/{pid}/stories/{story_id}")
        except Exception:
            pass

    # Link needs/produces resources if provided
    for res_id in body.get("needs_resources", []):
        await pool.execute("""
            INSERT INTO story_resources (story_id, resource_id, relation)
            VALUES ($1, $2, 'needs') ON CONFLICT DO NOTHING
        """, story_id, res_id)
    for res_id in body.get("produces_resources", []):
        await pool.execute("""
            INSERT INTO story_resources (story_id, resource_id, relation)
            VALUES ($1, $2, 'produces') ON CONFLICT DO NOTHING
        """, story_id, res_id)

    # Advisory: check for resource conflicts with other active stories
    warnings = []
    produces = body.get("produces_resources", [])
    if produces:
        conflicts = await pool.fetch("""
            SELECT sr.story_id, s.title, sr.resource_id, gr.name
            FROM story_resources sr
            JOIN stories s ON s.id = sr.story_id
            JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.resource_id = ANY($1)
            AND sr.relation IN ('produces', 'modifies')
            AND s.state NOT IN ('done', 'cancelled')
            AND s.goal_id = $2 AND s.id != $3
        """, produces, pid, story_id)
        for c in conflicts:
            warnings.append(
                f"Resource '{c['name']}' is also modified by story '{c['title']}'. "
                f"Consider adding a dependency."
            )

    _log.info("Created story %s in goal %s: %s", story_id, pid, title)

    # Notify all connected channels (Slack, Notion, etc.) — story.created lifecycle event
    try:
        from app.channels.base import relay_lifecycle_event
        assigned_display = ""
        if body.get("assigned_to"):
            assigned_display = await pool.fetchval(
                "SELECT display_name FROM users WHERE uid = $1", body["assigned_to"],
            ) or body["assigned_to"]
        await relay_lifecycle_event(pid, "story.created", {
            "story_id": story_id,
            "story_title": title,
            "assigned_to": assigned_display,
            "priority": body.get("priority", "medium"),
        })
    except Exception as exc:
        _log.warning("Channel lifecycle relay failed for story-created %s: %s", story_id, exc)
    result = {"ok": True, "story_id": story_id, "title": title}
    if warnings:
        result["warnings"] = warnings
    return JSONResponse(result)


@router.post("/internal/goals/{pid}/stories/{sid}/modify")
async def internal_update_story(pid: str, sid: str, request: Request):
    """Update a story's fields. Used by update_story tool."""
    verify_internal_bearer(request)

    body = await request.json()
    pid = await resolve_goal_id(pid)
    pool = get_pool()

    story = await pool.fetchrow("SELECT * FROM stories WHERE id = $1 AND goal_id = $2", sid, pid)
    if not story:
        return JSONResponse({"ok": False, "error": "Story not found"}, status_code=404)

    # Engine merge stories are owned by reconcile (it rewrites depends_on/title/assignee
    # every reconcile) and the Git Engine view (approve/cancel). A GO/tool edit of
    # depends_on or assigned_to here fights reconcile and corrupts the merge gate.
    if (story.get("story_kind") or "code") == "merge":
        return JSONResponse(
            {"ok": False,
             "error": "Merge stories are managed by the Git Engine and can't be edited."},
            status_code=409,
        )

    # Resolve assigned_to to a canonical uid if the caller is changing it.
    # Same rules as create — uid or email only, never display name.
    if "assigned_to" in body:
        assignee_uid, assignee_err = await _resolve_assignee_to_uid(
            body.get("assigned_to") or "", pool,
        )
        if assignee_err:
            return JSONResponse(
                {"ok": False, "error": assignee_err}, status_code=400,
            )
        body["assigned_to"] = assignee_uid

    if "story_weight" in body and body["story_weight"] not in (None, ""):
        body["story_weight"] = int(body["story_weight"])

    # requested_by_uid: optional, not yet populated by GO (see
    # app/ledger/schema.py:STORY_ASSIGNMENT_CHANGED) — resolved the same way
    # as assigned_to (uid or email) so it's ready the moment GO starts
    # sending it.
    requested_by_uid = None
    if body.get("requested_by_uid"):
        requested_by_uid, requester_err = await _resolve_assignee_to_uid(
            body["requested_by_uid"], pool,
        )
        if requester_err:
            return JSONResponse({"ok": False, "error": requester_err}, status_code=400)

    reassigning = "assigned_to" in body and body["assigned_to"] != (story.get("assigned_to") or "")

    updates = ["updated_at = now()"]
    values = []
    idx = 1
    for field in ("description", "acceptance_criteria", "priority",
                  "estimated_effort", "assigned_to", "story_weight"):
        if field in body:
            updates.append(f"{field} = ${idx}")
            values.append(body[field])
            idx += 1
    if "depends_on" in body:
        updates.append(f"depends_on = ${idx}")
        values.append(body["depends_on"])
        idx += 1

    if len(updates) <= 1:
        return JSONResponse({"ok": False, "error": "No fields to update"}, status_code=400)

    values.extend([sid, pid])
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            f"UPDATE stories SET {', '.join(updates)} WHERE id = ${idx} AND goal_id = ${idx + 1}",
            *values)

        if reassigning:
            if requested_by_uid:
                actor_row = await conn.fetchrow(
                    "SELECT actor_id FROM users WHERE uid = $1", requested_by_uid,
                )
                actor_id = actor_row["actor_id"] if actor_row else _SYSTEM_ACTOR_ID
                actor_type = "human" if actor_row else "system"
            else:
                actor_id, actor_type = _SYSTEM_ACTOR_ID, "system"
            await append_event(
                conn,
                actor_id=actor_id, actor_type=actor_type,
                event_type=STORY_ASSIGNMENT_CHANGED,
                payload={
                    "story_id": sid,
                    "from_assignee": story.get("assigned_to") or "",
                    "to_assignee": body["assigned_to"] or "",
                    "requested_by_uid": requested_by_uid or "",
                },
                context_goal_id=pid,
            )
            _log.info(
                "Story %s reassigned %s -> %s (actor_type=%s, requested_by_uid=%s)",
                sid, story.get("assigned_to") or "(none)", body["assigned_to"] or "(none)",
                actor_type, requested_by_uid or "(none)",
            )

    changed = [f for f in ("description", "acceptance_criteria", "priority",
                           "depends_on", "assigned_to", "estimated_effort",
                           "story_weight") if f in body]
    _log.info("Updated story %s: %s", sid, changed)
    return JSONResponse({"ok": True, "story_id": sid, "updated": changed})


@router.post("/internal/goals/{pid}/stories/{sid}/cancel")
async def internal_cancel_story(pid: str, sid: str, request: Request):
    """Cancel a story. Used by cancel_story tool."""
    verify_internal_bearer(request)

    body = await request.json()
    reason = body.get("reason", "Cancelled")
    pid = await resolve_goal_id(pid)
    pool = get_pool()

    story = await pool.fetchrow("SELECT * FROM stories WHERE id = $1 AND goal_id = $2", sid, pid)
    if not story:
        return JSONResponse({"ok": False, "error": "Story not found"}, status_code=404)

    old_state = story["state"]
    story_kind = story.get("story_kind") or "code"

    # INVARIANT: a STARTED code story is never cancelled. Once a story is in_progress
    # (or blocked, which is reached only from in_progress), its branch is cut and work
    # is committed — cancelling would strand commits and risk merging cancelled work.
    # Cancel is only for not-yet-started stories (proposed/accepted), which are pre-cut
    # so re-derivation is clean. A started story must be completed, or explicitly
    # aborted via the Git Engine lane-cancel (which tears the branch down deliberately).
    # (Merge stories are exempt — cancelling one is the human merge-REJECT path.)
    if story_kind != "merge" and old_state in ("in_progress", "blocked"):
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Cannot cancel '{(story['title'] or '')[:50]}' — it is already "
                    f"{old_state}, so its branch is cut and work exists. A started story "
                    f"must be completed, or explicitly aborted via the Git Engine view; it "
                    f"cannot be cancelled."
                ),
            },
            status_code=409,
        )

    # Clear active_thread_id on terminal transition (cancelled is a
    # terminal state per the watchdog's reap rules). Prevents stale
    # join matches between this finished story and the SHCA's persistent
    # thread when a new story comes in on the same thread later.
    # Wrapped in a transaction with the ledger event emit so the row
    # update and the audit event commit together.
    async with pool.acquire() as conn, conn.transaction():
        # A MERGE story (engine git-land) cancelled by a human is a merge REJECTION,
        # not a torn-down workstream → 'rejected' (keeps dependents blocked for re-plan;
        # 'cancelled' would read as "producer removed → dep obsolete" and wrongly unblock
        # them — spec §12 / git_graph dep rule). Code stories stay 'cancelled'.
        await conn.execute(
            "UPDATE stories SET "
            "  state = CASE WHEN story_kind = 'merge' THEN 'rejected' ELSE 'cancelled' END, "
            "  result_summary = $1, active_thread_id = NULL, updated_at = now() WHERE id = $2",
            reason, sid)
        # Dependency-bridging (code stories only): re-link every dependent of this story
        # onto THIS story's own dependencies, so a chain a→b→c with b cancelled becomes
        # a→c on the SAME branch — instead of orphaning c into its own workstream (which
        # would split the branch and leave a stale merge dependency). Classic DAG
        # node-removal with edge bridging; dedup + drop self-references.
        if story_kind != "merge":
            await conn.execute(
                "UPDATE stories d SET "
                "  depends_on = (SELECT COALESCE(array_agg(DISTINCT e), '{}') "
                "                FROM unnest(array_remove(d.depends_on, $1) || $2::text[]) AS e "
                "                WHERE e <> d.id), "
                "  updated_at = now() "
                "WHERE d.goal_id = $3 AND $1 = ANY(d.depends_on)",
                sid, list(story["depends_on"] or []), pid,
            )
        else:
            # Cancelling a MERGE story (→ rejected above) → cascade-cancel the group's
            # engine diff/fix stories; their integration is moot once the land is
            # abandoned. (Node-abandon for a cut node is the Git Engine lane-cancel.)
            from app.services.git_graph.merge_lane import cancel_engine_stories_for_group
            await cancel_engine_stories_for_group(conn, story.get("merge_group_id"))
        _go_actor, _go_type = await go_actor(pid)
        await append_event(
            conn,
            actor_id=_go_actor, actor_type=_go_type,
            event_type=STORY_STATE_CHANGED,
            payload={
                "story_id": sid,
                "title": story["title"],
                "from_state": old_state,
                "to_state": ("rejected" if story_kind == "merge" else "cancelled"),
                "reason": reason,
            },
            context_goal_id=pid,
        )

    # Re-derive the git topology so the cancelled story's PLANNED node is GC'd and the
    # bridged chain re-collapses correctly. Cancel is pre-cut (invariant), so reconcile
    # only prunes PLANNED nodes + cancels their merge stories — it never touches a live
    # branch. NOTIFY wakes the engine to act on the re-derivation.
    if get_settings().gitgraph_enabled:
        try:
            from app.services.git_graph.reconcile import reconcile as _gg_reconcile
            from app.services.git_graph.dispatch import notify_git_dispatch as _gg_notify
            await _gg_reconcile(pid)
            await _gg_notify(pid)
        except Exception as exc:
            _log.warning("cancel_story: git reconcile failed (goal=%s story=%s): %s", pid, sid, exc)

    # Notify all connected channels (Slack, Notion, etc.)
    try:
        from app.channels.base import relay_lifecycle_event
        await relay_lifecycle_event(pid, "story.state_changed", {
            "story_id": sid,
            "story_title": story["title"],
            "old_state": old_state,
            "new_state": ("rejected" if story_kind == "merge" else "cancelled"),
        })
    except Exception as exc:
        _log.warning("Channel lifecycle relay failed for story cancellation %s: %s", sid, exc)

    # Notify assignee
    if story.get("assigned_to"):
        try:
            from app.notifications import push_notification
            await push_notification(
                story["assigned_to"], type="story_cancelled",
                title=f"Story cancelled: {story['title'][:60]}",
                body=reason[:120],
                goal_id=pid, task_id=sid,
                link=f"/goals/{pid}/stories")
        except Exception:
            pass

    _log.info("Cancelled story %s: %s", sid, reason)
    return JSONResponse({"ok": True, "story_id": sid, "cancelled": True})


async def _merge_progress_summary(current: str, new_update: str) -> str:
    """Merge a new progress update into the existing summary using Gemini."""
    settings = get_settings()
    if not settings.gemini_api_key:
        # Fallback: just append
        if current:
            return f"{current}\n\n---\n\n{new_update}"
        return new_update

    prompt = f"""You are merging a progress update into an existing story summary.

Current summary:
{current or "No progress yet."}

New update:
{new_update}

Write an updated summary that incorporates the new information.
Keep it concise (under 500 words). Focus on: what was done, key decisions,
current state, and any deviations from the plan. Drop outdated details
that the new update supersedes. Write in past tense for completed work,
present tense for current state."""

    try:
        from google import genai
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as exc:
        _log.warning("Gemini merge failed, using append: %s", exc)
        if current:
            return f"{current}\n\n---\n\n{new_update}"
        return new_update
