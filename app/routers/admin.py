"""Admin panel routes — signup approval, system management."""
import logging
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_log = logging.getLogger(__name__)
from fastapi.templating import Jinja2Templates

from app.auth import require_admin, get_current_user, SYSTEM_ACTOR_ID
from app.roles import SystemRole, SYSTEM_ROLE_LABELS, GOAL_ROLE_LABELS
from app.database import get_pool
from app.flash import flash_redirect
from app.ledger.journal import append_event
from app.ledger.schema import SIGNUP_REQUESTED, SIGNUP_APPROVED, SIGNUP_REJECTED, USER_CREATED, USER_SYSTEM_ROLE_CHANGED, GOAL_MEMBER_ROLE_CHANGED

router = APIRouter(prefix="/settings")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["SYSTEM_ROLE_LABELS"] = SYSTEM_ROLE_LABELS
templates.env.globals["GOAL_ROLE_LABELS"] = GOAL_ROLE_LABELS

_USERS_PAGE_SIZE_DEFAULT = 10
_USERS_PAGE_SIZE_CHOICES = (8, 10, 15)
_GOAL_MEMBERSHIPS_PAGE_SIZE = 25


def _build_page_range(current: int, total: int) -> list:
    """Return page numbers to display, with None where ellipsis should appear."""
    if total <= 1:
        return []
    pages: set[int] = {1, total}
    for p in range(max(1, current - 2), min(total, current + 2) + 1):
        pages.add(p)
    result: list = []
    prev = None
    for p in sorted(pages):
        if prev is not None and p - prev > 1:
            result.append(None)
        result.append(p)
        prev = p
    return result


@router.get("/signup-requests", response_class=HTMLResponse)
async def list_signup_requests(
    request: Request,
    user: dict = Depends(require_admin),
):
    pool = get_pool()
    pending_rows = await pool.fetch(
        "SELECT * FROM signup_requests WHERE status = 'pending' ORDER BY created_at DESC",
    )
    rejected_rows = await pool.fetch(
        """SELECT * FROM signup_requests WHERE status = 'rejected'
           ORDER BY reviewed_at DESC NULLS LAST, created_at DESC""",
    )

    # How many times each uid has submitted a signup request (informational —
    # surfaces repeat resubmission attempts to the reviewing admin).
    count_rows = await pool.fetch(
        """SELECT payload->>'uid' AS uid, count(*) AS request_count
           FROM ledger_events WHERE event_type = $1 GROUP BY payload->>'uid'""",
        SIGNUP_REQUESTED,
    )
    request_counts = {r["uid"]: r["request_count"] for r in count_rows}

    pending = [dict(r) | {"request_count": request_counts.get(r["uid"], 1)} for r in pending_rows]
    rejected = [dict(r) | {"request_count": request_counts.get(r["uid"], 1)} for r in rejected_rows]

    return templates.TemplateResponse(
        "admin/signup_requests.html",
        {
            "request": request,
            "user": user,
            "pending_requests": pending,
            "rejected_requests": rejected,
        },
    )


