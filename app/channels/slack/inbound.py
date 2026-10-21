"""Slack inbound adapter for the generic channel system.

Registered in channels/registry.py as _ADAPTERS["slack"] so the generic
/channels/slack/events webhook route dispatches here.

Channel routing (using platform_channel_bindings):
  - actor_id IS NULL  → goal channel → route to GO session
  - actor_id IS NOT NULL → user-goal channel → route to SHCA session
  - orch_channel_id in goal_platform_integrations.config → read-only, delete human messages
  - anything else → ignore

Echo prevention: bot messages filtered by bot_user_id from integration credentials.
Duplicate delivery: base.is_duplicate() with "slack" + Slack event_id.
"""

from app.channels.slack.client import make_async_client
import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from typing import Optional

from fastapi import Request
from fastapi.responses import PlainTextResponse, JSONResponse

from app.channels import base
from app.channels.base import route_to_session as _route_to_session

_log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


class SlackInboundAdapter:
    """Inbound adapter for Slack events conforming to the generic channel interface."""

    async def verify_request(self, body: bytes, headers: dict) -> bool:
        """Verify Slack request signature (async — router calls via _verify helper).

        Reads signing secret from platform_integrations.config first (set via
        admin UI), falling back to SLACK_SIGNING_SECRET env var. Rejects
        requests older than 5 minutes to prevent replay attacks.

        FAILS CLOSED. This used to return True when no secret could be
        found anywhere, logging "dev mode" -- and `slack_signing_secret`
        defaults to "". All three inbound surfaces (events, actions,
        commands) gate on this one function, so a half-finished setup, a
        dropped DB row or a cleared secret meant the service accepted
        unsigned POSTs from anyone and dispatched them to real handlers:
        approve a plan, push a diff, replan a story. There is no
        environment where that is wanted, so there is no escape hatch
        here -- an escape hatch is just the production configuration
        nobody meant to ship. An unconfigured deployment gets no Slack,
        which is the correct amount of Slack for a deployment that has
        not been configured.
        """
        from app.channels.slack.config import get_signing_secret
        from app.config import get_settings

        try:
            secret = await get_signing_secret()
        except Exception as exc:
            # A DB blip must not become a 500: Slack disables an endpoint
            # that keeps erroring, and recovering from that is manual.
            _log.warning("Slack signature check: could not read signing secret: %s", exc)
            return False
        if not secret:
            secret = get_settings().slack_signing_secret or ""

        if not secret:
            _log.error(
                "Slack signing secret not configured — REJECTING request. "
                "Set it at /settings/integrations.")
            return False

        timestamp = headers.get("x-slack-request-timestamp", "")
        signature = headers.get("x-slack-signature", "")
        if not timestamp or not signature:
            return False

        try:
            if abs(time.time() - int(timestamp)) > 300:
                _log.warning("Slack webhook: request older than 5 minutes — rejected")
                return False
        except (ValueError, TypeError):
            return False

        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
        computed = "v0=" + hmac.new(
            secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, signature)

    async def handle_event(self, body: bytes, headers: dict) -> None:
        """Parse and route a Slack event payload."""
        try:
            payload = json.loads(body)
        except Exception:
            _log.warning("Slack inbound: invalid JSON payload")
            return

        # url_verification is handled synchronously in channels/router.py
        if payload.get("type") == "url_verification":
            return

        if payload.get("type") != "event_callback":
            _log.debug("Slack inbound: ignoring payload type=%s", payload.get("type"))
            return

        team_id = payload.get("team_id", "")
        event = payload.get("event", {})
        event_type = event.get("type", "")

        # Dedup by Slack's event_id. The claim is taken BEFORE handling,
        # which is what makes it atomic, and released again if handling
        # raises -- see base.forget_event for why losing the event is the
        # worse of the two failures.
        event_id = payload.get("event_id") or str(uuid.uuid4())
        if await base.is_duplicate("slack", event_id):
            return
        try:
            return await self._handle_claimed_event(payload, team_id, event,
                                                    event_type, body, headers)
        except Exception:
            await base.forget_event("slack", event_id)
            raise

    async def _handle_claimed_event(self, payload, team_id, event, event_type,
                                    body, headers):
        """The body of handle_event, once the dedup claim is held."""

        # Filter bot's own messages
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        try:
            from app.channels.slack.config import get_workspace_credentials
            from app.database import get_pool
            pool = get_pool()
            ws_creds = await get_workspace_credentials(team_id, pool)
            bot_user_id = (ws_creds or {}).get("bot_user_id", "")
            if bot_user_id and event.get("user") == bot_user_id:
                return
        except Exception as _exc:
            _log.warning("Could not resolve bot_user_id for self-message filter: %s", _exc)

        # Echo prevention: if this event's ts matches an outbound relay we stamped
        # on messages.external_message_id, it's our own echo — skip it.
        event_ts = event.get("ts", "")
        if event_ts:
            try:
                from app.database import get_pool as _get_pool
                _pool = get_pool()
                _echo = await _pool.fetchval(
                    "SELECT id FROM messages WHERE external_message_id = $1 LIMIT 1",
                    event_ts,
                )
                if _echo:
                    _log.debug(
                        "Slack inbound: skipping echo (ts=%s matched messages.external_message_id)",
                        event_ts,
                    )
                    return
            except Exception as _eid_exc:
                _log.debug("Slack inbound: external_message_id check failed (non-fatal): %s", _eid_exc)

        # Drop synthetic membership events
        subtype = event.get("subtype", "") or ""
        if subtype in (
            "channel_join", "channel_leave", "channel_topic", "channel_purpose",
            "channel_name", "bot_add", "bot_remove", "group_join", "group_leave",
            "channel_archive", "channel_unarchive",
        ):
            _log.debug("Slack inbound: skipping synthetic subtype=%s", subtype)
            return

        # Dedup app_mention + message duplicate: skip message if it contains a bot mention
        if event_type == "message" and bot_user_id:
            text = event.get("text", "") or ""
            if f"<@{bot_user_id}>" in text:
                _log.debug("Slack inbound: skipping message that duplicates app_mention")
                return

        channel = event.get("channel", "")
        user = event.get("user", "")

        if event_type == "member_joined_channel":
            await _handle_member_joined(team_id, event, pool)
            return

        # file_shared uses channel_id/user_id, not channel/user — handle before the guard.
        if event_type == "file_shared":
            fs_channel = event.get("channel_id", "")
            fs_user = event.get("user_id", "")
            if fs_channel and fs_user:
                await _handle_file_shared(team_id, fs_channel, fs_user, event.get("file_id", ""), pool)
            return

        if not channel or not user:
            return

        _log.info(
            "Slack inbound: type=%s team=%s channel=%s user=%s",
            event_type, team_id, channel, user,
        )

        await _route_event(team_id, channel, user, event, pool)

    async def handle_action(self, payload: dict) -> dict | None:
        """Dispatch a Block Kit action or modal submission."""
        from app.channels.slack.actions import handle_slack_action
        return await handle_slack_action(payload)

    async def handle_command(
        self,
        command: str,
        text: str,
        user_id: str,
        channel_id: str,
        team_id: str,
        response_url: str,
        trigger_id: str = "",
    ) -> dict:
        """Dispatch a slash command and return the JSON response body."""
        from app.channels.slack.commands import handle_slack_command
        reply = await handle_slack_command(
            command=command,
            text=text,
            user_id=user_id,
            channel_id=channel_id,
            team_id=team_id,
            response_url=response_url,
            trigger_id=trigger_id,
        )
        return {"response_type": "ephemeral", "text": reply}

    async def oauth_install(self, request: Request, user: dict):
        """Redirect admin to Slack OAuth consent screen.

        Reads client_id from platform_integrations.config first (set via admin
        setup form), falling back to the SLACK_CLIENT_ID env var so existing
        deployments that pre-date the setup form continue to work.
        """
        from fastapi.responses import RedirectResponse
        from app.config import get_settings
        from app.channels.slack.config import get_app_credentials
        from app.database import get_pool
        import urllib.parse

        db_creds = await get_app_credentials()
        client_id = db_creds.get("client_id", "")

        if not client_id:
            settings = get_settings()
            client_id = settings.slack_client_id or ""

        if not client_id:
            return JSONResponse(
                {"error": "Slack client_id not configured — fill in the Slack setup form on /settings/integrations"},
                status_code=500,
            )

        # One source of truth, derived from the API methods we call --
        # see app/channels/slack/scopes.py. Slack grants the INTERSECTION
        # of this and the manifest, so a hand-kept second list here is a
        # silently under-granted scope waiting to happen.
        from app.channels.slack.scopes import scope_string
        scopes = scope_string()

        base_url = str(request.base_url).replace("http://", "https://")
        callback_url = f"{base_url}channels/slack/oauth/callback"

        # CSRF: without a state parameter, anyone who can get an admin to
        # follow a link can have us exchange and store THEIR code as this
        # deployment's workspace credentials.
        from app.channels.slack.oauth_state import issue as _issue_state
        state = await _issue_state(get_pool())

        slack_url = (
            f"https://slack.com/oauth/v2/authorize"
            f"?client_id={client_id}"
            f"&scope={urllib.parse.quote(scopes)}"
            f"&redirect_uri={urllib.parse.quote(callback_url)}"
            f"&state={urllib.parse.quote(state)}"
        )
        _log.info("Redirecting %s to Slack OAuth", user.get("email", "?"))
        return RedirectResponse(url=slack_url)

    async def oauth_callback(self, request: Request, user: dict):
        """Handle Slack OAuth redirect — exchange code for tokens and store workspace.

        Reads client_id/client_secret from platform_integrations.config first,
        falling back to env vars for deployments that pre-date the setup form.
        """
        from fastapi.responses import RedirectResponse
        from app.config import get_settings
        from app.channels.slack.config import upsert_workspace, get_app_credentials
        from app.database import get_pool
        import json as _json
        import httpx

        code = request.query_params.get("code", "")
        error = request.query_params.get("error", "")

        if error:
            _log.warning("Slack OAuth error: %s", error)
            return RedirectResponse(url=f"/settings/integrations?error={error}", status_code=303)
        if not code:
            return JSONResponse({"error": "Missing OAuth code"}, status_code=400)

        from app.channels.slack.oauth_state import verify as _verify_state
        if not await _verify_state(get_pool(), request.query_params.get("state", "")):
            _log.warning("Slack OAuth callback rejected: bad or missing state")
            return RedirectResponse(
                url="/settings/integrations?error=bad_oauth_state", status_code=303)

        # Resolve client credentials: DB config takes priority over env vars
        db_creds = await get_app_credentials()
        pool = get_pool()
        db_row = await pool.fetchrow(
            "SELECT config FROM platform_integrations WHERE platform = 'slack' LIMIT 1",
        )
        db_cfg = {}
        if db_row and db_row["config"]:
            raw = db_row["config"]
            db_cfg = _json.loads(raw) if isinstance(raw, str) else dict(raw)

        settings = get_settings()
        client_id = db_cfg.get("client_id") or settings.slack_client_id or ""
        client_secret = db_cfg.get("client_secret") or settings.slack_client_secret or ""

        if not client_id or not client_secret:
            _log.error("Slack OAuth callback: client credentials not configured")
            return RedirectResponse(url="/settings/integrations?error=missing_client_credentials", status_code=303)

        base_url = str(request.base_url).replace("http://", "https://")
        callback_url = f"{base_url}channels/slack/oauth/callback"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": callback_url,
                },
            )
            data = resp.json()

        if not data.get("ok"):
            error_msg = data.get("error", "unknown")
            _log.error("Slack OAuth token exchange failed: %s", error_msg)
            return RedirectResponse(url=f"/settings/integrations?error={error_msg}", status_code=303)

        workspace_id = (data.get("team") or {}).get("id", "")
        workspace_name = (data.get("team") or {}).get("name", "")
        bot_token = data.get("access_token", "")
        bot_user_id = data.get("bot_user_id", "")
        app_id = data.get("app_id", "")
        scopes = (data.get("scope", "") or "").split(",")

        if not workspace_id or not bot_token:
            _log.error("Slack OAuth response missing workspace_id or bot_token")
            return RedirectResponse(url="/settings/integrations?error=missing_token", status_code=303)

        await upsert_workspace(
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            bot_token=bot_token,
            bot_user_id=bot_user_id,
            app_id=app_id,
            installed_by=user.get("uid", ""),
            scopes=scopes,
            client_id=client_id,
            pool=pool,
        )
        _log.info(
            "Slack workspace installed: %s (%s) by %s",
            workspace_name, workspace_id, user.get("email", "?"),
        )
        return RedirectResponse(url="/settings/integrations?success=slack_installed", status_code=303)


