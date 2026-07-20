# merge_orchestrator

Drives the story integration flow — everything that happens inside the two system-gate tasks (diff-review and integrate-to-trunk).

The dispatcher calls into this service when it encounters a task with `executor='system-gate'`. This service owns the full gate lifecycle and never runs inside a Claude session.

## Files

| File | Purpose |
|---|---|
| `orchestrator.py` | Entry points: `handle_diff_review_gate()`, `handle_integrate_gate()`, `handle_webhook_event()` |
| `description_builder.py` | Builds the MR description from story, plan, phase sentinels, git log, diff stats |
| `conflict_handler.py` | Conflict detection, resolution branch creation, mini-task spawning |
| `signal_manager.py` | `resource_activity_signals` lifecycle — write, check, complete, abandon |
| `workspace_transition.py` | Post-merge workspace state: checkout trunk, verify, build bridge message |

## Responsibility boundary

This service:
- Writes to `integration_attempts`, `resource_activity_signals`, `resource_links`, `approvals`
- Calls `git_providers.*` for all provider operations
- Calls workspace RPCs (`shca.workspace.*`) for checkout and diff
- Posts to Assistant threads for human notifications
- Does NOT dispatch Claude sessions (that is the dispatcher's job)
- Does NOT touch the DAG dependency graph directly (marks task state; dependency resolution is handled by sentinel_parser)

## Entry points

```python
# Called by dispatcher when task.executor == 'system-gate'
await orchestrator.handle_diff_review_gate(task: Task, story: dict) -> None
await orchestrator.handle_integrate_gate(task: Task, story: dict) -> None

# Called by webhooks/event_dispatcher.py when a git event arrives
await orchestrator.handle_webhook_event(event: GitEvent) -> None
```

## Full design

See `documentation/story-integration/FLOW.md` and `documentation/story-integration/RESOURCE_SIGNALS.md`.
