"""Operator-mode story workspace provisioning.

When a story with execution_mode='operator' is imported to a SHCA, this
module sets up the on-disk workspace where Claude Code will run, BEFORE
the operator kickoff is sent to SHCA. SHCA never starts operating until
this completes successfully.

Layout (created on the openclaw VM via shca.workspace.setup RPC, inside
the SHCA agent's own workspace):

  <agentWorkspaceDir>/stories/<story_id>/
    ├── repo/                     ← git clone --single-branch --branch <X>
    │   └── stories/<story_id>/   ← Claude writes scope.md + plans/ here
    │                                (lives in the repo so they ship to git)
    ├── CLAUDE.md                 ← workspace-root operator system prompt
    │                                (auto-loaded by Claude Code; do-not-edit)
    ├── docs/DECISIONS.md         ← workspace scratch (NOT in git repo)
    ├── docs/CODE_NOTES.md
    ├── .claude/settings.json     ← permissions + PreToolUse hooks
    ├── RESOURCES.md              ← built here from goal/story resource state
    └── IDENTITY.md               ← built here

The gateway resolves <agentWorkspaceDir> from the agent id, so the
control panel only needs to send agentId + storyId.

State machine on stories.workspace_state:
  none → cloning → provisioning → ready
  failed (any failure)

Cleanup is automatic: when the SHCA is reaped via agents.delete, the
agent's whole workspace dir (including stories/<id>/) is removed.
"""
import asyncio
import json
import logging
import uuid
from typing import Optional

from app import local_dev
from app.config import get_settings
from app.database import get_pool
from app.github_tokens import generate_installation_token
from app.ledger.journal import append_event
from app.services.git_graph.repo_identity import canonical_repo_id, is_write_relation
from app.ledger.schema import WORKTREE_CREATED, WORKTREE_DELETED


_log = logging.getLogger(__name__)


from app.ledger.actors import SYSTEM_ACTOR_ID  # one canonical, registered actor
from app.services.story_clone_layout import assign_subdirs, group_clones
from app.services.story_workspace_layout import resolve_workspace_clones, write_clones

# Bounded inline retry for the shca.workspace.setup RPC (A). A just-minted GitHub
# installation token can briefly lag before git can use it (seen live as a
# transient "Repository not found" on the clone), and clone/network blips are
# transient too. We re-mint a fresh token and retry a few times before declaring
# the workspace 'failed'. A genuinely permanent error still fails after the cap;
# the periodic provision-retry backstop (B, in app/main.py) then retries over
# minutes and finally leaves it failed + surfaced.
_SETUP_MAX_ATTEMPTS = 3
_SETUP_RETRY_BACKOFF_S = 2.0


# ── State machine helpers ──────────────────────────────────────────────────

async def _set_workspace_state(
    pool, story_id: str, new_state: str, error: Optional[str] = None,
) -> None:
    if new_state == "ready":
        await pool.execute(
            """UPDATE stories
                  SET workspace_state = $1,
                      workspace_state_error = NULL,
                      workspace_provisioned_at = NOW(),
                      workspace_provision_attempts = 0,
                      updated_at = NOW()
                WHERE id = $2""",
            new_state, story_id,
        )
    else:
        # Increment the attempt counter only on a 'failed' transition — the
        # provision-retry backstop (B) uses it as a bounded retry budget so a
        # permanently-broken provision can't loop forever. 'cloning'/'provisioning'
        # transitions leave it unchanged.
        await pool.execute(
            """UPDATE stories
                  SET workspace_state = $1,
                      workspace_state_error = $2,
                      workspace_provision_attempts = workspace_provision_attempts
                          + CASE WHEN $1 = 'failed' THEN 1 ELSE 0 END,
                      updated_at = NOW()
                WHERE id = $3""",
            new_state, error, story_id,
        )


async def clear_operator_workspace_on_assistant_delete(
    pool, goal_id: str, thread_id: str,
) -> None:
    """Mark operator stories as workspace-deleted when their assistant is destroyed.

    Called by any code path that deletes an assistant from the gateway so the story
    stops appearing in token-refresh and provisioning queries. Looks up by
    goal_id + active_thread_id (the same join the watchdog uses) and only
    touches stories whose workspace is in a live state (not already none/deleted).
    """
    updated = await pool.execute(
        """UPDATE stories
              SET workspace_state       = 'deleted',
                  workspace_path        = NULL,
                  workspace_state_error = NULL,
                  updated_at            = NOW()
            WHERE goal_id         = $1
              AND active_thread_id = $2
              AND workspace_state NOT IN ('none', 'deleted')""",
        goal_id, thread_id,
    )
    _log.debug(
        "operator_workspace: cleared workspace state for goal=%s thread=%s (%s)",
        goal_id, thread_id, updated,
    )


# ── Repo resolution ────────────────────────────────────────────────────────

async def _resolve_repo_for_story(pool, goal_id: str, story_id: str) -> Optional[dict]:
    """Find the repo+branch this story should clone. Prefers kind='branch'
    (explicit story branch) over kind='repo' (goal-wide default).

    relation='produces' ranks strictly above 'modifies' (2026-08-20 fix): a
    grind-spawned Sync/Fix story is granted its OWN repo as 'produces' and any
    ripple-check sibling repos as 'modifies' (see grind.py's
    _spawn_engine_code_story), inserted in the same DB transaction — without
    this tiebreak they tie exactly on created_at and a sibling repo could sort
    first, making the story's workspace/cwd resolve to the WRONG repo (root
    cause of stories ds_0056006825e2a8 / ds_e45fc37b26f0c4 committing into
    agent_os_openclaw while linked to a agentos git_node). This is the same
    instability class already fixed for story_workspace_nodes.is_primary
    (story_workspace_layout.resolve_workspace_clones) and local_hydrate's
    _resolve_live_repos — this is the third and last read path over the old
    unstable created_at tiebreak.
    """
    row = await pool.fetchrow(
        """
        SELECT gr.id, gr.name, gr.kind, gr.uri, gr.clone_url, gr.branch_name,
               gr.repo_name, gr.repo_owner, gr.role, gr.base_branch,
               sr.relation
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1
           AND gr.goal_id = $2
           AND gr.status = 'active'
           AND gr.kind IN ('repo', 'branch')
           AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
           -- NEVER clone the repo's TRUNK designation (role='trunk' branch): the
           -- workspace must be the story's role='feature' branch, or the assistant
           -- commits straight to trunk. Without this, the trunk row (created before
           -- the engine's feature branch) sorts first and the workspace clones `dev`.
           AND NOT (gr.kind = 'branch' AND gr.role = 'trunk')
         ORDER BY (sr.relation = 'produces') DESC, (gr.role = 'feature') DESC,
                  (gr.kind = 'branch') DESC, gr.created_at ASC
         LIMIT 1
        """,
        story_id, goal_id,
    )
    if not row:
        return None
    return dict(row)


async def resolve_repos_for_story(pool, goal_id: str, story_id: str) -> list[dict]:
    """Every repo this story's workspace should clone, write repos first.

    Multi-repo successor to `_resolve_repo_for_story` (plan M1.2). Each entry carries
    `is_write`: write repos (relation produces/modifies) get a branch and are committed
    from; context repos (relation 'needs') are cloned read-only for reference.

    IMPORTANT — this deliberately does NOT reuse git_graph.resolve_story_repos: the
    engine resolver falls back to the goal's sole repo when a story has no grant yet,
    but provisioning must NOT clone a bare repo — it lazy-waits for the engine-cut
    feature branch instead (see the note in repo_identity.resolve_story_repo). Cloning
    the fallback here would check out the trunk and the assistant would commit to it.

    Ordering: write repos before context repos, and within each group the same
    feature-branch-first ordering `_resolve_repo_for_story` uses — so element 0 is
    exactly what the single-repo resolver would have returned.

    Within the write group, relation='produces' ranks strictly above 'modifies'
    (2026-08-20 fix, mirrors `_resolve_repo_for_story` — see its docstring): a
    ripple-check sibling grant (grind.py's _spawn_engine_code_story) is 'modifies',
    not 'produces', specifically so it can never tie with — and outrank — the
    story's own repo on the old created_at tiebreak.
    """
    rows = await pool.fetch(
        """
        SELECT gr.id, gr.name, gr.kind, gr.uri, gr.clone_url, gr.branch_name,
               gr.repo_name, gr.repo_owner, gr.role, gr.base_branch,
               sr.relation
          FROM story_resources sr
          JOIN goal_resources gr ON gr.id = sr.resource_id
         WHERE sr.story_id = $1
           AND gr.goal_id = $2
           AND gr.status = 'active'
           AND gr.kind IN ('repo', 'branch')
           AND gr.repo_name IS NOT NULL AND gr.repo_name <> ''
           AND NOT (gr.kind = 'branch' AND gr.role = 'trunk')
         ORDER BY (sr.relation IN ('produces', 'modifies')) DESC,
                  (sr.relation = 'produces') DESC,
                  (gr.role = 'feature') DESC, (gr.kind = 'branch') DESC, gr.created_at ASC
        """,
        story_id, goal_id,
    )
    # Collapse to one entry per (repo, LANE), not per repo.
    #
    # Per repo was right about one thing and wrong about another. Right: a
    # story normally holds BOTH the bare repo grant and the engine-cut branch
    # for it, and those are one clone -- `group_clones` still folds those
    # together. Wrong: a story writing TWO LANES of one repository holds two
    # engine branches, and the second hit `continue` and was never cloned,
    # never recorded and never mentioned to the assistant. Two lanes are two
    # checkouts exactly as two repositories are, which is how the engine has
    # always treated them -- one node, one branch, per write target.
    resolved = group_clones([dict(r) for r in rows])
    for entry in resolved:
        entry["git_repo_id"] = canonical_repo_id(entry["repo_owner"],
                                                 entry["repo_name"])
        entry["is_write"] = is_write_relation(entry["relation"])
    resolved.sort(key=lambda e: not e["is_write"])
    _log.debug(
        "operator_workspace: story=%s resolved %d repo(s) to clone (%d write, %d context)",
        story_id, len(resolved),
        sum(1 for e in resolved if e["is_write"]),
        sum(1 for e in resolved if not e["is_write"]),
    )
    return resolved


def _clone_descriptors(primary: dict, extras: list[dict]) -> list[dict]:
    """The story's clone set as a single ordered list, primary first.

    ONE place computes the on-disk layout, because three consumers must agree on it
    exactly: the gateway RPC (what gets cloned where), `story_workspace_nodes` (what
    we record), and the agent-facing docs (what CLAUDE.md/IDENTITY.md tell the
    assistant). A disagreement between any two of them sends the assistant to a
    directory that isn't there.

    EVERY repo goes under `repos/<owner>__<name>`, single-repo stories included. The
    layout used to branch on repo count, which meant a story's directory MOVED the
    moment it was granted a second repo, invalidating every path already recorded
    against it. One shape for every story removes that transition entirely.

    THE ONE EXCEPTION IS A SECOND LANE OF THE SAME REPOSITORY, which has to go
    somewhere else because the name above is already taken -- it becomes
    `repos/<owner>__<name>@<lane>`. That is not a new transition of the kind the
    paragraph above removed: a story writing two lanes has two of them from its
    first provisioning, and a story writing one never sees a suffix at all. The
    rule lives in `story_clone_layout.assign_subdirs`, which is also what the
    execution pod's mount SQL says.

    This decides the layout for a NEW clone only. Stories already on disk keep what
    was recorded for them: `resolve_plan_root` prefers the recorded plan path (a
    story that has a plan keeps its plan root for life) and the local lane's
    `_resolve_subdir` returns `existing_subdir or intended`. Nothing relocates a
    directory that already exists.
    """
    # THE LANE DECIDES THE DIRECTORY when there is more than one of them, and
    # that is a property of the SET -- so it is computed over the whole list
    # before any descriptor is built, not per repo inside the loop. A
    # single-lane story is unchanged: `assign_subdirs` returns exactly
    # `repos/<owner>__<name>` for it, the path every existing workspace,
    # recorded node and plan root already uses.
    sources = [dict(primary, is_write=True)] + [dict(e) for e in extras]
    for src in sources:
        src.setdefault("relation", "produces" if src["is_write"] else "needs")
    assign_subdirs(sources)

    descriptors = []
    for source in sources:
        is_write = bool(source["is_write"])
        # (order normalised after the loop — see below)
        descriptors.append({
            "subdir": source["subdir"],
            "repo_owner": source["repo_owner"] or "",
            "repo_name": source["repo_name"] or "",
            "clone_url": source.get("clone_url") or source.get("uri") or "",
            # A read-only context repo has no cut branch of its own. 'main' is the
            # fallback rather than the primary's trunk — that trunk belongs to a
            # DIFFERENT repo and would not exist here.
            "branch_name": source["branch_name"] or "main",
            "is_write": is_write,
            "lane": source.get("lane") or "",
            "git_repo_id": canonical_repo_id(
                source["repo_owner"], source["repo_name"],
            ),
        })
    # Normalise to (write first, then subdir) — the SAME order
    # primary_write_subdir() reads back out of story_workspace_nodes. Without this the
    # docs could name one primary repo while a DB-backed lookup picked another, and
    # plans would be written somewhere nothing else looks.
    descriptors.sort(key=lambda d: (not d["is_write"], d["subdir"]))
    return descriptors


