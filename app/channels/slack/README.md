# Slack Channel Connector

Self-contained Slack integration conforming to the generic channel system
(`channels/registry.py`). No imports from `app.slack.*` — this package is the
authoritative Slack implementation and is fully registered in all channel
registry maps.

---

## Architecture

```
Slack API
  │
  ├─ POST /channels/slack/events    → inbound.SlackInboundAdapter.handle_event()
  ├─ POST /channels/slack/actions   → actions.handle_slack_action()
  ├─ POST /channels/slack/commands  → commands.handle_slack_command()
  ├─ GET  /channels/slack/oauth/install   → inbound.SlackInboundAdapter.oauth_install()
  └─ GET  /channels/slack/oauth/callback  → inbound.SlackInboundAdapter.oauth_callback()

Outbound (agent → Slack)
  base.relay_all_outbound()        → outbound.relay_agent_message()  [Block Kit card or plain text]
  base.relay_to_goal_channel()     → outbound.post_to_goal_page()
  base.relay_to_thread_channel()   → outbound.post_to_thread_page()
  base.relay_lifecycle_event()     → lifecycle.relay_lifecycle_event()
```

---

## Files

| File | Purpose |
|---|---|
| `adapter.py` | (none — adapter class lives in `inbound.py`) |
| `inbound.py` | `SlackInboundAdapter` — signature verification, event routing to GO/Assistant sessions |
| `outbound.py` | `relay_agent_message`, `post_to_goal_page`, `post_to_thread_page`, `relay_card` — send messages/cards to Slack channels |
| `lifecycle.py` | `relay_lifecycle_event` — translates generic channel events (story.created, story.state_changed, goal.deleted, …) to Slack messages |
| `actions.py` | `handle_slack_action` — Block Kit button clicks and modal submissions |
| `commands/` | Slash command dispatcher + `registry.py` (single source of truth) + one file per top-level command (`goal`, `story`, `task`, `resources`, `memory`) and per nested subcommand (`work`, `done`, `human_review`, `delete`, `refresh`, `pause`, `resume`, `status`) |
| `config.py` | Workspace credential lookup, per-goal channel config, Assistant binding helpers |
| `thread_map.py` | Slack user ID ↔ internal actor mapping via `platform_user_map` |
| `blocks.py` | Block Kit card renderers (`render_card_as_blocks`, `strip_response_buttons`, etc.) |
| `provisioner.py` | `SlackProvisioner` — goal and member Slack channel provisioning |

---

## Data model

All Slack state lives in the generic channel tables:

| Table | Purpose |
|---|---|
| `platform_integrations` | Workspace credentials (`bot_token`, `bot_user_id`, `workspace_id`) in `credentials_json` JSONB |
| `goal_platform_integrations` | Slack enabled per goal; `config` JSONB holds `{goal_channel_id, orch_channel_id, …}` |
| `platform_channel_bindings` | One row per channel; `actor_id IS NULL` = GO main/orch channel, `actor_id IS NOT NULL` = Assistant user channel |
| `platform_user_map` | Slack user ID → internal actor ID (`platform='slack'`, `mapping_status='confirmed'`) |

Legacy tables (`slack_workspaces`, `slack_user_map`, `slack_user_goal_channels`, `slack_story_channels`) are not used by this package.

---

## Channel model

- **Goal channel** (`goal_channel_id` in `goal_platform_integrations.config`) — public channel for system-level announcements (story created, story done, etc.)
- **Orchestrator channel** (`orch_channel_id`) — read-only, human messages deleted; used by the Goal Orchestrator for visibility
- **Assistant user channel** — one persistent channel per `(goal, user)`, named `<goal-id>-<user-slug>-<agent-id>`; `current_story_id` rotates as the user works through stories; found via `platform_channel_bindings WHERE actor_id IS NOT NULL`

---

## Slash commands

### Command registry (single source of truth)

All slash commands are declared once in [`commands/registry.py`](commands/registry.py)
as a list of `SlackCommand` entries (name, summary, usage, group, admin flag, and a
thin dispatch adapter). Both the dispatcher (`commands/__init__.py`) and the `/help`
command read from that list, so:

- **adding a command** = add one `SlackCommand` entry + its handler module
- **removing a command** = delete its entry
- `/help` and the "unknown command" message update automatically — there is no
  second place to edit, and help can never drift from what's actually wired up

`/help` renders the live registry, grouped, with `(admin)` markers and the
interactive card buttons. The tables below are a human-readable mirror of the
registry; the registry is authoritative.

> **Slack App config:** every top-level slash command (including `/help`) must also
> be registered in the Slack App's *Slash Commands* settings pointing at
> `/channels/slack/commands`. Subcommands (e.g. `/story accept`, `/goal invite`)
> need no Slack-side change — they're parsed from the command text.

Only `/goal`, `/story`, `/task`, `/resources`, `/memory`, and `/help` are registered as
actual Slack slash commands (see `documentation/slack/manifest-*.yaml`). Everything else below is
a **subcommand** parsed from the command text — typing e.g. `/pause` or `/kill` directly
does nothing in Slack; you must go through the owning top-level command.

**Read / inspect**