# ── Event routing ──────────────────────────────────────────────────────────────

async def _route_event(team_id: str, channel: str, user: str, event: dict, pool) -> None:
    """Route a Slack event to the right handler using platform_channel_bindings."""
    from app.channels.base import get_binding
    from app.channels.slack.config import find_goal_by_orch_channel

    binding = await get_binding("slack", channel)
    if binding:
        if binding.get("actor_id") is None:
            # Goal channel (GO thread)
            await _handle_goal_channel_event(team_id, binding, event, pool)
        else:
            # User-goal channel (SHCA thread)
            await _handle_user_goal_channel_event(team_id, binding, event, pool)
        return

    # Check orchestrator channel (stored in goal_platform_integrations.config)
    ws_creds = await _get_workspace_creds(team_id, pool)
    workspace_id = (ws_creds or {}).get("workspace_id", team_id)
    orch = await find_goal_by_orch_channel(workspace_id, channel, pool)
    if orch:
        await _handle_orch_channel_event(orch, event)
        return

    _log.debug("Slack inbound: channel %s not mapped to any goal — ignoring", channel)


async def _get_workspace_creds(team_id: str, pool) -> Optional[dict]:
    """Return workspace credentials dict for a team_id."""
    from app.channels.slack.config import get_workspace_credentials
    try:
        return await get_workspace_credentials(team_id, pool)
    except Exception:
        return None