def _git_credential_broker_url() -> str:
    """Absolute /internal/git-credential URL for workspace credential helpers, or ''.

    Returns '' (meaning "use the legacy inline-token helper") unless brokering is
    enabled AND a public control-panel URL is configured. Both are required: the story
    VM is not the control panel, so it cannot infer the address, and installing a
    helper pointing at an unreachable URL would break git for every story.
    """
    settings = get_settings()
    if not settings.git_credential_broker_enabled:
        return ""
    base = (settings.control_panel_public_url or "").strip().rstrip("/")
    if not base:
        _log.warning(
            "git credential brokering is enabled but control_panel_public_url is "
            "unset — falling back to the inline-token helper",
        )
        return ""
    return f"{base}/internal/git-credential"


def token_repo_names_for(primary: dict, extras: list[dict]) -> list[str]:
    """Repo names the installation token must be scoped to, de-duplicated.

    generate_installation_token takes bare repo NAMES (not owner/name), which is
    fine because a single GitHub App installation is owner-scoped.
    """
    names = [primary["repo_name"]]
    for extra in extras:
        if extra["repo_name"] not in names:
            names.append(extra["repo_name"])
    return names


async def _record_story_workspace_nodes(
    pool, story_id: str, clones: list[dict],
) -> None:
    """Replace the story's story_workspace_nodes rows with `clones`.

    Delete-then-insert rather than upsert: provisioning re-clones the workspace from
    scratch every time, so a repo that is no longer granted must not leave a stale row
    pointing at a directory that no longer exists. Runs in one transaction so a reader
    never sees an empty node set for a story that has a live workspace.

    Best-effort by contract of the caller — a bookkeeping failure must not fail an
    otherwise-good workspace.
    """
    async with pool.acquire() as conn, conn.transaction():
        # Sticky primary (research/NREPO_LOCAL_EXECUTION_PLAN.md §4.8): decided ONCE,
        # never re-derived. `created_at`/`subdir` sorting disagreed across two
        # checkouts of the same story because a multi-write story's repos are created
        # in one batched transaction and tie on `created_at` — no sort key is actually
        # stable here. Read the existing decision (if any) BEFORE the delete below
        # removes it.
        #
        # KEYED BY (repo, BRANCH), because a repo id is not a clone. A story
        # writing two lanes of one repository has two rows sharing a
        # git_repo_id, and matching on that alone marked BOTH of them primary
        # -- which makes `primary_write_subdir` (is_primary DESC, then write
        # first, then subdir) pick whichever of the two sorts first, and it can
        # pick the other one next time. The story's plan root would move
        # between its two lanes across re-provisions, which is precisely the
        # instability this sticky decision exists to remove.
        existing_primary = await conn.fetchrow(
            "SELECT git_repo_id, branch_name FROM story_workspace_nodes "
            "WHERE story_id = $1 AND is_primary LIMIT 1",
            story_id,
        )
        primary_key = (tuple(existing_primary) if existing_primary else None)
        # The existing primary might not be among the new clones at all (that repo's
        # grant was revoked, or that lane is no longer written) — fall back to
        # deciding fresh rather than recording a story with no primary at all.
        if primary_key and not any(
            (c["git_repo_id"], c["branch_name"]) == primary_key for c in clones
        ):
            primary_key = None
        await conn.execute(
            "DELETE FROM story_workspace_nodes WHERE story_id = $1", story_id,
        )
        for i, clone in enumerate(clones):
            if primary_key:
                is_primary = (clone["git_repo_id"],
                              clone["branch_name"]) == primary_key
            else:
                is_primary = i == 0  # first-ever record (or the old primary is gone): decide now
            await conn.execute(
                """INSERT INTO story_workspace_nodes
                       (story_id, git_repo_id, branch_name, subdir, is_write,
                        repo_owner, repo_name, is_primary, trunk_branch)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   ON CONFLICT (story_id, git_repo_id, branch_name) DO UPDATE
                     SET subdir       = EXCLUDED.subdir,
                         is_write     = EXCLUDED.is_write,
                         repo_owner   = EXCLUDED.repo_owner,
                         repo_name    = EXCLUDED.repo_name,
                         is_primary   = EXCLUDED.is_primary,
                         trunk_branch = EXCLUDED.trunk_branch,
                         updated_at   = now()""",
                story_id,
                clone["git_repo_id"],
                clone["branch_name"],
                clone["subdir"],
                clone["is_write"],
                clone["repo_owner"] or "",
                clone["repo_name"] or "",
                is_primary,
                # THE DIFF BASE, PER CLONE. The column has existed since the
                # table did and `local_hydrate` has always served it to the
                # extension as `trunk` -- but nothing ever wrote it, so every
                # row in both lanes carried '' and the local review prompt
                # fell through to "if you cannot establish the trunk, say
                # so". See `workspace_trunks.annotate_trunks`.
                clone.get("trunk_branch") or "",
            )
    _log.info(
        "operator_workspace: recorded %d workspace node(s) for story=%s (%s)",
        len(clones), story_id,
        ", ".join(f"{c['subdir']}@{c['branch_name']}" for c in clones),
    )


async def primary_write_subdir(pool, story_id: str) -> str:
    """Workspace-relative dir holding a story's plans and scope.

    'repo' for a single-repo/legacy workspace — which is what every caller assumed
    literally before multi-repo, and why this exists: the plan-path gate
    (internal_assistant.validate_plan) and the plan push (plan_approval) both hardcoded
    `repo/`, so under a multi-repo layout the assistant's plan write was REJECTED and an
    approved plan was never committed. Neither is reachable with multi_repo_enabled off,
    but both make multi-repo planning impossible.

    Ordering matches `_clone_descriptors` exactly (write repos first, then subdir) so the
    directory named here is the same one CLAUDE.md/RESOURCES.md tell the agent to use.
    Falls back to 'repo' on any error, preserving pre-multi-repo behaviour.
    """
    # The resolver already orders write-repos-first then subdir, so the first WRITE clone
    # is the same row the old `WHERE is_write ORDER BY subdir ASC LIMIT 1` returned. That
    # shared ordering is what keeps this function's answer identical to the directory
    # `_clone_descriptors` created and CLAUDE.md advertises.
    writes = write_clones(await resolve_workspace_clones(pool, story_id))
    return (writes[0]["subdir"] if writes else "repo") or "repo"


def repo_clone_subdir(owner: str, name: str) -> str:
    """Workspace-relative directory for one repo's clone: `repos/<owner>__<name>`.

    A flat, collision-free name (two owners can share a repo name) that is also a
    valid single path segment. The doubled underscore separates owner from name
    without colliding with the single underscores GitHub allows inside either part.
    Agent-facing docs (CLAUDE.md / RESOURCES.md) must describe this exact layout.
    """
    return f"repos/{(owner or '').strip()}__{(name or '').strip()}"


# ── Scaffolding content builders ───────────────────────────────────────────

def _build_default_settings_json() -> str:
    """Settings written into <story-workspace>/.claude/settings.json.

    Permission model:
      - allow list covers normal coding tools (Read/Edit/Write/Grep/Glob)
        plus a constrained Bash whitelist (no network, no sudo, no rm).
      - deny list ONLY targets sensitive file patterns (.env, secrets,
        .ssh) and dangerous bash verbs (sudo, curl, wget, ssh, scp).
        Earlier broader denies like `Read(//home/**)` blocked the
        workspace itself (which lives under /home/openclaw/...), so
        path-bounding is now the PreToolUse hook's job — it has the
        workspace root in env and can do realpath-based validation.
    """
    return json.dumps({
        "permissions": {
            "allow": [
                "Read", "Grep", "Glob", "Edit", "Write",
                # Claude's own built-in research + planning tools.
                # WebSearch and WebFetch let Claude check current library
                # versions, framework conventions, security advisories, and
                # API shapes during planning + coding. TodoWrite is its
                # internal scratchpad for multi-step phase tasks (doesn't
                # touch the workspace). All three are sandboxed by Claude
                # Code — WebFetch is GET-only, WebSearch returns text only,
                # TodoWrite is in-memory.
                "WebSearch", "WebFetch", "TodoWrite",
                "Bash(git:*)", "Bash(pytest:*)", "Bash(python:*)",
                "Bash(python3:*)", "Bash(pip:*)", "Bash(pip3:*)",
                "Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)", "Bash(pnpm:*)",
                "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(head:*)",
                "Bash(tail:*)", "Bash(wc:*)", "Bash(find:*)", "Bash(echo:*)",
                "Bash(mkdir:*)", "Bash(touch:*)", "Bash(pwd)", "Bash(true)",
                "Skill", "Agent",
            ],
            "deny": [
                "Read(/**/.env*)", "Read(/**/secrets/**)", "Read(/**/.ssh/**)",
                "Read(/**/.gnupg/**)", "Read(/**/.aws/**)",
                "Bash(sudo:*)", "Bash(rm:-rf /*)",
                "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)",
                "Bash(nc:*)", "Bash(ncat:*)",
            ],
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write|Read|Bash",
                    "hooks": [{
                        "type": "command",
                        "command": local_dev.hook_command("restrict-to-workspace.sh"),
                    }],
                },
                # Plan-file DAG validation. On every Write/Edit, the hook
                # POSTs the proposed content to /internal/shca/validate-plan;
                # malformed Task DAG YAML or wrong-location plan writes are
                # rejected with the canonical schema in the error so Claude
                # self-corrects in-turn. Fails open on any infra trouble —
                # DB-side ingest still validates at plan-approval time.
                {
                    "matcher": "Edit|Write",
                    "hooks": [{
                        "type": "command",
                        "command": local_dev.hook_command("validate-plan-dag.sh"),
                    }],
                },
                # Kill-switch pause gate. Runs before EVERY tool call during
                # a managed phase turn (AGENT_TASK_ID is set). If the system or
                # goal is paused via the kill switch, exits 2 so Claude sees a
                # tool block and must output PHASE_TASK_BLOCKED — stopping the
                # turn at the next tool boundary without losing in-flight work.
                # Fails open (exit 0) on any network or infrastructure error so
                # a slow control panel never blocks unrelated tool calls.
                {
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": local_dev.hook_command("pause-check.sh"),
                    }],
                },
            ],
            # PostToolUse fires after every tool call. The hook posts a
            # small progress event to the control panel so the SHCA chat
            # can show "🔍 Reading X" / "✏️ Editing Y" cards live —
            # otherwise the UI is dead-air during long Claude turns.
            "PostToolUse": [{
                "matcher": "Edit|Write|Read|Glob|Grep|Bash|NotebookEdit",
                "hooks": [{
                    "type": "command",
                    "command": local_dev.hook_command("claude-tool-progress.sh"),
                }],
            }],
            # Stop fires when the phase turn ends. enforce-sentinel.sh checks the
            # final assistant message ends with EXACTLY one PHASE_TASK_* sentinel on
            # its own last line; if missing it exits 2 to block the stop and make
            # Claude re-emit it (Phase 2a — kills off-protocol/buried-sentinel at the
            # source instead of the 40-min janitor). FAILS OPEN on any error and
            # gives up after one forced retry (stop_hook_active) so it can never wedge
            # a turn; a genuinely missing sentinel then falls through to the
            # dispatcher's off-protocol→failed path.
            "Stop": [{
                "hooks": [{
                    "type": "command",
                    "command": local_dev.hook_command("enforce-sentinel.sh"),
                }],
            }],
        },
    }, indent=2) + "\n"