| Command | Handler | Description |
|---|---|---|
| `/help` | `commands/registry.py` | List every available command + card buttons (generated from the registry) |
| `/goal` | `commands/goal.py` | Goal summary: story counts, in-progress, blocked |
| `/goal status [audit]` | `commands/status.py` | Health dashboard: pause state + active tasks + blocked stories; `audit` adds recent activity + token usage |
| `/goal members` | `commands/goal.py` | List goal members and their roles |
| `/story [id]` | `commands/story.py` | List stories or show detail for one story |
| `/task list <story-id>` / `/task <id>` | `commands/task.py` | List a story's tasks, or show one task's status |
| `/resources [filter]` | `commands/resources.py` | List goal resources (optionally filter by kind or name); `/resources scan <id>` triggers async scan |
| `/memory <query>` | `commands/memory.py` | Full-text search across Assistant memory, decisions, and task history |

**Work actions**

| Command | Handler | Description |
|---|---|---|
| `/story work <title-or-id>` | `commands/work.py` | Start working on a story in your Assistant channel |
| `/story create <title>` | `commands/story.py` | Create a new story in 'proposed' state |
| `/story accept\|start\|block\|unblock <id>` | `commands/story.py` | Story lifecycle transitions via `apply_story_transition` (`block` needs a reason) |
| `/story done <id> [feedback] [--force]` | `commands/done.py` | Mark story or task done (admin: `--force`) |
| `/story review <id> [approve\|reject] [feedback]` | `commands/human_review.py` | Clear a pending Human-review checkpoint |
| `/task delete <id>` | `commands/delete.py` | Hard-delete a task tree (admin only) |
| `/goal invite <email> [role]` | `commands/goal.py` | Add an existing control-panel user to this goal (admin only) |
| `/resources grant\|revoke <res> <story\|task> <subj> [relation]` | `commands/resources.py` | Allocate / de-allocate a resource via `resource_allocation` service (admin only) |
| `/goal refresh` | `commands/refresh.py` | Reset the gateway session for the agent owning this channel |

**Execution control (admin only)**

| Command | Handler | Description |
|---|---|---|
| `/goal pause [reason]` | `commands/pause.py` | Pause goal execution (freezes autonomous dispatch and replies) |
| `/goal resume` | `commands/resume.py` | Resume a paused goal |

Force-kill is **not** exposed via Slack — see "Intentionally web-only" below.

Commands with `-prod` / `-dev` / `-feature` suffixes (e.g. `/goal-prod`) are normalized to
their base form; `render_command_help()` re-suffixes them back for display so `/help` and
`/<command> help` always show the invocation exactly as the user must type it.

**Services called by commands**

All commands are thin adapters calling canonical service functions — the same functions the web UI uses. No business logic lives in the command handlers.

| Command | Service |
|---|---|
| `/goal pause`, `/goal resume` | `app.services.execution_control.pause / resume` |
| `/story done` (story path), `/story accept\|start\|block\|unblock` | `app.services.story_transitions.apply_story_transition` |
| `/story create` | `app.ledger.journal.append_event` + `channels.base.relay_lifecycle_event` |
| `/resources grant\|revoke` | `app.services.resource_allocation.grant_resource / revoke_resource` |
| `/resources scan` | `app.services.resource_scanner.scan_resource` |
| `/goal invite` | `app.services.goal_membership.add_goal_member` |
| `/goal status audit` | reads `ledger_events` + `token_calls` (same store as the web monitor) |
| `/memory` | `app.memory.search_memory` |

### Intentionally web-only (not exposed via Slack)

These are deliberately *not* Slack commands because the action's shape doesn't fit a
slash command or would produce a broken half-feature — documented here so the gap is
explicit, not an oversight:

| Action | Why web-only |
|---|---|
| **Create a goal** | Needs a strict unique goal-ID (which becomes the channel name), a GO-model choice, and a *separate* Slack-enablement step. A goal created from Slack would have no channel. Flow is: create goal in web → enable Slack channel. |
| **Register a resource** (`pwt_register_resource`) | `repo` is rejected (needs a human GitHub-App install + Resources panel); structured kinds (`gcp_*`, …) require a `metadata` object that doesn't fit a flat slash command. Agents still register resources via their tools mid-conversation. |
| **Plan approval / RA verdict** | Reachable in Slack via the Block Kit **Approve / Request Changes** card buttons (`actions.py`), not as a slash command. |
| **Force-kill a task** | One-way-door, safety-critical action — restricted to Admin Controls in the web UI so it always goes through the full confirmation flow. `/task kill` in Slack replies with a pointer to Admin Controls rather than performing the kill. |

---

## Interactive actions (Block Kit)

`actions.py` handles `block_actions` (button clicks) and `view_submission` (modal submits).

Action ID format: `card_<kind>_<task-id>[_<extra>]`

| Kind | Behaviour |
|---|---|
| `approve` | Approve plan / completion |
| `changes` | Open "Request Changes" modal to collect feedback |
| `replan` | Request re-plan |
| `retry` | Retry failed task |
| `cancel` | Cancel task |
| `reply` | Free-form reply to a question card |
| `details` | Show plan/context in modal or upload as file if >30 000 chars |

---

## Deploying / updating the Slack app

Slack must be configured to send events to the new endpoint paths:

| Slack app setting | Value |
|---|---|
| Event Request URL | `https://<host>/channels/slack/events` |
| Interactivity Request URL | `https://<host>/channels/slack/actions` |
| Slash Commands Request URL | `https://<host>/channels/slack/commands` |

The legacy `app/slack/` routes (`/slack/events`, `/slack/actions`, `/slack/commands`) remain active until the Slack app config is updated and `app/slack/` is retired.