"""Kill switch — admin control endpoints.

Execution control is a PLATFORM-ADMIN-ONLY capability. Goal-level admins
(goal.manager) deliberately have NO path to pause, resume, or force-kill — they
run their goal's work but do not freeze or kill the runtime. platform.editor can
view the control panel and every goal's pause state, but every mutating action
(system pause/resume, goal pause/resume, agent stop/resume, force-kill) is
admin-only — editor has read-only access here, same as the rest of /settings.
The endpoints are thin: they check auth, call the pure ``execution_control`` /
``agent_kill`` services, emit the ``execution.*`` ledger event (audit lives
here, in one place), and wake the controller on resume so work picks back up
immediately.

Scopes (see app/services/execution_control.py for the full model):
  * system         — global freeze, platform-admin-only (POST /settings/controls/pause|resume)
  * goal           — freeze one goal, platform-admin-only (POST /settings/controls/goals/{pid}/pause|resume)
  * agent_instance — graceful stop of one instance, platform-admin-only (POST /settings/controls/agents/{id}/stop|resume)
  * force-kill     — hard, TERMINAL stop keyed by task (no restart — kill is a
    one-way door), platform-admin-only: POST /settings/controls/agents/{id}/force-kill,
    /settings/controls/tasks/{tid}/force-kill

Pause/resume writes go through execution_control's retrying mutators; if the DB
write can't be made durable the mutator raises and we return 503 so the operator
sees a clear "system issue" instead of a silently dangling state.

The system-status banner fragment (GET /admin/controls/status) is readable by any
authenticated user so the freeze banner shows for everyone; the system-freeze
action button is gated to admins in the template (editors see read-only status
text instead — never a dead-end 403). The same hide-don't-disable pattern now
applies to every other action button on the control panel (goal pause/resume,
force-kill): editor sees state, admin sees the button.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, require_admin
from app.database import get_pool
from app.roles import SystemRole
from app.services import execution_control, agent_kill

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Fallback ledger actor when an operator somehow lacks an actor_id. The one
# canonical, registered system actor shared by all system-attributed ledger
# events (auth.py now uses this same …0001 value too).
from app.ledger.actors import SYSTEM_ACTOR_ID as _SYSTEM_ACTOR_ID


# ── helpers ───────────────────────────────────────────────────────────────────

def _actor_uuid(user: dict) -> uuid.UUID:
    """The operator's actor_id for ledger attribution, or the system actor."""
    raw = user.get("actor_id")
    try:
        return uuid.UUID(str(raw)) if raw else _SYSTEM_ACTOR_ID
    except (ValueError, TypeError):
        return _SYSTEM_ACTOR_ID


async def _emit(event_type: str, payload: dict, user: dict,
                goal_id: str = "", task_id: str = "") -> None:
    """Append an execution.* event to the ledger. Best-effort: an audit-write
    failure must never block the operator's control action."""
    try:
        from app.ledger.journal import append_event  # noqa: PLC0415
        pool = get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await append_event(
                conn,
                event_type=event_type,
                payload=payload,
                actor_id=_actor_uuid(user),
                actor_type="human",
                context_goal_id=goal_id or "",
                context_task_id=task_id or "",
            )
    except Exception as exc:
        _log.warning("admin_controls: ledger emit failed for %s: %s", event_type, exc)


async def _trigger_controller() -> None:
    """Wake the task controller so resumed work dispatches immediately.

    ``trigger`` is an async coroutine — it MUST be awaited. The previous version
    imported a non-existent name and called it unawaited, so every resume
    silently fell back to the ~15s idle poll instead of waking the controller.
    Failures are logged at WARNING (not DEBUG) so a broken wake is visible."""
    try:
        from app.task_controller import trigger as tc_trigger  # noqa: PLC0415
        await tc_trigger()
    except Exception as exc:
        _log.warning("admin_controls: controller trigger failed: %s", exc)