def _build_plans_index_md() -> str:
    return (
        "# Plan index\n\nNo plans yet.\n\n"
        "Each plan version lives at `v<N>.md`. The current version is\n"
        "tracked in the `plan_versions` table; this index mirrors it for humans.\n"
    )


def _build_decisions_md() -> str:
    return (
        "# Decision log\n\n"
        "Append-only. Format: `YYYY-MM-DD HH:MM (actor): decision`.\n\n---\n"
    )


def _build_code_notes_md(story_id: str) -> str:
    return (
        f"# Code notes for story {story_id}\n\n"
        "Coding work appends notes here as it progresses. Used for plan "
        "handoff and replan context.\n"
    )


async def _build_resources_md_for_story(
    pool, goal_id: str, story_id: str, agent_id: str, github_token: str,
    clones: Optional[list[dict]] = None, no_checkout: bool = False,
) -> str:
    """Generate RESOURCES.md for the story workspace using the existing
    resource_provisioning machinery. We reuse the same generator that
    populates SHCA's own workspace RESOURCES.md — same content shape,
    just targeted at the story workspace."""
    from app.resource_provisioning import generate_resources_md
    # Operator/claude-code story workspaces have the repo pre-cloned at
    # `<story-workspace>/repo` by shca.workspace.setup — RESOURCES.md must tell
    # Claude the repo is already at `repo/` (cwd = story workspace root) and to
    # keep all work under `repo/` and NOT self-clone (a second checkout is
    # invisible to the diff/commit-review gate, which only reads `repo/`).
    return await generate_resources_md(
        agent_id=agent_id,
        goal_id=goal_id,
        github_token=github_token,
        story_id=story_id,
        repo_precloned=True,
        clones=clones,
        no_checkout=no_checkout,
    )


def _primary_clone_dir(clones: Optional[list[dict]]) -> str:
    """Workspace-relative dir of the repo the story primarily writes to.

    Defaults to the legacy `repo` when no clone set was passed, so callers that have
    not been migrated (and every single-repo story) keep the historical wording.
    """
    if not clones:
        return "repo"
    for clone in clones:
        if clone.get("is_write"):
            return clone["subdir"]
    return clones[0]["subdir"]


def _clone_layout_lines(clones: Optional[list[dict]]) -> list[str]:
    """Agent-facing bullet list of every clone, write status made explicit.

    Returns [] for the single-repo case so the existing docs read exactly as before —
    a lone "the repo is at repo/" line is clearer than a one-item table.
    """
    if not clones or len(clones) <= 1:
        return []
    # A LANE COLUMN WHEN THERE ARE LANES, and only then. Two checkouts of ONE
    # repository are told apart by where they merge — `backend` and `frontend`,
    # the names the person used — not by the branch, which is the engine's
    # derived `feat/<goal>-<story>-<lane>` and appears nowhere in the brief. A
    # table whose Repository column says the same thing twice, with two branch
    # names nobody has seen before, is a table the model has to guess from.
    lanes = any(c.get("lane") for c in clones)
    head = "| Directory | Repository | Branch | Access |"
    rule = "|---|---|---|---|"
    if lanes:
        head = "| Directory | Repository | Merges into | Branch | Access |"
        rule = "|---|---|---|---|---|"
    lines = ["", "## Checkouts in this workspace", "", head, rule]
    for clone in clones:
        access = "**write**" if clone.get("is_write") else "read-only (context)"
        lane = f" `{clone['lane']}` |" if lanes else ""
        if lanes and not clone.get("lane"):
            lane = " (the repo's trunk) |"
        lines.append(
            f"| `{clone['subdir']}/` | {clone['repo_owner']}/{clone['repo_name']} "
            f"|{lane} `{clone['branch_name']}` | {access} |"
        )
    lines += [
        "",
        "- Commit ONLY inside the **write** checkouts. A read-only one is cloned so "
        "you can read its code for context — never edit or commit there.",
        "- Each row is a separate git checkout: run git commands with `cd` into that "
        "directory (there is no top-level repo).",
    ]
    if lanes:
        lines.append(
            "- Two rows may name the SAME repository. They are different branches "
            "of it, kept in different directories on purpose — work for one lane "
            "goes in that lane's directory and nowhere else. They land together "
            "as one merge group, so a change split across the wrong two "
            "directories merges as two unrelated edits."
        )
    return lines


async def _build_identity_md_for_story(
    pool, goal_id: str, story_id: str, clones: Optional[list[dict]] = None,
) -> str:
    """Operator-mode IDENTITY.md. Tells Claude Code which story it's
    working on. The file is written into the story workspace, so the
    workspace path is implicit (`pwd` resolves it).

    `clones` is the story's clone set from `_clone_descriptors`. When it holds more
    than one repo the layout wording switches to the multi-repo `repos/<..>/` form;
    when it is None or single, the text is unchanged from the single-repo original.
    """
    story = await pool.fetchrow(
        "SELECT title, description, acceptance_criteria FROM stories WHERE id = $1",
        story_id,
    )
    if not story:
        return f"# Identity\n\nStory: {story_id}\n"

    goal = await pool.fetchrow(
        "SELECT name, description FROM goals WHERE id = $1", goal_id,
    )
    goal_title = (goal["name"] if goal else "") or "(unknown)"
    _repo_dir = _primary_clone_dir(clones)
    _is_multi = bool(clones) and len(clones) > 1

    lines = [
        "# Identity",
        "",
        f"**Story:** {story['title']}",
        f"**Story ID:** `{story_id}`",
        f"**Goal:** {goal_title}",
        f"**Goal ID:** `{goal_id}`",
        "",
        "You are running inside the story's workspace directory.",
        (
            f"Run `pwd` if you need the absolute path. The git repo is at `./{_repo_dir}`."
            if not _is_multi else
            # "repositories" was wrong for the case this sentence is most
            # needed in: two branches of ONE repository. Saying so sends the
            # model looking for a second repo, finds one directory named after
            # the repo it already has, and the obvious conclusion is that
            # something is misconfigured.
            "Run `pwd` if you need the absolute path. This story has MORE THAN ONE "
            f"checkout under `./repos/` — different repositories, or different "
            f"branches of one — and you primarily write to `./{_repo_dir}` "
            f"(see the table below)."
        ),
        "",
        "## Mission",
        story["description"] or "_(no description provided)_",
    ]
    if story["acceptance_criteria"]:
        lines += [
            "",
            "## Acceptance criteria",
            story["acceptance_criteria"],
        ]
    lines += _clone_layout_lines(clones)
    _other_write_subdirs_id = (
        [c["subdir"] for c in write_clones(clones) if c["subdir"] != _repo_dir]
        if _is_multi else []
    )
    lines += [
        "",
        "## Working notes",
        "- All file edits must be inside this workspace.",
        (
            f"- The repo is at `{_repo_dir}/` — `cd {_repo_dir}` for git operations."
            if not _is_multi else
            f"- Your primary (write) repo is `{_repo_dir}/` — `cd {_repo_dir}` for git "
            "operations. Other repos each have their own checkout; see the table above."
        ),
        f"- Plans live at `{_repo_dir}/stories/<story_id>/plans/cycle-NN-<slug>.md` — one per planning cycle (NN = 01, 02, …), inside the repo so they ship to git.",
        f"- Story-level scope lives at `{_repo_dir}/stories/<story_id>/scope.md`.",
        *(
            [
                "- **Multi-repo: copy both files identically into every OTHER write repo** "
                "at the same relative path — " +
                ", ".join(f"`{s}/stories/<story_id>/`" for s in _other_write_subdirs_id) +
                " — so each write repo's own git history carries the plan too."
            ] if _other_write_subdirs_id else []
        ),
        "- `docs/DECISIONS.md` is an append-only log (workspace scratch, NOT committed).",
        "- `docs/CODE_NOTES.md` is your scratchpad as you work (workspace scratch, NOT committed).",
        "- See `CLAUDE.md` at the workspace root for the canonical operator rules — it's auto-loaded by Claude Code every turn.",
    ]
    return "\n".join(lines) + "\n"


async def _build_claude_md_for_story(
    pool, goal_id: str, story_id: str, clones: Optional[list[dict]] = None,
) -> str:
    """Workspace-root CLAUDE.md — Claude Code auto-reads it every turn.

    This is the operator system prompt for the Claude Code session. It
    spells out the story metadata, workspace layout, the active phase
    registry (queried live from claude_phases), per-phase behaviour rules,
    and the Task DAG schema with explicit rejection examples.

    Written at provisioning time and NEVER updated by Claude — the
    PreToolUse validator hook rejects writes to this file.
    """
    story = await pool.fetchrow(
        "SELECT title FROM stories WHERE id = $1", story_id,
    )
    goal = await pool.fetchrow(
        "SELECT name FROM goals WHERE id = $1", goal_id,
    )
    story_title = (story and story["title"]) or "(unknown story)"
    goal_title = (goal and goal["name"]) or "(unknown goal)"

    # Fetch active phases dynamically so the CLAUDE.md is always in sync
    # with whatever phases are registered in the DB — no code change needed
    # when phases are added or disabled via the admin UI.
    active_phases = await pool.fetch(
        "SELECT name, description FROM claude_phases WHERE status = 'active' ORDER BY name",
    )
    _phase_rows = "\n".join(
        f"| `{p['name']}` | {p['description']} |" for p in active_phases
    )
    _phase_table = f"| Phase | What it is |\n|---|---|\n{_phase_rows}"
    _phase_names = ", ".join(f"`{p['name']}`" for p in active_phases)
    _phase_count = len(active_phases)

    # Layout wording: single-repo stories keep the historical `repo/` text verbatim;
    # multi-repo stories get the real per-repo tree plus an explicit write/read-only
    # table, because an assistant told "the repo is at repo/" in a workspace that has
    # no `repo/` dir will fail its very first git command.
    _repo_dir = _primary_clone_dir(clones)
    _is_multi = bool(clones) and len(clones) > 1
    # The primary repo's subtree is spelled out in both cases (it is where plans and
    # scope live); in the multi-repo case the sibling clones are listed after it.
    _primary_subtree = (
        f"│   ├── stories/\n"
        f"│   │   └── {story_id}/    ← THIS story's subdir (you create + own this)\n"
        f"│   │       ├── scope.md             ← story-level scope (lives across all features)\n"
        f"│   │       └── plans/\n"
        f"│   │           └── cycle-NN-<slug>.md ← one plan per cycle (NN=01,02,…), frozen after approval\n"
        f"│   ├── src/, tests/, etc.            ← the project's own code\n"
        f"│   └── ..."
    )
    # 2026-08-20: the plan/scope must exist in EVERY write repo, not just the
    # primary — confirmed live that a review/approval flow touching a sibling
    # write repo (e.g. a code-reviewer task scoped to it) had no plan/scope file
    # to find there at all, since only `_repo_dir` ever got one. Write once,
    # then copy identically into every other write repo's own tree — same
    # relative path, so each repo's git history carries it independently.
    _write_subdirs = [c["subdir"] for c in write_clones(clones)] if _is_multi else []
    _other_write_subdirs = [s for s in _write_subdirs if s != _repo_dir]

    if _is_multi:
        _other = [c for c in clones if c["subdir"] != _repo_dir]
        _repo_tree = "\n".join([
            f"├── {_repo_dir}/   ← PRIMARY write repo. Write plan/scope here FIRST,",
            f"│   │                then copy them identically into every other write repo.",
            _primary_subtree,
            *[
                f"├── {c['subdir']}/   ← {c['repo_owner']}/{c['repo_name']} @ {c['branch_name']} "
                f"({'write — commit here too, AND gets its own copy of plan/scope' if c.get('is_write') else 'READ-ONLY context, never commit'})"
                for c in _other
            ],
        ])
        _repo_layout_note = (
            f"\n**This story spans {len(clones)} repositories.** Each is a separate git\n"
            f"checkout under `repos/` — there is NO top-level `repo/` directory here.\n"
            f"Commit only inside repos marked *write*; read-only repos are cloned so you\n"
            f"can read their code for context. `cd` into a repo before running git.\n"
            + (
                f"\n**Plan/scope go in EVERY write repo, identically** — not just "
                f"`{_repo_dir}/`. After writing `stories/{story_id}/scope.md` and the "
                f"plan under `{_repo_dir}/`, copy both files byte-for-byte into the same "
                f"relative path in each of: {', '.join(f'`{s}/`' for s in _other_write_subdirs)}. "
                "Each write repo's own git history should carry the plan independently — a "
                "review or task scoped to a sibling repo has nowhere else to find it.\n"
                if _other_write_subdirs else ""
            )
        )
    else:
        _repo_tree = "\n".join([
            f"├── {_repo_dir}/                ← the git checkout (single branch). Everything",
            f"│   │                      that needs to be committed must live here.",
            f"│   │                      Source files, plans, scope — all under {_repo_dir}/.",
            _primary_subtree,
        ])
        _repo_layout_note = ""

    # Triple-backtick handling: this content includes ```yaml fences, so
    # we use a Python triple-quoted string and write it out verbatim.
    return f"""# CLAUDE.md — operator workspace context

You are a Claude Code session running inside a story workspace. This file
is your project instructions — Claude Code reads it automatically on every
turn. **DO NOT edit, delete, or modify this file.** It's managed by the
control panel; your changes here will be overwritten and may cause the
operator system to misroute you.

---

## Story you are working on

- **Story id**: `{story_id}`
- **Story title**: {story_title}
- **Goal id**: `{goal_id}`
- **Goal**: {goal_title}

---

## Workspace layout

Your working directory — and the value of `$CLAUDE_PROJECT_DIR` — is **this
story's** workspace root (i.e. `…/stories/{story_id}`). Every path in the
layout below is relative to it:
{_repo_layout_note}
```
<workspace_root>/        ← your cwd
{_repo_tree}
├── docs/                ← workspace-level scratch (NOT in the git repo)
│   ├── CODE_NOTES.md    ← append-only notes you can write during execution
│   ├── DECISIONS.md     ← append-only decision log
│   └── (no plans subfolder — plans live under {_repo_dir}/stories/{story_id}/plans/)
├── IDENTITY.md          ← workspace metadata (read-only for you)
├── RESOURCES.md         ← project resources + git credentials (read-only for you)
├── CLAUDE.md            ← THIS file (do not edit)
├── .claude/             ← Claude Code settings, hooks, agents
└── skills/              ← SHCA-only; you do not read these
```

Files outside `{_repo_dir}/` (like `docs/CODE_NOTES.md`) are **workspace scratch** —
useful for your own notes between turns, but they are NOT committed to git.
Anything you want preserved in version control MUST go inside `{_repo_dir}/`.

> **Writing the plan — the one path mistake to avoid.** The plan's absolute path
> is `$CLAUDE_PROJECT_DIR/{_repo_dir}/stories/{story_id}/plans/cycle-NN-<slug>.md`.
> Because `$CLAUDE_PROJECT_DIR` is ALREADY `…/stories/{story_id}`, that full path
> contains `stories/{story_id}` **twice** — once as your workspace dir, once
> inside `{_repo_dir}/`. Both are required; do NOT collapse them into one. And do NOT
> rebuild the path from a `workspace-…` directory you may see in `pwd` output:
> that outer `workspace-…` dir is the agent's home, one level ABOVE
> `$CLAUDE_PROJECT_DIR` — it is NOT your root. Anchoring the plan there
> (`…/workspace-…/{_repo_dir}/stories/{story_id}/plans/…`) writes into a phantom repo
> the git checkout never sees, and the validator hook will reject it.
{(
    f"""
