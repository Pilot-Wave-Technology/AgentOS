# Notion Provisioner — Goal and Member Setup

Creates and maintains the Notion page structure for goals and members.
Called by the goal admin UI when Notion is enabled and when new members join.

---

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Exports `NotionProvisioner`, `NotionBotProvisioner`, `bind_pending_shca_subpage` |
| `classes.py` | `NotionProvisioner` and `NotionBotProvisioner` (`ChannelProvisioner` subclasses) |
| `_goal.py` | `_do_provision_goal_structure` and `_import_existing_stories` |
| `_member.py` | `_do_provision_member_subpage` and `bind_pending_shca_subpage` |
| `_helpers.py` | `_parse_notion_page_id` (URL → UUID) and `_shca_for_member` (DB lookup) |

---

## Provisioner Classes (`classes.py`)

Both classes implement the `ChannelProvisioner` interface from `channels/base.py`.

### `NotionProvisioner` (OAuth)

```python
provision_goal(goal_id, integration_id, pool) → str | None
    # Reads parent_database_id and access_token from platform_integrations
    # Delegates to _do_provision_goal_structure
    # Returns error string on failure, None on success

provision_member(goal_id, uid, pool) → None
    # 1. relay_lifecycle_event(goal_id, "member.joined", {uid, display_name})
    # 2. _provision_member_page(goal_id, uid, pool)
```

### `NotionBotProvisioner` (internal token)

Subclass of `NotionProvisioner`. The only difference in `provision_goal`:
if `parent_database_id` is absent from the bot integration's config, it
falls back to the OAuth `notion` integration's `parent_database_id`.

### `_provision_member_page` (shared helper)

Queries any active `notion` or `notion_bot` integration for the goal, resolves
the token, and calls `_do_provision_member_subpage`. Used by both provisioner
classes so `provision_member` works regardless of which integration is active.

---

## Goal Provisioning (`_goal.py`)

### `_do_provision_goal_structure(goal_id, integration_id, access_token, parent_database_id, pool)`

Idempotent. Two branches based on whether a GO binding already exists:

#### Branch A — Update mode (existing GO binding found)

```
1. Load gpi.config
2. If stories_board_db_id is absent (old integration without databases):
   → create_stories_board_database + create_orchestrator_db under goal_page_id
   → persist new db IDs to gpi.config
   → _import_existing_stories
3. Log and return — member subpages are not re-created in update mode
```

#### Branch B — Create mode (no existing GO binding)

```
1. Call create_goal_structure → returns goal_page_id, stories_board_db_id,
   stories_board_ds_id, orch_db_id, orch_ds_id
2. INSERT platform_channel_bindings (GO binding: actor_id IS NULL)
3. Persist all db IDs to gpi.config
4. _import_existing_stories (create cards for stories that already exist)
5. _do_provision_member_subpage for each existing goal member
6. _ensure_parent_gallery_view (one-time gallery view on the parent Goals DB)
```

### `_import_existing_stories(goal_id, stories_board_db_id, access_token, pool)`

Fetches all stories for the goal from the DB, fetches existing `Story ID` values
from the Stories Board (`fetch_existing_story_ids`), and creates cards only for
stories not already represented. Safe to call multiple times.

### `_ensure_parent_gallery_view(access_token, parent_database_id, integration_id, pool)`

Adds a gallery view to the workspace Goals database. Runs once per integration
lifetime, tracked via `platform_integrations.config.parent_gallery_setup`.

---

## Member Provisioning (`_member.py`)

### `_do_provision_member_subpage(goal_id, uid, integration_id, access_token, pool)`

Creates the user's personal Notion page under the goal page and binds it to
their Assistant thread.

```
1. Find goal_page_id from platform_channel_bindings (actor_id IS NULL)
2. Look up display_name + actor_id from users table
3. create_member_subpage(goal_id, goal_page_id, uid, display_name, access_token)
4. _shca_for_member(pool, goal_id, actor_id) → { thread_id, agent_id }
   Found → INSERT platform_channel_bindings with actor_id, thread_id, agent_id
   Not found → INSERT a pending binding (thread_id NULL, agent_id NULL)
```

The active-Assistant INSERT uses `ON CONFLICT DO UPDATE` so re-provisioning a member
refreshes `thread_id`/`agent_id` (handles Assistant recreation). The pending INSERT
uses `ON CONFLICT DO NOTHING` so it never clobbers an already-bound page.

Pending state is `thread_id IS NULL` on the binding row itself — there is no
separate table or JSON map. This pattern is platform-agnostic.

### `bind_pending_shca_subpage(pool, goal_id, uid, agent_id, thread_id)`

Called from `outbound/_binding.py` when an Assistant session first starts. Claims any
pending binding (a `platform_channel_bindings` row with `thread_id IS NULL`):

```
1. Look up actor_id for uid
2. SELECT pending bindings for (goal_id, actor_id) on active notion/notion_bot
   integrations WHERE thread_id IS NULL
3. For each: UPDATE the row, setting thread_id + agent_id (guarded by
   thread_id IS NULL so concurrent Assistant starts can't double-claim)
```

---

## Deferred Binding Flow

When a member page is created but the user has no Assistant thread yet (e.g., they
haven't opened AgentOS yet), the binding is deferred:

```
provision_member called
    ↓
_do_provision_member_subpage
    ↓
No Assistant thread found
    ↓
INSERT platform_channel_bindings (actor_id set, thread_id NULL, agent_id NULL)

    ... later, when the user opens AgentOS and their Assistant starts ...

outbound.relay_agent_message (first agent message on the thread)
    ↓ binding lookup misses
    ↓ _lazy_bind_shca_thread(goal_id, thread_id)
    ↓ bind_pending_shca_subpage(pool, goal_id, uid, agent_id, thread_id)
    ↓ binding now exists in platform_channel_bindings
    ↓ relay succeeds on retry
```

---

## Helpers (`_helpers.py`)

### `_parse_notion_page_id(raw) → str`

Extracts a Notion page UUID from a URL or bare ID string. Handles:
- Full URLs (`https://www.notion.so/.../<32-hex-chars>`)
- Dashed UUID format (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)
- Raw 32-character hex string

Returns `""` if the input cannot be parsed.

### `_shca_for_member(pool, goal_id, actor_id) → dict | None`

Queries for the active Assistant thread for a user in a goal:

```sql
SELECT t.id AS thread_id, a.id AS agent_id
FROM threads t
JOIN agents a ON a.id = t.agent_id
JOIN agent_contexts ac ON ac.agent_id = a.id
WHERE ac.goal_id = $1
  AND a.type = 'SHCA'
  AND a.status = 'active'
  AND a.owner_actor_id = $2
  AND (t.status IS NULL OR t.status IN ('active', 'open'))
  AND t.task_id IS NULL
LIMIT 1
```

Returns `{ thread_id, agent_id }` or `None` if no active thread exists.
