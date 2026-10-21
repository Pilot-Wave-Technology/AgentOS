"""Web-tier HTTP surface for the GO Git Dependency-Graph Manager (plan §11).

The web tier (multi-replica) hosts HTTP. It does DB-only topology work and fires
`NOTIFY git_dispatch` so the single-leader engine WORKER performs the actual git
ops (branch cut / sync / merge). Branch mutations never happen here — only the
engine worker touches branches (plan §5.0).

Step 5a surface: reconcile trigger, live node status, and the workspace
`synced-to` cooperative-sync record. Webhooks (push/PR/delete) are Step 9.
All endpoints are internal (Bearer = openclaw_auth_token), like the other
/internal engine/tool endpoints.
"""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role
from app.config import get_settings
from app.database import get_pool, resolve_goal_id
from app.services.git_graph import branches as gg_branches
from app.services.git_graph.dispatch import notify_git_dispatch
from app.services.git_graph.plan import compute_plan
from app.services.git_graph.reconcile import reconcile

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
_log = logging.getLogger(__name__)

# Stored webhook body cap (audit only — the discriminator fields are separate columns).
_MAX_WEBHOOK_PAYLOAD_BYTES = 256 * 1024


# ── Operator / verification UI (user-authed, goal-scoped) ────────────────────
# These render the live git-graph state + offer the WEB-SAFE triggers (reconcile,
# approve, refresh). Branch cut + merge are performed by the engine worker (the
# single writer) — the UI never mutates branches directly.

async def _require_member(uid: str, pid: str) -> str:
    """get_goal_role RETURNS None for a non-member (it does NOT raise), so the
    membership gate must be explicit — else any authenticated user could read /
    act on another goal's git state."""
    role = await get_goal_role(uid, pid)
    if role is None:
        raise HTTPException(status_code=403, detail="not a goal member")
    return role


def _holder_sub_state(node_state: str, pr_number) -> str:
    """Human-readable sub-state of a lease holder for the blocked-visibility view (§10)."""
    if pr_number:
        return f"PR #{pr_number} open — awaiting human merge"
    return {
        "QUEUED": "preparing to merge", "SYNCING": "syncing trunk in",
        "MERGING": "merging", "MERGEABLE": "ready to open PR",
        "CONFLICTED": "resolving a conflict", "PROTECTION_BLOCKED": "blocked by protection",
        "ESCALATED": "escalated — needs attention",
    }.get(node_state, node_state or "—")


async def _merge_lanes(gid: str) -> list[dict]:
    """The merge lanes this goal participates in (§10 blocked-visibility): for each
    (repo, trunk), the active HOLDER + the ordered wait line. The lane is repo-scoped
    so it can mix workstreams from OTHER goals — each entry names its goal."""
    pool = get_pool()
    lane_keys = await pool.fetch(
        "SELECT DISTINCT git_repo_id, trunk_branch FROM git_merge_queue "
        "WHERE goal_id = $1 AND status IN ('waiting', 'active') AND trunk_branch IS NOT NULL",
        gid,
    )
    lanes: list[dict] = []
    for lk in lane_keys:
        repo_id, trunk = lk["git_repo_id"], lk["trunk_branch"]
        repo = await pool.fetchrow("SELECT owner, name FROM git_repos WHERE id = $1", repo_id)
        entries = await pool.fetch(
            "SELECT q.node_id, q.status, q.enqueue_seq, q.goal_id, q.active_since, "
            "       n.branch_name, n.state AS node_state, n.pr_number, g.name AS goal_name, "
            "       (SELECT s.assigned_to FROM stories s "
            "          JOIN git_merge_group_nodes gmn ON gmn.merge_group_id = s.merge_group_id "
            "        WHERE gmn.git_node_id = q.node_id AND s.story_kind = 'merge' LIMIT 1) AS owner "
            "FROM git_merge_queue q JOIN git_nodes n ON n.id = q.node_id "
            "LEFT JOIN goals g ON g.id = q.goal_id "
            "WHERE q.git_repo_id = $1 AND q.trunk_branch = $2 AND q.status IN ('waiting', 'active') "
            "ORDER BY (q.status = 'active') DESC, q.enqueue_seq ASC",
            repo_id, trunk,
        )
        holder = None
        waiting: list[dict] = []
        for e in entries:
            d = dict(e)
            d["is_this_goal"] = (e["goal_id"] == gid)
            d["sub_state"] = _holder_sub_state(e["node_state"], e["pr_number"])
            if e["status"] == "active" and holder is None:
                holder = d
            else:
                d["position"] = len(waiting) + 1
                waiting.append(d)
        lanes.append({
            "repo": f'{repo["owner"]}/{repo["name"]}' if repo else repo_id,
            "trunk_branch": trunk, "holder": holder, "waiting": waiting,
        })
    return lanes