> **Multi-repo: copy the plan/scope into every write repo.** Write them once
> under `{_repo_dir}/` (above), then copy both files identically into the same
> relative path under each of: {', '.join(f'`{s}/`' for s in _other_write_subdirs)}.
> A review or task scoped to a sibling write repo has no other way to find them.
""" if _other_write_subdirs else ""
)}
---

## The {_phase_count} phases (the only valid `phase` values for a Task DAG)

{_phase_table}

Do NOT invent phase names (`scaffold`, `markup`, `styling`, `verification`,
`developer`, `qa`, etc.). The DAG parser will reject anything else. Bundle
work that doesn't fit into a `coder` task with a clear description instead.

---

## Phase-based behavior

The control panel manages the story's current phase. You can ALWAYS read
the current phase from the most recent `story_state_snapshot` block in the
conversation (added by the control panel before each human message).

### `planning` phase

You are PLANNING, not implementing. The ONLY files you may write/edit:

- `{_repo_dir}/stories/{story_id}/scope.md` (create or update)
- `{_repo_dir}/stories/{story_id}/plans/cycle-NN-<slug>.md` (NEW file per planning cycle — NN=01,02,…)
{(
    f"- The SAME two files, byte-for-byte, at the same relative path under each "
    f"of: {', '.join(f'{s}/stories/{story_id}/' for s in _other_write_subdirs)} "
    "(multi-repo — every write repo needs its own copy)\n"
    if _other_write_subdirs else ""
)}
You may NOT:
- Write or edit source code (`.html`, `.js`, `.py`, `.css`, anything in `{_repo_dir}/src/`, `{_repo_dir}/tests/`, etc.)
- Run builds, tests, or any command that modifies the workspace outside the plan files
- Stage or commit anything

The PreToolUse validator hook enforces containment within this story's workspace
root — it does not itself pick a specific repo, so writing scope/plan into any
write repo's own `stories/{story_id}/` (primary or sibling) is allowed. Implementation happens AFTER
the human approves your plan.

### `awaiting_plan_approval` / `awaiting_review` / `awaiting_next` phases

Wait. Do not write anything. The human is reviewing or deciding.

### `executing` phase

The control panel will dispatch one task at a time as a `[DISPATCH] Task T<id>`
message. ONLY when you see a `[DISPATCH]` line are you authorized to implement.

Each `[DISPATCH]` reply must end with EXACTLY one sentinel:
- `PHASE_TASK_DONE T<id> — <one-line summary>` (success)
- `PHASE_TASK_BLOCKED T<id> — <one specific question>` (need input)
- `PHASE_TASK_FAILED T<id> — <one-line failure reason>` (hard error)

Do NOT commit or push during a phase task — the control panel handles git
commits at the diff-review gate after all tasks complete.

### `discovery` phase

Ask the human 2–4 SCOPE/BEHAVIOR clarifying questions. Don't ask
implementation-flavored questions ("should I touch file X"). Iterate
until the scope is clear, then move to planning.

---

## 🚨 Task DAG — the absolute non-negotiable format

The Task DAG is the LAST section of every plan file. The control panel
parses it literally to ingest your task list. Any drift from this format
breaks ingestion — the parser will reject your plan, the human will see
an error on approval, and you will have to fix and re-submit.

### Step 1: heading

EXACTLY this, on its own line:

`## Task DAG`

NOT `### Task DAG`. NOT `## DAG`. NOT `## Task DAG:`. NOT `## Tasks`.
Case-insensitive, but must be an h2 heading.

### Step 2: fenced YAML block

Immediately after the heading (optional blank line OK), a fenced code
block with the `yaml` language tag:

````
```yaml
tasks:
  - id: T1
    phase: coder
    title: One-line title
    description: |
      Multi-line description. Concrete, scoped, executable in one
      Claude turn. Include file paths, function names, what to change.
    acceptance: "One-line acceptance criterion as a quoted string."
    depends_on: []

  - id: T2
    phase: unit-tester
    title: Add unit tests for the new behavior
    description: |
      Cover the happy path and at least two edge cases.
    acceptance: "All tests pass."
    depends_on: [T1]

  - id: T3
    phase: integration-tester
    title: Run integration tests against the live service
    description: |
      Start the server, call the new endpoint with real HTTP requests,
      assert on response codes and body shapes per acceptance criteria.
    acceptance: "All integration tests pass against the running service."
    depends_on: [T1, T2]

  - id: T4
    phase: code-reviewer
    title: Review the diff
    description: |
      Confidence-scored review of the staged changes.
    acceptance: "No blocking issues."
    depends_on: [T1, T2, T3]

  - id: T5
    phase: deployment
    title: Deploy to staging
    description: |
      Run the deployment script and verify the service is healthy
      post-deploy.
    acceptance: "Service responds 200 on /health after deploy."
    depends_on: [T4]
```
````

Triple-backtick fence with `yaml` language tag. Plain triple-backticks
without the language tag WILL be rejected.

### Step 3: hard rules — the parser rejects ANY violation

Schema of each task — all five fields REQUIRED and non-empty:

| Field | Type | Format |
|---|---|---|
| `id` | string | Pattern `^T\\d+$` — `T1`, `T2`, `T10`. NEVER `1`, `task-1`, `T-1`. |
| `phase` | string | EXACTLY one of: {_phase_names}. Nothing else. |
| `title` | string | One short line. |
| `description` | string | Use YAML `\\|` block scalar for multi-line. Concrete and scoped. |
| `acceptance` | string | One quoted line. NEVER a list. |
| `depends_on` | list of strings | YAML list `[]`, `[T1]`, `[T1, T2]`. NEVER a string like `"T1, T2"`. Each entry must match `T\\d+` AND reference an existing task in this DAG. |

Structural rules:
- Top-level mapping with one key `tasks:` whose value is a non-empty YAML list
- Flat list ONLY — no nested subtasks under a task
- No extra fields beyond the six listed above (extras silently ignored, but stick to schema)
- No self-references (`T1` can't depend on `T1`)
- No cycles in the dependency graph
- No inline backticks inside YAML scalar values — they break the parser. Use plain text or double-quoted strings.

### Step 4: things that LOOK like a DAG but ARE NOT and WILL be rejected

The parser rejects all of these. Do not write them.

**❌ Text/ASCII diagrams** — looks pretty, parser cannot read it:
```
T1 → T2 → T3 → T4
         │
         └→ (T2/T3 may iterate as bugs surface)
```

**❌ Prose lists** — readable but not YAML:
```
T1 — Scaffold. Create src/calculator/ with three empty files.
T2 — Markup. Semantic HTML structure with button grid.
T3 — Styling. CSS grid with responsive breakpoints.
```

**❌ Markdown checklists** — not YAML at all:
```
- [ ] T1: Scaffold the structure
- [ ] T2: Add markup
```

**❌ Numbered headings** — looks like structure but parser doesn't see it:
```
### T1. Scaffold
### T2. Markup
```

**❌ Invented phase names** — parser only knows the registered phases:
```yaml
tasks:
  - id: T1
    phase: scaffold       # REJECTED — not a valid phase
  - id: T2
    phase: markup         # REJECTED
  - id: T3
    phase: developer      # REJECTED
```

**❌ `depends_on` as a string**:
```yaml
depends_on: "T1, T2"   # REJECTED — must be a YAML list [T1, T2]
```

**❌ Missing the YAML language tag on the fence**:
````
## Task DAG
```                 # REJECTED — needs ```yaml
tasks:
  ...
```
````

### Step 5: standard tail phases

The end of every DAG follows this sequence — include the phases that apply
to the story's scope:

1. **`integration-tester`** — runs the actual deliverable and validates it
   against acceptance criteria. Include for any story that produces or
   modifies runnable code — logic changes, logging, config wiring,
   app-level env var changes, or anything that affects executed code paths.
   Skip only for pure documentation or pure infrastructure files
   (e.g. cloudbuild.yaml, k8s manifests) that do not affect the running
   application.

2. **`code-reviewer`** — diff quality gate. **Always required.** This is the
   last non-deployment task. The diff-review card surfaces its summary to the
   human. Without it, the review card is empty.

3. **`deployment`** — deploy to staging/production. Decision flow:

   **Step 3a — scan the repo for CI/CD config** before writing the DAG. Look
   for any of these paths/patterns that already handle deploys:
   - `.github/workflows/` (GitHub Actions)
   - `cloudbuild.yaml` / `cloudbuild-*.yaml` (Google Cloud Build)
   - `.gitlab-ci.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml`
   - `fly.toml`, `vercel.json`, `netlify.toml`, `render.yaml`
   - `scripts/deploy*`, `Makefile` targets named `deploy`/`release`/`publish`

   **Step 3b — pick one of three cases**:
   - **(a)** Story scope explicitly requires a deploy step AND the human
     asked for one → include `deployment`.
   - **(b)** CI/CD config is present and the story's changes will be
     deployed automatically on merge → **do NOT silently omit `deployment`**.
     First tell the human in chat: *"I see CI/CD at `<path>` that auto-deploys
     on merge to `<branch>`. I'm planning to omit a `deployment` task from
     the DAG since CI handles it — OK?"* Wait for human confirmation, then
     proceed.
   - **(c)** No CI/CD config and no explicit deploy ask → omit `deployment`
     (default safe choice for library/script changes).

   Never silently skip `deployment` in case (b) — the human must acknowledge.

`code-reviewer` always depends on all coder + tester tasks. `deployment`
always depends on `code-reviewer`. `integration-tester` depends on coder
(and unit-tester if present) and is depended on by `code-reviewer`.

### Task descriptions are phase-scoped — never blend phase techniques

Each task's `description` must speak in the language of *that* phase's role.
Don't carry scaffolding from one phase into another. The validators downstream
enforce their own rules and will BLOCK a task whose description tells them
to do something their role forbids.

| Phase                | OK to mention                                                                                                | NOT OK to mention                                                                            |
|----------------------|--------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| `unit-tester`        | pytest / jest / vitest, mocks, fakes, monkey-patching, PYTHONPATH shadow modules, `conftest.py`, test fixtures | n/a — full unit-test toolbox is allowed                                                      |
| `integration-tester` | acceptance criteria, expected stdout/stderr, expected exit codes, real-service endpoints, bucket/object names | mocks, fakes, monkey-patching, PYTHONPATH shadow modules, `pytest` as the validation method, test files |
| `code-reviewer`      | diff inspection focus areas, style concerns, edge-case checks                                                | running tests, modifying source, deploying                                                   |
| `deployment`         | deploy command/path, post-deploy smoke check, version verification                                           | unit/integration validation logic, mocks                                                     |

If you catch yourself writing "use a fake X for scenario Y" inside an
**integration-tester** task, you're mis-routing it. Move that scaffolding to
a separate `unit-tester` task; leave the integration-tester task to validate
acceptance criteria against the real running deliverable.

### `integration-tester` tasks must enumerate runtime fixtures

Before writing the task description, list every value the validator will need
to exercise each acceptance criterion:

- bucket / database / API endpoint identifiers it'll connect to
- object keys / table names / URL paths
- expected response snippets, exit codes, stderr lines
- sample inputs (file contents, request payloads, command-line args)
- environment variables beyond the injected credentials

If any value is unknown — for example *"which GCS bucket should we test
against?"* or *"what's the name of a PDF object that exists in that
bucket?"* — **ask the human in chat during planning** and embed the answer
in the task description. Do not leave fixtures to be discovered by the
validator at execution time — that triggers an avoidable BLOCKED-iteration
loop where the operator has to round-trip every missing value.

Things the validator can legitimately derive at runtime (a UUID for a
known-missing object name, a timestamp suffix, a unique tmp file path)
don't need to be enumerated.

### Cloud resources are GO's responsibility — request, don't create

If the deliverable touches a cloud SDK (`google-cloud-*`, `boto3`, `azure-*`)
or runs `gcloud` / `aws` / `az` commands, the corresponding phase tasks
will need cloud resources (test SA, deploy SA, environment context).
**You do not register these yourself.** GO holds the authority to add
goal resources; you just request.

When you detect the need during planning:

1. Identify the provider from imports / SDK usage (GCP, AWS, Azure, ...).
2. Check whether the goal already has the resource (look at RESOURCES.md
   or call `resource_list`).
3. If missing, send a request via `message_go(type='request')` with
   a clear ask, e.g.:

       *"Story `<id>` needs a `gcp_test_sa` (integration-tester). Detected
       cloud: GCP (deliverable imports `google-cloud-storage`). Please
       attach one to env `<env-name>` (or create the env first if it
       doesn't exist)."*

4. Stop and wait. The GO will ask the human for the SA email (and create
   the parent env first if needed), then post back to the orchestrator
   channel: *"`gcp_test_sa` attached as `<id>`"*. Resume planning once
   that arrives.

Never call `register_resource` for cloud kinds (`gcp_*`, `aws_*`,
`azure_*`) yourself. Never fabricate an SA email, role ARN, project ID,
or other identifier — the human is the only source of truth, and GO is
the only entity allowed to register the resource.

### Typical DAG size

3–8 tasks for a typical feature. If you find yourself needing more than 8,
the feature is probably too large for one cycle — split it into multiple
plans (one per feature/cycle), each with its own smaller DAG.

---

## What you DON'T do

- Don't edit this CLAUDE.md (it's managed by the control panel)
- Don't write plans anywhere except `{_repo_dir}/stories/{story_id}/plans/`
- Don't write or commit code during planning
- Don't invent task phase names beyond the valid phases listed above
- Don't spawn subagents (no `Task` tool calls)
- Don't transition story phase yourself (the control panel and SHCA handle that)
"""


