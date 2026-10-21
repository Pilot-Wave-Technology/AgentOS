"""Admin page and endpoints for refreshing live agents in place.

WHY AN ENDPOINT AND NOT A SCRIPT. The control panel runs on Cloud Run and
nobody SSHes to it, so "run the refresh" has to be something a person can
do from a browser. It is also where the gateway client and the database
pool already live.

PLAN IS A SEPARATE ROUTE FROM APPLY, not a flag on one. A single endpoint
with `apply=false` is one typo away from writing to the whole fleet, and
the typo is invisible in a form post. Two routes means the dangerous one
has to be named -- and the plan is a GET, so it cannot be reached by a
form that meant to do nothing.

NEITHER ROUTE RESTARTS THE GATEWAY. Prompts only go live on a restart,
which stays a deliberate human act performed when nothing is running --
see `run.py` for why the three artefacts have to become live together,
and why a PARTIAL result must block it.

`?format=text` KEEPS THE PLAIN RENDERING. The page is for the person who
has to read the warnings; the text is for anyone scripting against it,
and for pasting into a conversation like this one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import (
    HTMLResponse, PlainTextResponse, RedirectResponse)
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.database import get_pool
from app.openclaw import gateway_admin
from app.services.agent_refresh.run import Result, refresh

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _page(request: Request, *, user: dict, goal_id: str, agent_type: str,
          result: Result | None = None, error: str = "",
          applied: bool = False,
          boot_resync_on: bool = False) -> HTMLResponse:
    """Render the page.

    `user` IS NOT OPTIONAL, and leaving it out is why the first version
    rendered a blank screen. `base.html` wraps the sidebar, the chrome
    and the scripts in `{% if user %}`; Jinja's default undefined is
    falsy rather than an error, so an omitted `user` does not raise --
    it silently renders nothing at all, with a 200. Every other router
    in this app passes it; this one did not.
    """
    count = result.eligible if result else 0
    return templates.TemplateResponse(
        "agent_refresh/index.html",
        {"request": request, "user": user,
         "boot_resync_on": boot_resync_on,
         "goal_id": goal_id, "agent_type": agent_type,
         "report": result.report if result else "",
         "refused": result.refused if result else "",
         "partial": result.partial if result else [],
         "applied": applied, "error": error, "count": count})


async def _boot_on() -> bool:
    """Is the boot-time prompt resync switched on?

    Read per request rather than cached: it is an operator lever, and a
    page that shows a stale value for a switch is worse than one that
    shows none.
    """
    # INSIDE the try, with the call. These were above it, so an
    # ImportError -- and there was one, `get_setting` for `get` --
    # skipped the guard entirely and 500'd the page.
    try:
        from app.services.kernel_prompt_resync import RESYNC_ON_BOOT_KEY
        from app.services.platform_settings import get
        return bool((await get(RESYNC_ON_BOOT_KEY))["value"])
    except Exception:                                       # noqa: BLE001
        # An unreadable switch reports OFF, matching what the boot path
        # does with the same failure -- the two must not disagree.
        return False


async def _run(*, apply: bool, goal_id: str, agent_type: str) -> Result:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await refresh(conn, gateway_admin, apply=apply,
                             goal_id=goal_id.strip(),
                             agent_type=agent_type.strip())


@router.post("/settings/agents/refresh/resync", response_class=HTMLResponse)
async def toggle_boot_resync(
    request: Request,
    on: str = Form(""),
    user: dict = Depends(require_admin),
):
    """Switch the boot-time prompt resync on or off.

    IT LIVES HERE because this page is where somebody reasons about
    prompt propagation, and a switch kept somewhere else is a switch
    nobody relates to the thing it affects. `platform_settings` stores
    it, so it survives a deploy and reads back -- unlike an environment
    variable, which is how four capabilities in this system stayed dark
    without saying so.
    """
    from app.services.kernel_prompt_resync import RESYNC_ON_BOOT_KEY
    from app.services.platform_settings import set_value

    wanted = on.strip().lower() in ("1", "true", "on", "yes")
    await set_value(RESYNC_ON_BOOT_KEY, wanted,
                    by=str(user.get("actor_id") or "admin"),
                    reason="toggled from the agent refresh page")
    # TAKES EFFECT ON THE NEXT CONTROL-PANEL BOOT, not now: the resync
    # only ever runs at startup, so flipping it on mid-life changes
    # nothing until the service restarts. Said on the page rather than
    # left for somebody to discover.
    return await agent_refresh_plan(request, user=user)


@router.get("/settings/agents", response_class=HTMLResponse)
async def agents_index(user: dict = Depends(require_admin)):
    """`/settings/agents` exists because the page below implies it does.

    A path with a segment nobody can visit is a 404 waiting to be
    reported as a bug -- somebody trims the last segment to see what is
    one level up, as they would on any other site, and gets nothing.
    """
    return RedirectResponse(url="/settings/agents/refresh", status_code=307)


@router.get("/settings/agents/refresh", response_class=HTMLResponse)
async def agent_refresh_plan(
    request: Request,
    goal_id: str = "",
    agent_type: str = "",
    format: str = "",
    user: dict = Depends(require_admin),
):
    """What a refresh WOULD do. Reads only; writes nothing anywhere."""
    try:
        result = await _run(apply=False, goal_id=goal_id,
                            agent_type=agent_type)
    except ValueError as exc:
        if format == "text":
            return PlainTextResponse(str(exc), status_code=400)
        return _page(request, user=user, goal_id=goal_id,
                     agent_type=agent_type, error=str(exc),
                     boot_resync_on=await _boot_on())
    logger.info("agent refresh: plan requested by %s (goal=%s type=%s)",
                user.get("actor_id"), goal_id or "*", agent_type or "*")
    if format == "text":
        return PlainTextResponse(result.report)
    return _page(request, user=user, goal_id=goal_id,
                 agent_type=agent_type, result=result,
                 boot_resync_on=await _boot_on())


@router.post("/settings/agents/refresh/apply", response_class=HTMLResponse)
async def agent_refresh_apply(
    request: Request,
    goal_id: str = Form(""),
    agent_type: str = Form(""),
    user: dict = Depends(require_admin),
):
    """Write the rendered prompts, tools and skills to live agents.

    SCOPE TO ONE GOAL FIRST. `goal_id` is the blast-radius control and
    the whole fleet is the default only because a refresh is eventually
    meant to cover it -- prove it on one goal and read the result before
    running it wide.
    """
    try:
        result = await _run(apply=True, goal_id=goal_id,
                            agent_type=agent_type)
    except ValueError as exc:
        return _page(request, user=user, goal_id=goal_id,
                     agent_type=agent_type, error=str(exc),
                     boot_resync_on=await _boot_on())

    if result.refused:
        # Rendered, not raised. The operator needs to read WHY -- an
        # agent writes MEMORY.md inside a turn, so a refusal here is the
        # memory guard doing its job, not an error to retry blindly.
        logger.info("agent refresh: apply refused -- %s", result.refused)
        return _page(request, user=user, goal_id=goal_id,
                     agent_type=agent_type, result=result, applied=False,
                     boot_resync_on=await _boot_on())

    logger.info("agent refresh: applied by %s -- %d agent(s), %d failed, "
                "%d PARTIAL", user.get("actor_id"), len(result.outcomes),
                len(result.failed), len(result.partial))
    return _page(request, user=user, goal_id=goal_id,
                 agent_type=agent_type, result=result, applied=True,
                 boot_resync_on=await _boot_on())
