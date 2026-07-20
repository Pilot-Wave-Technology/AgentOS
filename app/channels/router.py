"""Single FastAPI router handling all platform channel webhooks and OAuth callbacks."""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.auth import get_current_user
from app.channels.registry import get_adapter

router = APIRouter(prefix="/channels", tags=["channels"])
_log = logging.getLogger(__name__)


async def _verify(adapter, body: bytes, headers: dict) -> bool:
    """Call adapter.verify_request(), handling both sync and async implementations.

    Any channel adapter can declare verify_request as either a plain method or
    an async method — this wrapper normalises both so the router stays generic.
    Returns True if the adapter has no verify_request (no-op), or if verification
    passes; False if verification fails.
    """
    if not hasattr(adapter, "verify_request"):
        return True
    result = adapter.verify_request(body, headers)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def _store_verification_token(platform: str, token: str) -> None:
    try:
        from app.database import get_pool
        pool = get_pool()
        row = await pool.fetchrow(
            "SELECT config FROM platform_integrations WHERE platform = $1", platform
        )
        if row is None:
            return
        cfg = row["config"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg) if cfg else {}
        cfg = dict(cfg or {})
        cfg["pending_verification_token"] = token
        await pool.execute(
            "UPDATE platform_integrations SET config = $1 WHERE platform = $2",
            json.dumps(cfg), platform,
        )
    except Exception as exc:
        _log.warning("Could not store verification token for %s: %s", platform, exc)


@router.get("/{platform}/events")
async def platform_events_healthcheck(platform: str) -> Response:
    # Slack (and other platforms) send a GET to confirm the URL is reachable
    # before sending the POST challenge. Return 200 so the verification passes.
    return Response(status_code=200)


@router.post("/{platform}/events")
async def platform_events(platform: str, request: Request) -> Response:
    adapter = get_adapter(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")
    body = await request.body()
    headers = dict(request.headers)

    # Handle webhook verification challenges before signature check
    try:
        payload = json.loads(body)
        # Slack URL verification: must respond synchronously with the challenge
        if payload.get("type") == "url_verification":
            _log.info("Slack URL verification challenge for platform=%s", platform)
            return PlainTextResponse(payload.get("challenge", ""))
        # Notion-style verification token
        if "verification_token" in payload:
            token = payload["verification_token"]
            _log.info("Webhook verification challenge for platform=%s token=%s", platform, token)
            asyncio.create_task(_store_verification_token(platform, token))
            return JSONResponse({"verification_token": token})
    except json.JSONDecodeError:
        pass  # Non-JSON body — not a challenge, proceed to signature check

    if not await _verify(adapter, body, headers):
        _log.warning("Webhook signature invalid: platform=%s", platform)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    _log.info("Webhook received: platform=%s content_length=%d", platform, len(body))
    task = asyncio.create_task(adapter.handle_event(body, headers))

    def _on_done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            _log.warning("Channel event error (%s): %s", platform, t.exception())

    task.add_done_callback(_on_done)
    return Response(status_code=200)


@router.get("/{platform}/oauth/install")
async def oauth_install(
    platform: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    adapter = get_adapter(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")
    return await adapter.oauth_install(request, user)


@router.get("/{platform}/oauth/callback")
async def oauth_callback(
    platform: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    adapter = get_adapter(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")
    return await adapter.oauth_callback(request, user)


@router.get("/{platform}/actions")
async def platform_actions_healthcheck(platform: str) -> Response:
    return Response(status_code=200)


@router.post("/{platform}/actions")
async def platform_actions(platform: str, request: Request) -> Response:
    """Handle interactive component callbacks (e.g. Slack Block Kit button clicks)."""
    adapter = get_adapter(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")

    body = await request.body()
    headers = dict(request.headers)

    if not await _verify(adapter, body, headers):
        _log.warning("Actions signature invalid: platform=%s", platform)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Form-encoded: payload field contains URL-encoded JSON
    try:
        from urllib.parse import parse_qs
        form_data = parse_qs(body.decode("utf-8", errors="replace"))
        raw_payload = (form_data.get("payload") or [""])[0]
        payload = json.loads(raw_payload) if raw_payload else {}
    except Exception as exc:
        _log.warning("Failed to parse actions payload: platform=%s err=%s", platform, exc)
        raise HTTPException(status_code=400, detail="Malformed payload")

    _log.info("Interactive action received: platform=%s type=%s", platform, payload.get("type"))

    if hasattr(adapter, "handle_action"):
        result = await adapter.handle_action(payload)
        return JSONResponse(result if result is not None else {"ok": True})

    return JSONResponse({"ok": True})


@router.get("/{platform}/commands")
async def platform_commands_healthcheck(platform: str) -> Response:
    return Response(status_code=200)


@router.post("/{platform}/commands")
async def platform_commands(platform: str, request: Request) -> Response:
    """Handle slash commands (e.g. Slack /work, /goal, /task, etc.)."""
    adapter = get_adapter(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")

    body = await request.body()
    headers = dict(request.headers)

    if not await _verify(adapter, body, headers):
        _log.warning("Command signature invalid: platform=%s", platform)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        from urllib.parse import parse_qs
        form_data = parse_qs(body.decode("utf-8", errors="replace"))

        def _first(key: str) -> str:
            return (form_data.get(key) or [""])[0]

        command = _first("command")
        text = _first("text")
        user_id = _first("user_id")
        channel_id = _first("channel_id")
        team_id = _first("team_id")
        response_url = _first("response_url")
    except Exception as exc:
        _log.warning("Failed to parse command form: platform=%s err=%s", platform, exc)
        raise HTTPException(status_code=400, detail="Malformed form data")

    _log.info(
        "Slash command: platform=%s command=%s user=%s channel=%s",
        platform, command, user_id, channel_id,
    )

    if hasattr(adapter, "handle_command"):
        result = await adapter.handle_command(command, text, user_id, channel_id, team_id, response_url)
        return JSONResponse(result if result is not None else {"ok": True})

    return JSONResponse({"ok": True})
