"""DEPRECATED — superseded by app/channels/slack/ (generic channel system).
Kept alive only while production Slack app URLs still point to /slack/*.
Removal blocked by: backfill of slack_* → platform_* tables and URL cutover.
Do NOT add new features here.

Slack integration router — mounts all Slack endpoints under /slack/.

Endpoints:
  GET  /slack/oauth/install     — Start Slack OAuth flow
  GET  /slack/oauth/callback    — Handle OAuth redirect from Slack
  POST /slack/events            — Receive Slack events (messages, mentions)
  POST /slack/actions           — Receive Slack interactive actions (buttons)
  POST /slack/commands          — Receive Slack slash commands
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import get_settings
from app.slack.oauth import router as oauth_router

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])

# Mount OAuth sub-router
router.include_router(oauth_router)


async def _run_inbound_safely(team_id: str, event: dict, handler) -> None:
    """Run the Slack inbound handler as a background task, logging any
    exception. Without this wrapper, unhandled exceptions in `asyncio.create_task`
    disappear silently (`Task exception was never retrieved`)."""
    try:
        await handler(team_id, event)
    except Exception as exc:
        _log.error(
            "Slack inbound background task failed (team=%s type=%s): %s",
            team_id, event.get("type", "?"), exc, exc_info=True,
        )


# ── Signature verification ───────────────────────────────────────────────────

def _verify_slack_signature(request_body: bytes, timestamp: str, signature: str) -> bool:
    """Verify that the request came from Slack using the signing secret."""
    settings = get_settings()
    secret = settings.slack_signing_secret
    if not secret:
        _log.warning("SLACK_SIGNING_SECRET not set — skipping signature verification")
        return True  # Allow in dev, block in prod

    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False  # Request older than 5 minutes

    sig_basestring = f"v0:{timestamp}:{request_body.decode('utf-8')}"
    computed = "v0=" + hmac.new(
        secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


# ── Events endpoint ──────────────────────────────────────────────────────────

@router.post("/events")
async def slack_events(request: Request):
    """Receive events from Slack Events API.

    Handles:
    - URL verification (challenge response)
    - app_mention events (trigger @assistant)
    - message events (relay to GO or SHCA)
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "0")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # URL verification challenge (Slack sends this once during setup)
    if payload.get("type") == "url_verification":
        _log.info("Slack URL verification challenge received")
        return PlainTextResponse(payload.get("challenge", ""))

    # Event callback
    if payload.get("type") == "event_callback":
        event = payload.get("event", {})
        team_id = payload.get("team_id", "")
        event_type = event.get("type", "")

        _log.info(
            "Slack event: type=%s team=%s channel=%s user=%s thread_ts=%s",
            event_type, team_id,
            event.get("channel", "-"),
            event.get("user", "-"),
            event.get("thread_ts", "-"),
        )

        # Ignore bot's own messages (prevent echo loops).
        # Check bot_id, subtype, AND the bot's own user ID from slack_workspaces.
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return JSONResponse({"ok": True})
        # Also filter by bot user ID (messages posted via chat.postMessage
        # with username override show user=bot_user_id, not bot_id)
        try:
            from app.slack.config import get_workspace
            ws = await get_workspace(team_id)
            if ws and event.get("user") == ws.get("bot_user_id"):
                return JSONResponse({"ok": True})
        except Exception:
            pass

        # Echo prevention: skip messages we relayed outbound (stamped on
        # messages.external_message_id with the Slack ts).
        event_ts = event.get("ts", "")
        if event_ts:
            try:
                from app.database import get_pool as _get_pool
                _pool = _get_pool()
                _echo = await _pool.fetchval(
                    "SELECT id FROM messages WHERE external_message_id = $1 LIMIT 1",
                    event_ts,
                )
                if _echo:
                    _log.debug(
                        "Slack legacy router: skipping echo (ts=%s matched external_message_id)",
                        event_ts,
                    )
                    return JSONResponse({"ok": True})
            except Exception as _eid_exc:
                _log.debug("Slack legacy router: external_message_id check failed (non-fatal): %s", _eid_exc)

        # Route to inbound handler as a background task. Slack's webhook
        # timeout is ~3s; heavy flows (file download + PDF scan + GCS upload)
        # can take longer, which causes Slack to retry — and retries create
        # duplicates (e.g. double file registration). Ack immediately and
        # let the task run.
        try:
            from app.slack.inbound import handle_slack_event
            asyncio.create_task(
                _run_inbound_safely(team_id, event, handle_slack_event)
            )
        except ImportError:
            _log.debug("Slack inbound handler not yet implemented")
        except Exception as exc:
            _log.error("Slack event handler scheduling error: %s", exc, exc_info=True)

        return JSONResponse({"ok": True})

    return JSONResponse({"ok": True})


# ── Interactive actions endpoint ─────────────────────────────────────────────

@router.post("/actions")
async def slack_actions(request: Request):
    """Receive interactive actions from Slack (button clicks, modal submits)."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "0")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Slack sends actions as form-encoded with a "payload" field
    form = await request.form()
    payload_str = form.get("payload", "")
    if not payload_str:
        raise HTTPException(status_code=400, detail="Missing payload")

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    _log.info(
        "Slack action: type=%s user=%s",
        payload.get("type", "?"),
        payload.get("user", {}).get("id", "?"),
    )

    # Route to actions handler. For view_submission the handler may return
    # a Slack view response dict (e.g. {"response_action": "errors", ...}).
    response_body: dict = {"ok": True}
    try:
        from app.slack.actions import handle_slack_action
        result = await handle_slack_action(payload)
        if result:
            response_body = result
    except ImportError:
        _log.debug("Slack actions handler not yet implemented")
    except Exception as exc:
        _log.error("Slack action handler error: %s", exc, exc_info=True)

    return JSONResponse(response_body)


# ── Slash commands endpoint ──────────────────────────────────────────────────

@router.post("/commands")
async def slack_commands(request: Request):
    """Receive slash commands from Slack (/goal, /story, /task)."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "0")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    command = form.get("command", "")
    text = form.get("text", "")
    user_id = form.get("user_id", "")
    channel_id = form.get("channel_id", "")
    team_id = form.get("team_id", "")
    response_url = form.get("response_url", "")

    _log.info(
        "Slack command: %s %s (user=%s channel=%s team=%s)",
        command, text, user_id, channel_id, team_id,
    )

    # Route to commands handler
    try:
        from app.slack.commands import handle_slack_command
        response_text = await handle_slack_command(
            command, text, user_id, channel_id, team_id, response_url,
        )
        return JSONResponse({"response_type": "ephemeral", "text": response_text})
    except ImportError:
        return JSONResponse({
            "response_type": "ephemeral",
            "text": "Slack commands are not yet enabled.",
        })
    except Exception as exc:
        _log.error("Slack command handler error: %s", exc, exc_info=True)
        return JSONResponse({
            "response_type": "ephemeral",
            "text": f"Error: {exc}",
        })
