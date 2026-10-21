"""Shared logic for all platform channel adapters.

Inbound flow each adapter calls:
  1. is_duplicate()   — drop already-processed webhook deliveries
  2. resolve_actor()  — map external user id → actors row (lazy auto-create)
  3. get_binding()    — map (platform, external_channel_id) → thread + agent

Outbound: get_binding_by_thread() finds the external channel to post to.

File attachments: adapters build list[ChannelAttachment] via a thin
platform-specific extractor, then call ingest_attachments() which downloads,
registers via upload_and_register_resource, links to story, and refreshes sessions.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.database import get_pool

_log = logging.getLogger(__name__)


@dataclass
class ChannelAttachment:
    """Platform-agnostic file attachment ready to be downloaded and ingested."""
    name: str
    url: str
    mimetype: str
    size_bytes: int = 0
    # Headers required to authenticate the download (e.g. {"Authorization": "Bearer ..."})
    download_headers: dict = field(default_factory=dict)


async def ingest_attachments(
    goal_id: str,
    story_id: Optional[str],
    attachments: list[ChannelAttachment],
    uploader_uid: str,
) -> int:
    """Download, register, story-link, and refresh sessions for a list of attachments.

    Uses upload_and_register_resource as the single intake point (idempotent).
    refresh_all_goal_sessions is called once after all files land so the SHCA
    story-scoped query sees every new story_resources link in one pass.
    Returns the count of successfully registered files.
    """
    if not attachments:
        return 0

    from app.services.resource_upload import upload_and_register_resource, ResourceUploadError

    import httpx
    pool = get_pool()
    count = 0

    for att in attachments:
        if not att.url:
            continue
        try:
            async with httpx.AsyncClient(headers=att.download_headers, timeout=30) as client:
                resp = await client.get(att.url)
                if resp.status_code != 200:
                    _log.warning(
                        "Attachment download failed name=%s: HTTP %d", att.name, resp.status_code
                    )
                    continue
                file_bytes = resp.content

            result = await upload_and_register_resource(
                goal_id=goal_id,
                filename=att.name,
                file_bytes=file_bytes,
                uploader_uid=uploader_uid,
            )

            if story_id:
                try:
                    await pool.execute(
                        """
                        INSERT INTO story_resources (story_id, resource_id, relation)
                        VALUES ($1, $2, 'needs')
                        ON CONFLICT DO NOTHING
                        """,
                        story_id, result.resource_id,
                    )
                except Exception as exc:
                    _log.warning(
                        "Failed to link resource %s to story %s: %s",
                        result.resource_id, story_id, exc,
                    )

            count += 1
            _log.info(
                "Attachment ingested: goal=%s name=%s rid=%s is_new=%s",
                goal_id, att.name, result.resource_id, result.is_new,
            )
        except ResourceUploadError as exc:
            _log.warning("Attachment rejected goal=%s name=%s: %s", goal_id, att.name, exc)
        except Exception as exc:
            _log.warning("Attachment ingestion failed goal=%s name=%s: %s", goal_id, att.name, exc)

    if count:
        try:
            from app.resource_provisioning import refresh_all_goal_sessions
            asyncio.create_task(refresh_all_goal_sessions(goal_id))
        except Exception as exc:
            _log.warning(
                "refresh_all_goal_sessions failed after attachment ingest goal=%s: %s", goal_id, exc
            )

    return count


async def is_duplicate(platform: str, external_event_id: str) -> bool:
    """Atomic dedup check — returns True if event was already processed."""
    result = await get_pool().fetchval(
        """
        INSERT INTO platform_processed_events (platform, external_event_id)
        VALUES ($1, $2)
        ON CONFLICT (platform, external_event_id) DO NOTHING
        RETURNING external_event_id
        """,
        platform, external_event_id,
    )
    if result is None:
        _log.debug("Duplicate event dropped: platform=%s id=%s", platform, external_event_id)
        return True
    return False


async def forget_event(platform: str, external_event_id: str) -> None:
    """Release a dedup claim so a redelivery can be processed.

    `is_duplicate` claims the event BEFORE the handler runs, which is the
    only way to make the claim atomic -- but it means a handler that
    throws has consumed the event. The router has already answered 200 by
    then, so nothing retries it and the message is gone, with one warning
    to show for it.

    Claim-then-release is the standard shape, and the failure mode it
    trades into is a duplicate delivery rather than a silent loss. A
    duplicate is visible and the handlers are idempotent; a loss is
    neither.
    """
    try:
        await get_pool().execute(
            "DELETE FROM platform_processed_events "
            " WHERE platform = $1 AND external_event_id = $2",
            platform, external_event_id)
        _log.info("Released dedup claim after a failed handle: %s/%s",
                  platform, external_event_id)
    except Exception as exc:                                    # noqa: BLE001
        _log.warning("Could not release dedup claim %s/%s: %s",
                     platform, external_event_id, exc)


async def resolve_actor(
    platform: str,
    external_user_id: str,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
    email_domain_allowlist: Optional[list[str]] = None,
) -> Optional[uuid.UUID]:
    """Resolve an external platform user to an internal actor UUID.

    Resolution order:
      1. Cache hit in platform_user_map → return immediately
      2. Email match in users table     → confirmed mapping
      3. [Slack only] Legacy slack_user_map fallback → backfill into platform_user_map
      4. Domain allowlist check         → reject if configured and domain absent/unmatched
      5. Lazy auto-create               → pending_review (caller should drop message)

    Step 3 covers the migration window between the backfill migration and the
    Slack App URL flip.  Remove it (and _legacy_fallback) after cutover is verified.
    """
    pool = get_pool()

    row = await pool.fetchrow(
        "SELECT actor_id, mapping_status FROM platform_user_map WHERE platform=$1 AND external_user_id=$2",
        platform, external_user_id,
    )
    if row:
        if row["mapping_status"] == "confirmed":
            return row["actor_id"]
        _log.debug("Actor pending_review: platform=%s external=%s", platform, external_user_id)
        return None

    # DEPRECATED FALLBACK — remove after Slack cutover verified (see documentation/SLACK_CUTOVER.md)
    if platform == "slack":
        from app.channels.slack import _legacy_fallback
        actor_id = await _legacy_fallback.resolve_actor(external_user_id, pool)
        if actor_id is not None:
            await _legacy_fallback.backfill_actor(
                external_user_id, actor_id, display_name, email, pool,
            )
            return actor_id

    if email:
        actor_id = await pool.fetchval("SELECT actor_id FROM users WHERE email=$1", email)
        if actor_id:
            await pool.execute(
                """
                INSERT INTO platform_user_map
                    (platform, external_user_id, actor_id, display_name, email, mapping_status)
                VALUES ($1, $2, $3, $4, $5, 'confirmed')
                ON CONFLICT (platform, external_user_id) DO NOTHING
                """,
                platform, external_user_id, actor_id, display_name, email,
            )
            _log.info("Actor resolved via email: platform=%s external=%s actor=%s", platform, external_user_id, actor_id)
            return actor_id

    # Domain allowlist: only enforced when we actually have an email. If email
    # is absent (e.g. Notion webhooks before the Users API enrichment runs),
    # fall through to pending_review rather than silently dropping the message.
    if email and email_domain_allowlist:
        domain = email.split("@")[-1].lower()
        if domain not in email_domain_allowlist:
            _log.info("Actor dropped (domain not allowed): platform=%s email=%s", platform, email)
            return None

    new_actor_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO actors (id, type, display_name) VALUES ($1, 'user', $2) ON CONFLICT (id) DO NOTHING",
                new_actor_id, display_name or external_user_id,
            )
            await conn.execute(
                """
                INSERT INTO platform_user_map
                    (platform, external_user_id, actor_id, display_name, email, mapping_status)
                VALUES ($1, $2, $3, $4, $5, 'pending_review')
                ON CONFLICT (platform, external_user_id) DO NOTHING
                """,
                platform, external_user_id, new_actor_id, display_name, email,
            )
    _log.info("Actor auto-created (pending_review): platform=%s external=%s actor=%s", platform, external_user_id, new_actor_id)
    return None


async def get_binding(platform: str, external_channel_id: str) -> Optional[dict]:
    """Look up an active channel binding by platform + external channel id.

    Returns keys: goal_id, integration_id, actor_id, thread_id, agent_id,
    current_story_id, resource_kind, email_domain_allowlist.

    For Slack, falls back to the legacy slack_user_goal_channels table during the
    migration window and immediately backfills the result into platform_channel_bindings
    so all subsequent reads and writes go through the new system.
    Remove the fallback block after Slack cutover is verified (see documentation/SLACK_CUTOVER.md).
    """
    row = await get_pool().fetchrow(
        """
        SELECT pcb.goal_id, pcb.integration_id, pcb.actor_id,
               pcb.thread_id, pcb.agent_id, pcb.current_story_id, pcb.resource_kind,
               gpi.email_domain_allowlist
        FROM platform_channel_bindings  pcb
        JOIN platform_integrations      pi  ON pi.id            = pcb.integration_id
        JOIN goal_platform_integrations gpi ON gpi.goal_id      = pcb.goal_id
                                           AND gpi.integration_id = pcb.integration_id
        WHERE pi.platform             = $1
          AND pcb.external_channel_id = $2
          AND pcb.archived_at         IS NULL
          AND pi.status               = 'active'
          AND gpi.enabled             = true
        """,
        platform, external_channel_id,
    )
    if row is not None:
        return dict(row)

    # DEPRECATED FALLBACK — remove after Slack cutover verified (see documentation/SLACK_CUTOVER.md)
    if platform == "slack":
        from app.channels.slack import _legacy_fallback
        binding = await _legacy_fallback.get_binding(external_channel_id, get_pool())
        if binding is not None:
            # Backfill runs in the background — inbound routing proceeds immediately.
            import asyncio
            asyncio.create_task(
                _legacy_fallback.backfill_binding(binding, external_channel_id, get_pool())
            )
            return binding

    return None


class ChannelProvisioner:
    """Optional per-platform hook for goal-level and member-level setup.

    Register a subclass in registry.py to have it called automatically when a
    goal enables the platform, when a new member joins, and when a member is
    removed.  All methods are no-ops by default so subclasses only override
    what they need — e.g. NotionProvisioner has no deprovision_member override
    because its bot-token integration has no per-user access to revoke.

    Adding a new platform (Telegram, Google Chat, Teams, …):
      1. Create app/channels/<platform>/provisioner.py with a subclass.
      2. Override provision_goal / provision_member / deprovision_member as needed.
      3. Override parse_channel_id if the platform uses non-trivial ID formats.
      4. Override is_auto_provisioned if the platform auto-creates channels.
      5. Register in registry.py — done.  No changes to goals.py or settings.html.
    """

    async def provision_goal(self, goal_id: str, integration_id: str, pool) -> str | None:
        """Called when the platform is enabled for a goal.

        Returns an error string on failure, None on success (or no-op).
        """
        return None

    async def provision_member(self, goal_id: str, uid: str, pool) -> None:
        """Called when a member joins a goal that has this platform enabled."""

    async def deprovision_member(self, goal_id: str, uid: str, pool) -> None:
        """Called when a member is removed from a goal that has this platform enabled."""

    def parse_channel_id(self, raw: str) -> str:
        """Parse a channel/chat/page ID from user input (URL or bare ID).

        Returns the canonical ID string, or '' if the input is invalid.
        Default: return the stripped input as-is (works for most numeric IDs).
        """
        return raw.strip()

    def is_auto_provisioned(self, config: dict) -> bool:
        """Return True if this integration is configured for automatic channel
        creation so the UI can hide the manual bind form.  Default: False."""
        return False


async def route_to_session(
    binding: dict,
    sender: str,
    text: str,
    platform: str,
    sender_name: Optional[str] = None,
) -> None:
    """Insert an inbound message and dispatch it to the bound agent session.

    Determines whether the binding targets a GO thread or an SHCA thread and
    routes accordingly.  Callers must pass the originating platform slug so the
    message row is labelled correctly.

    sender_name: human-readable display name for broadcast; falls back to sender.
    """
    goal_id = binding["goal_id"]
    _tid = binding.get("thread_id")
    _aid = binding.get("agent_id")
    thread_id = str(_tid) if _tid else ""
    agent_id = str(_aid) if _aid else ""

    if not thread_id:
        _log.warning("Channel route: binding has no thread_id goal=%s platform=%s", goal_id, platform)
        return

    # Kill switch — conversational freeze gate for Notion + any generic platform
    # routed through here (Slack uses its own inbound path). If this goal or the
    # system is frozen, don't persist or dispatch; post a visible "paused" notice
    # back on the originating platform via the thread relay. The send_to_gateway
    # backstop would also drop the turn, but only the browser sees that — this
    # gives the platform user feedback on their own channel.
    from app.services import execution_control  # noqa: PLC0415
    _block = await execution_control.conversation_block(goal_id)
    if _block is not None:
        _log.info(
            "Channel route: dropped inbound (frozen scope=%s) goal=%s thread=%s platform=%s",
            _block.scope, goal_id, thread_id[:12], platform,
        )
        try:
            await relay_to_thread_channel(
                goal_id, thread_id,
                "⏸ Execution is paused (" + _block.scope + "). Resume in the "
                "control panel to continue."
                + (f" Reason: {_block.reason}" if _block.reason else ""),
                username="System",
            )
        except Exception as exc:
            _log.debug("Channel route: paused-notice relay failed goal=%s: %s", goal_id, exc)
        return

    pool = get_pool()

    # Read-only gate — the thread's owner left the goal (see threads_left_goal_at
    # migration / remove_goal_member). Slack doesn't need this check here: its
    # personal channel is archived on removal, which is itself read-only at the
    # platform level — there's no compose box to send from. This matters for
    # Notion (and any future platform routed through this generic path), which
    # has no equivalent platform-level lock.
    left_goal_at = await pool.fetchval(
        "SELECT left_goal_at FROM threads WHERE id = $1", thread_id,
    )
    if left_goal_at is not None:
        _log.info(
            "Channel route: dropped inbound (thread read-only) goal=%s thread=%s platform=%s",
            goal_id, thread_id[:12], platform,
        )
        try:
            await relay_to_thread_channel(
                goal_id, thread_id,
                "This member has left the goal — this conversation is read-only.",
                username="System",
            )
        except Exception as exc:
            _log.debug("Channel route: read-only notice relay failed goal=%s: %s", goal_id, exc)
        return

    msg_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO messages
               (id, thread_id, sender, role, content, token_count, platform)
           VALUES ($1, $2, $3, 'human', $4, 0, $5)""",
        msg_id, thread_id, sender, text, platform,
    )

    goal_row = await pool.fetchrow("SELECT go_thread_id FROM goals WHERE id = $1", goal_id)
    is_go_thread = goal_row and goal_row.get("go_thread_id") == thread_id

    try:
        if is_go_thread:
            from app.services.agent_driver import ensure_go_session
            session = await ensure_go_session(goal_id)
        else:
            # Resolve agent_id from the thread if the binding didn't carry it.
            if not agent_id:
                agent_id = await pool.fetchval(
                    "SELECT agent_id FROM threads WHERE id = $1", thread_id,
                ) or ""
                if agent_id:
                    _log.info(
                        "Channel route: agent_id resolved from thread goal=%s thread=%s agent=%s",
                        goal_id, thread_id[:12], agent_id,
                    )
                else:
                    _log.warning(
                        "Channel route: no agent_id for thread=%s goal=%s platform=%s — dropping",
                        thread_id[:12], goal_id, platform,
                    )
                    return
            from app.services.agent_driver import ensure_shca_session
            session = await ensure_shca_session(goal_id, agent_id, thread_id)

        if not session:
            _log.warning(
                "Channel route: could not start session goal=%s thread=%s platform=%s",
                goal_id, thread_id[:12], platform,
            )
            return

        # Push the inbound message to any open browser subscribers so they
        # see it in real-time without a page refresh.
        _display = sender_name or sender
        if is_go_thread:
            broadcast_payload = json.dumps({
                "type": "message",
                "role": "human",
                "sender": sender,
                "sender_name": _display,
                "content": text,
                "origin": platform,
            })
            await session._broadcast(thread_id, broadcast_payload)
            try:
                from app.routers.goal_thread import _broadcast_to_goal_thread
                await _broadcast_to_goal_thread(goal_id, broadcast_payload,
                                                thread_id)
            except Exception as exc:
                _log.debug("Goal thread UI broadcast failed goal=%s: %s", goal_id, exc)
        else:
            await session._broadcast(thread_id, json.dumps({
                "type": "human_message",
                "content": text,
                "sender": sender,
                "sender_name": _display,
                "origin": platform,
            }))

        await session.send_to_gateway(json.dumps({
            "type": "message",
            "content": text,
            "thread_id": thread_id,
        }))
        _log.info(
            "Channel routed: platform=%s goal=%s thread=%s agent=%s len=%d",
            platform, goal_id, thread_id[:12], agent_id, len(text),
        )
    except Exception as exc:
        _log.warning(
            "Channel route error: platform=%s goal=%s thread=%s: %s",
            platform, goal_id, thread_id[:12], exc, exc_info=True,
        )