async def _shared_repo_view(gid: str) -> list[dict]:
    """Cross-goal auto-visibility (§11/§0): for each repo THIS goal uses, if OTHER
    goals also have live workstreams on it, surface them — so a shared repo's
    cross-goal coupling is auto-visible (no manual declaration). The engine is already
    repo-scoped (one canonical git_repos row across goals), so this is a pure read."""
    pool = get_pool()
    repos = await pool.fetch(
        "SELECT DISTINCT git_repo_id FROM git_nodes WHERE goal_id = $1", gid)
    shared: list[dict] = []
    for r in repos:
        repo_id = r["git_repo_id"]
        others = await pool.fetch(
            "SELECT n.branch_name, n.state, n.trunk_branch, n.pr_number, "
            "       n.goal_id, g.name AS goal_name "
            "FROM git_nodes n LEFT JOIN goals g ON g.id = n.goal_id "
            "WHERE n.git_repo_id = $1 AND n.goal_id <> $2 "
            "  AND n.state NOT IN ('MERGED', 'ABANDONED') "
            # Don't surface nodes whose goal is gone — the engine reap terminalizes
            # them to ABANDONED, but filter here too so they disappear immediately.
            # (status='deleted' is the soft-delete; state='cancelled' is lifecycle-cancel.)
            "  AND (g.id IS NULL OR (g.status <> 'deleted' AND g.state <> 'cancelled')) "
            "ORDER BY g.name NULLS LAST, n.branch_name",
            repo_id, gid)
        if not others:
            continue
        repo = await pool.fetchrow("SELECT owner, name FROM git_repos WHERE id = $1", repo_id)
        goal_names = sorted({(o["goal_name"] or o["goal_id"]) for o in others})
        shared.append({
            "repo": f'{repo["owner"]}/{repo["name"]}' if repo else repo_id,
            "goals": goal_names,
            "nodes": [dict(o) for o in others],
        })
    return shared


async def _git_state(gid: str) -> dict:
    """Read the live git-graph state for a goal (read-only) — the derived plan
    preview, the git_nodes, the merge stories, the merge queue, the merge lanes, and
    the cross-goal shared-repo view."""
    pool = get_pool()
    plan = (await compute_plan(gid)).to_dict()
    nodes = await pool.fetch(
        "SELECT id, branch_name, state, base_sha, last_synced_trunk_sha, "
        "       merge_commit_sha, pr_number, mergeable, last_error, updated_at "
        "FROM git_nodes WHERE goal_id = $1 ORDER BY branch_name",
        gid,
    )
    queue = await pool.fetch(
        "SELECT q.node_id, q.status, q.enqueue_seq, n.branch_name "
        "FROM git_merge_queue q JOIN git_nodes n ON n.id = q.node_id "
        "WHERE q.goal_id = $1 ORDER BY q.enqueue_seq",
        gid,
    )
    merges = await pool.fetch(
        "SELECT id, title, state, merge_group_id, merge_approved_by, merge_approved_at "
        "FROM stories WHERE goal_id = $1 AND story_kind = 'merge' AND state <> 'cancelled' "
        "ORDER BY created_at",
        gid,
    )
    return {
        "plan": plan,
        "nodes": [dict(r) for r in nodes],
        "queue": [dict(r) for r in queue],
        "merges": [dict(r) for r in merges],
        "lanes": await _merge_lanes(gid),
        "shared_repos": await _shared_repo_view(gid),
    }


@router.get("/goals/{gid}/git", response_class=HTMLResponse)
async def git_engine_page(gid: str, request: Request, user: dict = Depends(get_current_user)):
    pid = await resolve_goal_id(gid)
    role = await _require_member(user["uid"], pid)
    pool = get_pool()
    goal = await pool.fetchrow("SELECT id, name AS title FROM goals WHERE id = $1", pid)
    state = await _git_state(pid)
    return templates.TemplateResponse(
        "git/engine.html",
        {
            "request": request, "user": user, "pid": pid, "role": role,
            "goal": dict(goal) if goal else {"id": pid, "title": pid},
            "gitgraph_enabled": get_settings().gitgraph_enabled,
            **state,
        },
    )


