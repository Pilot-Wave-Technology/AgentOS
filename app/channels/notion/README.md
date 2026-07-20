# Notion Channel — Implementation Reference

This document covers everything specific to the Notion integration living under
`app/channels/notion/` and `app/channels/notion_bot/`. For the generic channel
infrastructure (registry, base helpers, how to add a new platform) see the
parent `channels/README.md`.

---

## Two Integration Modes

| Mode | Platform slug | Auth type | Webhook signing | Use case |
|---|---|---|---|---|
| OAuth | `notion` | `credentials_kind='oauth'` — access token via OAuth flow | HMAC-SHA256 | Team workspaces: user authorises, bot inherits their page access |
| Internal token | `notion_bot` | `credentials_kind='api_key'` — token stored directly | None (internal integrations don't sign) | Dev/admin: single API key with wide access |

Both modes share all page-creation, outbound relay, and binding logic. The
only differences are:
- `NotionBotAdapter` skips signature verification (`verify_request` returns `True`).
- `NotionBotProvisioner.provision_goal` falls back to the OAuth integration's
  `parent_database_id` when no database ID is configured on the bot row.

### Token resolution (`secrets.py`)

```
resolve_token(credentials_kind, credentials_data, credentials_ref)
    credentials_kind = 'oauth'   → return credentials_data (OAuth access token)
    credentials_kind = 'api_key' → credentials_data ? return it : fetch from Secret Manager at credentials_ref
```

Always call `resolve_token` before using a Notion API key — never read `credentials_data` directly.

---

## Webhook Model (Signal-Only)

Notion webhooks carry only identifiers, never the content itself:

```json
{
  "type": "comment.created",
  "entity": { "id": "<comment_id>" },
  "data":   { "page_id": "<page_id>" },
  "authors": [{ "id": "<user_id>", "type": "person|bot", "name": "..." }]
}
```

After receiving a signal, the adapter fetches full content via the API
(`notion_client.comments.retrieve(comment_id)`).

### Supported event types

| `type` | Handler | Action |
|---|---|---|
| `comment.created` | `_handle_comment` | Fetch text → route to bound Assistant or GO session |
| `page.content_updated` | `_handle_page_update` | Send `[Notion page updated: <id>]` stub message |

All other event types are logged at INFO and ignored.

---

## Inbound Flow

```
POST /channels/notion/events   (or /channels/notion_bot/events)
    ↓
router.py: verify_request()
    notion:     HMAC-SHA256 of body vs signing secret
    notion_bot: always True (no signing)
    ↓
adapter.handle_event()
    ↓
1. is_duplicate(platform, x-notion-delivery-id)         ← dedup layer
2. drop if author.type == "bot"                          ← echo prevention layer 1
3. drop if comment_id in messages.external_message_id   ← echo prevention layer 2
4. get_binding(platform, page_id)                        ← page_id → thread mapping
   no binding → post redirect comment and return
5. drop if author.id == integration.bot_external_id     ← echo prevention layer 3
6. resolve_notion_actor(author) → actor_id              ← identity resolution
   unresolved/pending_review → drop
7. enforce member-page owner check (binding.actor_id must match commenter)
8. fetch comment text from API
9. route_to_session(binding, sender, text, platform="notion")
```

### Actor resolution (`identity.py`)

```
webhook author.id → platform_user_map lookup
    confirmed hit        → return actor_id immediately
    pending_review hit   → drop message (admin must approve)
    miss:
        enrich with email via Notion Users API (requires OAuth token)
        email match in users table → create confirmed mapping → return actor_id
        domain allowlist check    → reject if domain absent
        no match                  → auto-create actor (pending_review) → drop
```

Admins confirm pending mappings in `/admin/integrations`.

---

## Page Hierarchy

Provisioning creates this structure under the configured workspace database:

```
Database (parent_database_id — configured in integration settings)
└── Goal page                              ← GO thread binding (actor_id IS NULL)
    ├── Stories Board database             ← story cards (one row per story)
    │   └── Tasks Feed database (per story) ← task cards inline on story page
    ├── Orchestrator Feed database         ← append-only timeline of system events
    ├── Resources Feed database            ← resource lifecycle events
    └── [Member name] subpage (×N)         ← Assistant chat surface per user
                                              (actor_id = user.actor_id)
```

- **Goal page**: comments route to/from the GO session. View-only for members.
- **Stories Board database**: one card per story; Status syncs with AgentOS story state.
- **Tasks Feed database**: inline on each story page; one card per task.
- **Orchestrator Feed database**: append-only log of story/task lifecycle events.
- **Resources Feed database**: resource registration and update events.
- **Member subpages**: one per goal member — comments route to/from their Assistant thread. This is the primary human↔agent chat surface in Notion.

---

## Provisioning Flow

See `provisioner/README.md` for the full detail. Summary:

### `provision_goal` (idempotent)

```
existing GO binding found?
    Yes (update mode) → check for missing databases (upgrade path), skip member pages
    No  (create mode) → create_goal_structure → insert GO binding → persist DB IDs
                      → import existing stories → provision member pages for all members
```

Key config stored in `goal_platform_integrations.config`:

| Key | Value |
|---|---|
| `stories_board_db_id` | Notion database ID for the Stories Board |
| `stories_board_ds_id` | Notion data-source ID (for SDK v3 queries) |
| `orch_db_id` | Notion database ID for the Orchestrator Feed |
| `orch_ds_id` | Notion data-source ID for Orchestrator Feed |

### `provision_member`

```
1. relay member.joined lifecycle event
2. _do_provision_member_subpage:
   Assistant exists for member → INSERT platform_channel_bindings (thread_id + agent_id set)
   No Assistant yet          → INSERT pending binding (thread_id NULL, agent_id NULL)
```

Deferred bindings are `platform_channel_bindings` rows with `thread_id IS NULL` —
there is no separate table or JSON map. (Historically this used a
`gpi.config.pending_member_pages` `{uid: page_id}` map; that was removed in favour
of the NULL-thread_id convention, which is platform-agnostic.)

### Lazy binding (`bind_pending_shca_subpage`)

When an Assistant session first starts, `_lazy_bind_shca_thread` in `outbound/_binding.py`
claims any pending binding (a `platform_channel_bindings` row with `thread_id IS NULL`)
by filling in its `thread_id` and `agent_id`.

### Stale binding repair (`_repair_stale_shca_binding`)

Assistant threads are recreated after reap. On outbound relay miss, the repair function
looks up the binding by `(goal_id, actor_id)` instead of `thread_id`, updates
`thread_id` and `agent_id`, and returns the repaired binding immediately.

---

## Outbound Relay

See `outbound/README.md` for detail. Every relay function uses this binding lookup chain:

```
get_binding_by_thread("notion", thread_id, goal_id)
    miss → get_binding_by_thread("notion_bot", ...)
    miss → _repair_stale_shca_binding("notion", ...)
    miss → _repair_stale_shca_binding("notion_bot", ...)
    miss → _lazy_bind_shca_thread(...)  [then retry get_binding_by_thread]
    miss → no-op (log at INFO)
```

| Function | Purpose |
|---|---|
| `relay_agent_message` | Post agent reply as Notion comment; stamp `messages.external_message_id` |
| `relay_human_message` | Mirror UI-typed message to Notion (prefixed `[name]:`) |
| `relay_file_upload` | Post file-upload notice to the relevant page |
| `relay_orchestrator_message` | Append to Orchestrator Feed database |
| `post_to_goal_page` | Post to the goal page (GO thread target) |
| `post_to_thread_page` | Post to a specific member page (Assistant thread target) |

---

## Lifecycle Events

See `lifecycle/README.md` for detail. Entry point:

```
relay_lifecycle_event(goal_id, event_type, payload, thread_id=None)
```

| `event_type` | Notion action |
|---|---|
| `story.created` | Create story card in Stories Board; append Orchestrator event |
| `story.state_changed` | Update story card Status; append Orchestrator event; notify assigned user's member page |
| `task.created` | Create task card in the story's Tasks Feed; append Orchestrator event |
| `task.state_changed` | Update task card Status; append Orchestrator event |
| `task.completed` | Update task card; append Orchestrator event |
| `member.joined` | Provision member subpage (delegates to `_do_provision_member_subpage`) |
| `resource.registered` | Append to Resources Feed database |
| `goal.completed` | Post notice to goal page |
| `goal.deleted` | No-op |
| `orchestrator.message` | Append to Orchestrator Feed database |

---

## OAuth Flow (`oauth.py`)

```
GET /channels/notion/oauth/install   → redirect to Notion OAuth consent page
GET /channels/notion/oauth/callback  → exchange code → store in platform_integrations
```

Stores on successful exchange:
- `platform = 'notion'`, `credentials_kind = 'oauth'`
- `credentials_data = access_token`
- `bot_external_id = bot.bot_id` (from Notion token response — used for echo prevention)
- `config.parent_database_id` — extracted from the workspace if provided

---

## Database Columns Used

| Table | Relevant columns | Purpose |
|---|---|---|
| `platform_integrations` | `platform`, `status`, `credentials_kind`, `credentials_data`, `credentials_ref`, `bot_external_id`, `config.parent_database_id` | Integration row per workspace/token |
| `goal_platform_integrations` | `enabled`, `email_domain_allowlist`, `config.stories_board_db_id`, `config.orch_db_id`, `config.pending_member_pages` | Per-goal opt-in and provisioning state |
| `platform_channel_bindings` | `goal_id`, `integration_id`, `actor_id`, `external_channel_id`, `thread_id`, `agent_id`, `resource_kind='comment'` | Page ↔ thread mapping |
| `platform_user_map` | `platform`, `external_user_id`, `actor_id`, `mapping_status` | Notion user → internal actor |
| `platform_processed_events` | `platform`, `external_event_id` | Dedup by `x-notion-delivery-id` |
| `messages` | `external_message_id` | Echo-prevention: outbound comment ID stored here |

---

## File Map

```
app/channels/notion/
├── README.md          ← this file
├── auth.py            HMAC-SHA256 signature verification + get_integration helper
├── identity.py        resolve_notion_actor: Notion user → actor_id
├── oauth.py           OAuth install/callback: code exchange, token storage
├── secrets.py         resolve_token: credentials_kind → final API token string
│
├── inbound/           NotionInboundAdapter + event handlers (see inbound/README.md)
│   ├── __init__.py    NotionInboundAdapter class
│   ├── attachments.py _enrich_author_email via Notion Users API
│   ├── comments.py    _handle_comment: actor resolution → route_to_session
│   └── page_updates.py _handle_page_update: stub routing for page edits
│
├── lifecycle/         Lifecycle event dispatcher (see lifecycle/README.md)
│   ├── __init__.py    relay_lifecycle_event entry point
│   ├── _config.py     _get_notion_config: shared token + config loader
│   ├── goal.py        goal.completed, goal.deleted handlers
│   ├── story.py       story.created, story.state_changed handlers
│   └── task.py        task.created, task.state_changed, task.completed handlers
│
├── outbound/          Relay functions (see outbound/README.md)
│   ├── __init__.py    re-exports relay_agent_message, relay_human_message, etc.
│   ├── _binding.py    binding lookup chain, stale repair, lazy bind, split helpers
│   ├── agent.py       relay_agent_message, relay_human_message, relay_file_upload
│   └── system.py      post_to_goal_page, post_to_thread_page, relay_orchestrator_message
│
├── pages/             Notion API page/database creation (see pages/README.md)
│   ├── __init__.py    re-exports all public creation/query functions
│   ├── _blocks.py     rich-text helpers + STORY_STATE_TO_NOTION mapping
│   ├── cards.py       create_story_card, create_task_card, append_orchestrator_event, etc.
│   ├── databases.py   create_goal_structure, create_stories_board_database, create_orchestrator_db, etc.
│   ├── lookup.py      find_story_card, find_task_card, fetch_existing_story_ids
│   └── members.py     create_member_subpage, update_orchestrator_page
│
└── provisioner/       Goal and member provisioning (see provisioner/README.md)
    ├── __init__.py    exports NotionProvisioner, NotionBotProvisioner, bind_pending_shca_subpage
    ├── _goal.py       _do_provision_goal_structure, _import_existing_stories
    ├── _helpers.py    _parse_notion_page_id, _shca_for_member
    ├── _member.py     _do_provision_member_subpage, bind_pending_shca_subpage
    └── classes.py     NotionProvisioner, NotionBotProvisioner

app/channels/notion_bot/
├── __init__.py
└── adapter.py         NotionBotAdapter: skips signature verification, uses notion_bot platform rows
```