@router.post("/signup-requests/{uid}/review")
async def review_signup_request(
    uid: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    form = await request.form()
    action = form.get("action", "")
    assigned_role = form.get("role", SystemRole.PLATFORM_MEMBER)

    _log.info(
        "signup_request review: admin=%s target_uid=%s action=%s",
        user["uid"], uid, action or "<missing>",
    )

    if action not in ("approve", "reject"):
        _log.warning(
            "signup_request review: invalid action=%s admin=%s target_uid=%s",
            action or "<missing>", user["uid"], uid,
        )
        return RedirectResponse(url="/settings/signup-requests", status_code=303)

    # Validate role — default to platform.member if an unrecognised value is submitted
    if assigned_role not in SystemRole.ALL:
        _log.warning(
            "signup_request review: invalid role=%s submitted, defaulting to platform.member admin=%s target_uid=%s",
            assigned_role, user["uid"], uid,
        )
        assigned_role = SystemRole.PLATFORM_MEMBER

    pool = get_pool()
    new_status = "approved" if action == "approve" else "rejected"

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Update signup request
            await conn.execute(
                """UPDATE signup_requests
                   SET status = $1, reviewed_by = $2, reviewed_at = now()
                   WHERE uid = $3""",
                new_status, user["uid"], uid,
            )

            # Fetch signup request for event payload
            sr = await conn.fetchrow(
                "SELECT * FROM signup_requests WHERE uid = $1", uid,
            )

            if action == "approve" and sr:
                # Log approval event
                await append_event(
                    conn,
                    event_type=SIGNUP_APPROVED,
                    payload={
                        "uid": uid,
                        "email": sr["email"],
                        "requested_role": sr["requested_role"],
                        "assigned_role": assigned_role,
                        "approved_by": user["uid"],
                    },
                    actor_id=user["actor_id"],
                )

                # Create user row immediately so they can log in
                await conn.execute(
                    """INSERT INTO users (uid, email, display_name, photo_url, system_role, status)
                       VALUES ($1, $2, $3, $4, $5, 'active')
                       ON CONFLICT (uid) DO NOTHING""",
                    uid, sr["email"], sr["display_name"], sr["photo_url"], assigned_role,
                )
                new_row = await conn.fetchrow("SELECT * FROM users WHERE uid = $1", uid)
                if new_row:
                    # Enroll the ledger `actors` principal here too — this is the
                    # SECOND user-creation path (signup approval). Without it, an
                    # approved user has users.actor_id but no actors row, and the
                    # login-path enroll (auth.py) never fires because the user
                    # already exists → "unregistered actor" on their events.
                    await conn.execute(
                        """INSERT INTO actors (id, type, uid, display_name)
                           VALUES ($1, 'user', $2, $3)
                           ON CONFLICT DO NOTHING""",
                        new_row["actor_id"], uid,
                        sr["display_name"] or sr["email"] or uid,
                    )
                    await append_event(
                        conn,
                        event_type=USER_CREATED,
                        payload={
                            "uid": uid,
                            "email": sr["email"],
                            "display_name": sr["display_name"],
                            "system_role": assigned_role,
                            "via": "signup_approval",
                        },
                        actor_id=new_row["actor_id"],
                        actor_type="system",
                    )
                    _log.info(
                        "signup_request review: approved and user created uid=%s assigned_role=%s approved_by=%s",
                        uid, assigned_role, user["uid"],
                    )

            elif action == "reject" and sr:
                await append_event(
                    conn,
                    event_type=SIGNUP_REJECTED,
                    payload={
                        "uid": uid,
                        "email": sr["email"],
                        "rejected_by": user["uid"],
                    },
                    actor_id=user["actor_id"],
                )
                _log.info(
                    "signup_request review: rejected uid=%s rejected_by=%s",
                    uid, user["uid"],
                )

    return RedirectResponse(url="/settings/signup-requests", status_code=303)


# -- Users list ----------------------------------------------------------------

@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    user: dict = Depends(require_admin),
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=_USERS_PAGE_SIZE_DEFAULT),
):
    """Directory of platform users — viewable by anyone logged in, same as a
    team member list. Only platform.admin can manage roles (see user_detail /
    update_user_system_role); this view and its rows never render edit controls."""
    if page_size not in _USERS_PAGE_SIZE_CHOICES:
        page_size = _USERS_PAGE_SIZE_DEFAULT

    pool = get_pool()
    offset = (page - 1) * page_size

    if q:
        pattern = f"%{q}%"
        total: int = await pool.fetchval(
            "SELECT count(*) FROM users WHERE email ILIKE $1 OR display_name ILIKE $1",
            pattern,
        )
        rows = await pool.fetch(
            """SELECT uid, email, display_name, photo_url, system_role, status, created_at
               FROM users
               WHERE email ILIKE $1 OR display_name ILIKE $1
               ORDER BY created_at DESC
               LIMIT $2 OFFSET $3""",
            pattern, page_size, offset,
        )
    else:
        total = await pool.fetchval("SELECT count(*) FROM users")
        rows = await pool.fetch(
            """SELECT uid, email, display_name, photo_url, system_role, status, created_at
               FROM users
               ORDER BY created_at DESC
               LIMIT $1 OFFSET $2""",
            page_size, offset,
        )

    users_list = [dict(r) for r in rows]
    total_pages = max(1, (total + page_size - 1) // page_size)
    display_end = min(page * page_size, total)

    context = {
        "request": request,
        "user": user,
        "users_list": users_list,
        "q": q,
        "q_encoded": quote_plus(q),
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "page_size": page_size,
        "page_size_choices": _USERS_PAGE_SIZE_CHOICES,
        "display_end": display_end,
        "page_range": _build_page_range(page, total_pages),
    }
    _log.info(
        "settings/users: viewer=%s role=%s q=%r page=%d page_size=%d total=%d",
        user["uid"], user.get("system_role"), q, page, page_size, total,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("settings/users_rows.html", context)
    return templates.TemplateResponse("settings/users.html", context)


# -- Council runs (admin monitor + queue controls) -----------------------------
#
# Read-only viewer over the three parallel council queues, plus admin force-fail
# / clear-queue controls to unblock a stuck queue from the UI (each queue has a
# single-run gate, so one orphaned 'running' row blocks the rest). All actions
# just flip rows to a terminal status — they never delete history.

_COUNCIL_QUEUES = ("goal", "security", "platform")
_COUNCIL_LABELS = {"goal": "Goal Review", "security": "Security", "platform": "Platform Research"}


def _rows_affected(tag: str) -> int:
    try:
        return int(tag.split()[-1])
    except Exception:
        return 0


def _normalise_council_queue(queue: str) -> str:
    return queue if queue in _COUNCIL_QUEUES else "goal"


async def _fetch_council_rows(pool, queue: str) -> list[dict]:
    """Normalise each queue's rows into a common shape for the table:
    {id, primary, detail, status, trigger, created_at, finished_at, active}."""
    if queue == "security":
        rows = await pool.fetch(
            """SELECT id, NULLIF(repo_owner || '/' || repo_name, '/') AS primary_label,
                      branch_name, status, trigger, created_at, finished_at
               FROM security_review_runs
               WHERE NOT (status = 'done'
                          AND COALESCE(finished_at, created_at) < now() - interval '24 hours')
               ORDER BY (status IN ('pending','running')) DESC, created_at DESC
               LIMIT 100""",
        )
        return [{
            "id": r["id"], "primary": r["primary_label"] or "(repo)",
            "detail": r["branch_name"] or "", "status": r["status"],
            "trigger": r["trigger"] or "", "created_at": r["created_at"],
            "finished_at": r["finished_at"],
            "active": r["status"] in ("pending", "running"),
        } for r in rows]
    if queue == "platform":
        rows = await pool.fetch(
            """SELECT id, category, title, status, priority, discovered_by,
                      created_at, updated_at
               FROM platform_discoveries
               ORDER BY (status IN ('new','researching')) DESC, priority DESC, created_at DESC
               LIMIT 100""",
        )
        return [{
            "id": r["id"], "primary": r["title"] or "(discovery)",
            "detail": f"{r['category']} · priority {r['priority']}",
            "status": r["status"], "trigger": r["discovered_by"] or "",
            "created_at": r["created_at"], "finished_at": r["updated_at"],
            "active": r["status"] in ("new", "researching"),
        } for r in rows]
    # goal (default) — includes both kind='goal' and single kind='story' reviews.
    rows = await pool.fetch(
        """SELECT r.id, r.kind, r.story_id, r.reviewed_state, r.status, r.trigger,
                  r.created_at, r.finished_at, g.name AS goal_name
           FROM council_review_runs r
           LEFT JOIN goals g ON g.id = r.goal_id
           WHERE NOT (r.status = 'done'
                      AND COALESCE(r.finished_at, r.created_at) < now() - interval '24 hours')
           ORDER BY (r.status IN ('pending','running')) DESC, r.created_at DESC
           LIMIT 100""",
    )
    out: list[dict] = []
    for r in rows:
        if r["kind"] == "story":
            detail = "story review · " + (r["story_id"] or "?")
            if r["reviewed_state"]:
                detail += f" ({r['reviewed_state']})"
        else:
            detail = "goal review"
        out.append({
            "id": r["id"], "primary": r["goal_name"] or r["id"], "detail": detail,
            "status": r["status"], "trigger": r["trigger"] or "",
            "created_at": r["created_at"], "finished_at": r["finished_at"],
            "active": r["status"] in ("pending", "running"),
        })
    return out


async def _platform_discovery_state(pool) -> dict:
    """Effective weekly-discovery gate for the Platform Research tab toggle:
    the DB override (platform_schedule_state.discovery_enabled) if set, else the
    env flag. `council_on` tells the UI whether the platform scheduler is even
    running (the toggle is inert if the platform master flag is off)."""
    from app.config import get_settings
    override = await pool.fetchval(
        "SELECT discovery_enabled FROM platform_schedule_state WHERE id='singleton'")
    s = get_settings()
    default = bool(getattr(s, "platform_discovery_schedule_enabled", False))
    return {
        "on": bool(override) if override is not None else default,
        "council_on": bool(getattr(s, "platform_council_enabled", False)),
    }


async def _render_council(request, user, queue: str, fragment_only: bool = False):
    pool = get_pool()
    rows = await _fetch_council_rows(pool, queue)
    platform_state = await _platform_discovery_state(pool) if queue == "platform" else None
    context = {
        "request": request, "user": user, "queue": queue,
        "queues": _COUNCIL_QUEUES, "queue_labels": _COUNCIL_LABELS,
        "runs": rows, "active_count": sum(1 for r in rows if r["active"]),
        "platform_discovery": platform_state,
    }
    if fragment_only or request.headers.get("HX-Request"):
        return templates.TemplateResponse("settings/council_runs_rows.html", context)
    return templates.TemplateResponse("settings/council_runs.html", context)


@router.get("/council-runs", response_class=HTMLResponse)
async def council_runs_list(
    request: Request,
    user: dict = Depends(require_admin),
    queue: str = Query(default="goal"),
):
    return await _render_council(request, user, _normalise_council_queue(queue))


async def _council_fail_one(pool, queue: str, run_id: str) -> int:
    if queue == "security":
        tag = await pool.execute(
            "UPDATE security_review_runs SET status='failed', finished_at=NOW() "
            "WHERE id=$1 AND status IN ('pending','running')", run_id)
    elif queue == "platform":
        tag = await pool.execute(
            "UPDATE platform_discoveries SET status='failed', updated_at=NOW() "
            "WHERE id=$1 AND status IN ('new','researching')", run_id)
    else:
        tag = await pool.execute(
            "UPDATE council_review_runs SET status='failed', finished_at=NOW() "
            "WHERE id=$1 AND status IN ('pending','running')", run_id)
    return _rows_affected(tag)


async def _council_clear_queue(pool, queue: str) -> int:
    if queue == "security":
        tag = await pool.execute(
            "UPDATE security_review_runs SET status='failed', finished_at=NOW() "
            "WHERE status IN ('pending','running')")
    elif queue == "platform":
        tag = await pool.execute(
            "UPDATE platform_discoveries SET status='failed', updated_at=NOW() "
            "WHERE status IN ('new','researching')")
    else:
        tag = await pool.execute(
            "UPDATE council_review_runs SET status='failed', finished_at=NOW() "
            "WHERE status IN ('pending','running')")
    return _rows_affected(tag)


@router.post("/council-runs/{queue}/fail/{run_id}", response_class=HTMLResponse)
async def council_run_force_fail(
    queue: str, run_id: str, request: Request, user: dict = Depends(require_admin),
):
    queue = _normalise_council_queue(queue)
    pool = get_pool()
    n = await _council_fail_one(pool, queue, run_id)
    _log.warning("settings/council-runs: admin=%s force-failed %s run=%s (rows=%d)",
                 user["uid"], queue, run_id, n)
    return await _render_council(request, user, queue, fragment_only=True)


@router.post("/council-runs/{queue}/clear", response_class=HTMLResponse)
async def council_queue_clear(
    queue: str, request: Request, user: dict = Depends(require_admin),
):
    queue = _normalise_council_queue(queue)
    pool = get_pool()
    n = await _council_clear_queue(pool, queue)
    _log.warning("settings/council-runs: admin=%s CLEARED %s queue (rows=%d)",
                 user["uid"], queue, n)
    return await _render_council(request, user, queue, fragment_only=True)


@router.post("/council-runs/platform/discovery-toggle", response_class=HTMLResponse)
async def platform_discovery_toggle(request: Request, user: dict = Depends(require_admin)):
    """Flip the runtime on/off override for the weekly platform discovery sweep.
    Writes platform_schedule_state.discovery_enabled; the platform scheduler_loop
    reads it each tick (no redeploy needed)."""
    pool = get_pool()
    state = await _platform_discovery_state(pool)
    new_val = not state["on"]
    await pool.execute(
        "INSERT INTO platform_schedule_state (id, discovery_enabled) VALUES ('singleton', $1) "
        "ON CONFLICT (id) DO UPDATE SET discovery_enabled = EXCLUDED.discovery_enabled",
        new_val,
    )
    _log.warning("settings/council-runs: admin=%s set platform weekly discovery -> %s",
                 user["uid"], "ON" if new_val else "OFF")
    return await _render_council(request, user, "platform", fragment_only=True)


# -- User detail ---------------------------------------------------------------

@router.get("/users/{uid}", response_class=HTMLResponse)
async def user_detail(
    uid: str,
    request: Request,
    user: dict = Depends(get_current_user),
    page: int = Query(default=1, ge=1),
):
    """Show a single user's profile, system role, and goal memberships.

    Viewable by anyone logged in. `can_manage` (platform.admin only) tells the
    template whether to render the role-change forms — non-admins get plain
    read-only badges instead, never a disabled control. The mutating endpoints
    below stay require_admin regardless of what the template renders.

    Goal memberships are paginated the same way as the users list, since a
    user can end up a member of many goals over time.
    """
    pool = get_pool()
    target_row = await pool.fetchrow(
        "SELECT uid, email, display_name, photo_url, system_role, status, created_at FROM users WHERE uid = $1",
        uid,
    )
    if not target_row:
        raise HTTPException(status_code=404, detail="User not found")
    target = dict(target_row)

    offset = (page - 1) * _GOAL_MEMBERSHIPS_PAGE_SIZE
    total = await pool.fetchval(
        """SELECT count(*)
             FROM goal_members gm
             JOIN goals g ON g.id = gm.goal_id
            WHERE gm.uid = $1
              AND g.status = 'active'""",
        uid,
    )
    memberships = await pool.fetch(
        """SELECT gm.goal_id, gm.role, g.name AS goal_name, g.status AS goal_status
             FROM goal_members gm
             JOIN goals g ON g.id = gm.goal_id
            WHERE gm.uid = $1
              AND g.status = 'active'
            ORDER BY g.name
            LIMIT $2 OFFSET $3""",
        uid, _GOAL_MEMBERSHIPS_PAGE_SIZE, offset,
    )
    goal_memberships = [dict(r) for r in memberships]
    total_pages = max(1, (total + _GOAL_MEMBERSHIPS_PAGE_SIZE - 1) // _GOAL_MEMBERSHIPS_PAGE_SIZE)
    display_end = min(page * _GOAL_MEMBERSHIPS_PAGE_SIZE, total)

    can_manage = user.get("system_role") == SystemRole.PLATFORM_ADMIN
    _log.info(
        "user_detail: viewer=%s role=%s can_manage=%s viewing uid=%s page=%d total_goals=%d",
        user["uid"], user.get("system_role"), can_manage, uid, page, total,
    )
    context = {
        "request": request,
        "user": user,
        "target": target,
        "goal_memberships": goal_memberships,
        "system_roles": SystemRole.ALL,
        "can_manage": can_manage,
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "page_size": _GOAL_MEMBERSHIPS_PAGE_SIZE,
        "display_end": display_end,
        "page_range": _build_page_range(page, total_pages),
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("settings/user_goal_memberships_rows.html", context)
    return templates.TemplateResponse("settings/user_detail.html", context)


@router.post("/users/{uid}/system-role")
async def update_user_system_role(
    uid: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Update a user's platform system role."""
    form = await request.form()
    new_role = (form.get("role") or "").strip()
    if new_role not in SystemRole.ALL:
        raise HTTPException(status_code=400, detail="Invalid role")

    pool = get_pool()
    target_row = await pool.fetchrow(
        "SELECT uid, system_role, actor_id FROM users WHERE uid = $1", uid,
    )
    if not target_row:
        raise HTTPException(status_code=404, detail="User not found")

    previous_role = target_row["system_role"]
    if previous_role == new_role:
        return flash_redirect(f"/settings/users/{uid}", "No change — role already set.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET system_role = $1 WHERE uid = $2",
                new_role, uid,
            )
            await append_event(
                conn,
                event_type=USER_SYSTEM_ROLE_CHANGED,
                payload={
                    "uid": uid,
                    "previous_role": previous_role,
                    "new_role": new_role,
                    "changed_by": user["uid"],
                },
                actor_id=user["actor_id"],
            )

    _log.info(
        "update_user_system_role: uid=%s %s->%s by %s",
        uid, previous_role, new_role, user["uid"],
    )
    return flash_redirect(f"/settings/users/{uid}", "Role updated successfully.")


@router.post("/users/{uid}/goal-role")
async def update_user_goal_role_from_detail(
    uid: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Update a user's role within a specific goal (from the user detail page).

    Only platform.admin can do this from the settings surface — goal-level role
    changes via the goal members page go through goals.update_member_role which
    has the same last-admin guard.
    """
    form = await request.form()
    goal_id = (form.get("goal_id") or "").strip()
    new_role = (form.get("role") or "").strip()

    if not goal_id:
        raise HTTPException(status_code=400, detail="goal_id required")
    if new_role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="Invalid role")

    pool = get_pool()
    current_row = await pool.fetchrow(
        "SELECT role FROM goal_members WHERE goal_id = $1 AND uid = $2", goal_id, uid,
    )
    if not current_row:
        raise HTTPException(status_code=404, detail="User is not a member of that goal")

    current_role = current_row["role"]

    # Last-admin guard: shared with goals.update_member_role and
    # goal_membership.add_goal_member/remove_goal_member so the rule can't
    # drift between the four membership-mutation paths.
    if current_role == "admin" and new_role != "admin":
        from app.services.goal_membership import count_goal_admins
        if await count_goal_admins(pool, goal_id) <= 1:
            _log.warning(
                "update_user_goal_role_from_detail: blocked last-admin demotion goal=%s uid=%s by %s",
                goal_id, uid, user["uid"],
            )
            raise HTTPException(
                status_code=400,
                detail="Cannot demote: this user is the only goal.manager for that goal. Promote another member first.",
            )

    if current_role == new_role:
        return flash_redirect(f"/settings/users/{uid}", "No change — role already set.")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE goal_members SET role = $1 WHERE goal_id = $2 AND uid = $3",
                new_role, goal_id, uid,
            )
            await append_event(
                conn,
                event_type=GOAL_MEMBER_ROLE_CHANGED,
                payload={
                    "goal_id": goal_id,
                    "uid": uid,
                    "previous_role": current_role,
                    "new_role": new_role,
                    "changed_by": user["uid"],
                },
                actor_id=user["actor_id"],
                context_goal_id=goal_id,
            )

    _log.info(
        "update_user_goal_role_from_detail: goal=%s uid=%s %s->%s by %s",
        goal_id, uid, current_role, new_role, user["uid"],
    )
    return flash_redirect(f"/settings/users/{uid}", "Role updated successfully.")


@router.delete("/users/{uid}/goal-membership/{goal_id}")
async def remove_user_goal_membership(
    uid: str,
    goal_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Remove a user from a specific goal, from the settings user-detail page.

    Same authorization bar as the goal-role dropdown above: platform.admin
    only, no per-goal goal.manager check — this settings surface is an admin
    oversight page, deliberately more permissive than goals.py's DELETE
    /goals/{pid}/members/{uid} (which requires goal.manager on that specific
    goal, no platform.admin bypass).

    Delegates to remove_goal_member — the same service goals.py uses — so the
    full precondition chain (last-admin guard, active task/story blocking,
    not-yet-started-story reassignment) behaves identically everywhere a
    member gets removed, regardless of which page triggered it.
    """
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    reassignments = body.get("reassignments") or {}

    from app.services.goal_membership import remove_goal_member
    result = await remove_goal_member(goal_id, uid, user["uid"], user["actor_id"], reassignments)
    if result.get("status") == "needs_reassignment":
        return JSONResponse(result, status_code=409)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    _log.info(
        "remove_user_goal_membership: goal=%s uid=%s removed by=%s",
        goal_id, uid, user["uid"],
    )
    return JSONResponse(result)


# -- Integrations admin ---------------------------------------------------------

@router.get("/integrations", response_class=HTMLResponse)
async def integrations_admin(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Restricted to platform.admin (require_admin) —
    platform.editor/platform.member have no reason to see integration status. Only booleans (has_secret,
    has_signing_secret) and non-secret identifiers (client_id, workspace
    name/id) ever reach the template — no raw secret values are fetched
    here or rendered by admin/integrations.html. Only platform.admin can
    submit new credentials (see slack_setup below)."""
    pool = get_pool()

    # Notion
    notion_row = await pool.fetchrow(
        """SELECT redirect_uri, workspace_id, bot_external_id, status, config
           FROM platform_integrations WHERE platform = 'notion'""",
    )
    if notion_row:
        import json as _json
        notion = dict(notion_row)
        raw_cfg = notion.get("config") or {}
        cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
        notion["config"] = cfg
        notion["client_id"] = cfg.get("client_id", "")
        notion["has_secret"] = bool(cfg.get("client_secret"))
        notion["pending_verification_token"] = cfg.pop("pending_verification_token", None)
    else:
        notion = None

    # Notion Bot (internal token)
    notion_bot_row = await pool.fetchrow(
        "SELECT status, config, (credentials_data IS NOT NULL AND credentials_data != '') AS has_token FROM platform_integrations WHERE platform = 'notion_bot'",
    )
    if notion_bot_row:
        import json as _json
        notion_bot = dict(notion_bot_row)
        raw_cfg = notion_bot.get("config") or {}
        cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
        notion_bot["pending_verification_token"] = cfg.pop("pending_verification_token", None)
        notion_bot["parent_database_id"] = cfg.get("parent_database_id", "")
    else:
        notion_bot = None

    # Slack
    slack_workspaces = []
    slack_app = {}
    slack_token_stale = False
    try:
        import json as _json
        from app.channels.slack.config import list_workspaces as _slack_list, get_app_credentials as _slack_app_creds
        slack_workspaces = await _slack_list()
        slack_app = await _slack_app_creds()

        # Detect mismatch: bot token was issued by a different client_id than what is
        # currently saved. We only flag this when installed_with_client_id is present
        # (i.e. the token was installed after this tracking was added), to avoid
        # false positives on older installations that pre-date this field.
        config_client_id = slack_app.get("client_id", "")
        token_exists = bool(slack_workspaces)
        if not token_exists and config_client_id:
            # Credentials saved but OAuth never completed.
            slack_token_stale = True
        elif token_exists and config_client_id:
            pi_row = await pool.fetchrow(
                "SELECT credentials_json FROM platform_integrations WHERE platform = 'slack' LIMIT 1",
            )
            if pi_row and pi_row["credentials_json"]:
                raw = pi_row["credentials_json"]
                creds = _json.loads(raw) if isinstance(raw, str) else dict(raw)
                installed_with = creds.get("installed_with_client_id", "")
                if installed_with and installed_with != config_client_id:
                    slack_token_stale = True
    except Exception as _slack_exc:
        _log.warning("Failed to load Slack integration data: %s", _slack_exc, exc_info=True)

    return templates.TemplateResponse(
        "admin/integrations.html",
        {
            "request": request,
            "user": user,
            "notion": notion,
            "notion_bot": notion_bot,
            "slack_workspaces": slack_workspaces,
            "slack_app": slack_app,
            "slack_token_stale": slack_token_stale,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
            "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
        },
    )


@router.post("/integrations/slack/setup")
async def slack_setup(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Save Slack app credentials (client_id, client_secret, signing_secret) from the admin setup form."""
    from app.channels.slack.config import save_app_credentials, get_app_credentials

    form = await request.form()
    client_id = (form.get("client_id") or "").strip()
    client_secret = (form.get("client_secret") or "").strip()
    signing_secret = (form.get("signing_secret") or "").strip()

    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")

    if not client_secret:
        existing = await get_app_credentials()
        if not existing.get("has_secret"):
            raise HTTPException(status_code=400, detail="client_secret is required on first setup")

    await save_app_credentials(client_id, client_secret, signing_secret)
    _log.info("Slack app credentials saved by %s", user.get("email"))
    return RedirectResponse(url="/settings/integrations", status_code=303)


def _slack_slash_commands(url: str, cmd) -> list[dict]:
    """One entry per registered command, in the registry's own order.

    `usage_hint` is Slack's one-line prompt and comes from the command's
    own `hint` field. Parsing it out of `usage` was tried first and gave
    "[help]" for half of them, because the first line of that block is a
    header rather than an argument list.
    """
    from app.channels.slack.commands.registry import COMMANDS

    out = []
    for spec in COMMANDS:
        entry = {
            "command": cmd(spec.name),
            "url": url,
            "description": spec.summary[:150],
            "should_escape": False,
        }
        if spec.hint:
            entry["usage_hint"] = spec.hint[:120]
        out.append(entry)
    return out


def _slack_bot_scopes() -> list[str]:
    """Derived in app/channels/slack/scopes.py from the API methods we call,
    so this manifest and the OAuth consent URL cannot ask for different sets --
    Slack grants the intersection, and the gap shows up much later as a
    missing_scope swallowed by an except branch."""
    from app.channels.slack.scopes import bot_scopes
    return bot_scopes()


def _slack_bot_events() -> list[str]:
    from app.channels.slack.scopes import BOT_EVENTS
    return list(BOT_EVENTS)


@router.get("/integrations/slack/manifest")
async def slack_manifest(
    request: Request,
    user: dict = Depends(require_admin),
):
    """Return a Slack app manifest JSON pre-filled with this deployment's URLs.

    Command names include the SLACK_COMMAND_SUFFIX so the generated manifest
    matches exactly what this environment's Slack app must be configured with.
    """
    from fastapi.responses import JSONResponse
    from app.config import get_settings

    # When this handler runs on the management service behind the main-service
    # proxy, request.base_url reflects the management service's own URL. Use
    # X-Forwarded-Host/Proto (set by proxy_routes) to build the correct
    # public-facing URL for Slack webhook and redirect registration.
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        proto = request.headers.get("x-forwarded-proto", "https")
        base = f"{proto}://{forwarded_host}"
    else:
        base = str(request.base_url).rstrip("/").replace("http://", "https://")
    commands_url = f"{base}/channels/slack/commands"

    suffix = get_settings().slack_command_suffix.strip().lstrip("-")
    env_label = f" ({suffix})" if suffix else ""

    def cmd(name: str) -> str:
        return f"/{name}-{suffix}" if suffix else f"/{name}"

    manifest = {
        "_metadata": {"major_version": 2, "minor_version": 1},
        "display_information": {
            "name": f"AgentOS{env_label}",
            "description": "AI-powered project execution assistant",
        },
        "features": {
            "bot_user": {"display_name": f"AgentOS{env_label}", "always_online": True},
            # DERIVED FROM THE REGISTRY, like the scopes above. This was a
            # hand-kept second list, and it drifted exactly as the scope
            # lists did: it still advertised `/task`, which was removed with
            # the task DAG, and knew nothing of /run, /park or /errand. A
            # manifest is the only thing that registers a command with
            # Slack, so a command missing here does not exist however well
            # it is implemented -- and one listed here that no longer exists
            # answers "is not a recognised command" to anyone who tries it.
            "slash_commands": _slack_slash_commands(commands_url, cmd),
        },
        "oauth_config": {
            "redirect_urls": [f"{base}/channels/slack/oauth/callback"],
            "scopes": {
                "bot": _slack_bot_scopes(),
            },
        },
        "settings": {
            "event_subscriptions": {
                "request_url": f"{base}/channels/slack/events",
                "bot_events": _slack_bot_events(),
            },
            "interactivity": {
                "is_enabled": True,
                "request_url": f"{base}/channels/slack/actions",
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }
    _log.info("Slack manifest generated for %s by %s", base, user.get("email"))
    return JSONResponse(content=manifest)


# ── One-time backfill: push all active skills to live agent instances ──────
#
# Use case: skill_sync's fan-out fires on UI edits. When skills are inserted
# by a migration, existing live agents don't get the SKILL.md files in their
# workspaces. This endpoint walks the skills table and pushes everything to
# every applicable live instance. Safe to call repeatedly — gateway treats
# the writes as idempotent set_agent_skill calls.

@router.post("/skills/backfill")
async def skills_backfill(
    request: Request,
    user: dict = Depends(require_admin),
):
    import logging
    from app.services.skill_sync import push_skill_to_instances

    log = logging.getLogger(__name__)
    pool = get_pool()
    skills = await pool.fetch(
        """SELECT name, content, applicable_to
             FROM skills
            WHERE status = 'active'
            ORDER BY name""",
    )
    pushed_per_skill: dict[str, int] = {}
    for sk in skills:
        applicable: list[str] = list(sk["applicable_to"]) if sk["applicable_to"] else []
        if not applicable:
            continue
        await push_skill_to_instances(sk["name"], sk["content"], applicable)
        pushed_per_skill[sk["name"]] = len(applicable)

    log.info("skills_backfill triggered by %s: %d skills processed", user["uid"], len(skills))
    return {
        "ok": True,
        "skill_count": len(skills),
        "pushed_per_skill_applicable_types": pushed_per_skill,
        "note": "Check Cloud Run logs for per-skill / per-instance push results.",
    }


# ── Claude Code phase admin (SHCA Phase 4 Stage 3-a) ─────────────────────────
#
# Each phase row defines a Claude Code subagent that gets provisioned into
# `<story-workspace>/.claude/agents/<name>.md` when an operator-mode story
# workspace is created. The plan DAG references phases by name; the task
# controller dispatches each task with instructions to invoke the matching
# subagent.

from fastapi import HTTPException  # noqa: E402

from app.services import claude_phases as cp_svc  # noqa: E402


def _parse_tool_list(raw: str) -> list[str]:
    """Parse a comma- or newline-separated list of tool names from a form
    field. Trims whitespace, drops empties."""
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace("\r", "").replace("\n", ",").split(","):
        s = chunk.strip()
        if s:
            parts.append(s)
    return parts


@router.get("/claude-phases", response_class=HTMLResponse)
async def claude_phases_page(request: Request, user: dict = Depends(require_admin)):
    """List all Claude Code phases. Restricted to platform.admin (require_admin) —
    platform.editor/platform.member have no access."""
    phases = await cp_svc.list_phases(only_active=False)
    return templates.TemplateResponse(
        "admin/claude_phases.html",
        {"request": request, "user": user, "phases": phases,
         "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN},
    )


@router.post("/claude-phases")
async def claude_phases_create(request: Request, user: dict = Depends(require_admin)):
    """Create a new phase. Form fields: name, description, system_prompt,
    allowed_tools (comma/newline-separated), denied_tools, model.
    Admin-only: platform.editor has read-only access to Claude Phases."""
    form = await request.form()
    try:
        await cp_svc.create_phase(
            name=form.get("name", "").strip(),
            description=form.get("description", "").strip(),
            system_prompt=form.get("system_prompt", ""),
            allowed_tools=_parse_tool_list(form.get("allowed_tools", "")),
            denied_tools=_parse_tool_list(form.get("denied_tools", "")),
            model=form.get("model", "").strip(),
            created_by=user["uid"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/settings/claude-phases", status_code=303)


@router.get("/claude-phases/{name}", response_class=HTMLResponse)
async def claude_phase_detail(
    name: str, request: Request, user: dict = Depends(get_current_user),
):
    """View/edit a single phase. Viewable by anyone logged in; can_edit
    (platform.admin-only) tells the template whether to render the edit form."""
    phase = await cp_svc.get_phase(name)
    if not phase:
        raise HTTPException(status_code=404)
    # Render the as-deployed subagent .md so the admin can see exactly what
    # gets written into <story-workspace>/.claude/agents/<name>.md.
    rendered = cp_svc.render_phase_to_subagent_md(phase)
    return templates.TemplateResponse(
        "admin/claude_phase_detail.html",
        {
            "request": request,
            "user": user,
            "phase": phase,
            "rendered_md": rendered,
            "can_edit": user.get("system_role") == SystemRole.PLATFORM_ADMIN,
        },
    )


@router.post("/claude-phases/{name}")
async def claude_phase_update(
    name: str, request: Request, user: dict = Depends(require_admin),
):
    """Update a phase. Same field names as create.
    Admin-only: platform.editor has read-only access to Claude Phases."""
    form = await request.form()
    try:
        await cp_svc.update_phase(
            name=name,
            description=form.get("description"),
            system_prompt=form.get("system_prompt"),
            allowed_tools=_parse_tool_list(form.get("allowed_tools", "")),
            denied_tools=_parse_tool_list(form.get("denied_tools", "")),
            model=form.get("model"),
            status=form.get("status"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url=f"/settings/claude-phases/{name}", status_code=303)


@router.post("/claude-phases/{name}/disable")
async def claude_phase_disable(
    name: str, user: dict = Depends(require_admin),
):
    """Soft-delete (status='disabled'). Phase keeps existing in DB so
    historical task rows that reference it stay readable.
    Admin-only: platform.editor has read-only access to Claude Phases."""
    await cp_svc.delete_phase(name)
    return RedirectResponse(url="/settings/claude-phases", status_code=303)


@router.post("/integrations/notion/setup")
async def notion_setup(
    request: Request,
    user: dict = Depends(require_admin),
):
    from app.channels.notion.oauth import save_app_credentials, get_app_credentials

    form = await request.form()
    client_id = form.get("client_id", "").strip()
    client_secret = form.get("client_secret", "").strip()
    redirect_uri = form.get("redirect_uri", "").strip()
    parent_database_id = form.get("parent_database_id", "").strip()

    if not client_id or not redirect_uri:
        raise HTTPException(status_code=400, detail="client_id and redirect_uri are required")

    if not client_secret:
        existing = await get_app_credentials()
        client_secret = existing.get("client_secret", "")
        if not client_secret:
            raise HTTPException(status_code=400, detail="client_secret is required on first setup")

    await save_app_credentials(client_id, client_secret, redirect_uri, parent_database_id)
    _log.info("Notion app credentials saved by %s", user.get("email"))
    return flash_redirect("/settings/integrations", "Credentials saved.")


@router.post("/integrations/notion-bot/setup")
async def notion_bot_setup(
    request: Request,
    user: dict = Depends(require_admin),
):
    import json as _json
    form = await request.form()
    token = form.get("token", "").strip()
    parent_database_id = form.get("parent_database_id", "").strip()

    pool = get_pool()

    # Preserve existing token if blank was submitted
    if not token:
        existing_token = await pool.fetchval(
            "SELECT credentials_data FROM platform_integrations WHERE platform = 'notion_bot'",
        )
        if not existing_token:
            raise HTTPException(status_code=400, detail="token is required")
        token = existing_token

    # Merge parent_database_id into config, preserving other config keys
    existing_cfg_raw = await pool.fetchval(
        "SELECT config FROM platform_integrations WHERE platform = 'notion_bot'",
    )
    cfg = _json.loads(existing_cfg_raw) if isinstance(existing_cfg_raw, str) else (existing_cfg_raw or {})
    if parent_database_id:
        cfg["parent_database_id"] = parent_database_id
    elif "parent_database_id" in cfg and not parent_database_id:
        cfg.pop("parent_database_id", None)

    # Fetch the bot's Notion user ID for echo prevention (equivalent to Slack's bot_user_id).
    # Use notion_client (same SDK used for all other Notion API calls) — more reliable
    # than raw httpx. GET /users/me returns {"id": "<bot-uuid>", "type": "bot", ...}.
    bot_external_id = ""
    try:
        from notion_client import AsyncClient as _NotionClient
        _notion = _NotionClient(auth=token)
        me = await _notion.users.me()
        bot_external_id = me.get("id", "")
        if bot_external_id:
            _log.info("Notion Bot user ID fetched: %s", bot_external_id)
        else:
            _log.warning("Notion Bot /users/me returned no id: %s", me)
    except Exception as exc:
        _log.warning("Notion Bot could not fetch bot user ID: %s", exc)

    await pool.execute(
        """INSERT INTO platform_integrations (platform, credentials_kind, credentials_data, config, status, bot_external_id)
           VALUES ('notion_bot', 'api_key', $1, $2, 'active', $3)
           ON CONFLICT (platform) DO UPDATE
               SET credentials_data = EXCLUDED.credentials_data,
                   config           = EXCLUDED.config,
                   status           = 'active',
                   bot_external_id  = CASE WHEN EXCLUDED.bot_external_id != ''
                                           THEN EXCLUDED.bot_external_id
                                           ELSE platform_integrations.bot_external_id END,
                   updated_at       = now()""",
        token, _json.dumps(cfg), bot_external_id,
    )
    _log.info("Notion Bot credentials saved by %s (bot_id=%s)", user.get("email"), bot_external_id or "unknown")
    return flash_redirect("/settings/integrations", "Credentials saved.")


@router.post("/integrations/{platform}/dismiss-verification-token")
async def dismiss_verification_token(
    platform: str,
    user: dict = Depends(require_admin),
):
    if platform not in ("notion", "notion_bot"):
        raise HTTPException(status_code=404)
    import json as _json
    pool = get_pool()
    row = await pool.fetchrow("SELECT config FROM platform_integrations WHERE platform = $1", platform)
    if row:
        raw_cfg = row["config"] or {}
        cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
        cfg.pop("pending_verification_token", None)
        await pool.execute(
            "UPDATE platform_integrations SET config = $1 WHERE platform = $2",
            _json.dumps(cfg), platform,
        )
    return RedirectResponse(url="/settings/integrations", status_code=303)
