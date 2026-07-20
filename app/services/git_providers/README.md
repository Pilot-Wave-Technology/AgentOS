# git_providers

Provider-agnostic abstraction for git hosting operations (GitHub, GitLab).

The merge orchestrator calls this layer — it never talks to GitHub or GitLab directly. This means a GitHub API change only requires updating `github.py`, not the orchestrator.

## Files

| File | Purpose |
|---|---|
| `base.py` | `GitProvider` Protocol + shared dataclasses (`MRResult`, `MergeResult`, `MRStatus`, `GitEvent`) |
| `github.py` | GitHub REST API v3 implementation |
| `gitlab.py` | GitLab REST API v4 implementation |
| `factory.py` | Resolves the right `GitProvider` from a `goal_resources` row |

## Adding a new provider

1. Create `{provider_name}.py` implementing the `GitProvider` Protocol from `base.py`
2. Add a case to `factory.get_provider()`
3. Add provider detection to `app/services/webhooks/receiver.py`
4. Document the API version assumptions at the top of the implementation file

No orchestrator changes needed.

## API version tracking

Each implementation file documents which provider API version it was written against. Check those comments first when a provider upgrade breaks something.

## Full design

See `documentation/story-integration/GIT_PROVIDERS.md`.