# ── Provisioning orchestration ─────────────────────────────────────────────

def _rows_affected(status: str) -> int:
    try:
        return int(str(status).split()[-1])
    except Exception:
        return 0


async def is_write_repo_set_incomplete(pool, goal_id: str, story_id: str) -> bool:
    """True when a granted write repo, with its branch already cut, has no
    corresponding `story_workspace_nodes` row.

    Multi-repo fix, piece 1 (`research/MULTI_REPO_STORIES_PLAN.md` §12.5). This is the
    exact gap `dispatch_story_kickoff_to_assistant`'s freshness check used to miss: it
    only asked whether cloning finished, never whether it finished with every repo the
    story was granted. A write repo whose branch is cut a few seconds after the FIRST
    provisioning attempt gave up on it (see `resolve_repos_for_story`'s "drop rather
    than fatal" comment) is exactly the case this catches.

    Always False when `multi_repo_enabled` is off: a single-repo story cannot reach
    `workspace_state == 'ready'` without its one repo already recorded — provisioning
    hard-fails first (`if not repo["branch_name"]: ...`) — so this never changes
    behaviour for it.
    """
    if not get_settings().multi_repo_enabled:
        return False
    from app.services.story_workspace_layout import resolve_workspace_clones  # noqa: PLC0415
    granted = await resolve_repos_for_story(pool, goal_id, story_id)
    clones = await resolve_workspace_clones(pool, story_id)
    if not clones:
        # Empty is the LEGACY answer, not "nothing landed yet" — see
        # `resolve_workspace_clones`' own docstring. The single production writer of
        # `story_workspace_nodes` (`_record_story_workspace_nodes`) runs at PROVISIONING
        # TIME only, so a workspace provisioned before that recorder existed has no rows
        # and never will acquire any. Reading empty as "incomplete" therefore made this
        # predicate permanently true for every such story rather than transiently true:
        # it pinned the chat's "Landing repos" banner on forever, and — because
        # `story_has_had_kickoff` was THREAD-scoped while the re-clone it guards is
        # STORY-scoped — sent `dispatch_story_kickoff` down its destructive
        # re-provision branch once per new assistant thread, on a workspace that
        # already held real work.
        #
        # Safe because this function's own docstring argument ("a single-repo story
        # cannot reach ready without its one repo already recorded") holds for every
        # workspace current code provisions; reaching `ready` with zero rows means the
        # story predates the recorder, and a legacy layout has no partial set to finish.
        #
        # Confirmed live 2026-08-25 on story e97ea4fb: branch feat/MOD02-e97ea4fb cut
        # 2026-08-13 and still IN_PROGRESS, one granted repo, zero recorded rows.
        #
        # Stage K adds a SECOND way to be `ready` with zero rows: a docs-only
        # workspace, which clones nothing on purpose (`stories.workspace_kind`
        # says which). Returning False is right for that too, and for the same
        # reason — there is no partial clone set to finish. It matters that it
        # is right rather than lucky: True here would make the branch-ready
        # listener re-dispatch kickoff on every pass, for a workspace that is
        # already exactly what it is meant to be.
        return False
    # BY (repo, BRANCH), because a repository is not a checkout. Two lanes of
    # one repo share a git_repo_id, so comparing on that alone said "complete"
    # the moment EITHER lane was recorded — and the missing one is exactly the
    # half-provisioned set this predicate exists to notice. It would have
    # reported a two-lane story finished with one of its lanes never cloned.
    recorded = {(c["git_repo_id"], c["branch_name"]) for c in clones}
    for candidate in granted:
        if candidate["is_write"] and candidate.get("branch_name") \
                and (candidate["git_repo_id"],
                     candidate["branch_name"]) not in recorded:
            return True
    return False


async def _claim_or_wait_provisioning(pool, goal_id: str, story_id: str) -> str:
    """Serialize concurrent provisioning of one story's workspace.

    Two requests can call provision_story_workspace ~simultaneously for the same
    story — a double-clicked / two-tab `pick-shca?import_story=` open, or Slack
    `/work` overlapping a UI import. (Board Accept no longer provisions, so the
    old accept+import race is gone, but concurrent imports still can.) Two
    `git clone`s into the same `stories/<id>/repo` dir stomp each other ("invalid
    index-pack output" / "could not lock .git/config").
    Atomically claim the right to clone by flipping `workspace_state` to
    'cloning'; only one UPDATE wins. The loser WAITS for the winner to reach a
    terminal state instead of racing a second clone.

    A 'cloning'/'provisioning' state older than 5 minutes is treated as a stale
    (crashed) prior attempt and is reclaimable, so a story can never wedge.

    Returns: 'claimed' (we won — proceed), 'ready'/'failed' (the winner finished
    while we waited), or 'timeout'.
    """
    import asyncio
    claimed = await pool.execute(
        "UPDATE stories SET workspace_state='cloning', updated_at=now() "
        "WHERE id=$1 AND goal_id=$2 "
        "AND (COALESCE(workspace_state,'') NOT IN ('cloning','provisioning') "
        "     OR updated_at < now() - interval '5 minutes')",
        story_id, goal_id,
    )
    if _rows_affected(claimed) == 1:
        return "claimed"
    for _ in range(60):  # ~60s waiting for the concurrent winner
        await asyncio.sleep(1)
        st = await pool.fetchval(
            "SELECT workspace_state FROM stories WHERE id=$1 AND goal_id=$2",
            story_id, goal_id,
        )
        if st in ("ready", "failed"):
            return st
    return "timeout"


