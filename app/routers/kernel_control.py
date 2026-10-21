"""Pause / Resume / Cancel, from the conversation the story lives in.

THREAD-SCOPED BY CONSTRUCTION. Every route takes the thread id in the PATH and
never a story id in the body. `kernel_run_control` derives the story from
`threads.story_id`, so a button in one conversation cannot reach another
conversation's executor — including one somebody else is working. The goal in
the path is checked too, the same guard `chat._guard_thread_in_goal` applies for
the same reason: a thread id is caller-supplied.

WHY POST FOR ALL THREE. Each submits an intent that changes what a running
executor does. `controls` is the only GET, and it is what the page asks to know
which buttons to enable — re-checked on submit, because a page can be seconds
stale and a run moves on its own.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import get_current_user
from app.routers.goals import get_goal_role
from app.services.kernel_more_work import more_work
from app.services.kernel_run_control import (
    ControlRefused,
    cancel,
    controls_for,
    pause,
    resume,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["kernel-control"])

_BASE = "/goals/{pid}/threads/{thread_id}/run"


async def _member(pid: str, user: dict) -> None:
    role = await get_goal_role(user["uid"], pid)
    if role is None and user.get("system_role") != "admin":
        raise HTTPException(status_code=403, detail="Not a goal member")


async def _body(request: Request) -> dict:
    """JSON or form — the buttons post either depending on the page."""
    try:
        got = await request.json()
        return got if isinstance(got, dict) else {}
    except Exception:                                       # noqa: BLE001
        try:
            return dict(await request.form())
        except Exception:                                   # noqa: BLE001
            return {}


@router.get(_BASE + "/controls")
async def run_controls(pid: str, thread_id: str,
                       user: dict = Depends(get_current_user)):
    """What this conversation may do to its executor right now."""
    await _member(pid, user)
    return JSONResponse(await controls_for(thread_id, pid))


async def _apply(fn, pid, thread_id, user, **kw):
    await _member(pid, user)
    try:
        return JSONResponse(await fn(thread_id, pid, user["uid"], **kw))
    except ControlRefused as exc:
        # 409, not 400: the request was well formed and the RUN was in the
        # wrong state for it. A person who pressed Pause on a run that just
        # parked itself has made no mistake.
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:                                # noqa: BLE001
        log.exception("run control %s failed (thread=%s)", fn.__name__, thread_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(_BASE + "/activity")
async def run_activity(pid: str, thread_id: str, after: int = 0,
                       after_check: int = 0, seed: int = 0,
                       user: dict = Depends(get_current_user)):
    """Executor tool calls and verification outcomes, for the live ticker.

    A GET and nothing else. It writes no message, calls no gateway and wakes
    nobody — the two properties the ticker exists under. `after` and
    `after_check` are the cursors the browser holds, so this is a tail rather
    than a re-read; `seed` asks instead for a short backlog, which is what
    switching the ticker on sends so it has something to show at once.
    """
    await _member(pid, user)
    from app.services.kernel_run_control import activity
    return JSONResponse(await activity(
        thread_id, pid, after_seq=after,
        after_check=after_check, seed=min(max(seed, 0), 20)))


@router.post(_BASE + "/pause")
async def run_pause(pid: str, thread_id: str, request: Request,
                    user: dict = Depends(get_current_user)):
    body = await _body(request)
    return await _apply(pause, pid, thread_id, user,
                        reason=str(body.get("reason") or ""))


@router.post(_BASE + "/resume")
async def run_resume(pid: str, thread_id: str, request: Request,
                     user: dict = Depends(get_current_user)):
    """The message rides with the button — that is the point of it.

    It becomes the answer to the `direction` question the pause opened, which
    is how it reaches the executor already framed as a decision rather than an
    instruction. Empty is allowed: "carry on" is a legitimate thing to say.
    """
    body = await _body(request)
    return await _apply(resume, pid, thread_id, user,
                        message=str(body.get("message") or ""))


@router.post(_BASE + "/more-work")
async def run_more_work(pid: str, thread_id: str, request: Request,
                        user: dict = Depends(get_current_user)):
    """One more thing, while the story is still waiting to merge.

    NOT `resume` WITH A LONGER MESSAGE, though it would be easy to build
    it that way. `resume` releases a `direction` hold and carries a
    STEER: it says HOW, and the definition of done is untouched. This
    changes what done means, so it withdraws the approved plan and the
    executor owes a new one. Two different acts; one button for both
    would hide which one a person just performed.

    The text is REQUIRED here, where `resume` allows an empty message.
    "Carry on" is a legitimate steer; "more work, unspecified" is not a
    scope.
    """
    body = await _body(request)
    return await _apply(more_work, pid, thread_id, user,
                        what=str(body.get("what") or body.get("message") or ""))


@router.post(_BASE + "/cancel")
async def run_cancel(pid: str, thread_id: str, request: Request,
                     user: dict = Depends(get_current_user)):
    body = await _body(request)
    return await _apply(cancel, pid, thread_id, user,
                        reason=str(body.get("reason") or ""))