async def relay_all_outbound(
    goal_id: str,
    thread_id: str,
    content: str,
    msg_id=None,
) -> None:
    """Relay an agent message to every registered outbound channel.

    Calls each relay function independently — one platform failing does not
    block the others.  Wire this into chat.py / goal_thread.py on_message
    once the Notion integration is confirmed stable.
    """
    from app.channels.registry import get_outbound_relays
    seen: set[str] = set()
    for platform, relay_fn in get_outbound_relays():
        fn_key = f"{relay_fn.__module__}.{relay_fn.__qualname__}"
        if fn_key in seen:
            _log.debug("Outbound relay skipped duplicate fn: platform=%s fn=%s", platform, fn_key)
            continue
        seen.add(fn_key)
        try:
            await relay_fn(goal_id, thread_id, content, msg_id)
        except Exception as exc:
            _log.warning(
                "Outbound relay failed: platform=%s goal=%s thread=%s: %s",
                platform, goal_id, thread_id[:12] if thread_id else "", exc,
            )


async def relay_to_goal_channel(
    goal_id: str,
    content: str,
    username: str = "System",
) -> None:
    """Relay a message to every registered goal-channel relay.

    Use this when a system or lifecycle message should appear in the goal's
    top-level channel on every connected platform (e.g. Notion goal page).
    Each platform's relay function receives (goal_id, content, username).
    Platform failures are isolated — one failing does not block the others.
    """
    from app.channels.registry import get_goal_channel_relays
    for platform, relay_fn in get_goal_channel_relays():
        try:
            await relay_fn(goal_id, content, username)
        except Exception as exc:
            _log.warning(
                "Goal channel relay failed: platform=%s goal=%s: %s",
                platform, goal_id, exc,
            )


