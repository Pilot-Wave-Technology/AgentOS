# webhooks

Receives and routes inbound webhook events from git providers (GitHub, GitLab).

One public endpoint receives all events from all repos. Per-repo webhook registration happens automatically when a git repo resource is added to a goal.

## Files

| File | Purpose |
|---|---|
| `receiver.py` | FastAPI route handler — validates signatures, parses provider, routes to handler |
| `github_handler.py` | Parses GitHub event payloads → `GitEvent` |
| `gitlab_handler.py` | Parses GitLab event payloads → `GitEvent` |
| `event_dispatcher.py` | Routes `GitEvent` objects to `merge_orchestrator.handle_webhook_event()` |

## Security

Every inbound request is signature-validated before processing:
- GitHub: `X-Hub-Signature-256` HMAC-SHA256 against the shared secret
- GitLab: `X-Gitlab-Token` matches the stored secret

Requests that fail validation are rejected with HTTP 403. The secret is per-repo, stored encrypted in `goal_resources.metadata.webhook_secret`.

## Endpoint

`POST /webhooks/git` — registered once in the FastAPI app. All repos send events here.

## Per-repo registration

Triggered by the resource creation flow when `goal_resources.kind` is `git_repo` or `git_branch`. The system calls `git_provider.register_webhook()` and stores the returned `webhook_id` in `goal_resources.metadata.webhook_id`. Failure is surfaced on the resource card in the UI — resource creation is not blocked.

## Adding a new provider

1. Create `{provider}_handler.py` that converts provider payloads to `GitEvent` objects
2. Add provider detection to `receiver.py` (inspect headers for provider-specific markers)
3. Add signature validation for the provider's scheme
4. Register in `event_dispatcher.py`

The `GitEvent` dataclass in `app/services/git_providers/base.py` is the shared contract. Handlers produce it; the dispatcher consumes it. Provider differences are fully absorbed by the handler.

## Full design

See `documentation/story-integration/WEBHOOKS.md`.