async def provision_story_workspace(
    goal_id: str, story_id: str, agent_id: str, thread_id: str,
) -> dict:
    """Idempotent: provision the operator workspace for a story.

    `agent_id` is the SHCA TEMPLATE id (e.g. "abhishek-shca"); the SHCA
    INSTANCE id is `{template}-{goal_id}-{thread_id}` to match what
    _register_shca_instance creates on the gateway.

    This eagerly registers the SHCA on the gateway before the workspace
    setup RPC, so the gateway can rely on the agent being configured.
    The full _provision_resources run that happens later on WS open is
    idempotent (agents.create returns "already exists" and the rest of
    the provisioning is bootstrap-skipping by design).

    Returns dict with keys: ok (bool), workspace_path, repo_path, error.
    Sets stories.workspace_state through the lifecycle states.
    """
    pool = get_pool()

    # Never provision a story workspace / SHCA instance for a deleted goal.
    from app.services.goal_guard import goal_is_dead
    if await goal_is_dead(goal_id, pool):
        _log.info("provision_story_workspace: goal %s deleted — skipping provisioning", goal_id)
        return {"ok": False, "error": "goal_deleted"}

    from app.services.assistant_instance import resolve as _resolve_instance_id
    instance_id = await _resolve_instance_id(pool, agent_id, goal_id, thread_id)

    # Stage K. The kernel assistant does not do the work — an executor does, in
    # a sandbox with its own checkout — so its story workspace holds documents
    # and plans and nothing else. No clone, no token, no credential helper, no
    # Claude Code. The operator being replaced is untouched by this: it still
    # gets the full checkout it works in.
    from app.services.assistant_kind import (  # noqa: PLC0415
        WORKSPACE_CLONE, WORKSPACE_DOCS, is_kernel_assistant,
    )
    docs_only = await is_kernel_assistant(pool, agent_id)
    workspace_kind = WORKSPACE_DOCS if docs_only else WORKSPACE_CLONE

    # Git engine (Component B): ensure the story's workstream branch is being cut
    # BEFORE we resolve it. reconcile (idempotent) derives the git_node from the
    # current story DAG and wakes the engine to cut the branch; we then lazy-wait
    # below for the cut. Gated — byte-identical when GITGRAPH is off.
    if get_settings().gitgraph_enabled:
        try:
            from app.services.git_graph.reconcile import reconcile as _gg_reconcile
            from app.services.git_graph.dispatch import notify_git_dispatch as _gg_notify
            await _gg_reconcile(goal_id)
            await _gg_notify(goal_id)
        except Exception as exc:
            _log.warning(
                "provision_story_workspace: git auto-reconcile failed "
                "(goal=%s story=%s): %s — continuing", goal_id, story_id, exc,
            )

    # Resolve repo info. `repo` stays the PRIMARY repo (the first write repo) and
    # continues to drive every existing single-repo code path below — branch waiting,
    # token minting, stories.current_branch_name. `extra_repos` holds the additional
    # clones and is only ever non-empty when multi_repo_enabled is on, so with the
    # flag off this whole milestone is byte-identical to the previous behaviour.
    repo = await _resolve_repo_for_story(pool, goal_id, story_id)

    # Git-graph lazy-cut (§5.5.3): if this story is managed by the git engine and
    # its workstream branch hasn't been cut yet, WAIT for the engine rather than
    # hard-failing. "Not cut yet" shows up two ways: (a) NO branch resource is
    # linked to the story (the engine links it only AFTER cutting — §5.5.8 bridge —
    # so a grant-less engine-model story has no link), or (b) one is linked without
    # a branch_name. Either way, if a git_node for the story is PLANNED/BRANCHING,
    # leave workspace_state UNCHANGED (retry-able) and wait; Component C re-provisions
    # once the branch exists. Gated — off ⇒ behaviour is byte-identical to before.
    if get_settings().gitgraph_enabled and (not repo or not repo["branch_name"]):
        # EVERY NODE, not the first one. `LIMIT 1` was the whole of why a
        # story writing two branches only ever got one of them: the engine
        # cuts a node per write target -- two lanes of a repo, or two repos --
        # and the backfill below registers the branch of whichever node this
        # query happened to return. The other lane never became a grant, so it
        # was never resolved, never cloned, and never mentioned to anybody. A
        # single-target story returns one row and behaves exactly as before.
        nodes = [dict(n) for n in await pool.fetch(
            "SELECT n.id, n.goal_id, n.state, n.branch_name, n.git_repo_id, "
            "       n.trunk_branch, r.owner, r.name, r.clone_url "
            "FROM git_node_stories gns "
            "JOIN git_nodes n ON n.id = gns.git_node_id "
            "JOIN git_repos r ON r.id = n.git_repo_id "
            "WHERE gns.story_id = $1 ORDER BY n.id",
            story_id,
        )]
        # WAIT IF ANY IS STILL BEING CUT. Provisioning a workspace with one of
        # two branches ready would clone half the story and report success,
        # and the missing half looks to the assistant like a lane it was never
        # granted rather than one that is seconds away.
        node = (next((n for n in nodes
                      if n["state"] in ("PLANNED", "BRANCHING")), None)
                or (nodes[0] if nodes else None))
        if node and node["state"] in ("PLANNED", "BRANCHING"):
            _log.info(
                "provision_story_workspace: story=%s waiting for the git engine "
                "to cut its workstream branch (node state=%s) — retry-able",
                story_id, node["state"],
            )
            return {
                "ok": False, "waiting": True,
                "error": "waiting for the git engine to cut the workstream branch",
            }
        cuttable = [n for n in nodes if n["branch_name"]
                    and n["state"] not in ("MERGED", "ABANDONED")]
        if cuttable:
            # The workstream branch is ALREADY cut, but this story's branch-resource
            # link is missing — it joined the node AFTER the engine's cut pass linked
            # the then-current members (ensure_branch_resource runs only during the
            # cut, which never re-runs for a BRANCH_CREATED+ node). Backfill the link
            # (idempotent; links ALL members) so a 2nd/Nth story joining an existing
            # workstream clones the SAME branch instead of hard-failing.
            #
            # ONE TRANSACTION FOR ALL OF THEM, and one re-resolve afterwards. A
            # story writing two lanes needs BOTH links before the resolve, or it
            # reads back a half-granted set and provisions one lane as though
            # that were the whole story.
            try:
                from app.services.git_graph.branches import ensure_branch_resource
                async with pool.acquire() as conn, conn.transaction():
                    for n in cuttable:
                        await ensure_branch_resource(
                            conn, n, n["owner"], n["name"],
                            n["clone_url"] or "", n["trunk_branch"] or "",
                        )
                repo = await _resolve_repo_for_story(pool, goal_id, story_id)
                _log.info(
                    "provision_story_workspace: story=%s backfilled %d link(s) to "
                    "existing workstream branch(es): %s",
                    story_id, len(cuttable),
                    ", ".join(f"{n['branch_name']}({n['id']} {n['state']})"
                              for n in cuttable),
                )
            except Exception as exc:
                _log.exception(
                    "provision_story_workspace: branch-link backfill failed for "
                    "story=%s: %s", story_id, exc,
                )

    # A repo, cut and ready, is what the operator's whole workspace IS — so
    # its absence is fatal there. A docs-only workspace needs neither: an
    # errand legitimately has no repository at all, and a coding story's
    # checkout is the executor's problem, resolved from the story's grants at
    # dispatch. Failing here would refuse every errand for want of a repo it
    # was never going to use. (The lazy-cut wait above still applies when a
    # branch IS being cut — that returns `waiting` before reaching this.)
    if docs_only and (not repo or not repo["branch_name"]):
        _log.info(
            "provision_story_workspace: story=%s is docs-only and has %s — "
            "provisioning the document workspace anyway; the executor resolves "
            "its own checkout from the story's grants",
            story_id,
            "no repo grant" if not repo else "a repo whose branch is not cut yet",
        )
        repo = None
    elif not repo:
        msg = (
            f"no repo resource allocated to story {story_id} — "
            "cannot provision operator workspace"
        )
        await _set_workspace_state(pool, story_id, "failed", error=msg)
        return {"ok": False, "error": msg}
    elif not repo["branch_name"]:
        msg = (
            f"repo {repo['name']!r} has no branch_name set "
            "— the git engine must cut the story branch before operator mode can run"
        )
        await _set_workspace_state(pool, story_id, "failed", error=msg)
        return {"ok": False, "error": msg}

    # Additional repos beyond the primary (multi-repo plan M3.2). Resolved HERE — after
    # the lazy-cut wait and the branch-link backfill above may have re-resolved `repo` —
    # so the primary we exclude is the final one. The primary keeps driving every
    # single-repo path below; these are extra clones only.
    #
    # A WRITE repo with no cut branch yet is dropped rather than fatal: the git engine
    # cuts branches per node and Component C re-provisions, so failing the whole
    # workspace because repo #2's branch is a few seconds behind would strand a story
    # that is otherwise ready. A read-only context repo legitimately has no branch of
    # its own and is cloned at its default branch.
    extra_repos: list[dict] = []
    if get_settings().multi_repo_enabled and not docs_only:
        resolved_all = await resolve_repos_for_story(pool, goal_id, story_id)
        # Match the primary by resource id, not by position: resolve_repos_for_story
        # orders identically to _resolve_repo_for_story today, but matching on id means
        # a future ordering change cannot silently clone the primary into two subdirs.
        for candidate in resolved_all:
            if candidate["id"] == repo["id"]:
                continue
            if candidate["is_write"] and not candidate["branch_name"]:
                _log.info(
                    "provision_story_workspace: story=%s skipping write repo %s/%s — "
                    "its branch is not cut yet; Component C will re-provision",
                    story_id, candidate["repo_owner"], candidate["repo_name"],
                )
                continue
            extra_repos.append(candidate)
        if extra_repos:
            _log.info(
                "provision_story_workspace: story=%s is multi-repo — primary %s/%s "
                "plus %d additional clone(s): %s",
                story_id, repo["repo_owner"], repo["repo_name"], len(extra_repos),
                ", ".join(
                    f"{r['repo_owner']}/{r['repo_name']}"
                    f"({'write' if r['is_write'] else 'read-only'})"
                    for r in extra_repos
                ),
            )

    # Serialize against a concurrent provisioning of this SAME story (the accept
    # + import double-fire). Only one caller clones; the other waits for it
    # rather than racing a second git clone into the same dir.
    outcome = await _claim_or_wait_provisioning(pool, goal_id, story_id)
    if outcome == "ready":
        row = await pool.fetchrow("SELECT workspace_path FROM stories WHERE id=$1", story_id)
        _log.info("provision_story_workspace: story=%s provisioned by a concurrent "
                  "request — reusing (no second clone)", story_id)
        return {"ok": True, "workspace_path": (row and row["workspace_path"]) or "", "deduped": True}
    if outcome == "failed":
        return {"ok": False, "error": "a concurrent provisioning of this story failed"}
    if outcome == "timeout":
        return {"ok": False, "error": "timed out waiting for a concurrent provisioning of this story"}
    # outcome == 'claimed' — we own provisioning; workspace_state is now 'cloning'.

    # Mint installation token scoped to this story's repos. One token covers every
    # repo in the list (they share the GitHub App installation), so a multi-repo story
    # still needs exactly one mint — the scope just widens.
    # Nothing is cloned into a docs-only workspace, so there is nothing to
    # authenticate. Not minting is the point, not an optimisation: a token
    # that reaches a workspace is a token that can be read out of it.
    token_repo_names: list[str] = []
    github_token = ""
    if not docs_only:
        token_repo_names = token_repo_names_for(repo, extra_repos)
        try:
            token_result = await generate_installation_token(token_repo_names)
            github_token = token_result.get("token", "")
            if not github_token:
                raise RuntimeError(f"empty token returned: {token_result}")
        except Exception as exc:
            msg = f"token mint failed: {exc}"
            _log.exception("provision_story_workspace token mint failed (story=%s)", story_id)
            await _set_workspace_state(pool, story_id, "failed", error=msg)
            return {"ok": False, "error": msg}

    # Build scaffolding content. RESOURCES.md uses the TEMPLATE agent id
    # because that's how the resource_provisioning generator looks up GCS
    # paths / agent metadata. The workspace location is implicit (the file
    # is written into the workspace dir, so `pwd` resolves it).
    # The clone layout is computed ONCE and shared by the RPC, all three agent-facing
    # docs, and the story_workspace_nodes rows — see _clone_descriptors for why they
    # must agree. Computed before the docs because every one of them describes it.
    clone_descriptors = [] if docs_only else _clone_descriptors(repo, extra_repos)
    resources_md = await _build_resources_md_for_story(
        pool, goal_id, story_id, agent_id, github_token, clones=clone_descriptors,
        no_checkout=docs_only,
    )
    if docs_only:
        # A shorter, truthful set — see story_docs_workspace for why the
        # operator's documents cannot simply be reused with the repo lines
        # removed.
        from app.services import story_docs_workspace as _docs_ws  # noqa: PLC0415
        identity_md = await _docs_ws.build_identity_md(pool, goal_id, story_id)
        claude_md = ""
    else:
        identity_md = await _build_identity_md_for_story(
            pool, goal_id, story_id, clones=clone_descriptors,
        )
        claude_md = await _build_claude_md_for_story(
            pool, goal_id, story_id, clones=clone_descriptors,
        )

    # Option B: eagerly register the SHCA on the gateway BEFORE workspace
    # setup, so shca.workspace.setup can rely on the agent being configured.
    from app.openclaw import gateway_admin, GatewayError

    agent_row = await pool.fetchrow(
        "SELECT model, tools_allow, tools_deny, template_id FROM agents WHERE id = $1", agent_id,
    )
    from app.llm_models import default_for  # noqa: PLC0415
    # Fall back to the CONFIGURED assistant default (not a hardcoded gemini) so a
    # null-model assistant row provisions on the current default, never a surprise
    # downgrade. Normal rows carry a real model and never hit this.
    model = (agent_row and agent_row["model"]) or default_for("shca")
    # Resolve the assistant's tool allow/deny and pass them at CREATE. This is the
    # PRIMARY SHCA gateway-agent creator (story import, before the chat-connect
    # path), and the gateway honors tools ONLY at agents.create (agents.update is a
    # no-op). Without this the SHCA came up tools:null → the curated allow/deny (and
    # the sessions_* deny) were never enforced. Prefer the template (default-shca —
    # the current curated set, same source the GO uses) else the base agent row.
    _tools_param: dict = {}
    _t_allow: list = []
    _t_deny: list = []
    _tpl_id = agent_row["template_id"] if agent_row else None
    if _tpl_id:
        try:
            _trow = await pool.fetchrow(
                "SELECT tools_allow, tools_deny FROM templates WHERE id = $1", _tpl_id,
            )
            if _trow and _trow["tools_allow"]:
                _t_allow = list(_trow["tools_allow"])
                _t_deny = list(_trow["tools_deny"] or [])
        except Exception as _texc:
            _log.warning("provision_story_workspace: template %s tool lookup failed: %s", _tpl_id, _texc)
    if not _t_allow and agent_row:
        _t_allow = list(agent_row["tools_allow"] or [])
        _t_deny = list(agent_row["tools_deny"] or [])
    if _t_allow:
        _tools_param["allow"] = _t_allow
    if _t_deny:
        _tools_param["deny"] = _t_deny
    workspace_for_agent = local_dev.agent_workspace_path(instance_id)
    try:
        await gateway_admin.call("agents.create", {
            "name": instance_id,
            "workspace": workspace_for_agent,
            "model": model,
            "dispatchMode": "shca",
            **({"tools": _tools_param} if _tools_param else {}),
        }, timeout=30)
    except GatewayError as exc:
        if "already exists" not in str(exc):
            msg = f"agents.create RPC failed: {exc}"
            _log.warning(
                "provision_story_workspace: gateway rejected agents.create "
                "(story=%s instance=%s): %s",
                story_id, instance_id, exc,
            )
            await _set_workspace_state(pool, story_id, "failed", error=msg)
            return {"ok": False, "error": msg}

    await _set_workspace_state(pool, story_id, "cloning")

    # Pull active Claude Code phase definitions and add them as
    # .claude/agents/<name>.md scaffolding files. These become the
    # subagents the plan DAG references during execution.
    from app.services import claude_phases as cp_svc
    phase_files = {} if docs_only else await cp_svc.render_all_active_phases()

    files: dict[str, str] = {
        "RESOURCES.md": resources_md,
        "IDENTITY.md": identity_md,
        # CLAUDE.md is the workspace-root operator system prompt. Claude
        # Code auto-reads it on every turn. Templated per-story with the
        # story id, paths, four-phase enum, per-phase behaviour rules,
        # and the Task DAG schema with explicit rejection examples.
        # The PreToolUse validator hook rejects any write to this path.
        "CLAUDE.md": claude_md,
        # docs/ scaffolding is workspace-side metadata (NOT in the git
        # repo). Plans themselves get written by Claude into
        # repo/stories/<story_id>/plans/ so they ship to git alongside
        # the implementation.
        "docs/DECISIONS.md": _build_decisions_md(),
        "docs/CODE_NOTES.md": _build_code_notes_md(story_id),
        ".claude/settings.json": _build_default_settings_json(),
    }
    if docs_only:
        from app.services import story_docs_workspace as _docs_ws  # noqa: PLC0415
        files = _docs_ws.scaffolding_files(identity_md, resources_md)
    for fname, content in phase_files.items():
        files[f".claude/agents/{fname}"] = content

    # Teardown gate (council story review): shca.workspace.setup wipes every
    # OTHER stories/<id>/ dir to keep one story at a time. A story that is 'done'
    # but not yet 'reviewed' must survive — its final council review reads the
    # live workspace. So pass the set of 'done' sibling stories on this instance
    # as protectStoryIds; the gateway preserves those dirs while still creating
    # this story's. Once a story is reviewed (or cancelled/rejected) it drops off
    # this list and its dir is cleaned on the next import. (See the wipe loop in
    # agent_os_openclaw gateway server-methods/agents.ts.)
    #
    # Stage K widens this from "done but unreviewed" to "not finished with".
    # The wipe was written when one assistant held one story, so every other
    # dir was by definition abandoned. Once several stories run under one
    # assistant, the same loop deletes the workspace of a story that is
    # in_progress — and it fails silently, because the assistant only finds
    # out when it next reads a document that is gone. This is the increment
    # that can destroy work, so the list is inverted: name everything still
    # alive, and let the gateway remove only what is left.
    protect_rows = await pool.fetch(
        "SELECT id, state FROM stories "
        "WHERE goal_id = $1 AND shca_instance_id = $2 AND id <> $3 "
        "  AND state NOT IN ('cancelled', 'rejected', 'reviewed')",
        goal_id, instance_id, story_id,
    )
    protect_story_ids = [r["id"] for r in protect_rows]
    # The self-heal below is for done-but-unreviewed stories ONLY. An
    # in_progress story is not waiting on a review and must not be enqueued
    # for one just because it is now on the protect list.
    unreviewed_done = [r["id"] for r in protect_rows if r["state"] == "done"]
    if protect_story_ids:
        _log.info("provision_story_workspace: protecting live + unreviewed story dirs "
                  "%s from wipe (instance=%s)", protect_story_ids, instance_id)
    if unreviewed_done:
        # Self-heal: a protected story is 'done' but not yet 'reviewed', which
        # means its final review never succeeded (the done-hook fired once; it
        # may have failed). Re-enqueue it (deduped) so the dir doesn't stay
        # protected forever, and nudge the dispatcher.
        try:
            from app.services.council.runs import enqueue_story_review
            from app.services.council.dispatcher import council_dispatcher
            for sid in unreviewed_done:
                await enqueue_story_review(goal_id, sid, "done", trigger="reimport_retry")
            council_dispatcher.trigger()
        except Exception as exc:
            _log.warning("provision_story_workspace: could not re-enqueue reviews for "
                         "protected done stories %s: %s", unreviewed_done, exc)

    git_user_email = f"openclaw+story-{story_id}@noreply.agents"
    rpc_params = {
        "agentId": instance_id,
        "storyId": story_id,
        "protectStoryIds": protect_story_ids,
        # Still sent even when brokering: the gateway uses it for the clone itself
        # (which happens before any helper exists) and falls back to the inline
        # helper if we did not ask for brokering.
        "githubToken": github_token,
        "files": files,
    }
    # Ask the gateway for callback credential helpers instead of embedded tokens
    # (plan M5/G.6).
    #
    # ⚠ DEPLOY ORDER IS NOT OPTIONAL. An earlier comment here claimed an older gateway
    # "ignores the field", which is FALSE: ShcaWorkspaceSetupParamsSchema is
    # `additionalProperties: false`, so a gateway predating the multi-repo change
    # REJECTS `credentialBrokerUrl` (and `repos`) at schema validation and fails the
    # whole shca.workspace.setup RPC — no workspace at all, not a silent downgrade.
    # Backward compatibility runs one way only: a NEW gateway accepts an OLD control
    # panel's params. So the gateway must be redeployed BEFORE either flag is turned on.
    # The flags defaulting to off is what keeps that safe: with them off this field is
    # never sent and `repos` is never used.
    broker_url = "" if docs_only else _git_credential_broker_url()
    if broker_url:
        rpc_params["credentialBrokerUrl"] = broker_url
        _log.info(
            "provision_story_workspace: story=%s will use brokered git credentials "
            "(no token stored in .git/config)", story_id,
        )
    # ALWAYS the `repos` list, single-repo stories included. This RPC is what actually
    # creates the directories, so it must agree with `_clone_descriptors` exactly — and
    # `_clone_descriptors` now puts every repo under `repos/<owner>__<name>`. The legacy
    # `repo` object carries NO subdir (the gateway hardcodes `repo/` for it), so sending
    # it for a story recorded at `repos/<..>/` would clone to one path and record
    # another: the precise three-way disagreement `_clone_descriptors`' docstring exists
    # to prevent, and one that fails at the assistant's first git command rather than
    # here. The gateway schema declares `repos` with `minItems: 1`, so a one-entry list
    # is the supported shape, not a degenerate case. It still accepts the legacy `repo`
    # object from older control planes; we simply never send it.
    #
    # A docs-only workspace sends NEITHER `repo` nor `repos`, which the gateway
    # reads as "make the story directory and write the files, clone nothing".
    # An empty `repos: []` would not do: the schema declares minItems 1 and the
    # handler re-checks it, so the RPC would be refused rather than understood.
    if not docs_only:
        rpc_params["repos"] = [
            {
                "subdir": c["subdir"],
                "cloneUrl": c["clone_url"],
                "branch": c["branch_name"],
                "write": c["is_write"],
                "userName": "openclaw-shca",
                "userEmail": git_user_email,
            }
            for c in clone_descriptors
        ]

    await _set_workspace_state(pool, story_id, "provisioning")

    # (A) Bounded inline retry — re-mint a fresh repo-scoped token and retry the
    # setup RPC before declaring the workspace failed. Covers the transient
    # token-propagation / clone / network blip that otherwise strands the story.
    result = None
    setup_err: Optional[str] = None
    for attempt in range(1, _SETUP_MAX_ATTEMPTS + 1):
        if attempt > 1:
            # The previous token may have been the cause (propagation lag / a
            # momentary scope gap) — mint a fresh one for this retry.
            try:
                _retry_tok = await generate_installation_token(token_repo_names)
                if _retry_tok.get("token"):
                    rpc_params["githubToken"] = _retry_tok["token"]
            except Exception as _tok_exc:
                _log.warning(
                    "provision_story_workspace: token re-mint before setup retry "
                    "%d failed (story=%s): %s", attempt, story_id, _tok_exc,
                )

        try:
            result = await gateway_admin.call(
                "shca.workspace.setup", rpc_params, timeout=180,
            )
        except GatewayError as exc:
            setup_err = f"shca.workspace.setup RPC failed: {exc}"
            _log.warning(
                "provision_story_workspace gateway error (story=%s attempt=%d/%d): %s",
                story_id, attempt, _SETUP_MAX_ATTEMPTS, exc,
            )
            result = None
        except Exception as exc:
            setup_err = f"unexpected error in workspace RPC: {exc}"
            _log.exception(
                "provision_story_workspace unexpected error (story=%s attempt=%d/%d)",
                story_id, attempt, _SETUP_MAX_ATTEMPTS,
            )
            result = None

        # An RPC that returned but reported ok:false is also a (usually transient
        # clone) failure — treat it identically so the retry re-mints + re-clones.
        if result is not None and not result.get("ok"):
            setup_err = str(result.get("error") or result)
            _log.warning(
                "provision_story_workspace setup ok=false (story=%s attempt=%d/%d): %s",
                story_id, attempt, _SETUP_MAX_ATTEMPTS, setup_err,
            )
            result = None

        if result is not None:
            break  # success

        if attempt < _SETUP_MAX_ATTEMPTS:
            await asyncio.sleep(_SETUP_RETRY_BACKOFF_S * attempt)

    if result is None:
        err = setup_err or "shca.workspace.setup failed"
        await _set_workspace_state(pool, story_id, "failed", error=err)
        return {"ok": False, "error": err}

    ws_path = str(result.get("workspacePath") or "")
    repo_path = str(result.get("repoPath") or "")

    # Record what was actually cloned (multi-repo plan M3.1). Sourced from the primary
    # + extras we ASKED for, cross-checked against the gateway's per-repo result when
    # it sent one — the gateway is the authority on the on-disk layout. NOT best-effort:
    # see the except below — a cloned-but-unrecorded workspace reads as legacy system-wide
    # and resolves to a directory that was never created, so recording failure fails
    # provisioning. This is what lets `ready` + zero rows mean LEGACY and nothing else.
    try:
        gateway_repos = result.get("repos")
        by_subdir = (
            {str(g.get("subdir") or ""): g for g in gateway_repos}
            if isinstance(gateway_repos, list) else {}
        )
        recorded = [
            {
                **c,
                # Prefer the gateway's reported branch: it is what is actually checked
                # out on disk, which can differ from what we asked for if the clone
                # fell back. Ours is the fallback when the gateway sent no detail
                # (legacy single-repo response).
                "branch_name": str(
                    (by_subdir.get(c["subdir"]) or {}).get("branch") or c["branch_name"]
                ),
            }
            for c in clone_descriptors
        ]
        await _record_story_workspace_nodes(pool, story_id, recorded)
    except Exception as exc:
        # NOT best-effort any more. The ABSENCE of story_workspace_nodes rows is the
        # system-wide "this is a legacy workspace" flag, read by the resources doc, the
        # plan-path resolver, the DAG repo validation, the diff write-repo count, the
        # state snapshot and the credential broker. A workspace that cloned into
        # `repos/<owner>__<name>/` but failed to record would read as legacy from then
        # on, so all six would point at `repo/` — a directory that was never created.
        # The workspace is therefore NOT fine, whatever the clone did: fail provisioning
        # so it is re-provisioned rather than used with an unresolvable layout.
        msg = f"failed to record workspace nodes: {exc}"
        _log.error(
            "provision_story_workspace: %s (story=%s) — failing the workspace; a "
            "cloned-but-unrecorded workspace reads as legacy and resolves to the "
            "wrong directory", msg, story_id,
        )
        await _set_workspace_state(pool, story_id, "failed", error=msg)
        return {"ok": False, "error": msg}

    # Update stories.workspace_path / shca_instance_id / branch / phase
    # + flip state to ready. shca_instance_id is the staleness guard: if a
    # later import sees a different current instance id, re-provision.
    #
    # `workspace_kind` is recorded here rather than derived later because
    # `ready` with zero story_workspace_nodes rows already means "legacy
    # layout, look in repo/" everywhere in the system. It cannot ALSO mean
    # "nothing was cloned" — those two resolve to opposite places. The
    # branch is NULL for a docs-only story with no repo grant (an errand);
    # COALESCE keeps a previously-recorded branch rather than blanking it.
    branch_name = repo["branch_name"] if repo else None
    await pool.execute(
        """UPDATE stories
              SET workspace_path = $1,
                  shca_instance_id = $2,
                  current_branch_name = COALESCE($3, current_branch_name),
                  git_branch = COALESCE(NULLIF(git_branch, ''), $3),
                  current_phase = COALESCE(NULLIF(current_phase, ''), 'planning'),
                  workspace_kind = $5,
                  updated_at = NOW()
            WHERE id = $4""",
        ws_path, instance_id, branch_name, story_id, workspace_kind,
    )
    await _set_workspace_state(pool, story_id, "ready")

    # Populate story-scoped documents + RESOURCES.md now that the story
    # workspace exists. The RESOURCES.md written via the setup `files` dict
    # above is doc-less (it was built before the gateway agent existed and
    # never carries the `documents` list), and binary docs can't go through
    # the utf-8 setup writeFile loop. Now that the agent + story dir exist,
    # push the actual document files into stories/<sid>/docs/ and re-write
    # the story RESOURCES.md listing them — and keep the SHCA root in sync.
    # Mirrors the refresh path so re-import is correct immediately, not only
    # after the next token refresh. Best-effort: never fail provisioning.
    try:
        from app.resource_provisioning import (
            download_documents_to_workspace as _dl_docs,
            generate_resources_md as _gen_rmd,
        )
        _docs = await _dl_docs(
            gateway_admin, instance_id, goal_id,
            story_id=story_id, agent_id=agent_id,
        )
        if _docs:
            _rmd = await _gen_rmd(
                agent_id=agent_id, goal_id=goal_id,
                github_token=github_token, story_id=story_id, documents=_docs,
                repo_precloned=True, no_checkout=docs_only,
            )
            await gateway_admin.set_agent_file(
                instance_id, f"stories/{story_id}/RESOURCES.md", _rmd,
            )
            await gateway_admin.set_agent_file(instance_id, "RESOURCES.md", _rmd)
            _log.info(
                "provision_story_workspace: populated %d doc(s) + RESOURCES.md "
                "for story=%s", len(_docs), story_id,
            )
    except Exception as exc:
        _log.warning(
            "provision_story_workspace: doc/RESOURCES population failed "
            "(story=%s): %s — will self-heal on next refresh",
            story_id, exc,
        )

    head_sha = result.get("headSha")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                event_type=WORKTREE_CREATED,
                payload={
                    "story_id": story_id,
                    "workspace_path": ws_path,
                    "repo_path": repo_path,
                    "branch": branch_name,
                    "repo_name": (repo["repo_name"] if repo else ""),
                    "workspace_kind": workspace_kind,
                    "head_sha": head_sha,
                    "files_written": result.get("filesWritten"),
                },
                context_goal_id=goal_id,
            )

    return {
        "ok": True,
        "workspace_path": ws_path,
        "repo_path": repo_path,
        "head_sha": head_sha,
    }


