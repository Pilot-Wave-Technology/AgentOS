# Platform Channels — Architecture Reference

This document describes the generic channel infrastructure under `app/channels/`.
Every platform integration (Notion, Slack, Telegram, Teams, …) should be built
on top of these abstractions so that call sites in routers never import
platform-specific code directly.

---

## Directory Layout

```
app/channels/
├── README.md               ← this file
├── __init__.py
├── base.py                 ← shared logic: dedup, actor resolution, routing, outbound dispatch
│                              also: relay_to_goal_channel, relay_to_thread_channel, relay_lifecycle_event
├── registry.py             ← platform registration: adapters, provisioners, relay functions
├── router.py               ← FastAPI router: /channels/{platform}/events, /oauth/*
├── notion/                 ← Notion OAuth integration (see notion/README.md)
│   ├── README.md
│   ├── auth.py             ← HMAC-SHA256 signature verification + goal integration lookup
│   ├── identity.py         ← resolve Notion webhook author → internal actor_id
│   ├── oauth.py            ← OAuth install/callback: code exchange, token storage
│   ├── secrets.py          ← resolve API token from credentials_data or Secret Manager
│   ├── inbound/            ← NotionInboundAdapter + event handlers (see inbound/README.md)
│   ├── lifecycle/          ← lifecycle event dispatcher and handlers (see lifecycle/README.md)
│   ├── outbound/           ← relay functions: agent/human/file/system (see outbound/README.md)
│   ├── pages/              ← Notion page/database creation primitives (see pages/README.md)
│   └── provisioner/        ← NotionProvisioner, NotionBotProvisioner (see provisioner/README.md)
├── notion_bot/             ← Internal-token Notion variant (subclasses notion/)
│   ├── __init__.py
│   └── adapter.py          ← NotionBotAdapter: skips signature verification
└── slack/                  ← Full Slack integration (OAuth, events, commands, outbound, lifecycle)
    ├── inbound/            ← SlackInboundAdapter + event/action/command handlers
    ├── outbound/           ← relay_agent_message, relay_human_message, post_to_*
    ├── lifecycle/          ← relay_lifecycle_event for Slack
    ├── commands/           ← slash command handlers (/work, /goal, /story, /task, …)
    ├── actions/            ← Block Kit button/modal handlers
    └── provisioner.py      ← SlackProvisioner
```

---

## The adapter contract

> **Channels are input/output adapters. They translate platform-native events
> into internal calls and internal state into platform-native messages. They do
> not contain business logic.**
>
> Every state mutation — story transitions, execution pauses, resource
> allocations, membership changes, plan approvals — must go through
> `app/services/`. If the service function you need doesn't exist yet, add it
> there. Never write raw DB queries or business rules inside an adapter, command
> handler, or lifecycle relay.

See [`app/services/README.md`](../services/README.md) for the full catalogue of
canonical service functions and why each one must not be bypassed.

---

## Core Concepts

### Platform Integrations (`platform_integrations` table)

One row per platform per workspace. Stores credentials and global config.

| Column               | Meaning                                                                    |
|----------------------|----------------------------------------------------------------------------|
| `platform`           | slug: `notion`, `notion_bot`, `slack`, `telegram`, …                       |
| `status`             | `pending` / `active` / `revoked`                                           |
| `credentials_kind`   | `oauth` / `api_key`                                                        |
| `credentials_data`   | TEXT — single-value credential (e.g. an OAuth access token)                |
| `credentials_json`   | JSONB — multi-field credential (e.g. `{bot_token, workspace_id, …}`)       |
| `credentials_ref`    | Secret Manager resource name (alternative to either credentials column)     |
| `bot_external_id`    | platform-side bot/app ID (used for echo prevention)                        |
| `config`             | JSONB — platform-specific settings                                         |

### Goal-Level Opt-In (`goal_platform_integrations` table)

Controls which goals use which integration. One row per (goal, integration).