# ── Goal channel handler ───────────────────────────────────────────────────────

async def _handle_goal_channel_event(
    team_id: str,
    binding: dict,
    event: dict,
    pool,
) -> None:
    """Message in the goal channel — @mention triggers work-on-story, plain routes to GO."""
    goal_id = binding["goal_id"]
    channel = event.get("channel", "")
    slack_user = event.get("user", "")
    text = event.get("text", "") or ""
    event_type = event.get("type", "")
    files = event.get("files") or []

    bot_user_id = await _get_bot_user_id(goal_id, pool)
    user_uid = await _resolve_user(team_id, slack_user, goal_id, pool)
    if not user_uid:
        return

    if files:
        await _handle_file_uploads(goal_id, channel, slack_user, user_uid, files, goal_id, pool)
        if not text.strip():
            return

    is_mention = event_type == "app_mention" or (bot_user_id and f"<@{bot_user_id}>" in text)
    if is_mention:
        await _handle_work_on_story(team_id, goal_id, binding, slack_user, user_uid, text, pool)
        return

    resolved_text = await _resolve_mentions(text, team_id, pool)
    await _route_to_go(goal_id, user_uid, resolved_text, pool, binding)


# ── Orchestrator channel handler ───────────────────────────────────────────────

async def _handle_orch_channel_event(config: dict, event: dict) -> None:
    """Orchestrator channel is read-only — delete human messages and send ephemeral notice."""
    from slack_sdk.errors import SlackApiError

    channel = event.get("channel", "")
    slack_user = event.get("user", "")
    ts = event.get("ts", "")
    bot_token = config.get("bot_token", "")
    goal_channel_id = config.get("goal_channel_id", "")

    if not bot_token:
        return

    client = make_async_client(bot_token)
    try:
        goal_ref = f"<#{goal_channel_id}>" if goal_channel_id else "the goal channel"
        await client.chat_postEphemeral(
            channel=channel,
            user=slack_user,
            text=(
                f"This is a read-only coordination log. "
                f"Use {goal_ref} to talk to the Goal Orchestrator."
            ),
        )
        await client.chat_delete(channel=channel, ts=ts)
        _log.info("Deleted human message in orch channel: channel=%s ts=%s", channel, ts)
    except SlackApiError as exc:
        _log.warning("Orch read-only enforcement failed: %s", exc)


# ── User-goal channel handler ──────────────────────────────────────────────────

async def _handle_user_goal_channel_event(
    team_id: str,
    binding: dict,
    event: dict,
    pool,
) -> None:
    """Message in a user-goal channel — route to the bound SHCA session."""
    goal_id = binding["goal_id"]
    thread_id = str(binding["thread_id"]) if binding.get("thread_id") else ""
    agent_id = str(binding.get("agent_id", "")) if binding.get("agent_id") else ""
    channel = event.get("channel", "")
    slack_user = event.get("user", "")
    text = event.get("text", "") or ""
    files = event.get("files") or []
    current_story_id = str(binding.get("current_story_id", "")) if binding.get("current_story_id") else ""

    if not thread_id:
        _log.warning("Slack inbound: user-goal binding has no thread_id goal=%s channel=%s", goal_id, channel)
        return

    user_uid = await _resolve_user(team_id, slack_user, goal_id, pool)
    if not user_uid:
        return

    if files:
        await _handle_file_uploads(goal_id, channel, slack_user, user_uid, files, current_story_id, pool)
        if not text.strip():
            return

    resolved_text = await _resolve_mentions(text, team_id, pool)
    await _route_to_shca(goal_id, thread_id, agent_id, user_uid, resolved_text, pool)


# ── Member joined channel ──────────────────────────────────────────────────────

