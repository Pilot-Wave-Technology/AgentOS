# Notion Pages — Database and Page Creation

Primitives for creating and querying the Notion page/database structure that
mirrors AgentOS goals, stories, tasks, and members.

---

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports all public functions and `STORY_STATE_TO_NOTION` |
| `_blocks.py` | `_text()` rich-text helper; `STORY_STATE_TO_NOTION` state mapping |
| `databases.py` | Goal structure creation: goal page + all sub-databases |
| `cards.py` | Story/task card creation, Orchestrator/Resources Feed row append |
| `lookup.py` | Query helpers: find cards, fetch existing IDs |
| `members.py` | Member subpage creation and orchestrator page update |

---

## Database Creation (`databases.py`)

### `create_goal_structure(goal_id, goal_name, goal_objective, goal_status, members, parent_database_id, access_token) → dict`

Top-level call that creates the full Notion structure for a goal in one shot.

```
1. Create Goal page in parent_database_id
2. create_stories_board_database(goal_page_id, ...)
3. create_orchestrator_db(goal_page_id, ...)         → Orchestrator Feed
4. create_orchestrator_feed_database(goal_page_id, ...) → Resources Feed (legacy name)
5. Return { goal_page_id, stories_board_db_id, stories_board_ds_id,
             orch_db_id, orch_ds_id }
```

### `create_stories_board_database(parent_page_id, goal_name, notion, access_token) → (db_id, ds_id)`

Creates the Stories Board inline database under the goal page with properties:
`Name` (title), `Status` (select), `Story ID` (rich text), `Created` (date),
`Assigned To` (rich text), `Priority` (select).

Locks the database (`is_locked=True`) after creation to prevent UI edits.

### `create_tasks_feed_database(parent_page_id, story_title, notion, access_token) → (db_id, ds_id)`

Creates a Tasks Feed inline database on a story card page with properties:
`Name` (title), `Status` (select), `Task ID` (rich text), `Created` (date).

Locked after creation.

### `create_orchestrator_db(parent_page_id, goal_name, notion, access_token) → (db_id, ds_id)`

Creates the Orchestrator Feed database with properties:
`Event` (title), `Timestamp` (date), `Actor` (select: System/Agent/User), `Type` (select).

### `create_orchestrator_feed_database(parent_page_id, goal_name, notion, access_token) → (db_id, ds_id)`

Creates the Resources Feed database (separate from Orchestrator) with properties:
`Event` (title), `Event Type` (select), `Timestamp` (date), `Actor` (rich text), `Details` (rich text).

### `setup_gallery_view_on_db(access_token, db_id, ds_id, view_name) → None`

Adds a gallery view to a database via the Notion REST API. Called once per
integration lifetime (tracked in `platform_integrations.config.parent_gallery_setup`)
to add a gallery view to the parent Goals database.

---

## SDK Version Compatibility (`lookup.py`)

Notion SDK v3 uses `notion.data_sources.query(ds_id)` while v2 uses
`notion.databases.query(db_id)`. `_query_db(notion, db_id)` detects the installed
version and routes accordingly:

```python
async def _query_db(notion, db_id, **kwargs) -> dict:
    ds_id = await _get_ds_id(db_id, notion)      # retrieves data_source id
    if hasattr(notion, "data_sources"):
        return await notion.data_sources.query(ds_id, **kwargs)
    return await notion.databases.query(db_id, **kwargs)
```

Always use `_query_db` when querying databases — never call `databases.query` directly.

---

## Card Creation and Feed Appending (`cards.py`)

### `create_story_card(stories_board_db_id, story_id, title, state, assigned_to, priority, notion, access_token) → (story_page_id, tasks_db_id, tasks_ds_id)`

Creates a story card in the Stories Board. Also creates a Tasks Feed database inline
on the story page. Returns all three IDs. The card body includes a callout block
explaining the page is view-only (chat happens on member pages).

### `create_task_card(tasks_db_id, task_id, title, state, notion) → page_id`

Creates a task card in a Tasks Feed database.

### `update_page_select_property(page_id, property_name, value, notion) → None`

Sets a select property on any Notion page. Used to update story/task Status.

### `append_orchestrator_event(orch_db_id, event_title, event_type, actor, notion, timestamp) → None`

Appends a row to the Orchestrator Feed database. `actor` must be one of:
`"System"`, `"Agent"`, `"User"`.

### `append_feed_row(db_id, title, event_type, actor, details, notion, timestamp) → None`

Appends a row to the Resources Feed database (different schema from Orchestrator).

---

## Lookup Helpers (`lookup.py`)

### `find_story_card(stories_board_db_id, story_id, notion) → page_id | None`

Queries the Stories Board for a card whose `Story ID` property matches `story_id`.
Uses `_query_db` for SDK version compatibility.

### `find_task_card(tasks_db_id, task_id, notion) → page_id | None`

Queries a Tasks Feed database for a card whose `Task ID` matches.

### `find_story_tasks_db(story_page_id, notion) → (db_id, ds_id) | (None, None)`

Finds the inline Tasks Feed database on a story page by scanning the page's
child blocks for a database object.

### `fetch_existing_story_ids(stories_board_db_id, notion) → set[str]`

Returns all `Story ID` values already in the Stories Board. Used by
`_import_existing_stories` to skip cards that already exist.

---

## Member Subpages (`members.py`)

### `create_member_subpage(goal_id, goal_page_id, uid, display_name, access_token) → page_id`

Creates a subpage under the goal page titled `[display_name]`. The page is the
user's personal Assistant chat surface in Notion — comments on this page route to
their Assistant thread.

### `update_orchestrator_page(goal_page_id, orch_subpage_id, content, notion) → None`

Posts `content` as a comment on the Orchestrator page. Legacy helper kept for
backwards compatibility with older bindings that store `orch_subpage_id`.

---

## State Mapping (`_blocks.py`)

```python
STORY_STATE_TO_NOTION = {
    "proposed":    "Proposed",
    "accepted":    "Accepted",
    "in_progress": "In Progress",
    "blocked":     "Blocked",
    "done":        "Done",
    "cancelled":   "Cancelled",
    "rejected":    "Rejected",
}
```

Used when creating or updating story cards. The Notion select option must exist on
the database — these values are set up at database creation time in `databases.py`.