| Column                  | Meaning                                              |
|-------------------------|------------------------------------------------------|
| `goal_id`               | foreign key → goals                                  |
| `integration_id`        | foreign key → platform_integrations                  |
| `enabled`               | true/false — can be toggled per goal                 |
| `email_domain_allowlist`| restrict inbound to these email domains              |
| `config`                | JSONB — per-goal config (orch_subpage_id, etc.)      |

### Channel Bindings (`platform_channel_bindings` table)

Maps external channels (Notion page, Slack channel) to internal threads.

| Column                | Meaning                                                       |
|-----------------------|---------------------------------------------------------------|
| `integration_id`      | which integration row                                         |
| `goal_id`             | which goal                                                    |
| `actor_id`            | NULL = GO thread binding; UUID = Assistant thread binding          |
| `external_channel_id` | platform-side identifier (Notion page ID, Slack channel ID)   |
| `thread_id`           | internal thread this external channel maps to; NULL = pending (page exists but Assistant not yet started) |
| `agent_id`            | agent owning the thread; NULL while pending                   |
| `archived_at`         | soft-delete timestamp                                         |

### Dedup (`platform_processed_events` table)

Prevents double-processing of webhook deliveries via `ON CONFLICT DO NOTHING`.

---

## Inbound Flow

Every incoming platform event follows this sequence, implemented in `base.py`:

```
Webhook POST /channels/{platform}/events
    ↓
router.py: verify_request() → 401 if invalid signature
    ↓
router.py: handle verification challenge (notion/etc)
    ↓
adapter.handle_event(body, headers)
    ↓
1. is_duplicate(platform, delivery_id)     → drop if already seen
2. resolve_actor(platform, external_user_id, email, display_name)
                                           → actor UUID or None (drop if None)
3. get_binding(platform, external_channel_id)
                                           → {goal_id, thread_id, agent_id, …}
4. [platform-specific: fetch full content from API if webhook is signal-only]
5. route_to_session(binding, sender, text, platform, sender_name)
    ↓
    a. INSERT INTO messages (role='human', platform=...)
    b. ensure_go_session / ensure_shca_session
    c. session._broadcast → real-time UI update
    d. _broadcast_to_goal_thread → WebSocket subscribers (GO thread only)
    e. session.send_to_gateway → OpenClaw agent
```

### Actor Resolution (`resolve_actor` in `base.py`)

```
platform_user_map cache hit (confirmed)  → return actor_id immediately
platform_user_map cache hit (pending)    → return None (message dropped)
email match in users table               → create confirmed mapping
email domain allowlist check             → reject if domain not allowed
auto-create                              → insert actor + pending_review row, return None
```

Pending-review actors are created but messages are dropped until an admin confirms the mapping.

---

## Outbound Flow

Agent replies are relayed to all registered outbound channels via `relay_all_outbound`:

```
on_message callback fires (chat.py / goal_thread.py)
    ↓
relay_all_outbound(goal_id, thread_id, content, msg_id)   [base.py]
    ↓
registry.get_outbound_relays()   → [(platform, relay_fn), ...]
    ↓ (deduplicates by fn.__qualname__ to prevent double-posting)
relay_fn(goal_id, thread_id, content, msg_id)   per registered platform
```

### Human Message Relay

When a user types in the platform UI, the message is also relayed to external platforms:

```
WS message type="message" received
    ↓
INSERT INTO messages (role='human')
    ↓
[Slack] relay_human_message(goal_id, thread_id, content, uid)
[Notion] relay_human_message(goal_id, thread_id, content, sender_display_name)
```

### Goal/Thread Channel Relay (system messages, lifecycle events)

