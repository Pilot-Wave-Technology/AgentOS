"""Notification endpoints consumed by the sidebar bell and notification list."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.database import get_pool
from app.notifications import mark_read, mark_all_read

router = APIRouter(prefix="/notifications")
templates = Jinja2Templates(directory="app/templates")


@router.get("/unread-count")
async def unread_count(user: dict = Depends(get_current_user)):
    """
    Polled by the sidebar bell via HTMX every 15s.
    Reads a single field from one row — no fan-out risk.
    """
    pool = get_pool()
    count = await pool.fetchval(
        "SELECT unread_notifications FROM users WHERE uid = $1",
        user["uid"],
    ) or 0

    if count:
        return HTMLResponse(
            f'<span id="notif-badge" '
            f'class="absolute -top-1 -right-1 w-4 h-4 bg-indigo-500 rounded-full '
            f'text-[10px] flex items-center justify-center font-bold">'
            f'{min(count, 99)}</span>'
        )
    return HTMLResponse('<span id="notif-badge"></span>')


@router.get("", response_class=HTMLResponse)
async def notification_list(request: Request, user: dict = Depends(get_current_user)):
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT * FROM notifications
           WHERE uid = $1
           ORDER BY created_at DESC
           LIMIT 40""",
        user["uid"],
    )
    notifs = [dict(r) for r in rows]

    return templates.TemplateResponse(
        "partials/notification_list.html",
        {"request": request, "user": user, "notifs": notifs},
    )


@router.post("/{notif_id}/read")
async def mark_one_read(notif_id: str, user: dict = Depends(get_current_user)):
    await mark_read(user["uid"], notif_id)
    return JSONResponse({"ok": True})


@router.post("/mark-all-read")
async def mark_all(user: dict = Depends(get_current_user)):
    await mark_all_read(user["uid"])
    return JSONResponse({"ok": True})
