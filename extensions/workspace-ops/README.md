# Workspace Operations Plugin

Admin RPC methods for the AgentOS story-integration flow: previewing a merge inside an agent's workspace, and generating a diff for human review before code is actually integrated.

## Files

| File | Purpose |
|---|---|
| [`index.ts`](index.ts) | The whole plugin — registers `workspace.mergePreview` and `workspace.gitDiff` |

Both methods accept a `session_key` in the format `agent:<agentId>:<rest>` — the same key format used everywhere in the control panel's gateway protocol — and resolve the agent's workspace directory via the gateway's own `resolveAgentWorkspaceDir` core API (loaded dynamically from `dist/extensionAPI.js`, since extensions can't import TypeScript source directly at runtime).

## `workspace.mergePreview`

Runs a preview merge — `git merge --no-commit --no-ff` — inside the agent's cloned repo, without committing.

**Request:** `{ session_key, source_branch, target_branch, write_report_to?, repo_url?, github_token?, story_id? }`

**Response:** `{ success, conflict_files, changed_files, source_sha, target_sha, preview_merge_sha }`

- **On success:** the workspace is left in the merged preview state (`--no-commit`) so the integration-tester agent can run its checks against it.
- **On conflict:** the merge is aborted (`git merge --abort`) and `conflict_files` lists what collided, so the workspace returns to a clean state either way.

Notable behavior:
- The repo is located by checking `stories/<story_id>/repo/` first (where the coder phase clones it), falling back to scanning workspace subdirectories for a `.git` directory.
- If `repo_url` is provided and no `origin` remote exists yet (typical — workspaces are git-init'd locally with no remote), it's added; if `origin` already exists, its URL is updated.
- If `github_token` is provided, it's embedded in the remote URL via the `x-access-token` convention for the fetch, then the clean URL is restored immediately after — the token is never persisted in git config.
- `write_report_to` is resolved relative to the workspace directory with an explicit path-traversal check (must stay inside the workspace) before anything is written.

## `workspace.gitDiff`

**Request:** `{ session_key, baseRef, headRef, github_token? }`

**Response:** `{ diff }`

Fetches both refs, then returns `git diff origin/<baseRef>...<headRef>`. Used by the integrate gate to generate the net diff shown on the human approval card.

## Related docs

- [agentos — documentation/story-integration/SYSTEM_GATE.md](https://github.com/Pilot-Wave-Technology/AgentOS/blob/main/documentation/story-integration/SYSTEM_GATE.md) and [CODE_INTEGRATION.md](https://github.com/Pilot-Wave-Technology/AgentOS/blob/main/documentation/story-integration/CODE_INTEGRATION.md) — full design and the control panel's merge orchestrator that calls these RPCs
- [ARCHITECTURE.md](../../ARCHITECTURE.md#gateway-side-extensions) for how this fits alongside the other gateway extensions