async def _abort_inflight_tasks(goal_id: str | None) -> None:
    """Abort any in-flight tasks after a pause (M5 + M6).

    Phase tasks (claude-code-phase): shca.claude.abort (by taskId) + release
    creds + mark BLOCKED — mirrors the per-story pause in shca.py.

    Embedded PI runs (IA/GO/SHCA non-CLI agents): shca.embedded.abort (M6).
    System-wide only — goal-scoped embedded abort isn't supported by the runner
    (no goal_id tracking inside pi-embedded-runner).

    Fires for system pause (goal_id=None, all goals) or goal pause
    (goal_id set). Best-effort: never raises — a failure here must not block
    the pause response reaching the operator.
    """
    try:
        from app.openclaw import gateway_admin  # noqa: PLC0415
        from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
        pool = get_pool()

        if goal_id:
            tasks = await pool.fetch(
                """SELECT id FROM tasks
                    WHERE goal_id = $1 AND executor = 'claude-code-phase'
                      AND state IN ('in_progress', 'dispatching') AND status = 'active'""",
                goal_id,
            )
            scope_label = f"goal={goal_id}"
        else:
            tasks = await pool.fetch(
                """SELECT id FROM tasks
                    WHERE executor = 'claude-code-phase'
                      AND state IN ('in_progress', 'dispatching') AND status = 'active'
                    LIMIT 50""",
            )
            scope_label = "system"

        _log.info(
            "admin_controls: aborting %d in-flight phase task(s) after %s pause",
            len(tasks), scope_label,
        )
        for t in tasks:
            tid = t["id"]
            try:
                res = await gateway_admin.call("shca.claude.abort", {"taskId": tid}, timeout=30)
                _log.info("admin_controls: abort sent task=%s aborted=%s", tid, res.get("aborted"))
            except Exception as exc:
                _log.warning("admin_controls: shca.claude.abort failed task=%s: %s", tid, exc)
            try:
                await release_credentials_safe(tid, "pause:abort")
            except Exception:
                pass
            await pool.execute(
                "UPDATE tasks SET state = 'blocked', blocked_reason = $2, updated_at = NOW() "
                "WHERE id = $1 AND state IN ('in_progress', 'dispatching')",
                tid,
                "Paused by operator — Claude turn aborted. Mark done or retry from the task board.",
            )

        # M6: For a system-wide pause, also abort any active embedded PI runs
        # (IA/GO/SHCA agents managed by pi-embedded-runner, not the CLI path).
        # Goal-scoped embedded abort is not supported — the runner has no
        # goal_id index, so we only fire this on system pause to avoid collateral.
        if goal_id is None:
            try:
                res = await gateway_admin.call("shca.embedded.abort", {}, timeout=30)
                _log.info("admin_controls: embedded abort (system) aborted=%s", res.get("aborted"))
            except Exception as exc:
                _log.warning("admin_controls: shca.embedded.abort failed: %s", exc)

    except Exception as exc:
        _log.warning(
            "admin_controls: _abort_inflight_tasks failed (scope=%s): %s",
            goal_id or "system", exc,
        )


def _system_issue(action: str, exc: Exception) -> JSONResponse:
    """A pause/resume write failed even after retries — surface it to the operator
    as a clear system issue (503) rather than letting the UI think it succeeded.
    No dangling state: the row was either written or it wasn't, and we say so."""
    _log.error("admin_controls: %s failed as a system issue: %s", action, exc)
    return JSONResponse(
        {"ok": False, "system_issue": True,
         "error": (f"{action} could not be completed — system issue talking to the "
                   "database. Nothing changed; please retry. If it persists, check "
                   "control-panel/database health.")},
        status_code=503,
    )


async def _reason_from(request: Request) -> str:
    """Pull an optional {reason} from a JSON or form body. Never raises."""
    try:
        data = await request.json()
        if isinstance(data, dict):
            return (data.get("reason") or "").strip()
    except Exception:
        try:
            form = await request.form()
            return (form.get("reason") or "").strip()
        except Exception:
            pass
    return ""


async def _flag_from(request: Request, name: str) -> bool:
    """Pull a boolean from a JSON or form body. Never raises.

    Form bodies carry strings, so "false"/"0"/"off" have to mean False
    -- a checkbox posting the string "false" read as truthy is how a
    switch turns itself on.
    """
    raw = None
    try:
        data = await request.json()
        if isinstance(data, dict):
            raw = data.get(name)
    except Exception:                                       # noqa: BLE001
        try:
            raw = (await request.form()).get(name)
        except Exception:                                   # noqa: BLE001
            raw = None
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "on", "yes")


# ── SYSTEM scope ────────────────────────────────────────────────────────────

# ── Verification, platform-wide ────────────────────────────────────────
#
# OFF BY DEFAULT. With it off, a story or errand that proposes completion
# is measured against its own plan's checks, published, and parked for a
# person to accept or ask for more -- and no verification pod, prepare
# Job or criterion Job is created. `VERIFYING` stays in the state machine
# with nothing entering it, so a run already in flight when the switch
# moves is drained rather than stranded.
#
# PLATFORM-WIDE AND PLATFORM ADMIN ONLY. `require_admin` is the same gate
# Freeze System uses. Every change is appended to the hash-chained
# ledger: who, when, from what, to what, and why.