# ── Claude Code session pre-spawn for code stories ────────────────────────
#
# Phase 4 Stage 4: spawn Claude eagerly at story import time (right after
# workspace provisioning) for code stories. This eliminates the SHCA-side
# fragility where SHCA had to remember to call sessions_spawn — sometimes
# it would forget, pass the wrong storyId, or fail Option B resolution.
#
# After this runs:
#   • stories.current_claude_session_id is populated
#   • Claude's first-turn response (its clarifying questions) is in the
#     chat as a 'claude-code' message — so the user sees them as soon as
#     they open the conversation
#   • SHCA's planning skill skips its own spawn and just sessions_send
#
# Generic stories (no repo / branch resource) skip pre-spawn entirely —
# they don't use Claude Code at all.


_CLAUDE_SESSION_INIT_TASK = """You are being initialized as the operator for story: {title} (id: {story_id}).

This is a session-init turn — NOT a planning turn. Do NOT scan the repo, read RESOURCES.md, or ask questions yet. Your real briefing with planning instructions will arrive in the next turn from the SHCA assistant.

For now, just reply with exactly:

  READY

That's all. No exploration, no commentary."""


async def _is_code_story(pool, story_id: str) -> bool:
    """A story is a code story iff at least one attached resource has
    kind in ('repo', 'branch'). Matches the same check used by
    build_story_state_snapshot."""
    return bool(await pool.fetchval(
        """SELECT 1
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND gr.kind IN ('repo', 'branch')
            LIMIT 1""",
        story_id,
    ))


