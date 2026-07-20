# Notion Lifecycle — Event Handlers

Translates platform-agnostic AgentOS lifecycle events into Notion actions
(database row creation, page comments, card status updates).

---

## Files

| File | Purpose |
|---|---|
| `__init__.py` | `relay_lifecycle_event` — dispatcher; routes event_type to the right handler |
| `_config.py` | `_get_notion_config` — shared helper: loads access token + gpi.config for a goal |
| `story.py` | `_on_story_created`, `_on_story_state_changed` |
| `task.py` | `_on_task_created`, `_on_task_state_changed`, `_on_task_completed` |
| `goal.py` | `_on_goal_completed`, `_on_member_joined`, `_on_resource_registered` |

---

## Entry Point (`__init__.py`)

```python
async def relay_lifecycle_event(goal_id, event_type, payload, thread_id=None)
```

Registered in `channels/registry.py` as `_LIFECYCLE_RELAYS["notion"]`.
Called from `base.relay_lifecycle_event` whenever AgentOS emits a lifecycle event.

Dispatch table:

| `event_type` | Handler | Module |
|---|---|---|
| `story.created` | `_on_story_created` | `story.py` |
| `story.state_changed` | `_on_story_state_changed` | `story.py` |
| `task.created` | `_on_task_created` | `task.py` |
| `task.state_changed` | `_on_task_state_changed` | `task.py` |
| `task.completed` | `_on_task_completed` | `task.py` |
| `member.joined` | `_on_member_joined` | `goal.py` |
| `resource.registered` | `_on_resource_registered` | `goal.py` |
| `goal.completed` | `_on_goal_completed` | `goal.py` |
| `goal.deleted` | no-op | — |
| `orchestrator.message` | `relay_orchestrator_message` | `outbound/system.py` |

---

## Config Loader (`_config.py`)

`_get_notion_config(goal_id)` returns `(access_token, cfg, integration_id)` or `None`.

- Queries `platform_integrations` joined with `goal_platform_integrations` for an active,
  enabled Notion integration on the goal.
- `cfg` is the parsed `goal_platform_integrations.config` JSONB — contains
  `stories_board_db_id`, `orch_db_id`, `stories_board_ds_id`, `orch_ds_id`, etc.
- Returns `None` if no active integration is found; all handlers return early in that case.

---

## Story Handlers (`story.py`)

### `_on_story_created(goal_id, payload)`

```
payload: { story_id, story_title, assigned_to, state, priority }

1. Create story card in Stories Board database (pages.create_story_card)
   Card contains: Name, Status, Story ID, Created, Assigned To, Priority
   Card page body: callout block explaining the page is view-only
2. Append row to Orchestrator Feed database (pages.append_orchestrator_event)
   Event: "📌 Story created: {title} · {assigned_to}"
```

### `_on_story_state_changed(goal_id, payload, thread_id)`

```
payload: { story_id, story_title, old_state, new_state }

1. Find story card in Stories Board by Story ID property (pages.find_story_card)
2. Update card Status select property to the new state label
3. Append row to Orchestrator Feed database
4. Notify assigned user:
   thread_id provided → post_to_thread_page(goal_id, thread_id, msg)
   not provided       → look up assigned_to from stories table
                      → find actor_id from users table
                      → find platform_channel_bindings row for (goal_id, actor_id)
                      → post_to_thread_page(goal_id, binding.thread_id, msg)
```

Story state → Notion status mapping (`STORY_STATE_TO_NOTION` in `pages/_blocks.py`):

| AgentOS state | Notion select |
|---|---|
| `proposed` | `Proposed` |
| `accepted` | `Accepted` |
| `in_progress` | `In Progress` |
| `blocked` | `Blocked` |
| `done` | `Done` |
| `cancelled` | `Cancelled` |
| `rejected` | `Rejected` |

---

## Task Handlers (`task.py`)

All task handlers load the story's `tasks_db_id` by looking up the story in
the Stories Board (`find_story_card`) then finding the inline Tasks Feed database
(`find_story_tasks_db`).

### `_on_task_created(goal_id, payload)`

Creates a task card in the Tasks Feed database and appends an Orchestrator event.

### `_on_task_state_changed(goal_id, payload)`

Updates the task card Status property and appends an Orchestrator event.

### `_on_task_completed(goal_id, payload)`

Updates the task card to Done and appends an Orchestrator event with the result summary.

---

## Goal Handlers (`goal.py`)

### `_on_member_joined(goal_id, payload)`

```
payload: { uid, display_name }

Delegates to provisioner._do_provision_member_subpage to create the member's
Notion subpage under the goal page and bind it to their Assistant thread.
```

### `_on_resource_registered(goal_id, payload)`

Appends a row to the Resources Feed database with event type `resource.registered`.

### `_on_goal_completed(goal_id, payload)`

Posts a completion notice to the goal page as a comment.