@router.get("/settings/controls/verification")
async def verification_get(user: dict = Depends(require_admin)):
    from app.services import platform_settings

    return JSONResponse(
        await platform_settings.get(platform_settings.VERIFICATION))


@router.post("/settings/controls/verification")
async def verification_set(request: Request,
                           user: dict = Depends(require_admin)):
    """Turn verification on or off for the whole platform."""
    from app.services import platform_settings

    wanted = await _flag_from(request, "enabled")
    reason = await _reason_from(request)
    by = user.get("email") or user.get("uid", "")
    try:
        moved = await platform_settings.set_value(
            platform_settings.VERIFICATION, wanted, by=by, reason=reason)
    except Exception as exc:
        return _system_issue("Set verification", exc)

    # THE AUDIT IS THE POINT, so it records the transition and not just
    # the new value: "somebody set it to off" and "somebody turned it
    # off" are different facts when two admins act a second apart.
    await _emit("execution.verification_changed",
                {"was": moved["was"], "now": moved["now"],
                 "reason": reason, "by": by}, user)
    _log.info("VERIFICATION %s -> %s by %s (reason=%s)",
              moved["was"], moved["now"], by, reason or "<none>")
    return JSONResponse({"ok": True, **moved})


@router.post("/settings/controls/pause")
async def system_pause(request: Request, user: dict = Depends(require_admin)):
    reason = await _reason_from(request)
    if await execution_control.is_system_paused():
        return JSONResponse({"ok": True, "already_paused": True})
    try:
        await execution_control.pause("system", "", reason, user.get("email") or user.get("uid", ""))
    except Exception as exc:
        return _system_issue("Freeze system", exc)
    await _emit("execution.system_paused", {"reason": reason, "by": user.get("email", "")}, user)
    _log.info("SYSTEM execution paused by %s (reason=%s)", user.get("email"), reason or "<none>")
    # Abort in-flight turns so the freeze takes effect immediately (M5).
    await _abort_inflight_tasks(None)
    return JSONResponse({"ok": True})


@router.post("/settings/controls/resume")
async def system_resume(user: dict = Depends(require_admin)):
    try:
        removed = await execution_control.resume("system", "")
    except Exception as exc:
        return _system_issue("Resume system", exc)
    await _emit("execution.system_resumed", {"by": user.get("email", "")}, user)
    await _trigger_controller()
    _log.info("SYSTEM execution resumed by %s (was_paused=%s)", user.get("email"), removed)
    return JSONResponse({"ok": True, "already_running": not removed})


# ── GOAL scope (platform-admin-only — editor is read-only, goal-managers have no path) ──

@router.post("/settings/controls/goals/{pid}/pause")
async def goal_pause(pid: str, request: Request, user: dict = Depends(require_admin)):
    # Platform-admin-only. Goal-level admins (goal.manager) deliberately cannot
    # pause, and platform.editor is read-only here — execution control is now
    # an admin-only capability across the board.
    reason = await _reason_from(request)
    if await execution_control.get_pause("goal", pid):
        return JSONResponse({"ok": True, "already_paused": True})
    try:
        await execution_control.pause("goal", pid, reason, user.get("email") or user.get("uid", ""))
    except Exception as exc:
        return _system_issue("Pause goal", exc)
    await _emit("execution.goal_paused", {"reason": reason, "by": user.get("email", "")}, user, goal_id=pid)
    _log.info("GOAL %s execution paused by %s (reason=%s)", pid, user.get("email"), reason or "<none>")
    # Abort in-flight turns so the freeze takes effect immediately (M5).
    await _abort_inflight_tasks(pid)
    return JSONResponse({"ok": True})


@router.post("/settings/controls/goals/{pid}/resume")
async def goal_resume(pid: str, user: dict = Depends(require_admin)):
    # Platform-admin-only. Goal-level admins (goal.manager) deliberately cannot
    # resume, and platform.editor is read-only here — execution control is now
    # an admin-only capability across the board.
    try:
        removed = await execution_control.resume("goal", pid)
    except Exception as exc:
        return _system_issue("Resume goal", exc)
    await _emit("execution.goal_resumed", {"by": user.get("email", "")}, user, goal_id=pid)
    await _trigger_controller()
    _log.info("GOAL %s execution resumed by %s (was_paused=%s)", pid, user.get("email"), removed)
    return JSONResponse({"ok": True, "already_running": not removed})