async def _handle_member_joined(team_id: str, event: dict, pool) -> None:
    """A Slack user joined one of our channels — gate membership by email."""
    from slack_sdk.errors import SlackApiError
    from app.channels.base import get_binding
    from app.channels.slack.config import get_workspace_credentials, find_goal_by_orch_channel
    from app.channels.slack.thread_map import auto_map_by_email

    channel = event.get("channel", "")
    slack_user = event.get("user", "")
    if not channel or not slack_user:
        return

    ws_creds = await get_workspace_credentials(team_id, pool)
    if not ws_creds or not ws_creds.get("bot_token"):
        return
    bot_token = ws_creds["bot_token"]
    bot_user_id = ws_creds.get("bot_user_id", "")

    # Ignore the bot joining its own channels
    if slack_user == bot_user_id:
        return

    # Find the goal owning this channel
    binding = await get_binding("slack", channel)
    goal_id = None
    if binding:
        goal_id = binding["goal_id"]
    else:
        workspace_id = ws_creds.get("workspace_id", team_id)
        orch = await find_goal_by_orch_channel(workspace_id, channel, pool)
        if orch:
            goal_id = orch["goal_id"]

    if not goal_id:
        _log.debug("Slack member_joined: channel %s not mapped — ignoring", channel)
        return

    # Look up user's email via Slack profile
    client = make_async_client(bot_token)
    try:
        info = await client.users_info(user=slack_user)
        profile = (info.get("user") or {}).get("profile") or {}
        email = profile.get("email", "")
        display_name = profile.get("display_name") or profile.get("real_name") or ""
    except SlackApiError as exc:
        _log.warning("Slack member_joined: users.info failed for %s: %s", slack_user, exc)
        return

    if not email:
        _log.info("Slack member_joined: no email for %s — kicking from goal channel", slack_user)
        await _kick_and_notify(client, channel, slack_user, "Your Slack profile has no email address — access denied.")
        return

    uid = await auto_map_by_email(slack_user, email, display_name, pool=pool)
    if not uid:
        _log.info(
            "Slack member_joined: email %s not in users table — kicking from goal channel %s",
            email, channel,
        )
        await _kick_and_notify(client, channel, slack_user,
                         "Your email address is not registered in the control panel.")
        return

    # Check goal membership
    is_member = await pool.fetchval(
        "SELECT 1 FROM goal_members WHERE goal_id = $1 AND uid = $2", goal_id, uid,
    )
    if not is_member:
        _log.info("Slack member_joined: uid=%s not a goal member — kicking from %s", uid, channel)
        await _kick_and_notify(client, channel, slack_user,
                         "You are not a member of this goal in the control panel.")
        return

    _log.info(
        "Slack member_joined: verified uid=%s email=%s in goal=%s channel=%s",
        uid, email, goal_id, channel,
    )


async def _kick_and_notify(client, channel: str, slack_user: str, reason: str) -> None:
    """Kick a user from a channel and send them a DM explaining why."""
    from slack_sdk.errors import SlackApiError
    try:
        await client.conversations_kick(channel=channel, user=slack_user)
    except SlackApiError as exc:
        _log.warning("Kick failed for %s from %s: %s", slack_user, channel, exc)
    try:
        dm = await client.conversations_open(users=slack_user)
        dm_channel = (dm.get("channel") or {}).get("id", "")
        if dm_channel:
            await client.chat_postMessage(
                channel=dm_channel,
                text=f":no_entry: You were removed from a channel: {reason}",
            )
    except SlackApiError as exc:
        _log.warning("Could not DM kick reason to %s: %s", slack_user, exc)


# ── User resolution ────────────────────────────────────────────────────────────

async def _resolve_user(team_id: str, slack_user: str, goal_id: str, pool) -> Optional[str]:
    """Resolve a Slack user to a control-panel UID, mapping via email if needed."""
    from app.channels.slack.thread_map import get_user_uid, auto_map_by_email

    uid = await get_user_uid(slack_user, pool)
    if uid:
        return uid

    # Try email lookup via Slack profile
    try:
        from app.channels.slack.config import get_slack_config_for_goal
        cfg = await get_slack_config_for_goal(goal_id, pool)
        if not cfg or not cfg.get("bot_token"):
            return None
        client = make_async_client(cfg["bot_token"])
        info = await client.users_info(user=slack_user)
        profile = (info.get("user") or {}).get("profile") or {}
        email = profile.get("email", "")
        display_name = profile.get("display_name") or profile.get("real_name") or ""
        if email:
            uid = await auto_map_by_email(slack_user, email, display_name, pool=pool)
            if not uid:
                _log.warning(
                    "Slack user resolution: no users row matched email=%s for slack_user=%s team=%s goal=%s",
                    email, slack_user, team_id, goal_id,
                )
            return uid
        _log.warning(
            "Slack user resolution: Slack profile has no email for slack_user=%s team=%s goal=%s",
            slack_user, team_id, goal_id,
        )
    except Exception as exc:
        _log.warning("Slack user resolution failed for slack_user=%s team=%s goal=%s: %s", slack_user, team_id, goal_id, exc)
    return None


async def _get_bot_user_id(goal_id: str, pool) -> str:
    """Return the bot_user_id for the Slack integration active for this goal."""
    from app.channels.slack.config import get_slack_config_for_goal
    cfg = await get_slack_config_for_goal(goal_id, pool)
    return (cfg or {}).get("bot_user_id", "")


# ── Session routing ────────────────────────────────────────────────────────────