@router.get("/goals/{gid}/git/ui/state", response_class=HTMLResponse)
async def git_ui_state(gid: str, request: Request, user: dict = Depends(get_current_user)):
    pid = await resolve_goal_id(gid)
    await _require_member(user["uid"], pid)
    state = await _git_state(pid)
    return templates.TemplateResponse(
        "git/_state.html", {"request": request, "pid": pid, **state})


@router.post("/goals/{gid}/git/ui/reconcile", response_class=HTMLResponse)
async def git_ui_reconcile(gid: str, request: Request, user: dict = Depends(get_current_user)):
    pid = await resolve_goal_id(gid)
    await _require_member(user["uid"], pid)
    try:
        await reconcile(pid)
        await notify_git_dispatch(pid)
    except Exception:
        _log.exception("git UI reconcile failed for goal %s", pid)
    state = await _git_state(pid)
    return templates.TemplateResponse(
        "git/_state.html", {"request": request, "pid": pid, **state})


@router.post("/goals/{gid}/git/ui/approve/{story_id}", response_class=HTMLResponse)
async def git_ui_approve(
    gid: str, story_id: str, request: Request, user: dict = Depends(get_current_user)
):
    pid = await resolve_goal_id(gid)
    await _require_member(user["uid"], pid)
    pool = get_pool()
    await pool.execute(
        "UPDATE stories SET merge_approved_at = COALESCE(merge_approved_at, now()), "
        "  merge_approved_by = COALESCE(merge_approved_by, $2), updated_at = now() "
        "WHERE id = $1 AND goal_id = $3 AND story_kind = 'merge'",
        story_id, user["uid"], pid,
    )
    await notify_git_dispatch(pid)
    state = await _git_state(pid)
    return templates.TemplateResponse(
        "git/_state.html", {"request": request, "pid": pid, **state})


@router.post("/goals/{gid}/git/ui/retry/{node_id}", response_class=HTMLResponse)
async def git_ui_retry(
    gid: str, node_id: str, request: Request, user: dict = Depends(get_current_user)
):
    """Re-queue a STUCK node after a human fixed the underlying cause (granted a missing
    App permission, repaired CI, reconfigured a required-signing trunk, reopened a PR).
    ESCALATED / CONFLICTED / PROTECTION_BLOCKED are otherwise terminal with no exit — this
    is THE recovery action. Resets the node to PLANNED so the engine re-runs the whole
    pipeline idempotently (create_ref is a no-op on the existing ref → advance →
    READY → re-enqueue), clears last_error + the fix-attempt counter, and reactivates a
    rejected merge story. Live git truth (behind_by / checks) re-gates everything."""
    pid = await resolve_goal_id(gid)
    await _require_member(user["uid"], pid)
    pool = get_pool()
    node = await pool.fetchrow(
        "SELECT id, goal_id, state FROM git_nodes WHERE id = $1", node_id)
    if node is None or node["goal_id"] != pid:
        raise HTTPException(status_code=404, detail="node not found")
    if node["state"] in ("ESCALATED", "CONFLICTED", "PROTECTION_BLOCKED"):
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE git_nodes SET state = 'PLANNED', last_error = NULL, "
                "  repark_count = 0, updated_at = now() WHERE id = $1 "
                "  AND state IN ('ESCALATED', 'CONFLICTED', 'PROTECTION_BLOCKED')",
                node_id)
            # If the escalation rejected the merge story (human PR-close), reactivate it
            # so enqueue_ready_merges re-enqueues the node once it reaches READY again.
            from app.services.story_state_ledger import record_states_where
            await record_states_where(
                conn, "proposed",
                where=("story_kind = 'merge' AND state = 'rejected' "
                       "AND merge_group_id IN (SELECT merge_group_id FROM "
                       "git_merge_group_nodes WHERE git_node_id = $1)"),
                params=(node_id,), actor_uid=user["uid"],
                action="merge_retry_requeued")
        _log.info("git retry: node %s re-queued (PLANNED) by %s", node_id, user["uid"])
        await notify_git_dispatch(pid)
    state = await _git_state(pid)
    return templates.TemplateResponse(
        "git/_state.html", {"request": request, "pid": pid, **state})