async def relay_to_thread_channel(
    goal_id: str,
    thread_id: str,
    content: str,
    username: str = "System",
) -> None:
    """Relay a message to every registered thread-channel relay.

    Use this when a system message should appear in the channel mapped to a
    specific thread (e.g. SHCA Notion subpage, Slack story channel).
    Each platform's relay function receives (goal_id, thread_id, content, username).
    """
    from app.channels.registry import get_thread_channel_relays
    for platform, relay_fn in get_thread_channel_relays():
        try:
            await relay_fn(goal_id, thread_id, content, username)
        except Exception as exc:
            _log.warning(
                "Thread channel relay failed: platform=%s goal=%s thread=%s: %s",
                platform, goal_id, thread_id[:12] if thread_id else "", exc,
            )


async def relay_lifecycle_event(
    goal_id: str,
    event_type: str,
    payload: dict,
    thread_id: Optional[str] = None,
) -> None:
    """Dispatch a lifecycle event to every registered lifecycle relay.

    Standard event types:
      story.state_changed  → payload: {story_id, story_title, old_state, new_state}
      story.created        → payload: {story_id, story_title, assigned_to}
      goal.completed       → payload: {goal_name}
      goal.deleted         → payload: {goal_name}
      orchestrator.message → payload: {content, username}

    Each platform's relay_lifecycle_event receives (goal_id, event_type, payload, thread_id).
    """
    from app.channels.registry import get_lifecycle_relays
    for platform, relay_fn in get_lifecycle_relays():
        try:
            await relay_fn(goal_id, event_type, payload, thread_id)
        except Exception as exc:
            _log.warning(
                "Lifecycle relay failed: platform=%s goal=%s event=%s: %s",
                platform, goal_id, event_type, exc,
            )