async def _route_to_go(goal_id: str, user_uid: str, text: str, pool,
                       binding: dict | None = None) -> None:
    """Route a message to the GO session for the thread this channel IS.

    THE BINDING NAMES THE THREAD. This used to ignore the binding entirely
    and re-read `goals.go_thread_id`, which was right while a goal had one
    GO channel and is wrong the moment it has several: every channel's
    replies collapsed into the single goal-wide thread, so a message sent
    in the channel for story B appeared in the conversation about story A.
    Falls back to go_thread_id only for a binding that carries no thread --
    the goal-wide channel, which is still a real thing.
    """
    from app.services.agent_driver import ensure_go_session

    if not text or not text.strip():
        return

    _log.info("Slack inbound → GO: goal=%s text_len=%d", goal_id, len(text))
    try:
        go_thread_id = (binding or {}).get("thread_id") or await pool.fetchval(
            "SELECT go_thread_id FROM goals WHERE id = $1", goal_id)
        if not go_thread_id:
            _log.warning("Slack inbound: no thread for goal=%s", goal_id)
            return

        # Kill switch — conversational freeze gate (Slack → GO). Drop the inbound
        # turn and post a visible "paused" notice back to the Slack channel via
        # the generic thread relay. The send_to_gateway backstop would also drop
        # it, but only browser subscribers see that notice — this reaches Slack.
        from app.services import execution_control  # noqa: PLC0415
        _block = await execution_control.conversation_block(goal_id)
        if _block is not None:
            _log.info("Slack inbound → GO dropped (frozen scope=%s) goal=%s", _block.scope, goal_id)
            try:
                from app.channels.base import relay_to_thread_channel
                await relay_to_thread_channel(
                    goal_id, str(go_thread_id),
                    "⏸ Execution is paused (" + _block.scope + "). Resume in the "
                    "control panel to continue.",
                    username="System",
                )
            except Exception as _exc:
                _log.warning("Could not post execution-paused notification to Slack (GO): %s", _exc)
            return

        import json
        import uuid as _uuid
        from app.database import get_pool
        real_pool = get_pool()
        msg_id = str(_uuid.uuid4())
        await real_pool.execute(
            """INSERT INTO messages (id, thread_id, sender, role, content, token_count, platform)
               VALUES ($1, $2, $3, 'human', $4, 0, 'slack')""",
            msg_id, go_thread_id, user_uid, text,
        )
        # Increment unread for all other goal-thread participants so their web
        # sidebar badges update in real-time via pg_notify → user WS.
        # Non-critical: badge failure must never block Slack message routing.
        try:
            from app.unread import increment_unread as _increment_unread
            # Pass no sender_uid so ALL participants (including the Slack sender)
            # get incremented. The sender was outside the web UI, so their badge
            # should reflect this message as new activity too.
            await _increment_unread(real_pool, go_thread_id)
        except Exception as _ue:
            _log.warning("slack-inbound: increment_unread (GO human msg) failed (non-fatal): %s", _ue)
        session = await ensure_go_session(goal_id)
        # Push to any open GO thread UI subscribers in real-time.
        # The GO thread uses its own goal-keyed subscriber registry
        # (_goal_thread_subscribers), not session_manager._subscribers,
        # so we must call _broadcast_to_goal_thread directly.
        try:
            display = await real_pool.fetchval(
                "SELECT display_name FROM users WHERE uid = $1", user_uid,
            )
            from app.routers.goal_thread import _broadcast_to_goal_thread
            # NAMED. Slack's goal channel is the MAIN GO thread; an
            # errand thread's watchers must not see it.
            await _broadcast_to_goal_thread(goal_id, json.dumps({
                "type": "message",
                "role": "human",
                "sender": user_uid,
                "sender_name": display or "",
                "content": text,
                "origin": "slack",
            }), go_thread_id)
        except Exception as _be:
            _log.debug("Slack inbound: GO thread broadcast failed (non-fatal): %s", _be)
        if session:
            await session.send_to_gateway(
                json.dumps({"type": "message", "content": text, "thread_id": str(go_thread_id)}),
                persisted_message_id=msg_id,
            )
    except Exception as exc:
        _log.warning("Slack inbound: GO routing failed goal=%s: %s", goal_id, exc, exc_info=True)


async def _route_to_shca(
    goal_id: str,
    thread_id: str,
    agent_id: str,
    user_uid: str,
    text: str,
    pool,
) -> None:
    """Route a message to an SHCA session."""
    from app.services.agent_driver import ensure_shca_session

    if not text or not text.strip():
        return

    # If the binding's agent_id is stale or null, look it up from the thread
    # record so ensure_shca_session uses the right session key and the live
    # broadcast reaches the browser's WS subscriber.
    if not agent_id:
        _resolved = await pool.fetchval(
            "SELECT agent_id FROM threads WHERE id = $1", thread_id,
        )
        if _resolved:
            agent_id = str(_resolved)
            _log.info("Slack inbound → SHCA: resolved agent_id=%s from thread", agent_id[:12])
        else:
            _log.warning("Slack inbound → SHCA: agent_id missing and thread %s not found", thread_id[:12])

    _log.info("Slack inbound → SHCA: goal=%s thread=%s agent=%s text_len=%d",
              goal_id, thread_id[:12], agent_id[:12] if agent_id else "(none)", len(text))
    try:
        # Kill switch — conversational freeze gate (Slack → SHCA).
        from app.services import execution_control  # noqa: PLC0415
        _block = await execution_control.conversation_block(goal_id)
        if _block is not None:
            _log.info("Slack inbound → SHCA dropped (frozen scope=%s) goal=%s thread=%s",
                      _block.scope, goal_id, thread_id[:12])
            try:
                from app.channels.base import relay_to_thread_channel
                await relay_to_thread_channel(
                    goal_id, thread_id,
                    "⏸ Execution is paused (" + _block.scope + "). Resume in the "
                    "control panel to continue.",
                    username="System",
                )
            except Exception as _exc:
                _log.warning("Could not post execution-paused notification to Slack (SHCA): %s", _exc)
            return

        import json
        import uuid as _uuid
        from app.database import get_pool
        real_pool = get_pool()

        # Q&A relay: if a phase task is blocked awaiting a human answer on this
        # thread, route the message directly to it — same as the WebSocket chat
        # path in chat.py. The conversation_block() check above already gates
        # the paused case (returns early with a Slack notice), so we only reach
        # here when execution is live.
        blocked_task = await real_pool.fetchrow(
            """SELECT t.* FROM tasks t
                JOIN stories s ON s.id = t.story_id
               WHERE s.active_thread_id = $1
                 AND t.executor = 'claude-code-phase'
                 AND t.state = 'blocked'
                 AND t.last_blocker_question IS NOT NULL
               ORDER BY t.last_blocker_at DESC NULLS LAST
               LIMIT 1""",
            thread_id,
        )
        if blocked_task:
            try:
                from app.services.phase_dispatcher import dispatch_resume_with_answer
                import asyncio as _asyncio_qa
                # Persist the human message so chat history is complete before dispatch.
                msg_id = str(_uuid.uuid4())
                await real_pool.execute(
                    """INSERT INTO messages (id, thread_id, sender, role, content, token_count, platform)
                       VALUES ($1, $2, $3, 'human', $4, 0, 'slack')""",
                    msg_id, thread_id, user_uid, text,
                )
                _asyncio_qa.create_task(dispatch_resume_with_answer(dict(blocked_task), text))
                _log.info(
                    "Slack Q&A relay: forwarded to blocked task=%s goal=%s (%d chars)",
                    blocked_task["id"], goal_id, len(text),
                )
                return
            except Exception:
                _log.exception("Slack Q&A relay failed, falling back to SHCA (goal=%s)", goal_id)

        msg_id = str(_uuid.uuid4())
        await real_pool.execute(
            """INSERT INTO messages (id, thread_id, sender, role, content, token_count, platform)
               VALUES ($1, $2, $3, 'human', $4, 0, 'slack')""",
            msg_id, thread_id, user_uid, text,
        )
        session = await ensure_shca_session(goal_id, agent_id, thread_id)
        # Live delivery to open UI tabs is handled by the chat_module fanout
        # listener: send_to_gateway(persisted_message_id=msg_id) calls
        # mark_existing_queued → NOTIFY chat_msg → fanout → broadcast_to_thread.
        # A direct _broadcast here was removed because it created a second
        # human_message frame (one direct + one via fanout = duplicate bubble).
        if session:
            await session.send_to_gateway(
                json.dumps({"type": "message", "content": text, "thread_id": thread_id}),
                persisted_message_id=msg_id,
            )
    except Exception as exc:
        _log.warning("Slack inbound: SHCA routing failed goal=%s thread=%s: %s", goal_id, thread_id[:12], exc, exc_info=True)