# ── AGENT-INSTANCE scope: graceful stop / resume ──────────────────────────────

@router.post("/settings/controls/agents/{instance_id}/stop")
async def agent_stop(instance_id: str, request: Request, user: dict = Depends(require_admin)):
    """Graceful stop: the instance's current turn finishes; its next subtask
    won't dispatch until resumed. No work is lost."""
    reason = await _reason_from(request)
    try:
        await execution_control.pause("agent_instance", instance_id, reason, user.get("email") or user.get("uid", ""))
    except Exception as exc:
        return _system_issue("Stop agent", exc)
    await _emit("execution.agent_stopped", {"instance_id": instance_id, "reason": reason}, user)
    _log.info("AGENT instance %s gracefully stopped by %s", instance_id, user.get("email"))
    return JSONResponse({"ok": True})


@router.post("/settings/controls/agents/{instance_id}/resume")
async def agent_resume(instance_id: str, user: dict = Depends(require_admin)):
    try:
        removed = await execution_control.resume("agent_instance", instance_id)
    except Exception as exc:
        return _system_issue("Resume agent", exc)
    await _emit("execution.agent_resumed", {"instance_id": instance_id}, user)
    await _trigger_controller()
    _log.info("AGENT instance %s resumed by %s (was_stopped=%s)", instance_id, user.get("email"), removed)
    return JSONResponse({"ok": True, "already_running": not removed})


# ── Force-kill (hard) + restart, keyed by the running task ───────────────────

@router.post("/settings/controls/agents/{instance_id}/force-kill")
async def agent_force_kill(instance_id: str, request: Request, user: dict = Depends(require_admin)):
    """Force-kill the task currently running on this instance (for IA / persistent
    workers, which the admin panel lists by instance).

    Platform-admin-only: force-kill is the most invasive, terminal action, so
    goal-level admins (goal.manager) deliberately have no path here."""
    reason = await _reason_from(request)
    task = await agent_kill.resolve_task_for_instance(instance_id)
    if task is None:
        return JSONResponse({"ok": False, "error": "no running task on this instance"}, status_code=404)
    result = await _force_kill_and_audit(task["id"], reason, user)
    return JSONResponse(result)


@router.post("/settings/controls/tasks/{task_id}/force-kill")
async def task_force_kill(task_id: str, request: Request, user: dict = Depends(require_admin)):
    """Force-kill a specific task's agent (used for claude-code-phase tasks, which
    the panel lists by task since they share the long-lived SHCA instance).

    Platform-admin-only (see agent_force_kill). A missing/inactive task is
    reported by force_kill_task itself as ok=False → 404 below."""
    reason = await _reason_from(request)
    result = await _force_kill_and_audit(task_id, reason, user)
    status = 200 if result.get("ok") else 404
    return JSONResponse(result, status_code=status)


async def _force_kill_and_audit(task_id: str, reason: str, user: dict) -> dict:
    result = await agent_kill.force_kill_task(task_id, reason, user.get("email") or user.get("uid", ""))
    if result.get("ok"):
        warning = (result.get("warning") or "").strip()
        await _emit(
            "execution.agent_killed",
            {
                "executor": result.get("executor"),
                "aborted": result.get("aborted"),
                "instance_destroyed": result.get("instance_destroyed"),
                "warning": warning,
                "reason": reason,
                "by": user.get("email", ""),
            },
            user, goal_id=result.get("goal_id", ""), task_id=task_id,
        )
        _log.info("Task %s force-killed by %s (warning=%s)", task_id, user.get("email"), warning or "<none>")
        # The kill couldn't be confirmed on the gateway — tell goal admins so the
        # "kill" is never silently a no-op while an agent keeps running.
        if warning:
            await _notify_unconfirmed_kill(result.get("goal_id", ""), task_id, warning)
    return result


async def _notify_unconfirmed_kill(goal_id: str, task_id: str, warning: str) -> None:
    """Push an in-app notification to every goal admin when a force-kill could not
    be confirmed on the gateway. Best-effort — must not block the kill response."""
    if not goal_id:
        return
    try:
        from app.notifications import push_notification  # noqa: PLC0415
        pool = get_pool()
        admins = await pool.fetch(
            "SELECT uid FROM goal_members WHERE goal_id = $1 AND role = 'admin'", goal_id,
        )
        for m in admins:
            await push_notification(
                m["uid"],
                type="agent_kill_unconfirmed",
                title="Force-kill not confirmed",
                body=warning,
                goal_id=goal_id,
                task_id=task_id,
                link="/settings/controls",
            )
    except Exception as exc:
        _log.warning("admin_controls: unconfirmed-kill notification failed task=%s: %s", task_id, exc)