```
relay_to_goal_channel(goal_id, content, username)      [base.py]
    ↓ dispatches to all registered _GOAL_CHANNEL_RELAYS
    → Slack: post_to_goal_channel
    → Notion: post_to_goal_page

relay_to_thread_channel(goal_id, thread_id, content, username)   [base.py]
    ↓ dispatches to all registered _THREAD_CHANNEL_RELAYS
    → Slack: post_to_story_channel
    → Notion: post_to_thread_page

relay_lifecycle_event(goal_id, event_type, payload, thread_id)   [base.py]
    ↓ dispatches to all registered _LIFECYCLE_RELAYS
    → Slack: lifecycle.relay_lifecycle_event
    → Notion: lifecycle.relay_lifecycle_event
```

### Card / Task Status Relay

Task status cards (Task Completed, Review Completed, etc.) are relayed via `assistant_relay.py`:

```
post_card_to_assistant_thread(task, card, content_markdown)
    ↓
relay_all_outbound(goal_id, source_thread_id, content_markdown, msg_id)
    → all registered outbound platforms receive the card as text
```

---

## Registry (`registry.py`)

The registry is the single place to register a new platform. No other files need to change.

```python
_ADAPTERS: dict[str, str] = {
    "platform_slug": "app.channels.platform.inbound.AdapterClass",
}
_PROVISIONERS: dict[str, str] = {
    "platform_slug": "app.channels.platform.provisioner.ProvisionerClass",
}
_OUTBOUND_RELAYS: dict[str, str] = {
    "platform_slug": "app.channels.platform.outbound.relay_agent_message",
}
_GOAL_CHANNEL_RELAYS: dict[str, str] = {
    "platform_slug": "app.channels.platform.outbound.post_to_goal_page",
}
_THREAD_CHANNEL_RELAYS: dict[str, str] = {
    "platform_slug": "app.channels.platform.outbound.post_to_thread_page",
}
_LIFECYCLE_RELAYS: dict[str, str] = {
    "platform_slug": "app.channels.platform.lifecycle.relay_lifecycle_event",
}
```

All entries are loaded lazily via `importlib` so missing dependencies (e.g. `notion_client`
not installed) degrade gracefully — the platform is skipped with a warning log, never a crash.

---

## Provisioning (`ChannelProvisioner` in `base.py`)

Called by `goals.py` when a platform is enabled for a goal or a new member joins.

```python
class ChannelProvisioner:
    async def provision_goal(self, goal_id, integration_id, pool) -> str | None:
        """Set up platform resources for a goal. Returns error string or None."""

    async def provision_member(self, goal_id, uid, pool) -> None:
        """Set up platform resources when a member joins a goal."""

    def parse_channel_id(self, raw: str) -> str:
        """Normalize a user-supplied channel/page ID from URL or bare ID."""

    def is_auto_provisioned(self, config: dict) -> bool:
        """True if the platform auto-creates channels (hides manual bind form in UI)."""
```

`provision_goal` is called each time the goal admin enables the integration — it must be
idempotent. Check for existing bindings/resources before creating new ones.

---

## Adding a New Platform

Follow these steps to add, say, `telegram`:

```
1. Create app/channels/telegram/
   ├── __init__.py
   ├── inbound.py      ← TelegramInboundAdapter(verify_request, handle_event)
   ├── outbound.py     ← relay_agent_message, relay_human_message, post_to_goal_page, post_to_thread_page
   ├── lifecycle.py    ← relay_lifecycle_event(goal_id, event_type, payload, thread_id)
   └── provisioner.py  ← TelegramProvisioner(ChannelProvisioner)

2. Register in registry.py:
   _ADAPTERS["telegram"]          = "app.channels.telegram.inbound.TelegramInboundAdapter"
   _PROVISIONERS["telegram"]      = "app.channels.telegram.provisioner.TelegramProvisioner"
   _OUTBOUND_RELAYS["telegram"]   = "app.channels.telegram.outbound.relay_agent_message"
   _GOAL_CHANNEL_RELAYS["telegram"] = "app.channels.telegram.outbound.post_to_goal_page"
   _THREAD_CHANNEL_RELAYS["telegram"] = "app.channels.telegram.outbound.post_to_thread_page"
   _LIFECYCLE_RELAYS["telegram"]  = "app.channels.telegram.lifecycle.relay_lifecycle_event"

3. Done. No other files need to change.
```