# ── Work-on-story flow ─────────────────────────────────────────────────────────

async def _handle_work_on_story(
    team_id: str,
    goal_id: str,
    binding: dict,
    slack_user: str,
    user_uid: str,
    text: str,
    pool,
    response_url: str = "",
) -> None:
    """@mention in goal channel — validate story then find or create a user-goal channel."""
    _log.info("Slack inbound: work-on-story flow goal=%s user=%s", goal_id, user_uid)

    from app.channels.slack.config import get_slack_config_for_goal
    from app.channels.slack.thread_map import get_slack_user_id_for_actor

    cfg = await get_slack_config_for_goal(goal_id, pool)
    if not cfg or not cfg.get("bot_token"):
        return

    # goal_channel_id is stored in gpi.config; fall back to binding if somehow absent
    goal_channel_id = cfg.get("goal_channel_id") or binding.get("external_channel_id", "")
    bot_token = cfg["bot_token"]
    client = make_async_client(bot_token)

    # ── 0. Kill switch — do not START new agent work on a frozen goal/system. ──
    # Gate up front so no story is claimed/transitioned and no assistant session
    # is provisioned. The shared provision_assistant_thread_for_story also raises
    # ExecutionPausedError as a backstop, but gating here gives the user a clear
    # ephemeral notice instead of a silent no-op. dispatch_block fails closed.
    from app.services import execution_control  # noqa: PLC0415
    _blk = await execution_control.dispatch_block(goal_id)
    if _blk is not None:
        _log.info(
            "Slack work-on-story refused — execution paused (scope=%s) goal=%s uid=%s",
            _blk.scope, goal_id, user_uid,
        )
        try:
            await client.chat_postEphemeral(
                channel=goal_channel_id, user=slack_user,
                text=(f"⏸ Execution is paused ({_blk.scope}). "
                      "Resume in the control panel before starting work on a story."),
            )
        except Exception:
            pass
        return

    # ── 1. Story search ──────────────────────────────────────────────────────
    clean = _MENTION_RE.sub("", text).strip()
    clean = re.sub(r"^work on\s+", "", clean, flags=re.IGNORECASE).strip()

    stories = await pool.fetch("""
        SELECT id, title, state, assigned_to, description, priority, depends_on
          FROM stories
         WHERE goal_id = $1 AND state NOT IN ('done', 'cancelled', 'rejected')
           -- Merge stories are landed by the Git Engine, not /work-able by a human.
           AND COALESCE(story_kind, 'code') <> 'merge'
         ORDER BY created_at DESC
    """, goal_id)

    story = _match_story(stories, clean)
    if not story:
        await _ephemeral_story_list(client, goal_channel_id, slack_user, stories, user_uid)
        return

    # ── 2. Assignment check ──────────────────────────────────────────────────
    if story["assigned_to"] and story["assigned_to"] != user_uid:
        await client.chat_postEphemeral(
            channel=goal_channel_id, user=slack_user,
            text=(
                f"*{story['title']}* is assigned to someone else. "
                f"Ask the Goal Orchestrator to reassign if you want to take it."
            ),
        )
        return

    # ── 3. Claim unassigned ──────────────────────────────────────────────────
    if not story["assigned_to"]:
        await pool.execute(
            "UPDATE stories SET assigned_to = $1, updated_at = now() WHERE id = $2",
            user_uid, story["id"],
        )

    # ── 4. Dependency check ──────────────────────────────────────────────────
    blockers = await _unmet_dependencies(story, pool)
    if blockers:
        blocker_list = "\n".join(f"• *{b['title']}* ({b['state']})" for b in blockers)
        await client.chat_postEphemeral(
            channel=goal_channel_id, user=slack_user,
            text=f"*{story['title']}* is blocked by:\n{blocker_list}",
        )
        return

    # ── 5. Resource readiness check ──────────────────────────────────────────
    res_count = await pool.fetchval("""
        SELECT COUNT(*)
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1 AND gr.status = 'active'
    """, story["id"])
    if not res_count:
        await client.chat_postEphemeral(
            channel=goal_channel_id, user=slack_user,
            text=(
                f"*{story['title']}* has no resources allocated yet — "
                f"the assistant can't plan against an empty RESOURCES.md.\n\n"
                f"Ask the Goal Orchestrator to grant the repo and branch via "
                f"`grant_resource`, then try again."
            ),
        )
        _log.info(
            "Slack work-on-story blocked: story=%s zero active resources (uid=%s goal=%s)",
            story["id"], user_uid, goal_id,
        )
        return

    user_row = await pool.fetchrow("SELECT actor_id, display_name FROM users WHERE uid = $1", user_uid)
    if not user_row or not user_row["actor_id"]:
        return

    actor_id = user_row["actor_id"]
    display_name = user_row["display_name"] or user_uid
    slack_user_id = await get_slack_user_id_for_actor(actor_id, pool)

    # Provision (or rotate) the assistant thread for this story — same path as
    # web UI "Open in assistant". Sets active_thread_id, transitions story
    # to in_progress, and returns the persistent (thread_id, agent_id).
    try:
        from app.routers.assistant import provision_assistant_thread_for_story
        thread_id, agent_id, assistant_busy = await provision_assistant_thread_for_story(
            user_uid, goal_id, story["id"],
        )
    except Exception as exc:
        _log.warning("Slack work-on-story: provision_assistant_thread_for_story failed: %s", exc)
        return

    if not thread_id:
        return

    if assistant_busy:
        _log.info(
            "Slack work-on-story: story=%s assigned but NOT started — assistant thread=%s "
            "is already working on another story", story["id"], thread_id[:12] if thread_id else "-",
        )
        if response_url:
            try:
                async with __import__("httpx").AsyncClient(timeout=5) as _c:
                    await _c.post(response_url, json={
                        "response_type": "ephemeral",
                        "replace_original": True,
                        "text": (
                            f"*{story['title']}* is assigned to you, but your assistant is "
                            f"still working on another story — it'll start automatically "
                            f"once that's done."
                        ),
                    })
            except Exception as _exc:
                _log.warning("work-on-story: could not post busy follow-up: %s", _exc)
        return

    # ── 6. Fire session provisioning in parallel with channel creation ───────
    from app.services.agent_driver import ensure_shca_session
    session_task = asyncio.create_task(ensure_shca_session(goal_id, agent_id, thread_id))

    # ── 7. The STORY's channel, which is the one the work happens in.
    #
    # This used to call `ensure_user_goal_channel`, which kept a private
    # channel per (member × goal) and is gone. That tier predated story
    # channels and had stopped carrying anything: the conversation, the
    # cards and the agent's replies all land in the story's own channel.
    #
    # It was also the source of `<goal>-<name>-<env>-2`. Its lookup
    # matched only an UNARCHIVED binding for the actor, so once a
    # binding was archived it created a FRESH Slack channel -- and the
    # old one still existed, so Slack appended a numeric suffix. The
    # unarchive path it carried sat on the found-a-binding branch and
    # was therefore unreachable from the one state that needed it.
    #
    # `ensure_channel_for_thread` does not have that failure: a unique
    # index on (integration_id, thread_id) means a second caller adopts
    # the first's channel instead of making another, and it records
    # `channel_name` and invites participants, neither of which the old
    # path did.
    from app.channels.slack.thread_channels import ensure_channel_for_thread
    try:
        channel_id = await ensure_channel_for_thread(
            pool,
            goal_id=goal_id,
            thread_id=thread_id,
            title=story.get("title") or "",
            actor_id=actor_id,
            agent_id=agent_id,
            story_id=story["id"],
        )
    except Exception:
        session_task.cancel()
        raise

    # None means Slack is OFF for this goal, which is ordinary and not a
    # failure -- so the kickoff below still runs. The old code aborted
    # here, which turned "no Slack" into "the story never starts".
    if not channel_id:
        _log.info("Slack work-on-story: no channel for thread %s (Slack off "
                  "for goal %s); continuing with kickoff", thread_id[:12], goal_id)

    # Point the user at the story's channel.
    if response_url and channel_id:
        try:
            async with __import__("httpx").AsyncClient(timeout=5) as _c:
                await _c.post(response_url, json={
                    "response_type": "ephemeral",
                    "replace_original": True,
                    "text": (
                        f"✅ Ready — head to <#{channel_id}> to work on "
                        f"*{story['title']}* with your assistant."
                    ),
                })
        except Exception as _exc:
            _log.warning("work-on-story: could not post follow-up: %s", _exc)

    try:
        await session_task
    except Exception as exc:
        _log.warning(
            "ensure_shca_session (parallel) failed goal=%s thread=%s: %s",
            goal_id, thread_id[:12] if thread_id else "-", exc,
        )

    # Refresh RESOURCES.md for this story before routing the kickoff message.
    # Two cases this guards:
    #   - Fresh session: ensure_shca_session wrote RESOURCES.md during workspace
    #     provisioning; refresh is effectively idempotent but confirms it's current.
    #   - Story rotation (existing session, new story): the in-memory session has
    #     stale RESOURCES.md from the previous story. Without this call the SHCA
    #     reads the old brief, hits the stale-resources safety rail, and stops.
    try:
        from app.resource_provisioning import refresh_story_resources
        refreshed = await refresh_story_resources(goal_id, story["id"])
        if not refreshed:
            _log.info(
                "refresh_story_resources returned False for goal=%s story=%s — "
                "session may not be in memory yet; kickoff will trigger provisioning",
                goal_id, story["id"],
            )
    except Exception as exc:
        _log.warning(
            "refresh_story_resources failed pre-kickoff (goal=%s story=%s): %s — proceeding",
            goal_id, story["id"], exc,
        )

    # Dispatch the story kickoff message (same as web UI "Open in assistant").
    # This posts the GO→assistant message that sets operator mode and instructs the
    # assistant to read IDENTITY.md/RESOURCES.md and begin planning.
    try:
        from app.routers.assistant import dispatch_story_kickoff_to_assistant
        ok = await dispatch_story_kickoff_to_assistant(goal_id, story["id"], thread_id, agent_id, user_uid)
        if not ok:
            # SAY SO. A False here is usually not a failure: the story is
            # already `in_progress`, but the git engine has not finished
            # cutting its workstream branch, so the workspace is not there
            # yet and the kickoff was deferred. The branch-ready listener
            # resumes it on its own.
            #
            # The person, though, had just been told their channel was
            # ready. Saying nothing leaves them typing into a channel whose
            # assistant has not been started, concluding it is broken, and
            # starting the story again -- which is how a quiet deferral
            # turns into a support question.
            _log.warning(
                "dispatch_story_kickoff_to_assistant returned False: goal=%s story=%s",
                goal_id, story["id"],
            )
            await _say_waiting_for_workspace(
                goal_id, story["id"], thread_id, pool, response_url)
    except Exception as exc:
        _log.warning(
            "Slack work-on-story: dispatch_story_kickoff_to_shca failed: %s", exc,
        )