@router.get("/goals/{gid}/git/plan-preview")
async def git_plan_preview(gid: str, request: Request, user: dict = Depends(get_current_user)):
    """Read-only JSON of the derived plan (zero writes, zero GitHub) — the safest
    verification: see what the engine WOULD do from the current story DAG."""
    pid = await resolve_goal_id(gid)
    await _require_member(user["uid"], pid)
    return JSONResponse((await compute_plan(pid)).to_dict())


from app.internal_auth import verify_internal_bearer as _check_auth  # SSOT: app/internal_auth.py


@router.post("/goals/{gid}/git/reconcile")
async def git_reconcile(gid: str, request: Request):
    """Run the topology reconcile for a goal (DB-only, idempotent, safe in the web
    tier), then wake the engine to cut any newly-ready branches."""
    _check_auth(request)
    gid = await resolve_goal_id(gid)
    res = await reconcile(gid)
    await notify_git_dispatch(gid)
    return JSONResponse(res.to_dict())


@router.get("/git/nodes/{node_id}/status")
async def git_node_status(node_id: str, request: Request):
    """Live read-only ahead/behind status of a node's branch (plan §11)."""
    _check_auth(request)
    pool = get_pool()
    async with pool.acquire() as conn:
        node = await conn.fetchrow(
            "SELECT id, git_repo_id, branch_name, state FROM git_nodes WHERE id = $1",
            node_id,
        )
        if node is None:
            raise HTTPException(status_code=404, detail="node not found")
        node = dict(node)
        if node["state"] in ("PLANNED", "BRANCHING"):
            # No ref to compare yet — report state without a live GitHub call.
            return JSONResponse({"node_id": node_id, "state": node["state"]})
        status = await gg_branches.refresh_node_status(conn, node)
    return JSONResponse({"state": node["state"], **status})


@router.post("/git/nodes/{node_id}/synced-to")
async def git_node_synced_to(node_id: str, request: Request):
    """The workspace records the trunk SHA it cooperatively pulled (plan §5.5.2).

    CAS guarded on state='IN_PROGRESS' so it is the single cross-process write to
    git_nodes and can never lost-update against the engine (which never restacks
    an IN_PROGRESS branch)."""
    _check_auth(request)
    body = await request.json()
    sha = (body.get("sha") or "").strip()
    if not sha:
        return JSONResponse({"ok": False, "error": "sha required"}, status_code=400)
    pool = get_pool()
    updated = await pool.fetchrow(
        "UPDATE git_nodes SET last_synced_trunk_sha = $2, updated_at = now() "
        "WHERE id = $1 AND state = 'IN_PROGRESS' RETURNING id",
        node_id, sha,
    )
    if updated:
        await notify_git_dispatch()
        return JSONResponse({"ok": True})
    return JSONResponse(
        {"ok": False, "error": "node not found or not IN_PROGRESS"}, status_code=409
    )


@router.post("/git/merge-stories/{story_id}/approve")
async def approve_merge_story(story_id: str, request: Request):
    """Human approval gate for a merge story (plan §6/§11). Records the approval
    (idempotent — first approver wins) and wakes the engine; the engine then
    enqueues the group's READY nodes (READY + approved → QUEUED) and the lane
    lands them. Human approval is REQUIRED before any node is enqueued/merged.

    NOTE: the actual land needs the GitHub App `pull_requests:write` re-consent.
    This endpoint only records approval + enqueues; it never merges (engine-only)."""
    _check_auth(request)
    # P0-C — human-approval integrity: a merge approval must be a human decision,
    # never an agent's, and the approver must be a member of the story's goal.
    # (Browser path /goals/{gid}/git/ui/approve/{story_id} already enforces this;
    # this internal endpoint took approved_by from the body unchecked.)
    from app.internal_auth import get_agent_principal  # noqa: PLC0415
    if get_agent_principal(request) is not None:
        return JSONResponse(
            {"ok": False, "error": "merge approvals may only be decided by a human, not an agent"},
            status_code=403)
    body = await request.json()
    approver = (body.get("approved_by") or "").strip()   # uid of the approving human
    if not approver:
        return JSONResponse(
            {"ok": False, "error": "approved_by (uid) required"}, status_code=400)
    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT id, goal_id, story_kind, merge_group_id FROM stories WHERE id = $1",
        story_id,
    )
    if story is None:
        raise HTTPException(status_code=404, detail="story not found")
    if story["story_kind"] != "merge" or not story["merge_group_id"]:
        return JSONResponse({"ok": False, "error": "not a merge story"}, status_code=400)
    if await get_goal_role(approver, story["goal_id"]) is None:
        return JSONResponse(
            {"ok": False, "error": "approved_by is not a member of this story's goal"},
            status_code=403)
    # Record approval once; a re-approve keeps the original approver but still wakes
    # the engine (e.g. to enqueue a node that only just reached READY).
    await pool.execute(
        "UPDATE stories SET merge_approved_at = COALESCE(merge_approved_at, now()), "
        "  merge_approved_by = COALESCE(merge_approved_by, $2), updated_at = now() "
        "WHERE id = $1",
        story_id, approver,
    )
    await notify_git_dispatch(story["goal_id"])
    return JSONResponse({"ok": True, "approved_by": approver})