async def prespawn_claude_for_code_story(
    *,
    goal_id: str,
    story_id: str,
    story_title: str,
    instance_id: str,
    workspace_path: str,
    thread_id: str,
) -> dict:
    """Pre-spawn the Claude Code session for a code story.

    Idempotent: if `stories.current_claude_session_id` is already set,
    skips. Otherwise calls `shca.claude.invoke` with the first-turn
    briefing, persists the returned session_id, and writes Claude's
    first response (typically clarifying questions) to the chat as a
    `claude-code` message so the user sees it on chat open.

    Returns:
      { ok, session_id, text_chars, skipped? }
    """
    from app.openclaw import gateway_admin, GatewayError

    pool = get_pool()

    if not await _is_code_story(pool, story_id):
        _log.debug("prespawn: story %s is not a code story — skip", story_id)
        return {"ok": True, "skipped": "not_code_story"}

    # Atomic compare-and-swap: try to claim the prespawn slot by
    # marking current_claude_session_id with an in-progress sentinel.
    # If the row already has a real session_id (or another concurrent
    # caller already claimed the slot), the UPDATE affects 0 rows and
    # we bail. This prevents rapid double-clicks from spawning two
    # sessions for the same story.
    _PRESPAWN_INPROGRESS = "__prespawn_in_progress__"

    claimed = await pool.fetchval(
        """UPDATE stories
              SET current_claude_session_id = $1, updated_at = NOW()
            WHERE id = $2
              AND (current_claude_session_id IS NULL
                   OR current_claude_session_id = '')
            RETURNING id""",
        _PRESPAWN_INPROGRESS, story_id,
    )

    if claimed is None:
        # Either another prespawn is in progress, or a session is
        # already attached. Re-read to figure out which.
        existing = await pool.fetchval(
            "SELECT current_claude_session_id FROM stories WHERE id = $1",
            story_id,
        )
        if existing == _PRESPAWN_INPROGRESS:
            _log.info(
                "prespawn: another prespawn in progress for story %s — skip",
                story_id,
            )
            return {"ok": True, "skipped": "concurrent_in_progress"}
        _log.info(
            "prespawn: story %s already has session_id=%s — skip",
            story_id, (existing or "")[:8],
        )
        return {"ok": True, "skipped": "already_spawned", "session_id": existing}

    init_task = _CLAUDE_SESSION_INIT_TASK.format(
        title=story_title, story_id=story_id,
    )

    _log.info(
        "prespawn: creating Claude session for code story %s (instance=%s, cwd=%s)",
        story_id, instance_id[:24], workspace_path,
    )
    try:
        result = await gateway_admin.call(
            "shca.claude.invoke",
            {
                "agentId": instance_id,
                "storyId": story_id,
                "cwd": workspace_path,
                "task": init_task,
            },
            timeout=120,  # session-init is short — Claude just acks
        )
    except GatewayError as exc:
        _log.warning(
            "prespawn: shca.claude.invoke failed for story=%s: %s — "
            "releasing prespawn slot so SHCA can retry via lazy spawn",
            story_id, exc,
        )
        await pool.execute(
            "UPDATE stories SET current_claude_session_id = NULL "
            "WHERE id = $1 AND current_claude_session_id = $2",
            story_id, _PRESPAWN_INPROGRESS,
        )
        return {"ok": False, "error": f"invoke failed: {exc}"}
    except Exception as exc:
        _log.exception("prespawn: unexpected error for story=%s", story_id)
        await pool.execute(
            "UPDATE stories SET current_claude_session_id = NULL "
            "WHERE id = $1 AND current_claude_session_id = $2",
            story_id, _PRESPAWN_INPROGRESS,
        )
        return {"ok": False, "error": str(exc)}

    session_id = (result.get("sessionId") or "").strip()

    if session_id:
        # cli-spawn ALSO POSTs to /claude-session — this is belt-and-
        # suspenders to make sure the column is populated before SHCA's
        # first planning turn runs.
        await pool.execute(
            "UPDATE stories SET current_claude_session_id = $1, "
            "updated_at = NOW() WHERE id = $2",
            session_id, story_id,
        )
        _log.info(
            "prespawn: session_id=%s persisted for story=%s (session-init only; "
            "SHCA will send the full briefing on its first planning turn)",
            session_id[:8], story_id,
        )

    # We intentionally do NOT post Claude's "READY" response to chat —
    # it's noise. SHCA's first planning turn produces the real first
    # conversation message the human will see.

    return {
        "ok": True,
        "session_id": session_id,
    }


# ── Cleanup ────────────────────────────────────────────────────────────────

async def delete_story_workspace(
    goal_id: str, story_id: str, agent_id: str, thread_id: str,
) -> dict:
    """Remove a story workspace from the SHCA agent's dir on the VM.

    Called when a story transitions to a terminal state (done/cancelled/
    rejected). Idempotent — succeeds if already gone or if the SHCA has
    been reaped (its whole workspace is gone with it). `agent_id` is the
    SHCA TEMPLATE id; the instance id is reconstructed the same way as
    provision_story_workspace.
    """
    pool = get_pool()
    from app.services.assistant_instance import resolve as _resolve_instance_id
    instance_id = await _resolve_instance_id(pool, agent_id, goal_id, thread_id)

    from app.openclaw import gateway_admin, GatewayError

    try:
        result = await gateway_admin.call(
            "shca.workspace.deleteStory",
            {"agentId": instance_id, "storyId": story_id},
            timeout=30,
        )
    except GatewayError as exc:
        _log.warning(
            "delete_story_workspace gateway error (story=%s instance=%s): %s",
            story_id, instance_id, exc,
        )
        return {"ok": False, "error": str(exc)}

    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error") or result)}

    # Best-effort ledger event + state flip.
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                    event_type=WORKTREE_DELETED,
                    payload={
                        "story_id": story_id,
                        "instance_id": instance_id,
                        "existed": result.get("existed", False),
                    },
                    context_goal_id=goal_id,
                )
    except Exception as exc:
        _log.warning(
            "delete_story_workspace: ledger emit failed (story=%s): %s",
            story_id, exc,
        )

    return {"ok": True, "existed": result.get("existed", False)}