async def _say_waiting_for_workspace(goal_id: str, story_id: str,
                                     thread_id: str, pool,
                                     response_url: str = "") -> None:
    """Tell the person their story is queued behind the git engine.

    Distinguishes the two reasons a kickoff defers, because the answers
    differ: a workspace that is not ready yet resumes by itself, while
    an assistant already busy on another story needs that one finished.
    """
    row = await pool.fetchrow(
        "SELECT workspace_state, state FROM stories WHERE id = $1", story_id)
    waiting = (row or {}).get("workspace_state") not in ("ready",)
    text = (
        "Your workspace is still being prepared — the branch for this story "
        "has not been cut yet. It starts on its own as soon as it is; you do "
        "not need to run anything again."
        if waiting else
        "Your assistant is finishing another story first. This one starts "
        "when that one is done."
    )
    try:
        from app.channels.base import relay_to_thread_channel
        await relay_to_thread_channel(goal_id, thread_id, text)
    except Exception as exc:                                   # noqa: BLE001
        _log.warning("could not tell %s why the kickoff deferred: %s",
                     story_id, exc)


def _match_story(stories, text: str) -> Optional[dict]:
    """Match a story by exact short-id prefix first, then case-insensitive title substring."""
    if not text:
        return None
    t = text.lower()
    for s in stories:
        if str(s["id"])[:8] in text:
            return dict(s)
    for s in stories:
        title = s["title"].lower()
        if title in t or t in title:
            return dict(s)
    return None