@router.post("/goals/{gid}/git/lanes/{node_id}/cancel", response_class=HTMLResponse)
async def cancel_merge_lane(
    gid: str, node_id: str, request: Request, user: dict = Depends(get_current_user),
):
    """Explicit cancel-authority action (§12) — the human escape for a stuck lane (the
    engine never auto-releases / no timeout). AUTHORITY: a member of the HOLDER's goal,
    OR a member of a goal blocked behind it on the same lane (the cross-goal cancel
    rule). Closes the PR, abandons the node, frees the slot (engine promotes the next),
    and marks the merge story 'rejected' so dependents STAY BLOCKED for re-plan."""
    pid = await resolve_goal_id(gid)
    await _require_member(user["uid"], pid)
    pool = get_pool()
    node = await pool.fetchrow(
        "SELECT id, goal_id, git_repo_id, trunk_branch, branch_name, pr_number, state "
        "FROM git_nodes WHERE id = $1", node_id,
    )
    if not node:
        raise HTTPException(status_code=404, detail="node not found")
    # Authority: a member of the holder's goal, OR (the viewing goal `pid`, of which the
    # user is a member) is waiting behind this holder on the same lane.
    allowed = (await get_goal_role(user["uid"], node["goal_id"])) is not None
    if not allowed:
        waiting_here = await pool.fetchval(
            "SELECT 1 FROM git_merge_queue q JOIN git_nodes n2 ON n2.id = q.node_id "
            "WHERE q.git_repo_id = $1 AND q.trunk_branch = $2 AND q.status = 'waiting' "
            "  AND n2.goal_id = $3 LIMIT 1",
            node["git_repo_id"], node["trunk_branch"], pid,
        )
        allowed = bool(waiting_here)
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="Only the holding goal's members, or a goal blocked behind it, may cancel this lane.")

    # Close the PR on GitHub (best-effort) so the cancel reaches GitHub too.
    if node["pr_number"] and node["state"] != "MERGED":
        try:
            from app.services.git_graph import github_ops as gh
            repo = await pool.fetchrow(
                "SELECT owner, name FROM git_repos WHERE id = $1", node["git_repo_id"])
            if repo:
                token = await gh.mint_repo_token(
                    repo["name"],
                    {"contents": "read", "metadata": "read", "pull_requests": "write"})
                await gh.close_pr(token, repo["owner"], repo["name"], node["pr_number"])
        except Exception:
            _log.exception("git cancel: close_pr failed for node %s", node_id)

    # Abandon the node, free the lease slot (engine promotes next on its tick), reject
    # the merge story (dependents stay blocked via the merge-dep rule), and cascade-
    # cancel the group's engine diff/fix stories so their assistant integration tasks
    # don't keep running on a torn-down branch (the orphaned 'Integrate dev …' stories).
    # One transaction so a crash can't leave a torn abandon state.
    from app.services.git_graph.merge_lane import (
        cancel_engine_stories_for_node,
        fan_out_cancel_notices,
        notify_merge_group_node_cancelled,
        reject_merge_story_if_last_live_node,
    )
    _pending_notices: list[tuple[str, str]] = []
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE git_nodes SET state = 'ABANDONED', last_error = $2, updated_at = now() "
            "WHERE id = $1 AND state <> 'MERGED'",
            node_id, f"merge cancelled by {user['uid']}"[:500])
        await conn.execute(
            "UPDATE git_merge_queue SET status = 'parked' WHERE node_id = $1", node_id)
        # Reject the merge story only if this was the group's last live node — on a
        # multi-repo story the healthy sibling repos must stay landable. When they
        # survive, tell the operator instead: the merge story stays open and would
        # otherwise never complete, with nothing on screen explaining why.
        _rejected = await reject_merge_story_if_last_live_node(
            conn, node_id, f"merge cancelled by {user['uid']}",
        )
        if not _rejected:
            _pending_notices = await notify_merge_group_node_cancelled(
                conn, node_id, f"merge cancelled by {user['uid']}",
            )
        # Node-scoped (M2.4): the operator cancelled THIS lane. A sibling repo sharing
        # the merge group is untouched and its diff/fix story must keep running.
        for g in await conn.fetch(
            "SELECT merge_group_id FROM git_merge_group_nodes WHERE git_node_id = $1", node_id
        ):
            await cancel_engine_stories_for_node(conn, node_id, g["merge_group_id"])
    # After COMMIT — notify_new_message takes its own pool connection, so firing it
    # inside the transaction would announce a message that a rollback then removed.
    await fan_out_cancel_notices(_pending_notices)
    await notify_git_dispatch(node["goal_id"])
    _log.info("git cancel: node %s (lane %s) cancelled by %s", node_id,
              node["branch_name"], user["uid"])
    state = await _git_state(pid)
    return templates.TemplateResponse(
        "git/_state.html", {"request": request, "pid": pid, **state})