# ── Read views: admin control panel ────────────────────────────────────────

@router.get("/settings/controls", response_class=HTMLResponse)
async def control_panel(
    request: Request,
    user: dict = Depends(require_admin),
    q: str = "",
    page: int = 1,
    per: int = 10,
):
    """Kill switch control panel — goals list with search + pagination, plus system state.

    Restricted to platform.admin (require_admin).
    """
    if per not in (5, 10, 15, 25):
        per = 10
    page = max(1, page)
    panel = await _panel_data()
    goals_data = await _goals_list_data(q=q, page=page, per=per)
    can_edit = user.get("system_role") == SystemRole.PLATFORM_ADMIN
    return templates.TemplateResponse(
        "admin/controls.html",
        {"request": request, "user": user, "q": q, "page": page, "per": per,
         "can_edit": can_edit, **panel, **goals_data,
         **await _verification_state(),
         **await _checkin_state()},
    )


async def _verification_state() -> dict:
    """What the verification switch says, for the page that renders it.

    READ HERE AND NOT IN `_panel_data`, which the 10s HTMX fragment also
    calls. An operator switch changes a few times a year; putting it on
    the poll would run one query every ten seconds per open panel for an
    answer the fragment does not render -- the exact shape `_panel_data`
    documents having already removed twice.
    """
    from app.services import platform_settings

    try:
        row = await platform_settings.get(platform_settings.VERIFICATION)
    except Exception as exc:                                  # noqa: BLE001
        _log.warning("admin_controls: verification switch unreadable: %s", exc)
        return {"verification_on": False, "verification_unknown": True,
                "verification_by": "", "verification_at": None}
    return {"verification_on": bool(row.get("value")),
            "verification_unknown": False,
            "verification_by": row.get("updated_by") or "",
            "verification_at": row.get("updated_at")}


async def _checkin_state() -> dict:
    """How often GO checks in on a running autonomous errand.

    SAME PLACE AND SAME REASONING as the switch above: read on the page
    render, never on the 10s fragment poll.
    """
    from app.services import errand_checkin

    try:
        return {"checkin_seconds": await errand_checkin.window_s(),
                "checkin_min": errand_checkin.MIN_S,
                "checkin_max": errand_checkin.MAX_S,
                "checkin_unknown": False}
    except Exception as exc:                                  # noqa: BLE001
        _log.warning("admin_controls: check-in interval unreadable: %s", exc)
        return {"checkin_seconds": errand_checkin.DEFAULT_S,
                "checkin_min": errand_checkin.MIN_S,
                "checkin_max": errand_checkin.MAX_S,
                "checkin_unknown": True}


@router.post("/settings/controls/errand-checkin")
async def errand_checkin_set(request: Request,
                             user: dict = Depends(require_admin)):
    """Set the errand check-in interval, in seconds.

    CLAMPED RATHER THAN REJECTED. A number outside the range is a typo,
    and the useful answer to a typo is the nearest legal value with the
    page showing what actually landed -- not a form that refuses and
    loses what was typed.
    """
    from app.services import errand_checkin, platform_settings

    body = {}
    try:
        body = await request.json()
    except Exception:                                         # noqa: BLE001
        body = dict(await request.form())
    try:
        wanted = int(str(body.get("seconds", "")).strip())
    except (TypeError, ValueError):
        return JSONResponse({"error": "seconds must be a whole number"},
                            status_code=400)
    wanted = max(errand_checkin.MIN_S, min(errand_checkin.MAX_S, wanted))
    reason = await _reason_from(request)
    by = user.get("email") or user.get("uid", "")
    try:
        moved = await platform_settings.set_int(
            platform_settings.CHECKIN_SECONDS, wanted, by=by, reason=reason)
    except Exception as exc:                                  # noqa: BLE001
        return _system_issue("set errand check-in interval", exc)
    await _emit("execution.setting_changed",
                {"key": platform_settings.CHECKIN_SECONDS,
                 "was": moved.get("was"), "now": moved.get("now"),
                 "reason": reason, "by": by}, user)
    _log.info("ERRAND CHECK-IN %ss -> %ss by %s (reason=%s)",
              moved.get("was"), moved.get("now"), by, reason or "<none>")
    return {"ok": True, **moved}