async def _ephemeral_story_list(client, channel_id: str, slack_user: str, stories, user_uid: str) -> None:
    """Post an ephemeral showing available stories when the user's request didn't match."""
    mine = [s for s in stories if s["assigned_to"] == user_uid]
    rest = [s for s in stories if s["assigned_to"] != user_uid]
    lines = ["I couldn't identify which story you want to work on."]
    if mine:
        lines.append("\n*Assigned to you:*")
        lines.extend(f"• *{s['title']}* (`{str(s['id'])[:8]}`) — {s['state']}" for s in mine[:10])
    if rest:
        lines.append("\n*Other active stories in this goal:*")
        lines.extend(f"• *{s['title']}* (`{str(s['id'])[:8]}`) — {s['state']}" for s in rest[:10])
    if not stories:
        lines.append("No active stories in this goal.")
    try:
        await client.chat_postEphemeral(channel=channel_id, user=slack_user, text="\n".join(lines))
    except Exception as exc:
        _log.warning("Failed to post story list ephemeral: %s", exc)


async def _unmet_dependencies(story: dict, pool) -> list[dict]:
    """Return dependency stories that are not yet in a terminal state."""
    deps = story.get("depends_on") or []
    if not deps:
        return []
    rows = await pool.fetch("""
        SELECT id, title, state FROM stories
         WHERE id = ANY($1) AND state NOT IN ('done', 'cancelled', 'rejected')
    """, list(deps))
    return [dict(r) for r in rows]


# ── Mention resolution ─────────────────────────────────────────────────────────

async def _resolve_mentions(text: str, team_id: str, pool) -> str:
    """Replace <@SLACK_USER_ID> with @display_name (uid: ...) for agent readability."""
    if not text or "<@" not in text:
        return text

    mentions = set(_MENTION_RE.findall(text))
    if not mentions:
        return text

    from app.channels.slack.thread_map import get_user_uid

    replacements: dict[str, str] = {}
    for slack_uid in mentions:
        uid = await get_user_uid(slack_uid, pool)
        display = ""
        if uid:
            display = await pool.fetchval("SELECT display_name FROM users WHERE uid = $1", uid) or ""
        if display:
            replacements[slack_uid] = f"@{display} (uid: {uid})" if uid else f"@{display}"

    if not replacements:
        return text

    def _sub(m):
        return replacements.get(m.group(1), m.group(0))

    return _MENTION_RE.sub(_sub, text)


# ── File attachment helpers ─────────────────────────────────────────────────────

async def _extract_attachments(files: list, goal_id: str, pool) -> "list":
    """Build ChannelAttachment list from Slack file objects.

    Fetches the bot token once and attaches it as a download header so
    ingest_attachments can download private Slack URLs.
    """
    from app.channels.base import ChannelAttachment
    from app.channels.slack.config import get_slack_config_for_goal

    cfg = await get_slack_config_for_goal(goal_id, pool)
    if not cfg or not cfg.get("bot_token"):
        _log.warning("Slack _extract_attachments: no bot_token for goal=%s", goal_id)
        return []

    bot_token = cfg["bot_token"]
    attachments = []
    for f in files:
        url = f.get("url_private_download") or f.get("url_private") or ""
        if not url:
            continue
        attachments.append(ChannelAttachment(
            name=f.get("name") or f.get("title") or "upload",
            url=url,
            mimetype=f.get("mimetype", "application/octet-stream"),
            size_bytes=f.get("size", 0),
            download_headers={"Authorization": f"Bearer {bot_token}"},
        ))
    return attachments


async def _handle_file_uploads(
    goal_id: str,
    channel: str,
    slack_user: str,
    user_uid: str,
    files: list,
    story_id: str,
    pool,
) -> None:
    """Download and register Slack file uploads as goal_resources."""
    from app.channels.base import ingest_attachments

    attachments = await _extract_attachments(files, goal_id, pool)
    if not attachments:
        return

    count = await ingest_attachments(goal_id, story_id or None, attachments, user_uid)
    _log.info(
        "Slack file uploads ingested: goal=%s story=%s count=%d/%d",
        goal_id, story_id or "-", count, len(attachments),
    )


async def _handle_file_shared(
    team_id: str,
    channel_id: str,
    slack_user: str,
    file_id: str,
    pool,
) -> None:
    """Handle a file_shared event — drag-drop uploads that arrive without a message.

    Fetches full file info via files.info, then routes through _handle_file_uploads
    using the channel's binding to determine goal and story context.
    """
    from app.channels.base import get_binding
    from app.channels.slack.config import get_workspace_credentials

    if not file_id:
        return

    binding = await get_binding("slack", channel_id)
    if not binding:
        _log.debug("Slack file_shared: channel %s not mapped — ignoring", channel_id)
        return

    goal_id = binding["goal_id"]
    story_id = str(binding.get("current_story_id", "")) if binding.get("current_story_id") else ""

    user_uid = await _resolve_user(team_id, slack_user, goal_id, pool)
    if not user_uid:
        return

    ws_creds = await _get_workspace_creds(team_id, pool)
    bot_token = (ws_creds or {}).get("bot_token", "")
    if not bot_token:
        _log.warning("Slack file_shared: no bot_token for team=%s", team_id)
        return

    try:
        client = make_async_client(bot_token)
        info = await client.files_info(file=file_id)
        file_obj = info.get("file", {})
    except Exception as exc:
        _log.warning("Slack file_shared: files.info failed file=%s: %s", file_id, exc)
        return

    await _handle_file_uploads(goal_id, channel_id, slack_user, user_uid, [file_obj], story_id, pool)