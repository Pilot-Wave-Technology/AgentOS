"""Setting a story's timings profile from the control panel.

ADMIN-ONLY, like the kill switch and for a related reason. Two of these
knobs are destructive when they reach work that did not ask for them --
`reclaim_stale_hours` at 0.02 takes a failed run's pod, volume and
workspace 72 seconds after it fails, which is precisely the run whose
workspace answers the question -- so the scope is one story and the
setter is a person, recorded.

THE SETTING IS THE ONLY WAY TO TEST THE SLOW PATHS HONESTLY. Proving
that a two-hour tool does not stall a leg means running one for two
hours, or compressing the clocks for one story and watching the same
code take the same branches in twenty seconds. The second is what this
is for, and the expiry is what keeps it from becoming the first thing
that goes wrong next month.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.database import get_pool
from app.flash import flash_redirect
from app.services import timings_admin
from kernel import timings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/timings")
templates = Jinja2Templates(directory="app/templates")

PAGE = "/settings/timings"


@router.get("", response_class=HTMLResponse)
async def page(request: Request, user: dict = Depends(require_admin)):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await timings_admin.live_rows(conn)
    return templates.TemplateResponse("admin/timings.html", {
        "request": request, "user": user, "rows": rows,
        "knobs": timings_admin.knob_table(),
        "profiles": timings.PROFILES,
        "ttl_choices": timings_admin.TTL_CHOICES,
        "ttl_default": timings_admin.TTL_DEFAULT,
    })


@router.post("")
async def set_for_story(
    request: Request,
    story_id: str = Form(...),
    profile: str = Form(...),
    hours: float = Form(timings_admin.TTL_DEFAULT),
    overrides: str = Form(""),
    reason: str = Form(""),
    user: dict = Depends(require_admin),
):
    story_id = (story_id or "").strip()
    if not story_id:
        return flash_redirect(PAGE, "A story id is required.",
                              flash_type="error")
    if profile not in timings.PROFILES:
        return flash_redirect(PAGE, f"Unknown profile {profile!r}.",
                              flash_type="error")
    # AN EXPIRY, ALWAYS, AND A SHORT ONE. There is no "never" on the form
    # and none here either: a value outside the range is clamped rather
    # than refused, because the failure mode worth preventing is a
    # profile that outlives the session, not a mistyped number.
    hours = min(max(float(hours or timings_admin.TTL_DEFAULT), 0.25),
                float(timings_admin.TTL_MAX_HOURS))

    cleaned, complaints = timings_admin.clean_overrides(overrides)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM stories WHERE id = $1",
                                  story_id)
        if row is None:
            return flash_redirect(PAGE, f"No story {story_id!r}.",
                                  flash_type="error")
        await timings_admin.set_profile(
            conn, story_id, profile, hours, cleaned,
            str(user.get("email") or user.get("uid") or ""), reason.strip())

    # LOGGED WITH THE PERSON, because "why did that run behave oddly" is
    # asked days later against logs, not against this page.
    log.info("timings: %s set to %s for %.2fh by %s (overrides=%s)",
             story_id, profile, hours, user.get("email"), cleaned)

    note = (f"{story_id} runs as {timings.label(profile)} for "
            f"{hours:g}h.")
    if complaints:
        return flash_redirect(PAGE, note + " " + " ".join(complaints),
                              flash_type="warning")
    return flash_redirect(PAGE, note)


@router.post("/{story_id}/clear")
async def clear_for_story(story_id: str,
                          user: dict = Depends(require_admin)):
    pool = get_pool()
    async with pool.acquire() as conn:
        await timings_admin.clear_profile(conn, story_id)
    log.info("timings: %s cleared by %s", story_id, user.get("email"))
    # RUNS ALREADY DISPATCHED ARE UNAFFECTED, and the message says so:
    # the stamp on an execution is what it runs under, and clearing the
    # row does not reach back into a pod that is already going.
    return flash_redirect(
        PAGE, f"{story_id} back to production timings. Runs already "
              f"dispatched keep the profile they started under.")
