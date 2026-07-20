"""Auth routes: login, signup, token exchange, logout."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request, Response, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_firebase_token, SYSTEM_ACTOR_ID
from app.config import get_settings
from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.schema import SIGNUP_REQUESTED

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Minimum wait after a rejection before the same uid may resubmit a signup request.
SIGNUP_RESUBMIT_COOLDOWN = timedelta(hours=1)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "firebase_api_key": get_settings().firebase_web_api_key,
            "firebase_project_id": get_settings().firebase_project_id,
        },
    )


@router.post("/auth/session")
async def create_session(
    response: Response,
    id_token: str = Form(...),
):
    """
    Called by the login/signup page JS after Firebase sign-in succeeds.
    Stores the Firebase ID token in an HttpOnly cookie so subsequent
    server-side requests can verify it.

    max_age is 7 days — the cookie is just a container. The token inside
    is refreshed by the client-side refresh loop in base.html every
    50 minutes (Firebase ID tokens expire after 60 min).
    """
    response = RedirectResponse(url="/goals", status_code=303)
    response.set_cookie(
        key="firebase_token",
        value=id_token,
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="lax",
        max_age=7 * 24 * 3600,  # 7 days — token inside is refreshed client-side
    )
    return response


@router.post("/auth/refresh")
async def refresh_session(request: Request, response: Response):
    """
    Called by the client-side refresh loop (base.html) every ~50 minutes.
    Receives a fresh Firebase ID token and updates the HttpOnly cookie.
    Returns 200 on success, 401 if the token is invalid.
    """
    body = await request.json()
    id_token = body.get("id_token", "").strip()
    if not id_token:
        return Response(status_code=400, content="id_token required")

    # Validate the new token before storing it
    try:
        verify_firebase_token(id_token)
    except Exception:
        return Response(status_code=401, content="Invalid token")

    resp = Response(status_code=200, content="ok")
    resp.set_cookie(
        key="firebase_token",
        value=id_token,
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return resp


@router.post("/auth/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("firebase_token")
    return response


# ── Signup flow ──────────────────────────────────────────────────────────────

@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return templates.TemplateResponse(
        "signup.html",
        {
            "request": request,
            "firebase_api_key": get_settings().firebase_web_api_key,
            "firebase_project_id": get_settings().firebase_project_id,
        },
    )


def _set_firebase_cookie(response, id_token: str):
    response.set_cookie(
        key="firebase_token",
        value=id_token,
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="lax",
        max_age=7 * 24 * 3600,
    )


def _retry_available_at(reviewed_at) -> Optional[datetime]:
    """Earliest time a rejected uid may resubmit, or None if not rejected/never reviewed."""
    if reviewed_at is None:
        return None
    return reviewed_at + SIGNUP_RESUBMIT_COOLDOWN


@router.post("/signup/request")
async def signup_request(
    request: Request,
    id_token: str = Form(...),
    requested_role: str = Form("member"),
    message: str = Form(""),
):
    """Verify Firebase token, create signup_request, log event."""
    claims = verify_firebase_token(id_token)
    uid = claims["uid"]
    email = claims.get("email", "")
    display_name = claims.get("name", "")
    photo_url = claims.get("picture", "")

    _log.info("signup_request: token verified uid=%s", uid)

    # Validate requested_role
    if requested_role not in ("editor", "member"):
        requested_role = "member"

    pool = get_pool()

    # A previously rejected uid must wait out the cooldown before resubmitting.
    existing = await pool.fetchrow(
        "SELECT status, reviewed_at FROM signup_requests WHERE uid = $1", uid,
    )
    if existing and existing["status"] == "rejected":
        retry_at = _retry_available_at(existing["reviewed_at"])
        if retry_at and retry_at > datetime.now(timezone.utc):
            _log.info(
                "signup_request: resubmission blocked by cooldown uid=%s retry_at=%s",
                uid, retry_at.isoformat(),
            )
            response = RedirectResponse(url="/signup/rejected", status_code=303)
            _set_firebase_cookie(response, id_token)
            return response

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO signup_requests
                       (uid, email, display_name, photo_url, requested_role, message)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (uid) DO UPDATE SET
                       email = $2, display_name = $3, photo_url = $4,
                       requested_role = $5, message = $6,
                       -- Never downgrade an already-approved account back to pending.
                       status = CASE WHEN signup_requests.status = 'approved'
                                     THEN signup_requests.status ELSE 'pending' END,
                       reviewed_by = CASE WHEN signup_requests.status = 'approved'
                                          THEN signup_requests.reviewed_by ELSE NULL END,
                       reviewed_at = CASE WHEN signup_requests.status = 'approved'
                                          THEN signup_requests.reviewed_at ELSE NULL END""",
                uid, email, display_name, photo_url, requested_role, message,
            )
            await append_event(
                conn,
                event_type=SIGNUP_REQUESTED,
                payload={
                    "uid": uid,
                    "email": email,
                    "display_name": display_name,
                    "requested_role": requested_role,
                },
                actor_id=SYSTEM_ACTOR_ID,
                actor_type="system",
            )

    _log.info("signup_request: recorded uid=%s requested_role=%s", uid, requested_role)

    # Set cookie so /signup/pending can identify the user
    response = RedirectResponse(url="/signup/pending", status_code=303)
    _set_firebase_cookie(response, id_token)
    return response


@router.get("/signup/pending", response_class=HTMLResponse)
async def signup_pending(request: Request):
    return templates.TemplateResponse("signup_pending.html", {"request": request})


@router.get("/signup/rejected", response_class=HTMLResponse)
async def signup_rejected(request: Request):
    can_retry = True
    retry_at_display = None

    token = request.cookies.get("firebase_token")
    if token:
        try:
            claims = verify_firebase_token(token)
            uid = claims["uid"]
            pool = get_pool()
            sr = await pool.fetchrow(
                "SELECT reviewed_at FROM signup_requests WHERE uid = $1 AND status = 'rejected'",
                uid,
            )
            if sr:
                retry_at = _retry_available_at(sr["reviewed_at"])
                if retry_at and retry_at > datetime.now(timezone.utc):
                    can_retry = False
                    retry_at_display = retry_at.strftime("%b %d, %Y %H:%M UTC")
        except Exception:
            pass  # Informational only — worst case we just show the retry link early.

    return templates.TemplateResponse(
        "signup_rejected.html",
        {
            "request": request,
            "can_retry": can_retry,
            "retry_at_display": retry_at_display,
        },
    )