### Required function signatures

```python
# inbound adapter
class TelegramInboundAdapter:
    def verify_request(self, body: bytes, headers: dict) -> bool: ...
    async def handle_event(self, body: bytes, headers: dict) -> None: ...
    # Required if the platform has interactive components (buttons, modals):
    async def handle_action(self, payload: dict) -> dict | None: ...
    # Required if the platform has slash/bot commands:
    async def handle_command(self, command, text, user_id, channel_id, team_id, response_url) -> dict: ...
    # Optional OAuth (skip if not needed):
    async def oauth_install(self, request, user) -> Response: ...
    async def oauth_callback(self, request, user) -> Response: ...

# outbound
async def relay_agent_message(goal_id, thread_id, content, msg_id=None) -> None: ...
async def relay_human_message(goal_id, thread_id, content, sender_display_name="") -> None: ...
async def post_to_goal_page(goal_id, content, username="System") -> None: ...
async def post_to_thread_page(goal_id, thread_id, content, username="System") -> None: ...

# lifecycle
async def relay_lifecycle_event(goal_id, event_type, payload, thread_id=None) -> None: ...

# provisioner (only override what's needed)
class TelegramProvisioner(ChannelProvisioner):
    async def provision_goal(self, goal_id, integration_id, pool): ...
    async def provision_member(self, goal_id, uid, pool): ...
```

### Standard lifecycle event types

| `event_type`             | `payload` keys                                                     |
|--------------------------|--------------------------------------------------------------------|
| `story.created`          | `story_id`, `story_title`, `assigned_to`, `state`, `priority`      |
| `story.state_changed`    | `story_id`, `story_title`, `old_state`, `new_state`                |
| `task.created`           | `task_id`, `task_title`, `story_id`, `state`                       |
| `task.state_changed`     | `task_id`, `task_title`, `story_id`, `old_state`, `new_state`      |
| `task.completed`         | `task_id`, `task_title`, `story_id`, `result`                      |
| `member.joined`          | `uid`, `display_name`                                              |
| `resource.registered`    | `resource_id`, `name`, `kind`                                      |
| `goal.completed`         | `goal_name`                                                        |
| `goal.deleted`           | `goal_name`                                                        |
| `orchestrator.message`   | `content`, `username`                                              |

---

## Webhook URL Pattern

All webhooks are routed via the generic router:

```
POST /channels/{platform}/events     → adapter.handle_event()
GET  /channels/{platform}/oauth/install    → adapter.oauth_install()
GET  /channels/{platform}/oauth/callback   → adapter.oauth_callback()
```

Verification challenges (`{"verification_token": "..."}` in body) are handled
automatically by `router.py` before the adapter sees the payload.

---

## Echo Prevention

Without echo prevention, posting an outbound comment triggers an inbound webhook
that routes back as a new human message — an infinite loop.

Two layers:

1. **Author-type check**: Notion webhooks include `authors[0].type`. If `"bot"`, the
   inbound adapter drops it. Works for webhook-based platforms.

2. **External message ID stamp**: `relay_agent_message` stamps `messages.external_message_id`
   with the platform's comment/message ID. On inbound, we check `messages.external_message_id`
   before routing. Works for both bot and human-looking relay scenarios.

3. **Bot ID match**: The integration's `bot_external_id` is compared with the webhook's
   author ID. Dropped if they match.

---

## Database Tables Summary

| Table                        | Purpose                                               |
|------------------------------|-------------------------------------------------------|
| `platform_integrations`      | App-level integration credentials and config          |
| `goal_platform_integrations` | Per-goal opt-in and config                            |
| `platform_channel_bindings`  | External channel ↔ internal thread mapping            |
| `platform_processed_events`  | Dedup of webhook deliveries                           |
| `platform_user_map`          | External user ID ↔ internal actor mapping             |
| `actors`                     | Internal actor rows for external users                |