async def get_binding_by_thread(platform: str, thread_id: str, goal_id: Optional[str] = None) -> Optional[dict]:
    """Look up an active channel binding by internal thread_id.

    Used during outbound relay to find the external channel to post to.
    Returns keys: goal_id, integration_id, actor_id, external_channel_id, resource_kind, agent_id.
    Only returns a binding if the goal-level integration is currently enabled.

    Pass goal_id when the same thread serves multiple goals (e.g. a shared SHCA) so the
    correct goal's Notion page is selected rather than an arbitrary one.
    """
    if goal_id:
        row = await get_pool().fetchrow(
            """
            SELECT pcb.id, pcb.goal_id, pcb.integration_id, pcb.actor_id,
                   pcb.external_channel_id, pcb.resource_kind, pcb.agent_id
            FROM platform_channel_bindings  pcb
            JOIN platform_integrations      pi  ON pi.id             = pcb.integration_id
            JOIN goal_platform_integrations gpi ON gpi.integration_id = pcb.integration_id
                                              AND gpi.goal_id        = pcb.goal_id
            WHERE pi.platform   = $1
              AND pcb.thread_id = $2
              AND pcb.goal_id   = $3
              AND pcb.archived_at IS NULL
              AND pi.status     = 'active'
              AND gpi.enabled   = true
            """,
            platform, thread_id, goal_id,
        )
    else:
        row = await get_pool().fetchrow(
            """
            SELECT pcb.id, pcb.goal_id, pcb.integration_id, pcb.actor_id,
                   pcb.external_channel_id, pcb.resource_kind, pcb.agent_id
            FROM platform_channel_bindings  pcb
            JOIN platform_integrations      pi  ON pi.id             = pcb.integration_id
            JOIN goal_platform_integrations gpi ON gpi.integration_id = pcb.integration_id
                                              AND gpi.goal_id        = pcb.goal_id
            WHERE pi.platform   = $1
              AND pcb.thread_id = $2
              AND pcb.archived_at IS NULL
              AND pi.status     = 'active'
              AND gpi.enabled   = true
            """,
            platform, thread_id,
        )
    return dict(row) if row else None