@router.get("/settings/controls/agents", response_class=HTMLResponse)
async def control_panel_fragment(request: Request, user: dict = Depends(require_admin)):
    """HTMX-polled fragment: currently just the terminal (force-killed) task list.

    Per-agent action buttons (stop / force-kill / resume) were intentionally
    removed from the UI; the underlying endpoints remain live (platform-admin
    only) but have no rendered controls. The fragment is kept so the polling
    mount stays in place if a per-agent view is re-surfaced later.
    """
    data = await _panel_data()
    return templates.TemplateResponse(
        "admin/_controls_fragment.html", {"request": request, "user": user, **data},
    )


# ── data helpers ──────────────────────────────────────────────────────────────

async def _goals_list_data(q: str = "", page: int = 1, per: int = 10) -> dict:
    """Paginated + searchable list of all active goals, with their kill-switch pause state.

    Goals paused via the kill switch (execution_pauses.scope='goal') are surfaced first
    so operators see what needs attention at a glance. Within each bucket the list is
    sorted by goal id for stable ordering.
    """
    pool = get_pool()

    search_pattern = f"%{q}%" if q else ""

    count_row = await pool.fetchval(
        """
        SELECT COUNT(*)
          FROM goals g
         WHERE g.status = 'active'
           AND g.id <> '__platform__'
           AND ($1 = '' OR g.id ILIKE $1 OR g.name ILIKE $1)
        """,
        search_pattern,
    )
    total: int = count_row or 0

    offset = (page - 1) * per
    rows = await pool.fetch(
        """
        SELECT g.id, g.name, g.status, g.created_at,
               ep.reason   AS pause_reason,
               ep.paused_by,
               ep.paused_at,
               (ep.scope IS NOT NULL) AS is_paused
          FROM goals g
          LEFT JOIN execution_pauses ep
                 ON ep.scope = 'goal' AND ep.scope_id = g.id
         WHERE g.status = 'active'
           AND g.id <> '__platform__'
           AND ($1 = '' OR g.id ILIKE $1 OR g.name ILIKE $1)
         ORDER BY (ep.scope IS NOT NULL) DESC, g.id ASC
         LIMIT $2 OFFSET $3
        """,
        search_pattern,
        per,
        offset,
    )

    goals = [dict(r) for r in rows]
    has_prev = page > 1
    has_next = offset + len(goals) < total

    def _page_url(p: int) -> str:
        params = []
        if q:
            params.append(f"q={q}")
        if p != 1:
            params.append(f"page={p}")
        if per != 10:
            params.append(f"per={per}")
        return "/settings/controls" + ("?" + "&".join(params) if params else "")

    page_start = offset + 1 if goals else 0
    page_end = offset + len(goals)

    return {
        "goals": goals,
        "total": total,
        "page_start": page_start,
        "page_end": page_end,
        "page_num": page,
        "prev_url": _page_url(page - 1) if has_prev else "",
        "next_url": _page_url(page + 1) if has_next else "",
    }


async def _panel_data() -> dict:
    """Gather system-freeze state and terminal (force-killed) tasks for the panel.

    Only the keys actually rendered are computed. An earlier version also ran an
    ``active_agents`` join (agent_instances × tasks × execution_pauses ×
    ledger_events, incl. a per-instance ``stuck`` flag) and a ``paused_goals``
    query, but neither is referenced by any template — goal pauses render via
    ``_goals_list_data`` on the main page, and per-instance action UI was
    intentionally removed. Both were dead queries running every 10s per open
    panel, so they've been dropped. If a per-agent / stuck-agent view is
    re-surfaced later, restore those queries alongside the template that reads
    them (endpoints in this router are still live). Stuck detection still runs
    server-side (``agent.stuck_after_pause`` ledger event + push notification in
    task_controller) — it just has no in-panel affordance today.
    """
    pool = get_pool()
    sys_block = await execution_control.is_system_paused()

    halted_tasks = await pool.fetch(
        """SELECT id, goal_id, title, executor, blocked_reason
             FROM tasks
            WHERE state = 'halted' AND status = 'active'
            ORDER BY updated_at DESC LIMIT 100""",
    )
    return {
        "system_paused": sys_block is not None,
        "system_reason": (sys_block.reason if sys_block else ""),
        "halted_tasks": [dict(r) for r in halted_tasks],
    }
