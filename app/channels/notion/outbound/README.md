# Notion Outbound — Relay Functions

Posts AgentOS messages and system events to Notion as comments or database rows.

---

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports all public relay functions |
| `_binding.py` | Binding lookup chain, stale repair, lazy bind, text chunking |
| `agent.py` | `relay_agent_message`, `relay_human_message`, `relay_file_upload` |
| `system.py` | `post_to_goal_page`, `post_to_thread_page`, `relay_orchestrator_message` |

---

## Binding Lookup Chain (`_binding.py`)

Every relay function resolves the Notion page to post to using this chain:

```
get_binding_by_thread("notion", thread_id, goal_id)
    miss → get_binding_by_thread("notion_bot", thread_id, goal_id)
    miss → _repair_stale_shca_binding("notion", goal_id, thread_id)
    miss → _repair_stale_shca_binding("notion_bot", goal_id, thread_id)
    miss → _lazy_bind_shca_thread(goal_id, thread_id)
           then retry get_binding_by_thread for both platforms
    miss → log INFO, return (no-op)
```

### Stale binding repair (`_repair_stale_shca_binding`)

Assistant threads are destroyed and recreated when idle timeout fires. The binding in
`platform_channel_bindings` still holds the old `thread_id`. The repair function:

1. Looks up the thread's owning agent → `owner_actor_id`
2. Finds the binding by `(goal_id, actor_id)` — ignores stale `thread_id`
3. `UPDATE platform_channel_bindings SET thread_id=<new>, agent_id=<new>`
4. Returns the repaired binding so the relay can proceed without delay

### Lazy binding (`_lazy_bind_shca_thread`)

Called when no binding is found for a thread at all. Looks for a pending binding
(a `platform_channel_bindings` row with `thread_id IS NULL`) created at provision
time when the Assistant didn't exist yet, and calls `bind_pending_shca_subpage` to fill
in its `thread_id`/`agent_id`. See `provisioner/README.md` for the deferred binding flow.

### Text chunking (`_split_for_notion`)

Notion rich-text blocks have a 2000 UTF-16 code-unit limit. `_split_for_notion`
splits long content into chunks that respect this limit, counting UTF-16 units
correctly (emoji outside the BMP cost 2 units each). Up to 100 chunks per comment.

---

## Agent Message Relay (`agent.py`)

### `relay_agent_message(goal_id, thread_id, content, msg_id=None)`

Called by `relay_all_outbound` (via registry) on every agent reply.

```
1. Skip if content empty or is an internal signal (NO_REPLY, HEARTBEAT_OK)
2. Resolve binding via lookup chain
3. Resolve API token for the integration
4. Split content into ≤2000 UTF-16 unit chunks
5. POST notion.comments.create(parent=page_id, rich_text=[...chunks...])
6. Stamp messages.external_message_id = returned comment ID
   (prevents inbound echo on the next webhook delivery)
```

### `relay_human_message(goal_id, thread_id, content, sender_display_name="")`

Mirrors a message typed in the AgentOS web UI to Notion. Prefixes with `[name]: `
when `sender_display_name` is provided. Posted by the bot — echo prevention layer 1
(author.type='bot') drops it on inbound automatically.

### `relay_file_upload(goal_id, uid, display_name, filename, size_kb, ext, sender_agent)`

Posts a `📎 [name] uploaded file.ext (EXT, N KB)` notice to the relevant page.
For GO uploads uses the goal's GO thread; for others uses the user's latest Assistant thread.

---

## System Relay (`system.py`)

### `post_to_goal_page(goal_id, content, username="System")`

Posts to the goal page (the GO thread's Notion page). Used by `relay_to_goal_channel`
in `base.py` for system-level announcements (e.g. member joined, goal completed).

### `post_to_thread_page(goal_id, thread_id, content, username="System")`

Posts to the page bound to a specific thread. Used by `relay_to_thread_channel`
in `base.py` and by lifecycle handlers to deliver per-user notifications.

### `relay_orchestrator_message(goal_id, content)`

Appends `content` to the Orchestrator Feed database for the goal. Fetches
`orch_db_id` from `gpi.config`. Used by the orchestrator router and lifecycle
handlers for coordination messages.

---

## Echo Prevention

Three layers prevent infinite relay loops (agent posts comment → Notion webhook → re-route):

| Layer | Mechanism | Handled by |
|---|---|---|
| 1 | `author.type == "bot"` — inbound adapter drops bot-authored webhooks | `inbound/comments.py` |
| 2 | `messages.external_message_id` — outbound stamps the comment ID; inbound checks before routing | `agent.py` + `inbound/comments.py` |
| 3 | `bot_external_id` match — inbound adapter drops comments from our own bot user ID | `inbound/comments.py` |

`relay_human_message` and `relay_orchestrator_message` rely on layer 1 only —
they are always posted by our bot token, so the inbound webhook carries `author.type='bot'`.