@router.post("/webhooks/github/{repo_id}")
async def github_webhook(repo_id: str, request: Request):
    """GitHub webhook intake — EVENT-AWARE + LOGGED (spec §4 / §0.1).

    Public endpoint (GitHub calls it), verified by HMAC — NOT the internal Bearer.
    It (1) verifies the signature, (2) parses `X-GitHub-Event`/`-Delivery` + the
    payload `action` + the merged-vs-closed discriminator, (3) PERSISTS the raw
    delivery to `git_webhook_events` (delivery_id UNIQUE → at-least-once dedup +
    replay/audit), and (4) NOTIFYs the engine. It NEVER mutates domain state
    (git_nodes / git_merge_queue) — that stays single-writer in the engine leader,
    which reads AUTHORITATIVE state from the API. Acks fast (<10s) so GitHub doesn't
    retry on our latency.

    The engine learns of CI/trunk changes only if the App is SUBSCRIBED to the
    relevant events (enable-time, user-provided): push, pull_request (incl.
    synchronize), pull_request_review, check_run/check_suite/status. A blocked merge
    CLICK fires no event — the engine reacts to the CAUSE events above. Consuming
    these rows (detection/finalize) is build §11 step 3."""
    raw = await request.body()
    secret = get_settings().github_webhook_secret
    if secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="bad webhook signature")

    event = request.headers.get("X-GitHub-Event", "") or ""
    delivery_id = request.headers.get("X-GitHub-Delivery", "") or ""
    try:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            payload = {}
    except (ValueError, TypeError):
        payload = {}

    action = payload.get("action")
    repo_full_name = (payload.get("repository") or {}).get("full_name")
    pr = payload.get("pull_request") or {}
    pr_number = pr.get("number")
    # `merged` is the closed-PR discriminator (merged vs human-cancel); only
    # meaningful on a pull_request `closed` event — NULL everywhere else.
    merged = pr.get("merged") if (event == "pull_request" and action == "closed") else None

    # Cap the stored body (large `push`/`pull_request` events would bloat the table).
    # The discriminator fields live in their own columns, so a truncated payload still
    # carries everything the engine needs; the marker preserves auditability.
    payload_json = json.dumps(payload)
    if len(payload_json) > _MAX_WEBHOOK_PAYLOAD_BYTES:
        payload_json = json.dumps(
            {"_truncated": True, "bytes": len(payload_json), "event": event, "action": action})

    # Log the delivery (dedup on delivery_id). Best-effort: a logging failure must
    # NOT drop the wake. ON CONFLICT DO NOTHING → a redelivery is a no-op, never a
    # 500 (a 500 would make GitHub retry forever).
    if delivery_id:
        try:
            await get_pool().execute(
                "INSERT INTO git_webhook_events "
                "(delivery_id, event, action, git_repo_id, repo_full_name, pr_number, merged, payload) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb) "
                "ON CONFLICT (delivery_id) DO NOTHING",
                delivery_id, event, action, repo_id, repo_full_name,
                pr_number, merged, payload_json,
            )
        except Exception:
            _log.exception("git webhook: failed to log delivery %s", delivery_id)

    # Always NOTIFY — even on a duplicate delivery: a redelivery may be the only
    # signal that survived a dropped earlier one. The engine re-derives from state.
    await notify_git_dispatch()
    return JSONResponse({"ok": True})
