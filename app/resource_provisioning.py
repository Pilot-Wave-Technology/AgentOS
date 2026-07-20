"""Resource provisioning — generates RESOURCES.md and manages credential lifecycle.

Handles:
  - RESOURCES.md generation with embedded credential setup commands
  - Credential lifecycle (provision → refresh → cleanup)
  - Session timeout recovery (re-provision on new session)
  - Mid-session resource changes (live sync)

Design principles:
  - Credentials are embedded in RESOURCES.md as git credential-helper commands
    (no separate .netrc files needed — avoids the ALLOWED_FILE_NAMES limitation)
  - A background refresh loop renews GitHub tokens before they expire (50-min cycle)
  - Provisioning completes BEFORE the agent receives its first message
  - On session recovery, workspace state is detected and instructions adjusted
"""
import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager, nullcontext
from typing import Callable, Optional

from app.database import get_pool
from app.github_tokens import generate_installation_token

_log = logging.getLogger(__name__)

# GitHub tokens expire in 1 hour — refresh at 50 minutes
_TOKEN_REFRESH_INTERVAL_SECONDS = 50 * 60

# Validation patterns for gcp_environment metadata — prevents malformed values
# from reaching shell command strings embedded in RESOURCES.md / dispatch messages.
_SA_EMAIL_RE = re.compile(
    r'^[a-zA-Z][a-zA-Z0-9\-]+@[a-zA-Z0-9\-]+\.iam\.gserviceaccount\.com$'
)
_GCP_PROJECT_RE = re.compile(r'^[a-z][a-z0-9\-]{4,28}[a-z0-9]$')

# Document pre-download limits and policy
_MAX_DOC_BYTES = 10 * 1024 * 1024  # 10 MiB cap per document (text or binary)
_TEXT_DOC_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv",
    ".html", ".htm", ".xml", ".log", ".rst", ".ini", ".toml",
    ".sql", ".sh", ".env", ".conf",
}
# Binary extensions we actively push to the agent workspace as base64.
# The `agents.files.set` gateway method accepts `encoding="base64"` when
# the OpenClaw fork build includes the base64 patch.
_BINARY_DOC_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
}


def _safe_doc_filename(name: str) -> str:
    """Make a goal_resources.name safe to use as a workspace file path under docs/."""
    base = name.strip().replace("\\", "/").split("/")[-1]
    # strip any non-portable chars; collapse whitespace to underscore
    base = re.sub(r"[^A-Za-z0-9._\-]+", "_", base).strip("._")
    return base or "document"


def _is_text_filename(name: str) -> bool:
    lower = name.lower()
    for ext in _TEXT_DOC_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


# ── Refresh gating: wrap mid-session RESOURCES.md writes so that any
#    send_to_gateway() call landing during the rewrite waits for the new
#    file to be pushed before the agent sees the message. If no live
#    _AgentSession is in memory (e.g. pure IA persistent worker with no
#    chat session object), the write runs directly — such workers are
#    task-driven and don't race with user sends the same way. ──
@asynccontextmanager
async def _refresh_gate(pid: str, agent_id: str, thread_id: str = ""):
    """Acquire the refresh_guard on a live session, if one exists.

    Importing session_manager inside the function avoids a circular
    import at module load time (openclaw.py imports from this module).
    """
    from app.openclaw import session_manager
    session = session_manager.find(pid, agent_id, thread_id) if pid and agent_id else None
    if session is None:
        yield
    else:
        async with session.refresh_guard():
            yield


# ── goal_resources → unified row format ──────────────────────────────────────

# Map goal_resources.kind to the type field the formatter expects
_KIND_TO_TYPE = {
    "repo": "git_repo",
    "branch": "git_repo",
    "document": "gcs_bucket",     # documents stored in GCS
    "file": "gcs_bucket",         # user-uploaded files in GCS
    "dataset": "bigquery_dataset",
    "gcp_environment": "gcp_environment",  # target GCP project + region context
    "gcp_test_sa": "gcp_test_sa",          # per-phase SA, integration-tester
    "gcp_deploy_sa": "gcp_deploy_sa",      # per-phase SA, deployment
    "service": "custom",
    "api_key": "custom",
    "design": "custom",
    "config": "custom",
    "environment": "custom",
    "other": "custom",
}


def _goal_resources_to_rows(goal_res_rows) -> list[dict]:
    """Convert goal_resources query results into the unified row format
    expected by generate_resources_md / _build_resource_entry.

    Handles all resource kinds — git repos, GCS files, BQ datasets, etc.

    Each row carries:
      - "human_context" (gr.human_context): resource-level description set at
        registration time; the same text everywhere the resource appears.
      - "go_note"       (sr.notes when available): GO's per-story tactical
        instruction for this specific story, rendered inline as "**GO note:**".
        Empty on goal-scoped rendering (no story_resources join).
    """
    # On git-engine goals the engine ALWAYS provides a feature branch; a repo row with
    # no branch_name must NOT silently fall back to the repo's default branch (the
    # TRUNK) as the "mandatory commit branch" — that would instruct the agent to commit
    # straight to dev/main. Leave it empty so the renderer emits "none assigned — ask
    # the GO". On non-engine goals the default branch IS the legitimate working ref, so
    # the fallback is preserved there.
    from app.config import get_settings as _get_settings
    _gitgraph = _get_settings().gitgraph_enabled

    rows = []
    for gr in goal_res_rows:
        gr = dict(gr)
        kind = gr.get("kind", "other")
        rtype = _KIND_TO_TYPE.get(kind, "custom")
        uri = gr.get("uri") or ""
        metadata = gr.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                import json
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        go_note = (gr.get("sr_notes") or "").strip()
        human_context = (gr.get("human_context") or "").strip()

        if kind in ("repo", "branch"):
            repo_url = gr.get("clone_url") or uri or ""
            if not repo_url and gr.get("repo_owner") and gr.get("repo_name"):
                repo_url = f"https://github.com/{gr['repo_owner']}/{gr['repo_name']}"
            if not repo_url:
                continue
            # Branch resources are stored with display name "repo:branch" but
            # for RESOURCES.md we want the clean repo name as the header — the
            # branch is surfaced explicitly on the "branch (mandatory)" line.
            # The clone URL (clone_url on the branch row, set to the parent
            # repo) is always the parent repo.
            display_name = gr["name"]
            if kind == "branch" and gr.get("repo_name"):
                display_name = gr["repo_name"]
            rows.append({
                "id": gr["id"], "name": display_name, "type": "git_repo",
                "uri": repo_url, "protected_branches": [], "human_context": human_context,
                "go_note": go_note,
                "gcp_project": None, "dataset": None, "bucket": None, "path_prefix": None,
                "access_level": "read-write",
                "constraints": None,
                "branch": gr.get("branch_name") or ("" if _gitgraph else gr.get("default_branch")) or "",
                "repo_name": gr.get("repo_name") or "",
                "repo_owner": gr.get("repo_owner") or "",
                "paths": [],
            })
        elif kind in ("document", "file"):
            # Documents/files are pre-downloaded into the agent workspace
            # under docs/<safe_name> by download_documents_to_workspace().
            # They appear in the "Documents" section of RESOURCES.md via
            # the `documents` list, NOT as gcs_bucket entries. Skip them
            # here to avoid showing gsutil instructions for files that are
            # already local.
            continue
        elif kind == "dataset":
            # BigQuery dataset — uri might be bigquery://project.dataset or
            # just stored in metadata
            gcp_project = metadata.get("gcp_project", "")
            dataset = metadata.get("dataset", "")
            if not gcp_project and "." in uri:
                # Try to parse from uri like "project.dataset"
                parts = uri.replace("bigquery://", "").split(".")
                if len(parts) >= 2:
                    gcp_project = parts[0]
                    dataset = parts[1]
            rows.append({
                "id": gr["id"], "name": gr["name"], "type": "bigquery_dataset",
                "uri": uri, "protected_branches": [], "human_context": human_context,
                "go_note": go_note,
                "gcp_project": gcp_project, "dataset": dataset,
                "bucket": None, "path_prefix": None,
                "access_level": "read",
                "constraints": None, "branch": "", "repo_name": "", "repo_owner": "",
                "paths": [],
            })
        elif kind == "gcp_environment":
            # Target GCP project for integration testing and deployment.
            # Credentials are injected per-phase at dispatch time — SA emails
            # are validated here so malformed values never reach shell commands.
            target_project = (metadata.get("target_project_id") or "").strip()
            target_region = (metadata.get("target_region") or "").strip()
            test_sa = (metadata.get("test_service_account") or "").strip()
            deploy_sa = (metadata.get("deploy_service_account") or "").strip()

            if target_project and not _GCP_PROJECT_RE.match(target_project):
                _log.warning(
                    "gcp_environment resource %s: invalid target_project_id %r — clearing",
                    gr["id"], target_project,
                )
                target_project = ""
            if test_sa and not _SA_EMAIL_RE.match(test_sa):
                _log.warning(
                    "gcp_environment resource %s: invalid test_service_account %r — clearing",
                    gr["id"], test_sa,
                )
                test_sa = ""
            if deploy_sa and not _SA_EMAIL_RE.match(deploy_sa):
                _log.warning(
                    "gcp_environment resource %s: invalid deploy_service_account %r — clearing",
                    gr["id"], deploy_sa,
                )
                deploy_sa = ""

            rows.append({
                "id": gr["id"], "name": gr["name"], "type": "gcp_environment",
                "uri": uri, "protected_branches": [], "human_context": human_context,
                "go_note": go_note,
                "gcp_project": None, "dataset": None,
                "bucket": None, "path_prefix": None,
                "access_level": "via service account impersonation (credentials injected per task)",
                "constraints": None, "branch": "", "repo_name": "", "repo_owner": "",
                "paths": [],
                "target_project_id": target_project,
                "target_region": target_region,
                "test_service_account": test_sa,
                "deploy_service_account": deploy_sa,
            })
        elif kind in ("gcp_test_sa", "gcp_deploy_sa"):
            # Per-phase service account resources. Same pattern as repo +
            # branch: gcp_environment carries project/region, gcp_test_sa
            # and gcp_deploy_sa each carry one SA. A goal can have either
            # or both, or neither.
            sa_email = (metadata.get("sa_email") or "").strip()
            target_project = (metadata.get("target_project_id") or "").strip()
            target_region = (metadata.get("target_region") or "").strip()

            if sa_email and not _SA_EMAIL_RE.match(sa_email):
                _log.warning(
                    "%s resource %s: invalid sa_email %r — clearing",
                    kind, gr["id"], sa_email,
                )
                sa_email = ""
            if target_project and not _GCP_PROJECT_RE.match(target_project):
                _log.warning(
                    "%s resource %s: invalid target_project_id %r — clearing",
                    kind, gr["id"], target_project,
                )
                target_project = ""

            role_label = "integration testing" if kind == "gcp_test_sa" else "deployment"
            rows.append({
                "id": gr["id"], "name": gr["name"], "type": kind,
                "uri": uri, "protected_branches": [], "human_context": human_context,
                "go_note": go_note,
                "gcp_project": None, "dataset": None,
                "bucket": None, "path_prefix": None,
                "access_level": f"via service account impersonation ({role_label})",
                "constraints": None, "branch": "", "repo_name": "", "repo_owner": "",
                "paths": [],
                "target_project_id": target_project,
                "target_region": target_region,
                "sa_email": sa_email,
                "role_label": role_label,
            })
        else:
            # Custom / service / api_key / config / etc.
            rows.append({
                "id": gr["id"], "name": gr["name"], "type": "custom",
                "uri": uri, "protected_branches": [], "human_context": human_context,
                "go_note": go_note,
                "gcp_project": None, "dataset": None,
                "bucket": None, "path_prefix": None,
                "access_level": "read",
                "constraints": None, "branch": "", "repo_name": "", "repo_owner": "",
                "paths": [],
            })
    return rows


# ── RESOURCES.md generation ──────────────────────────────────────────────────

async def generate_resources_md(
    agent_id: str,
    goal_id: str,
    github_token: Optional[str] = None,
    task_id: Optional[str] = None,
    story_id: Optional[str] = None,
    documents: Optional[list[dict]] = None,
    repo_precloned: bool = False,
) -> str:
    """
    Build the RESOURCES.md content for an agent in a goal.

    `repo_precloned=True` (operator / claude-code story workspaces): the control
    panel has ALREADY checked the repo out at `repo/` in the story workspace, so
    RESOURCES.md instructs the agent to `cd repo` and work there and to NOT clone.
    A self-clone (into `repos/`) would be invisible to the commit/diff-review gate,
    which only reads `repo/`. `False` (IA / general SHCA): the agent self-clones
    into `repos/`.

    Scope precedence:
      - task_id → story_resources (inherited) ∪ task_resource_deps
                  (task-level grants/exclusions written by
                  grant_resource / revoke_resource)
      - story_id → story_resources (SHCA in story mode)
      - neither → all goal resources (SHCA in general mode, GO)

    When a github_token is provided, embeds credential setup commands directly.

    `documents` is an optional list of {name, local_path, uri, status} dicts
    describing pre-downloaded documents that should be surfaced in RESOURCES.md.
    """
    pool = get_pool()
    _log.info(
        "generate_resources_md: agent=%s goal=%s task=%s story=%s docs=%d",
        agent_id, goal_id, task_id or "-", story_id or "-",
        len(documents) if documents else 0,
    )

    rows = []
    default_rows = []

    # Review tasks (RA) are placeholders — they don't carry their own
    # story_id, story_resources, or task_resource_deps. Their resource
    # needs are inherited from the task they're reviewing: the RA must
    # clone the same repo and see the same branch the work agents had.
    # Swap to the reviewed task's id so all downstream resolution
    # (story_id lookup, story_resources query, task_resource_deps
    # merge) runs against the work task, not the placeholder.
    if task_id:
        review_target = await pool.fetchval(
            "SELECT review_target_id FROM tasks WHERE id = $1", task_id,
        )
        if review_target:
            _log.info(
                "generate_resources_md: review task=%s → resolving against "
                "reviewed task=%s",
                task_id, review_target,
            )
            task_id = review_target

    if task_id and not story_id:
        # IA path: resolve the task's parent story and route through the same
        # story-scoped query used by SHCAs in story mode. The legacy
        # `task_resources JOIN resources` query is no longer consulted — the
        # task inherits resource access from its story's needs/produces set.
        resolved = await pool.fetchrow(
            "SELECT story_id, goal_id FROM tasks WHERE id = $1", task_id,
        )
        if resolved:
            story_id = resolved.get("story_id") or ""
            # Use the task's actual goal_id if caller passed a stale one
            if resolved.get("goal_id"):
                goal_id = resolved["goal_id"]
            _log.info(
                "generate_resources_md: task=%s resolved to story=%s goal=%s",
                task_id, story_id or "(none — falling back to goal scope)",
                goal_id,
            )

    # Story-level brief — shown at the top of RESOURCES.md so the agent
    # sees GO's tactical framing before the resource details. Uses the
    # story's human-readable description field.
    story_brief = ""
    if story_id:
        brief_row = await pool.fetchrow(
            "SELECT description FROM stories WHERE id = $1", story_id,
        )
        if brief_row and (brief_row["description"] or "").strip():
            story_brief = brief_row["description"].strip()

    if story_id:
        # Story-scoped base set: all resources linked to the story via
        # story_resources. sr.notes carries GO's per-story tactical note
        # (populated via grant_resource(subject_type='story', notes=…))
        # and is rendered inline as **GO note:**.
        #
        # DISTINCT ON (gr.id) collapses multiple rows per resource to one.
        # story_resources PK is (story_id, resource_id, relation), so a
        # resource linked with both 'produces' and 'needs' (e.g. a branch
        # auto-linked as 'produces' by create_branch then manually
        # granted as 'needs' by grant_resource) produces two rows —
        # and without this dedup, the renderer emits two identical
        # ## <type> — <name> blocks in RESOURCES.md.
        # ORDER BY prefers the row with a non-empty note so GO's
        # tactical guidance survives the collapse.
        goal_res_rows = await pool.fetch(
            """SELECT * FROM (
                 SELECT DISTINCT ON (gr.id)
                        gr.id, gr.name, gr.kind, gr.uri,
                        gr.repo_owner, gr.repo_name, gr.branch_name,
                        gr.clone_url, gr.default_branch, gr.parent_id,
                        gr.human_context, gr.metadata,
                        sr.relation, sr.notes AS sr_notes
                   FROM story_resources sr
                   JOIN goal_resources gr ON gr.id = sr.resource_id
                  WHERE sr.story_id = $1 AND gr.status = 'active'
                    -- NEVER surface the repo's TRUNK designation (role='trunk' branch
                    -- resource) as a story working ref. It exists only so the engine
                    -- knows the trunk; rendering it gives the assistant a writable `dev`
                    -- and it commits straight to trunk (bypassing the branch/PR/merge
                    -- model). The assistant works ONLY on its role='feature' branch.
                    AND NOT (gr.kind = 'branch' AND gr.role = 'trunk')
                  ORDER BY gr.id,
                           (COALESCE(sr.notes, '') <> '') DESC,
                           sr.relation
               ) dedup
               ORDER BY kind, name""",
            story_id,
        )
        goal_res_rows = list(goal_res_rows)

        # Task-scoped overrides from task_resource_deps (allocation layer;
        # see app/routers/orchestrator.py grant/revoke endpoints). Applied
        # on top of the story-inherited set:
        #   dep_type='excluded' → drop the inherited resource from this
        #                         task's view (story still has it).
        #   any other dep_type  → add this resource (or override its notes
        #                         if already inherited). The SHCA's note
        #                         replaces the GO's note for this task.
        # The PK is (task_id, resource_id, dep_type), so DISTINCT ON picks
        # the latest row per (task, resource) for a deterministic view.
        if task_id:
            task_override_rows = await pool.fetch(
                """SELECT DISTINCT ON (trd.resource_id)
                          trd.resource_id,
                          trd.dep_type AS relation,
                          trd.notes AS sr_notes
                     FROM task_resource_deps trd
                    WHERE trd.task_id = $1
                    ORDER BY trd.resource_id, trd.created_at DESC""",
                task_id,
            )
            excluded = {r["resource_id"] for r in task_override_rows if r["relation"] == "excluded"}
            additions_by_id = {
                r["resource_id"]: r
                for r in task_override_rows
                if r["relation"] != "excluded"
            }

            if excluded:
                before = len(goal_res_rows)
                goal_res_rows = [r for r in goal_res_rows if r["id"] not in excluded]
                _log.info(
                    "generate_resources_md: task=%s excluded %d inherited resources",
                    task_id, before - len(goal_res_rows),
                )

            # Override notes on resources that are inherited AND have a
            # task-level row.
            story_ids_present = {r["id"] for r in goal_res_rows}
            for i, r in enumerate(goal_res_rows):
                if r["id"] in additions_by_id:
                    override = additions_by_id[r["id"]]
                    merged = dict(r)
                    merged["sr_notes"] = override["sr_notes"]
                    merged["relation"] = override["relation"]
                    goal_res_rows[i] = merged

            # Pull in resources the task added that aren't inherited.
            new_resource_ids = [
                rid for rid in additions_by_id.keys() if rid not in story_ids_present
            ]
            if new_resource_ids:
                added = await pool.fetch(
                    """SELECT id, name, kind, uri,
                              repo_owner, repo_name, branch_name,
                              clone_url, default_branch, parent_id,
                              human_context, metadata
                         FROM goal_resources
                        WHERE id = ANY($1) AND status = 'active'""",
                    new_resource_ids,
                )
                for r in added:
                    row = dict(r)
                    row["relation"] = additions_by_id[r["id"]]["relation"]
                    row["sr_notes"] = additions_by_id[r["id"]]["sr_notes"]
                    goal_res_rows.append(row)

        # Branch supersedes parent repo: if the set has BOTH a branch and
        # its parent repo (GO links the repo as "needs"/the goal adds it, and a
        # branch is created as "produces" — e.g. the git-engine's §5.5.8 branch
        # bridge), drop the parent repo so the agent sees only the branch.
        # Otherwise RESOURCES.md shows two entries for the same codebase with
        # CONTRADICTORY "branch (mandatory)" lines (trunk vs the engine branch).
        #
        # Match by parent_id AND by (owner, name): the engine sets parent_id to the
        # first kind='repo' row it finds for the repo, which can be NULL or a DIFFERENT
        # row than the one linked to this story (lookup/timing gap) — the (owner, name)
        # fallback collapses them regardless. Carry the repo row's GO note onto the
        # branch row when the branch has none, so tactical guidance isn't lost.
        # Normalize to dicts: the main query yields immutable asyncpg Records, and
        # the note carry-over below mutates rows in place.
        goal_res_rows = [dict(r) for r in goal_res_rows]
        branch_parent_ids = {
            r["parent_id"] for r in goal_res_rows
            if r.get("kind") == "branch" and r.get("parent_id")
        }
        branch_repo_keys = {
            (r.get("repo_owner"), r.get("repo_name")) for r in goal_res_rows
            if r.get("kind") == "branch" and r.get("repo_name")
        }

        def _is_superseded_repo(r) -> bool:
            return r.get("kind") == "repo" and (
                r["id"] in branch_parent_ids
                or (r.get("repo_owner"), r.get("repo_name")) in branch_repo_keys
            )

        if branch_parent_ids or branch_repo_keys:
            # Carry a superseded repo's GO note to its branch row (when the branch
            # lacks one) before dropping it.
            for br in goal_res_rows:
                if br.get("kind") != "branch" or (br.get("sr_notes") or "").strip():
                    continue
                for rp in goal_res_rows:
                    if (_is_superseded_repo(rp)
                            and (rp.get("repo_owner"), rp.get("repo_name"))
                                == (br.get("repo_owner"), br.get("repo_name"))
                            and (rp.get("sr_notes") or "").strip()):
                        br["sr_notes"] = rp["sr_notes"]
                        break
            filtered = [r for r in goal_res_rows if not _is_superseded_repo(r)]
            if len(filtered) != len(goal_res_rows):
                _log.info(
                    "generate_resources_md: story=%s dropped %d parent-repo rows superseded by linked branches",
                    story_id, len(goal_res_rows) - len(filtered),
                )
            goal_res_rows = filtered
        rows = _goal_resources_to_rows(goal_res_rows)
    else:
        # No task_id or story_id — goal-scoped (SHCA general mode, GO).
        # Read ALL resource kinds from goal_resources.
        goal_res_rows = await pool.fetch(
            """SELECT id, name, kind, uri, repo_owner, repo_name, branch_name,
                      clone_url, default_branch, human_context, metadata
               FROM goal_resources
               WHERE goal_id = $1 AND status = 'active'
                 -- Same trunk-designation guard as the story-scoped path: never render
                 -- a role='trunk' branch as a writable working ref.
                 AND NOT (kind = 'branch' AND role = 'trunk')
               ORDER BY kind, name""",
            goal_id,
        )
        rows = _goal_resources_to_rows(goal_res_rows)

    if not rows and not default_rows:
        return "# Resources\n\nNo resources have been assigned to this agent.\n"

    # Generate GCS/BQ credentials for applicable resources
    from app.gcp_credentials import generate_gcs_readonly_token, generate_bq_readonly_token

    # Cache tokens (one per type, works across all resources)
    _gcs_token_cache: Optional[str] = None
    _bq_token_cache: Optional[str] = None

    async def _get_gcs_token() -> Optional[str]:
        nonlocal _gcs_token_cache
        if _gcs_token_cache is None:
            _gcs_token_cache = await generate_gcs_readonly_token() or ""
        return _gcs_token_cache or None

    async def _get_bq_token(gcp_project: str) -> Optional[str]:
        nonlocal _bq_token_cache
        if _bq_token_cache is None:
            _bq_token_cache = await generate_bq_readonly_token(gcp_project) or ""
        return _bq_token_cache or None

    # Build unified resource entries. Each entry is a dict with fixed
    # fields that the summary table and detail-block renderer both read:
    #   kind            — "repo" | "doc" | "dataset" | "gcs" | "custom"
    #   name            — display name (goes in the block header)
    #   address         — the "where to find it" string per-kind
    #                     (owner/repo for repos, docs/path for docs,
    #                     bigquery://… / gs://… / URL for services)
    #   access          — "read-write" | "read" | "read (SELECT only)"
    #   working_ref     — branch name for repos, empty otherwise
    #   credential      — single-line credential summary (rendered inline)
    #   go_note         — GO's per-story tactical note (sr.notes)
    #   detail_lines    — extra bullets for the block (size for docs,
    #                     denied branches for repos, usage snippets)
    entries: list[dict] = []

    all_rows = [(r, "explicit") for r in rows] + [(r, "default") for r in default_rows]
    for r, source in all_rows:
        r = dict(r)
        rtype = r["type"]
        access_level = r.get("access_level") or r.get("default_access", "read")

        gcs_token = None
        bq_token = None
        if rtype == "gcs_bucket":
            gcs_token = await _get_gcs_token()
        elif rtype == "bigquery_dataset":
            gcp_project = r.get("gcp_project", "")
            if gcp_project:
                bq_token = await _get_bq_token(gcp_project)

        entries.append(_build_resource_entry(
            r=r,
            access_level=access_level,
            source=source,
            github_token=github_token,
            gcs_token=gcs_token,
            bq_token=bq_token,
        ))

    # Documents are passed as a separate list because they're pre-downloaded
    # into the workspace before RESOURCES.md is rendered. We fold them into
    # the same unified entry shape so they appear alongside other resources
    # in the summary table and as matching "## doc — name" blocks.
    for d in documents or []:
        entries.append(_build_document_entry(d))

    if not entries:
        return "# Resources\n\nNo resources have been assigned to this agent.\n"

    # Resolve git author — use the SHCA that owns this goal, not the IA
    # doing the work. All commits from any agent in the pipeline are
    # attributed to the human's SHCA for audit clarity.
    git_author = agent_id
    if task_id:
        shca_row = await pool.fetchrow(
            """SELECT a.name FROM agents a
               JOIN agent_contexts ac ON a.id = ac.agent_id
               WHERE ac.goal_id = $1 AND a.type = 'SHCA' AND a.status = 'active'
               LIMIT 1""",
            goal_id,
        )
        if shca_row:
            git_author = shca_row["name"]

    has_repo = any(e["kind"] == "repo" for e in entries)
    has_documents = any(e["kind"] == "doc" for e in entries)

    # ── Assemble the document ──
    lines: list[str] = ["# Resources", ""]

    if story_brief:
        lines.append("## Story brief")
        lines.append("")
        lines.append(story_brief)
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.extend(_summary_table(entries))
    lines.append("")

    for e in entries:
        lines.extend(_render_resource_block(e))
        lines.append("")

    if has_repo and github_token:
        lines.append(_git_setup_section(github_token, git_author, repo_precloned=repo_precloned))

    lines.append(_rules_section(has_repo=has_repo, has_documents=has_documents, repo_precloned=repo_precloned))
    return "\n".join(lines)


# ── Unified entry construction ───────────────────────────────────────────────

def _build_resource_entry(
    r: dict,
    access_level: str,
    source: str,
    github_token: Optional[str],
    gcs_token: Optional[str],
    bq_token: Optional[str],
) -> dict:
    """Convert a resource row (from _goal_resources_to_rows) into a unified
    entry dict consumed by the summary table and detail block renderer."""
    rtype = r["type"]
    name = r["name"]
    human_context = (r.get("human_context") or "").strip()
    go_note = (r.get("go_note") or "").strip()

    # Prefer GO's tactical note; if the resource only has a human_context
    # (goal-level description), surface that instead so the agent doesn't
    # lose any guidance.
    note = go_note or human_context

    detail_lines: list[str] = []
    default_flag = " *(default)*" if source == "default" else ""

    if rtype == "git_repo":
        clone_url = r["uri"]
        owner = r.get("repo_owner") or ""
        repo_name = r.get("repo_name") or ""
        address = f"{owner}/{repo_name}" if owner and repo_name else clone_url
        branch = r.get("branch") or ""
        credential = "git credential helper pre-configured (token rotates ~50 min)" if github_token else "no github token provisioned"
        denied = sorted(
            set(r.get("protected_branches") or [])
            | set((r.get("constraints") or {}).get("denied_branches", []))
        )
        if denied:
            detail_lines.append(f"- **denied branches (never push):** {', '.join(denied)}")
        return {
            "kind": "repo",
            "name": name,
            "address": address,
            "access": access_level + default_flag,
            "working_ref": branch,
            "credential": credential,
            "go_note": note,
            "detail_lines": detail_lines,
            "clone_url": clone_url,
        }

    if rtype == "gcs_bucket":
        uri = r["uri"]
        if gcs_token:
            credential = f"`CLOUDSDK_AUTH_ACCESS_TOKEN={gcs_token}` (expires ~1 h; re-read RESOURCES.md after expiry)"
        else:
            credential = "not provisioned"
        return {
            "kind": "gcs",
            "name": name,
            "address": uri,
            "access": "read-only" + default_flag,
            "working_ref": "",
            "credential": credential,
            "go_note": note,
            "detail_lines": detail_lines,
        }

    if rtype == "bigquery_dataset":
        project = r.get("gcp_project", "") or ""
        dataset = r.get("dataset", "") or ""
        addr = f"bigquery://{project}.{dataset}" if project and dataset else (r["uri"] or "bigquery://(unknown)")
        if bq_token:
            credential = f"`CLOUDSDK_AUTH_ACCESS_TOKEN={bq_token}` (expires ~1 h; re-read RESOURCES.md after expiry)"
        else:
            credential = "not provisioned"
        return {
            "kind": "dataset",
            "name": name,
            "address": addr,
            "access": "read (SELECT only)" + default_flag,
            "working_ref": "",
            "credential": credential,
            "go_note": note,
            "detail_lines": detail_lines,
        }

    if rtype == "gcp_environment":
        target_project = r.get("target_project_id", "") or "(unknown)"
        target_region = r.get("target_region", "") or "(unknown)"
        return {
            "kind": "gcp_environment",
            "name": name,
            "address": f"gcp://{target_project}",
            "access": "via service account impersonation (injected at dispatch)" + default_flag,
            "working_ref": "",
            "credential": "injected at task dispatch time — not stored in workspace",
            "go_note": note,
            "detail_lines": detail_lines,
            "target_project_id": target_project,
            "target_region": target_region,
        }

    # Custom / api_key / service / config / other
    return {
        "kind": "custom",
        "name": name,
        "address": r["uri"] or "(no address)",
        "access": access_level + default_flag,
        "working_ref": "",
        "credential": "see goal secret configuration",
        "go_note": note,
        "detail_lines": detail_lines,
    }


def _build_document_entry(d: dict) -> dict:
    """Convert a pre-download result dict into a unified document entry.

    Filename canonicalisation: docs are written to the agent workspace
    via `_safe_doc_filename(name)`, which collapses spaces and other
    non-portable characters to underscores. Historically RESOURCES.md
    displayed the *original* name in the `## doc — <name>` header
    while the `**location:**` field showed the *safe* path — so an
    agent reading "doc — my spec.pdf" with location `docs/my_spec.pdf`
    would try `read docs/my spec.pdf` and hit "file not found".

    Fix: show the safe name as the canonical name in RESOURCES.md
    (matches the file on disk), and surface the human-friendly
    original under `**uploaded as:**` when they differ.
    """
    status = d.get("status") or ""
    ready = status == "ready" and d.get("local_path")
    detail_lines: list[str] = []
    size = d.get("size_bytes")
    if size:
        detail_lines.append(f"- **size:** {_format_bytes(size)}")
    if not ready:
        detail_lines.append(f"- **status:** {status or 'unknown'}")
    address = d.get("local_path") or d.get("uri") or "(no path)"

    original_name = d["name"]
    safe_name = _safe_doc_filename(original_name)
    if safe_name and safe_name != original_name:
        detail_lines.append(f"- **uploaded as:** `{original_name}`")

    return {
        "kind": "doc",
        "name": safe_name or original_name,
        "address": address,
        "access": "read-only" if ready else "unavailable",
        "working_ref": "",
        "credential": "local file (no credential needed)" if ready else "not available",
        "go_note": (d.get("go_note") or "").strip(),
        "detail_lines": detail_lines,
    }


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.1f} MiB"


# ── Rendering: summary table + detail blocks ─────────────────────────────────

def _summary_table(entries: list[dict]) -> list[str]:
    """Build the summary table rows. One line per resource."""
    header = "| Type    | Name | Address | Access | Working ref |"
    divider = "|---------|------|---------|--------|-------------|"
    out = [header, divider]
    for e in entries:
        ref = e.get("working_ref") or "—"
        name = e["name"].replace("|", "\\|")
        addr = e["address"].replace("|", "\\|")
        access = e["access"].replace("|", "\\|")
        out.append(f"| {e['kind']:<7} | {name} | {addr} | {access} | {ref} |")
    return out


def _render_resource_block(e: dict) -> list[str]:
    """Render one ## <kind> — <name> block with uniform labelled fields.

    Field order is fixed so the agent can rely on position when scanning:
      1. address  (labelled per-kind: "clone" / "location" / "uri")
      2. branch (mandatory)  — repos only, if a working ref is assigned
      3. access
      4. credential
      5. any kind-specific detail bullets (size, denied branches, etc.)
      6. GO note  — last, so it summarises the "how to use it here"
    """
    kind = e["kind"]
    out = [f"## {kind} — {e['name']}"]

    if kind == "repo":
        out.append(f"- **clone:** {e.get('clone_url') or e['address']}")
        if e.get("working_ref"):
            out.append(f"- **branch (mandatory):** `{e['working_ref']}` — checkout and commit on this branch only. Never create a new branch or switch to another branch.")
        else:
            out.append("- **branch (mandatory):** none assigned. Stop and ask the human (via your assistant) or the GO to provision one — never create one yourself.")
    elif kind == "gcp_environment":
        target_project = e.get("target_project_id", "(unknown)")
        target_region = e.get("target_region", "(unknown)")
        out.append(
            f"- **target GCP project:** `{target_project}` — the customer's project where "
            f"code runs and deploys. DIFFERENT from the platform project your VM/Cloud Run "
            f"lives in."
        )
        out.append(f"- **target region:** `{target_region}`")
        out.append(
            f"- **⚠️ project scope:** always pass `--project={target_project}` "
            f"(or `export CLOUDSDK_CORE_PROJECT={target_project}`) for every "
            f"gcloud/bq/gsutil command. Omitting it silently targets the wrong project."
        )
        out.append(
            "- **credentials:** a short-lived service-account credentials file is written "
            "into your workspace before each `integration-tester` and `deployment` task. "
            "Look for the `## GCP Credentials (this task only)` block at the top of the "
            "task message — it tells you the file path and which env var to export. "
            "Do not look for SA keys or tokens here."
        )
        out.append(
            "- **audit trail:** all operations are recorded in GCP Cloud Audit Logs under "
            "the target service account's identity."
        )
        out.append("")
        out.append("### Setup required (per service account, done by goal operator)")
        out.append(
            f"For each `gcp_test_sa` / `gcp_deploy_sa` resource attached to this goal, "
            f"grant the control-panel's Cloud Run service account "
            f"`roles/iam.serviceAccountTokenCreator` on the target SA. Without this grant "
            f"the control panel cannot mint per-task credentials and tasks will emit "
            f"`PHASE_TASK_BLOCKED`."
        )
    elif kind in ("gcp_test_sa", "gcp_deploy_sa"):
        sa_email = e.get("sa_email", "(unknown)")
        role_label = e.get("role_label", "")
        target_project = e.get("target_project_id") or "(from gcp_environment)"
        target_region = e.get("target_region") or "(from gcp_environment)"
        out.append(f"- **service account:** `{sa_email}`")
        out.append(f"- **role:** {role_label}")
        out.append(f"- **target project:** `{target_project}`")
        out.append(f"- **target region:** `{target_region}`")
        out.append(
            f"- **credentials:** an ephemeral key for this SA is written into your "
            f"workspace at task dispatch time and deleted at task end. The dispatch "
            f"message's `## GCP Credentials (this task only)` block tells you the path."
        )
    elif kind == "doc":
        out.append(f"- **location:** `{e['address']}`")
    elif kind == "dataset":
        out.append(f"- **uri:** {e['address']}")
    elif kind == "gcs":
        out.append(f"- **uri:** {e['address']}")
    else:
        out.append(f"- **uri:** {e['address']}")

    # gcp_environment renders access, credentials, and detail inline above
    if kind != "gcp_environment":
        out.append(f"- **access:** {e['access']}")
        out.append(f"- **credential:** {e['credential']}")
        out.extend(e.get("detail_lines") or [])

    if e.get("go_note"):
        out.append(f"- **GO note:** {e['go_note']}")

    return out


def _git_setup_section(github_token: str, author_name: str = "", repo_precloned: bool = False) -> str:
    """Generate setup section with git credential helper embedded in RESOURCES.md.

    The git author is set to the SHCA that initiated the task — all commits
    from any agent in the pipeline are attributed to the project manager.

    `repo_precloned=True` (operator/claude-code story workspaces): the repo is ALREADY
    checked out at `repo/` by the control panel, so instruct the agent to work there and
    NOT clone — a second checkout is invisible to the commit/diff-review gate (which only
    reads `repo/`). `False` (IA / general SHCA): the agent self-clones into `repos/`.
    """
    name = author_name or "agent"
    # Sanitize for git config (no quotes, no special chars)
    safe_name = name.replace('"', '').replace("'", "").strip()
    email = safe_name.lower().replace(" ", "-").replace("'", "") + "@noreply.agents"
    creds = f"""
## Git Setup (run ONCE at start of session)

Run these commands to configure git credentials and author identity:

```bash
git config --global credential.helper '!f() {{ echo "username=x-access-token"; echo "password={github_token}"; }}; f'
git config --global user.name "{safe_name}"
git config --global user.email "{email}"
```
"""
    if repo_precloned:
        return creds + """
## Workspace layout — work INSIDE `repo/`, never outside it

Your working directory (`cwd`) is the **story workspace root**. It contains:

- `repo/` — the git checkout, **already cloned and on the correct branch** (see the
  `branch (mandatory)` field). Everything that must be committed — source code, plans,
  scope — lives under `repo/`. Reference it with the `repo/` prefix from your cwd
  (e.g. `repo/src/…`, `repo/stories/<story_id>/plans/…`).
- `docs/` — workspace scratch (notes, decisions). NOT committed to git.
- `RESOURCES.md`, `IDENTITY.md`, `CLAUDE.md` — read-only workspace files at the root.

⚠️ **Do NOT `git clone`** — the repo is already at `repo/`. A second checkout (under
`repos/`, `~/`, or anywhere else) is invisible to the commit/diff-review gate, which reads
ONLY `repo/`, so any work outside `repo/` is silently lost. Keep every file you create or
edit under `repo/`. These credentials are session-scoped; if git auth fails, re-read
RESOURCES.md for a fresh token.
"""
    return creds + """
Then clone repos into the `repos/` directory in your workspace:

```bash
mkdir -p repos
# For each repo, check if already cloned before cloning again:
# [ -d repos/<name> ] || git clone <uri> repos/<name>
# If already cloned, update credentials and pull:
# git -C repos/<name> pull
```

**Important:** Always clone into `repos/` (workspace-relative), NOT `~/repos/`.
These credentials are session-scoped. If git auth fails, re-read RESOURCES.md for refreshed credentials.
"""


def _rules_section(has_repo: bool = False, has_documents: bool = False, repo_precloned: bool = False) -> str:
    """Generate the rules section based on what resource types are present."""
    rules = ["", "## Rules", ""]

    if has_repo:
        if repo_precloned:
            rules.append("- Your `cwd` is the story workspace root; the repo is **already checked out at `repo/`** under it. Keep EVERY file you create or edit (source, plans, scope) under `repo/` — the commit/diff-review gate reads only `repo/`, so anything outside it is silently lost.")
            rules.append("- Do NOT `git clone` — the repo is already there. A second checkout (under `repos/`, `~/`, anywhere) is invisible to the review gate.")
            rules.append("- Run the **Git Setup** commands ONCE (they configure the credential helper + author identity used when you commit).")
            rules.append("- You are already on the correct branch (`branch (mandatory)`). Stay on it for all commits — do NOT run `git checkout -b`, do NOT switch branches. If you need a different branch, ask the GO via your assistant — the GO owns branch creation.")
        else:
            rules.append("- Run the **Git Setup** commands ONCE before cloning.")
            rules.append("- Clone each repo into `repos/<repo-name>/` (workspace-relative, never `~/repos/`). If it's already cloned, `git -C repos/<repo-name> pull` instead of re-cloning.")
            rules.append("- **Follow the `branch (mandatory)` field for each repo.** Checkout that branch immediately after cloning and stay on it for all commits. Do NOT run `git checkout -b`, do NOT switch to any other branch. If you need a different branch, ask the GO via your assistant — the GO owns branch creation.")
        rules.append("- Never push to `main`, `master`, `production`, or any branch listed under `denied branches`.")
        rules.append("- If git auth fails, credentials rotated — re-read RESOURCES.md for the fresh token and re-run the Git Setup commands.")
        rules.append("- Commit frequently with clear messages.")

    if has_documents:
        rules.append("- Documents are already on disk at the `location:` path shown in each `doc` block. Open them with your file/pdf/read tools — do NOT download from GCS.")
        rules.append("- When new documents are added to the story, RESOURCES.md is rewritten automatically — re-list `docs/` to pick them up.")

    rules.append("- **Only use the resources listed above.** Anything not in the Summary table is out of scope for this story.")
    rules.append("- Read each `GO note` carefully — it's the GO's tactical guidance for how to use this specific resource in this specific story.")
    rules.append("")
    return "\n".join(rules)


# ── Document pre-download (used by both SHCA and IA paths) ───────────────────

async def download_documents_to_workspace(
    gateway_admin,
    gateway_agent_id: str,
    goal_id: str,
    story_id: Optional[str] = None,
    agent_id: str = "",
    push_binaries: bool = True,
) -> list[dict]:
    """Pre-download document/file resources from GCS into an agent workspace.

    Story-scoped when story_id is provided; otherwise goal-wide. Only text
    documents (md/json/csv/etc.) are written into the workspace as
    `docs/<safe_name>` — binaries are listed in RESOURCES.md with their
    gs:// URI but not downloaded.

    Returns: list of {name, local_path, uri, size_bytes, status} dicts that the
    caller passes to `generate_resources_md` so RESOURCES.md mirrors local state.
    """
    pool = get_pool()

    # Uploaded documents live in `gcs_uri` (added by migration 015);
    # older / external resources may use `uri`. Prefer gcs_uri when
    # present and fall back to uri. Without this COALESCE, uploaded
    # documents were silently skipped (they have empty `uri`).
    if story_id:
        rows = await pool.fetch(
            """SELECT gr.id, gr.name, gr.kind, gr.file_type,
                      COALESCE(NULLIF(gr.gcs_uri, ''), gr.uri) AS uri,
                      sr.notes AS sr_notes
                 FROM story_resources sr
                 JOIN goal_resources gr ON gr.id = sr.resource_id
                WHERE sr.story_id = $1 AND gr.status = 'active'
                  AND gr.kind IN ('document', 'file')
                  AND COALESCE(NULLIF(gr.gcs_uri, ''), gr.uri) LIKE 'gs://%'""",
            story_id,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, name, kind, file_type,
                      COALESCE(NULLIF(gcs_uri, ''), uri) AS uri,
                      ''::text AS sr_notes
                 FROM goal_resources
                WHERE goal_id = $1 AND status = 'active'
                  AND kind IN ('document', 'file')
                  AND COALESCE(NULLIF(gcs_uri, ''), uri) LIKE 'gs://%'""",
            goal_id,
        )

    _log.info(
        "download_documents_to_workspace: agent=%s gw=%s goal=%s story=%s candidates=%d",
        agent_id or "-", gateway_agent_id, goal_id, story_id or "-", len(rows),
    )

    if not rows:
        return []

    try:
        from app.storage import get_gcs_client
    except Exception as exc:
        _log.warning("GCS client unavailable, skipping document pre-download: %s", exc)
        return []

    client = get_gcs_client()
    results: list[dict] = []

    for r in rows:
        name = r["name"]
        uri = r["uri"]
        resource_id = r["id"]
        # sr.notes — GO's tactical per-story note for this doc; surfaced as
        # "**GO note:**" inside the doc's RESOURCES.md block. Empty on the
        # goal-scoped query (no story_resources join).
        go_note = (r.get("sr_notes") or "").strip()
        safe_name = _safe_doc_filename(name)
        # The resource display name often has no file extension (e.g.
        # "architecture plan"), which makes the binary-extension check below
        # misclassify the file as "unsupported" and skip the download
        # (ready=0, source-only). Derive the real extension from file_type
        # (preferred) or the GCS object name and ensure safe_name carries it,
        # so both the text/binary classification and the docs/<name> path are
        # correct.
        file_ext = (r.get("file_type") or "").strip().lstrip(".").lower()
        if not file_ext:
            obj = uri.rstrip("/").rsplit("/", 1)[-1]
            if "." in obj:
                file_ext = obj.rsplit(".", 1)[-1].lower()
        if file_ext and not safe_name.lower().endswith("." + file_ext):
            safe_name = f"{safe_name}.{file_ext}"
        local_path = f"docs/{safe_name}"

        is_text = _is_text_filename(safe_name)

        # Binary (PDF/DOCX/XLSX/images/…): if the caller wants binaries pushed
        # AND the extension is in the supported set, download from GCS and
        # push base64-encoded via the patched gateway agents.files.set.
        if not is_text:
            if not push_binaries:
                _log.info(
                    "Binary doc %s not pushed (push_binaries=False, e.g. IA worker) — source-only",
                    name,
                )
                results.append({
                    "name": name, "local_path": "", "uri": uri,
                    "size_bytes": None, "status": "binary (source-only)",
                    "go_note": go_note,
                })
                continue
            if safe_name.rsplit(".", 1)[-1].lower() not in {
                ext.lstrip(".") for ext in _BINARY_DOC_EXTENSIONS
            }:
                # Unknown binary extension — don't push arbitrary bytes
                _log.info(
                    "Binary doc %s has extension outside supported set — source-only",
                    name,
                )
                results.append({
                    "name": name, "local_path": "", "uri": uri,
                    "size_bytes": None, "status": "binary (unsupported-ext)",
                    "go_note": go_note,
                })
                continue
            if not uri.startswith("gs://"):
                continue
            parts = uri[5:].split("/", 1)
            if len(parts) != 2:
                _log.warning("Bad GCS uri for binary resource %s: %s", name, uri)
                continue
            bucket_name, blob_path = parts
            try:
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(blob_path)
                blob.reload()
                size = blob.size or 0
                if size > _MAX_DOC_BYTES:
                    _log.warning(
                        "Binary doc %s skipped: size %d exceeds %d bytes (uri=%s)",
                        name, size, _MAX_DOC_BYTES, uri,
                    )
                    results.append({
                        "name": name, "local_path": "", "uri": uri,
                        "size_bytes": size,
                        "status": f"too-large (>{_MAX_DOC_BYTES} bytes)",
                        "go_note": go_note,
                    })
                    continue

                data = blob.download_as_bytes()
                import base64 as _b64
                b64content = _b64.b64encode(data).decode("ascii")
                await gateway_admin.set_agent_file(
                    gateway_agent_id, local_path, b64content, encoding="base64",
                )
                # Story-scoped copy so the coding subprocess (cwd = story dir)
                # and the side panel get the file too, not just the SHCA root.
                if story_id:
                    await gateway_admin.set_agent_file(
                        gateway_agent_id, f"stories/{story_id}/{local_path}",
                        b64content, encoding="base64",
                    )
                _log.info(
                    "Pushed binary doc gw=%s story=%s name=%s -> %s (%d bytes)",
                    gateway_agent_id, story_id or "-", name, local_path, size,
                )
                results.append({
                    "name": name, "local_path": local_path, "uri": uri,
                    "size_bytes": size, "status": "ready",
                    "go_note": go_note,
                })
            except Exception as exc:
                _log.warning(
                    "Failed to push binary doc %s (uri=%s) into %s: %s",
                    name, uri, gateway_agent_id, exc,
                )
                results.append({
                    "name": name, "local_path": "", "uri": uri,
                    "size_bytes": None, "status": f"push-failed: {exc}",
                    "go_note": go_note,
                })
            continue

        if not uri.startswith("gs://"):
            continue
        parts = uri[5:].split("/", 1)
        if len(parts) != 2:
            _log.warning("Bad GCS uri for resource %s: %s", name, uri)
            continue
        bucket_name, blob_path = parts

        try:
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            blob.reload()
            size = blob.size or 0
            if size > _MAX_DOC_BYTES:
                _log.warning(
                    "Document %s skipped: size %d exceeds %d bytes (uri=%s)",
                    name, size, _MAX_DOC_BYTES, uri,
                )
                results.append({
                    "name": name, "local_path": "", "uri": uri,
                    "size_bytes": size, "status": f"too-large (>{_MAX_DOC_BYTES} bytes)",
                    "go_note": go_note,
                })
                continue

            data = blob.download_as_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                _log.warning(
                    "Document %s appears non-utf8 — skipping pre-download (uri=%s)",
                    name, uri,
                )
                results.append({
                    "name": name, "local_path": "", "uri": uri,
                    "size_bytes": size, "status": "non-utf8 (source-only)",
                    "go_note": go_note,
                })
                continue

            await gateway_admin.set_agent_file(gateway_agent_id, local_path, text)
            # Story-scoped copy (see binary branch above).
            if story_id:
                await gateway_admin.set_agent_file(
                    gateway_agent_id, f"stories/{story_id}/{local_path}", text,
                )
            _log.info(
                "Downloaded doc gw=%s story=%s name=%s -> %s (%d bytes)",
                gateway_agent_id, story_id or "-", name, local_path, size,
            )
            results.append({
                "name": name, "local_path": local_path, "uri": uri,
                "size_bytes": size, "status": "ready",
                "go_note": go_note,
            })
        except Exception as exc:
            _log.warning(
                "Failed to pre-download doc %s (uri=%s) into %s: %s",
                name, uri, gateway_agent_id, exc,
            )
            results.append({
                "name": name, "local_path": "", "uri": uri,
                "size_bytes": None, "status": f"download-failed: {exc}",
                "go_note": go_note,
            })

    _log.info(
        "download_documents_to_workspace DONE gw=%s story=%s ready=%d total=%d",
        gateway_agent_id, story_id or "-",
        sum(1 for d in results if d["status"] == "ready"), len(results),
    )
    return results


# ── Credential lifecycle ─────────────────────────────────────────────────────

class ResourceLifecycle:
    """Manages the resource provisioning lifecycle for an agent session.

    Lifecycle:
      1. provision()  — called before first message, blocks until complete
      2. _refresh_loop() — background task renewing tokens every 50 minutes
      3. stop()  — cancels refresh loop on session end

    Handles:
      - Session timeout recovery: fresh provision on new session
      - Mid-session resource changes: sync_resources_md() updates RESOURCES.md
      - Human pause: refresh loop runs independently of chat activity
      - Credential expiry: auto-refreshes before GitHub token's 1-hour TTL

    Error reporting:
      Accepts an optional on_error callback that receives (code, message, details)
      for surfacing provisioning/refresh failures to the browser via the session.
    """

    def __init__(
        self,
        gateway_admin,
        agent_id: str,
        gateway_agent_id: str,
        goal_id: str,
        on_error: Optional[Callable] = None,
        story_id: Optional[str] = None,
        session=None,
    ):
        self._gateway_admin = gateway_admin
        self._agent_id = agent_id
        self._gateway_agent_id = gateway_agent_id
        self._goal_id = goal_id
        self._on_error = on_error
        self._story_id = story_id
        # Back-reference to the live _AgentSession so _write_resources_md
        # can acquire refresh_guard during mid-session refreshes (token
        # rotation in _refresh_loop, or refresh_* callers). Safe to leave
        # None when there is no live session (e.g. a one-shot sync).
        self._session = session
        self._refresh_task: Optional[asyncio.Task] = None
        self._current_token: Optional[str] = None
        self._provisioned = asyncio.Event()
        self._stopped = False
        self.last_error: Optional[dict] = None  # Most recent error, inspectable by callers

    async def _report_error(self, code: str, message: str, details: dict = None, retryable: bool = False):
        """Report an error to the session for browser surfacing."""
        self.last_error = {"code": code, "message": message, "details": details}
        _log.error("ResourceLifecycle error [%s] for %s/%s: %s",
                   code, self._goal_id, self._agent_id, message)
        if self._on_error:
            try:
                await self._on_error(code, message, details, retryable)
            except Exception as exc:
                _log.warning("on_error callback failed: %s", exc)

    async def provision(self) -> None:
        """Provision resources and credentials. Blocks until complete.

        Called by SessionManager BEFORE the first message is sent to the gateway.
        """
        try:
            _log.info(
                "ResourceLifecycle.provision START agent=%s goal=%s story=%s gw=%s",
                self._agent_id, self._goal_id, self._story_id or "-",
                self._gateway_agent_id,
            )
            self._current_token = await self._generate_token_if_needed()
            # Download text documents into the agent workspace BEFORE writing
            # RESOURCES.md so the file accurately reflects what is local.
            documents = await self._download_documents()
            await self._write_resources_md(documents)
            self._provisioned.set()
            _log.info(
                "ResourceLifecycle.provision DONE agent=%s goal=%s story=%s docs=%d token=%s",
                self._agent_id, self._goal_id, self._story_id or "-",
                len(documents), "yes" if self._current_token else "no",
            )

            # Start background refresh loop
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            self._refresh_task.add_done_callback(self._refresh_done_callback)

        except Exception as exc:
            _log.error(
                "Resource provisioning failed for %s/%s: %s",
                self._goal_id, self._agent_id, exc,
            )
            await self._report_error(
                code="RESOURCE_PROVISION_FAILED",
                message=f"Failed to provision resources: {exc}",
                details={"agent_id": self._agent_id, "goal_id": self._goal_id},
                retryable=True,
            )
            # Still set provisioned so the session isn't blocked forever
            self._provisioned.set()

    async def wait_until_ready(self, timeout: float = 15.0) -> None:
        """Wait for initial provisioning to complete. Called before first message."""
        try:
            await asyncio.wait_for(self._provisioned.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _log.warning(
                "Resource provisioning timed out for %s/%s — proceeding without resources",
                self._goal_id, self._agent_id,
            )

    async def stop(self) -> None:
        """Cancel the refresh loop. Called on session teardown."""
        self._stopped = True
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _refresh_done_callback(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            _log.error("Resource refresh loop died: %s", exc, exc_info=exc)

    # ── Internal ──────────────────────────────────────────────────────────

    async def _is_readonly_agent(self) -> bool:
        """Return True if this agent should get a READ-ONLY GitHub token.

        Layering:
          - SHCA — has shell + git + read-write token. The assistant
            writes task specs and needs to inspect the current state
            of a branch (clone, git log, code reads) to produce specs
            that match reality. Read-write is intentional: occasional
            commits for scratch / experiments are allowed; canonical
            work that ships still flows through task creation → IA.
          - RA   — has shell + git; needs only clone + read to inspect
            the diff. Read-only token closes off accidental pushes.
          - GO   — does NOT have shell or git access. It uses
            resource_* tools to inspect repos and doesn't need a
            github token at all. We still mark it readonly here as
            defense-in-depth: if someone ever re-adds exec to GO's
            allowlist without thinking, the token is already read-only.
          - IA   — work agent; full read-write to commit code.

        Lookup:
          - GO is identified by the `go-` id prefix (its
            gateway_agent_id isn't in agent_instances; lives on
            goals.go_agent_id).
          - Everything else: join agent_instances → agents.type.
        """
        # Cheap shortcut: GO has a distinctive id prefix.
        if self._agent_id and self._agent_id.startswith("go-"):
            return True

        pool = get_pool()
        agent_type = await pool.fetchval(
            """SELECT a.type
                 FROM agent_instances ai
                 JOIN agents a ON a.id = ai.template_id
                WHERE ai.gateway_agent_id = $1
                ORDER BY ai.created_at DESC
                LIMIT 1""",
            self._agent_id,
        )
        # Only the RA is read-only at the token level. SHCAs are write-
        # capable so they can clone + inspect + commit scratch work
        # during spec writing.
        return (agent_type or "").upper() == "RA"

    async def _generate_token_if_needed(self) -> Optional[str]:
        """Generate a GitHub installation token if the agent has git repo access."""
        pool = get_pool()

        # Read git repos from goal_resources only (legacy `resources` table is no
        # longer consulted — the GO framework owns the registry).
        # When story-scoped, only consider repos linked to that story.
        if self._story_id:
            all_git = await pool.fetch(
                """SELECT gr.name, COALESCE(gr.clone_url, gr.uri) AS uri,
                          'read-write' AS access_level
                   FROM story_resources sr
                   JOIN goal_resources gr ON gr.id = sr.resource_id
                   WHERE sr.story_id = $1 AND gr.status = 'active'
                     AND gr.kind IN ('repo', 'branch')
                     AND gr.repo_name IS NOT NULL AND gr.repo_name != ''""",
                self._story_id,
            )
        else:
            all_git = await pool.fetch(
                """SELECT name, COALESCE(clone_url, uri) AS uri,
                          'read-write' AS access_level
                   FROM goal_resources
                   WHERE goal_id = $1 AND kind IN ('repo', 'branch')
                     AND status = 'active'
                     AND repo_name IS NOT NULL AND repo_name != ''""",
                self._goal_id,
            )

        _log.info(
            "_generate_token_if_needed: agent=%s goal=%s story=%s found_repos=%d",
            self._agent_id, self._goal_id, self._story_id or "-", len(all_git),
        )

        if not all_git:
            return None

        repo_names = []
        for r in all_git:
            repo_name = _extract_github_repo_name(r["uri"] or "")
            if repo_name and repo_name not in repo_names:
                repo_names.append(repo_name)

        if not repo_names:
            return None

        # Planner agents (GO, SHCA) get read-only tokens regardless of
        # what the goal_resources grant. They can clone for inspection
        # but cannot push — commits happen ONLY via task-scoped IAs.
        readonly = await self._is_readonly_agent()
        if readonly:
            scope = "read"
        else:
            has_write = any(r["access_level"] == "read-write" for r in all_git)
            scope = "write" if has_write else "read"

        permissions = {
            "contents": scope,
            "metadata": "read",
            # Match contents scope so write agents can also push .github/workflows/*
            "workflows": scope,
        }

        try:
            token_data = await generate_installation_token(repo_names, permissions)
            _log.info(
                "GitHub token minted for agent=%s scope=%s readonly_agent=%s repos=%s",
                self._agent_id, scope, readonly, repo_names,
            )
            return token_data["token"]
        except Exception as exc:
            _log.warning("Failed to generate GitHub token for %s: %s", self._agent_id, exc)
            return None

    async def _write_resources_md(self, documents: Optional[list[dict]] = None) -> None:
        """Generate and write RESOURCES.md to the agent workspace.

        If a live session is attached, the write happens inside the
        session's refresh_guard so any send_to_gateway() call landing
        mid-rewrite waits for the new file. Initial provisioning (no
        session yet, or session whose _resources_ready is still clear)
        is unaffected: refresh_guard is a no-op when the event isn't
        set on enter.
        """
        resources_md = await generate_resources_md(
            self._agent_id, self._goal_id, self._current_token,
            story_id=self._story_id, documents=documents,
            # A story-scoped lifecycle is an operator story workspace whose repo
            # is pre-cloned at repo/ (shca.workspace.setup) — instruct Claude to
            # cd repo, not self-clone. IA workers use task_id (repo_precloned stays
            # False → self-clone into repos/).
            repo_precloned=bool(self._story_id),
        )
        # For operator/story SHCAs, the SHCA root view (webchat) and the
        # story workspace (coding Claude subprocess cwd + control-panel side
        # panel) are different dirs. Write BOTH so a mid-session refresh
        # propagates the same content to the internal story RESOURCES.md —
        # the root-only write left stories/<sid>/RESOURCES.md stale.
        story_rel = (
            f"stories/{self._story_id}/RESOURCES.md" if self._story_id else None
        )
        try:
            if self._session is not None:
                async with self._session.refresh_guard():
                    await self._gateway_admin.set_agent_file(
                        self._gateway_agent_id, "RESOURCES.md", resources_md,
                    )
                    if story_rel:
                        await self._gateway_admin.set_agent_file(
                            self._gateway_agent_id, story_rel, resources_md,
                        )
            else:
                await self._gateway_admin.set_agent_file(
                    self._gateway_agent_id, "RESOURCES.md", resources_md,
                )
                if story_rel:
                    await self._gateway_admin.set_agent_file(
                        self._gateway_agent_id, story_rel, resources_md,
                    )
            self.last_error = None  # Clear any previous error on success
            _log.info(
                "Wrote RESOURCES.md agent=%s goal=%s story=%s bytes=%d docs=%d",
                self._agent_id, self._goal_id, self._story_id or "-",
                len(resources_md), len(documents) if documents else 0,
            )
        except Exception as exc:
            await self._report_error(
                code="RESOURCE_FILE_WRITE_FAILED",
                message=f"Failed to write RESOURCES.md: {exc}",
                details={"agent_id": self._agent_id, "goal_id": self._goal_id,
                         "gateway_error": str(exc)},
                retryable=True,
            )

    async def _download_documents(self) -> list[dict]:
        """Pre-download story- (or goal-) scoped documents into this session's workspace.

        Binaries (PDF/DOCX/etc.) are pushed for every agent type when they
        are linked to the relevant scope — GO/SHCA/IA all benefit from the
        same docs. If the GO or the user's assistant decides a particular
        binary belongs to a task's workflow, linking it to the story is
        enough to make it appear in every agent (including IA) workspace
        that's scoped to that story.
        """
        return await download_documents_to_workspace(
            self._gateway_admin, self._gateway_agent_id, self._goal_id,
            story_id=self._story_id, agent_id=self._agent_id,
        )

    async def _refresh_loop(self) -> None:
        """Background loop that refreshes GitHub tokens before they expire.

        Runs every 50 minutes (tokens expire after 60 minutes).
        Also handles the case where an agent is paused waiting for human input —
        the loop runs independently of chat activity.
        """
        while not self._stopped:
            await asyncio.sleep(_TOKEN_REFRESH_INTERVAL_SECONDS)
            if self._stopped:
                return

            # Kill switch — skip the refresh while this goal (or the system) is
            # frozen. Fresh credentials extend a frozen agent's window of
            # operation (M4). On resume the next tick mints a fresh token.
            try:
                from app.services import execution_control as _ec  # noqa: PLC0415
                _blk = await _ec.dispatch_block(self._goal_id)
                if _blk:
                    _log.info(
                        "token refresh: goal=%s skipped — paused (scope=%s reason=%s)",
                        self._goal_id, _blk.scope, _blk.reason or "<none>",
                    )
                    continue
            except Exception as exc:
                _log.debug("token refresh: pause-check failed, proceeding anyway: %s", exc)

            _log.info("Refreshing credentials for agent %s in goal %s", self._agent_id, self._goal_id)
            try:
                self._current_token = await self._generate_token_if_needed()
                # Re-download docs on each refresh so newly added documents
                # (registered after session start) become available without
                # forcing a session restart.
                documents = await self._download_documents()
                await self._write_resources_md(documents)
                _log.info(
                    "Credentials refreshed agent=%s docs=%d",
                    self._agent_id, len(documents),
                )
            except Exception as exc:
                await self._report_error(
                    code="CREDENTIAL_REFRESH_FAILED",
                    message=f"Credential refresh failed: {exc} — agent may lose git access. Will retry in {_TOKEN_REFRESH_INTERVAL_SECONDS // 60} min.",
                    details={"agent_id": self._agent_id, "goal_id": self._goal_id},
                    retryable=True,
                )


# ── Session credential provisioning (legacy entrypoint) ─────────────────────

async def provision_session_credentials(
    gateway_admin,
    agent_id: str,
    gateway_agent_id: str,
    goal_id: str,
    on_error: Optional[Callable] = None,
    story_id: Optional[str] = None,
    session=None,
) -> ResourceLifecycle:
    """
    Provision credentials and RESOURCES.md for an agent session.

    Returns a ResourceLifecycle object that the session should hold onto
    for credential refresh and cleanup.

    Args:
        on_error: Optional async callback(code, message, details, retryable)
                  for surfacing provisioning/refresh failures to the browser.
        story_id: If provided, RESOURCES.md is scoped to this story's
                  needs/produces/modifies relations only.
        session:  Live _AgentSession (when called from openclaw's
                  _provision_resources). Passed through to the
                  ResourceLifecycle so background refreshes acquire the
                  session's refresh_guard when rewriting RESOURCES.md.
    """
    lifecycle = ResourceLifecycle(
        gateway_admin, agent_id, gateway_agent_id, goal_id,
        on_error=on_error, story_id=story_id, session=session,
    )
    await lifecycle.provision()
    return lifecycle


# ── Sync helper (called on resource changes) ────────────────────────────────

async def sync_resources_md(
    gateway_admin,
    agent_id: str,
    goal_id: str,
) -> None:
    """Regenerate and write RESOURCES.md for an agent. Called on access changes.

    Skips agents whose gateway image is per-conversation (SHCAs use instances
    keyed off the conversation thread, not `agents.gateway_agent_id`). Those
    agents are refreshed on demand via `refresh_story_resources` when they're
    actively in a story-mode conversation.

    Note: This does NOT refresh the GitHub token — it regenerates RESOURCES.md
    with whatever token is currently active. If the agent session has a
    ResourceLifecycle running, that will handle token refresh on schedule.
    """
    pool = get_pool()

    agent_row = await pool.fetchrow(
        "SELECT gateway_agent_id, type FROM agents WHERE id = $1", agent_id,
    )
    if not agent_row:
        _log.info("sync_resources_md: agent %s not found, skipping", agent_id)
        return

    if (agent_row.get("type") or "").upper() == "SHCA":
        # SHCAs use per-conversation gateway instances. Pushing to the global
        # gateway_agent_id always errors with "unknown agent id". The actual
        # refresh path is refresh_story_resources, called from the orchestrator
        # tool endpoints once a story-mode session is active.
        _log.info(
            "sync_resources_md: skipping SHCA %s (per-conversation instance)",
            agent_id,
        )
        return

    gw_id = agent_row.get("gateway_agent_id") or ""
    if not gw_id:
        _log.info(
            "sync_resources_md: agent %s has no gateway_agent_id, skipping",
            agent_id,
        )
        return

    # Generate without token (token lifecycle is managed by ResourceLifecycle)
    resources_md = await generate_resources_md(agent_id, goal_id)
    try:
        async with _refresh_gate(goal_id, agent_id):
            await gateway_admin.set_agent_file(gw_id, "RESOURCES.md", resources_md)
        _log.info("Synced RESOURCES.md for agent %s in goal %s", agent_id, goal_id)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md for %s: %s", agent_id, exc)


async def _gateway_agent_exists(gateway_admin, gw_id: str) -> bool:
    """Return True if the gateway currently reports this agent in agents.list.

    Kept around for potential future diagnostics. Do NOT use this as a gate
    before a refresh: agents.list returns only agents currently LOADED in
    gateway memory, not agents whose workspace files still live on disk. An
    idle SHCA/IA that the gateway evicted from memory will report false
    here, but responds fine when next messaged (the gateway auto-loads on
    chat.send). Previous callers used this as a guard and trashed the
    agent_instances DB row on a false miss — we now always attempt the
    write and let set_agent_file's own error path handle genuine reaps.
    """
    try:
        result = await gateway_admin.call("agents.list")
        agent_ids = [a.get("id", "") for a in (result or {}).get("agents", [])]
        return gw_id in agent_ids
    except Exception as exc:
        _log.warning("agents.list failed while checking %s: %s", gw_id, exc)
        return False


async def refresh_story_resources(goal_id: str, story_id: str) -> bool:
    """Refresh RESOURCES.md AND IDENTITY.md (and re-download docs) for the SHCA
    instance attached to this story.

    Architectural contract: the GO never creates an SHCA workspace — that
    happens exactly once during pick-shca import. This helper only updates
    files on an EXISTING instance. If the gateway no longer knows the
    instance (workspace reaped), we mark the agent_instances row destroyed
    and bail out so nothing is recreated implicitly.

    Called when story_resources actually changes:
      - grant_resource (subject_type='story')
      - revoke_resource (subject_type='story')
      - register_resource (with story_id)
      - create_branch (with story_id)
    """
    from app.openclaw import session_manager, gateway_admin, _build_identity_md

    _log.info("refresh_story_resources START goal=%s story=%s", goal_id, story_id)

    pool = get_pool()
    story = await pool.fetchrow(
        "SELECT active_thread_id, assigned_to FROM stories WHERE id = $1 AND goal_id = $2",
        story_id, goal_id,
    )
    if not story or not story["active_thread_id"] or not story["assigned_to"]:
        _log.info(
            "refresh_story_resources: no active SHCA for story=%s (active_thread_id=%s assigned_to=%s) — defer to next session",
            story_id,
            (story or {}).get("active_thread_id") or "-",
            (story or {}).get("assigned_to") or "-",
        )
        return False

    from app.services.agent_registry.assistant_queries import get_user_assistant_agent_id_by_uid
    agent_id = await get_user_assistant_agent_id_by_uid(pool, story["assigned_to"])
    if not agent_id:
        _log.info("refresh_story_resources: no active SHCA for user=%s", story["assigned_to"])
        return False
    thread_id = story["active_thread_id"]
    session_key = (goal_id, agent_id, thread_id)
    session = session_manager._sessions.get(session_key)

    if not session:
        _log.info(
            "refresh_story_resources: story=%s has active_thread_id=%s but no in-memory session — will refresh on next session start",
            story_id, thread_id[:12] if thread_id else "-",
        )
        return False

    instance_id = getattr(session, "_gateway_agent_id", None)
    if not instance_id:
        _log.info(
            "refresh_story_resources: session has no _gateway_agent_id (story=%s)",
            story_id,
        )
        return False

    # Each story owns its own per-conversation gateway instance — log which
    # one we're targeting so multi-story refreshes are unambiguous.
    _log.info(
        "refresh_story_resources: targeting instance=%s for story=%s thread=%s",
        instance_id, story_id, thread_id[:12] if thread_id else "-",
    )

    # Verify the gateway still knows this instance. If not, the workspace
    # has been reaped — mark the row destroyed instead of triggering an
    # implicit recreation (which would violate the "GO never creates SHCA
    # workspace" contract).
    #
    # Note: we used to guard with _gateway_agent_exists here and mark the
    # row destroyed on a miss. That was wrong — agents.list only returns
    # agents currently LOADED in gateway memory, not agents whose
    # workspace files still exist on disk. An idle SHCA responds fine
    # when next messaged (gateway auto-loads on chat.send), so bailing
    # on the refresh (and trashing the DB row) left its RESOURCES.md
    # stale forever. We now attempt the write unconditionally and rely
    # on set_agent_file's own error path if the workspace is genuinely
    # reaped.

    # Reuse the session's ResourceLifecycle so token + docs flow through one
    # path; afterwards rebuild IDENTITY.md (it may now reference new
    # branches, decisions, or upstream handoffs).
    resource_lifecycle = getattr(session, "_resource_lifecycle", None)
    if resource_lifecycle:
        resource_lifecycle._story_id = story_id
        try:
            documents = await resource_lifecycle._download_documents()
            await resource_lifecycle._write_resources_md(documents)
        except Exception as exc:
            _log.warning(
                "refresh_story_resources: RESOURCES.md write failed (story=%s): %s",
                story_id, exc,
            )
            return False
    else:
        # No lifecycle → fallback: regenerate RESOURCES.md without token refresh.
        # story-scoped ⇒ operator workspace with a pre-cloned repo/ (repo_precloned).
        new_md = await generate_resources_md(
            agent_id, goal_id, github_token=None, story_id=story_id,
            repo_precloned=True,
        )
        try:
            # Gate the write through the session so any concurrent
            # send_to_gateway waits for the new file before firing.
            async with session.refresh_guard():
                await gateway_admin.set_agent_file(instance_id, "RESOURCES.md", new_md)
        except Exception as exc:
            _log.warning(
                "refresh_story_resources fallback RESOURCES.md write failed for story %s: %s",
                story_id, exc,
            )
            return False

    # Refresh IDENTITY.md too — branch info, decisions, and upstream handoffs
    # all live there and need to reflect the new resource state.
    try:
        identity_md = await _build_identity_md(pool, goal_id, thread_id)
        await gateway_admin.set_agent_file(instance_id, "IDENTITY.md", identity_md)
        _log.info(
            "refresh_story_resources DONE goal=%s story=%s instance=%s identity_bytes=%d",
            goal_id, story_id, instance_id, len(identity_md),
        )
    except Exception as exc:
        _log.warning(
            "refresh_story_resources: IDENTITY.md write failed (story=%s): %s — RESOURCES.md still updated",
            story_id, exc,
        )
        # RESOURCES.md was written; don't return False just because IDENTITY.md failed.

    return True


async def refresh_task_workers_for_story(goal_id: str, story_id: str) -> int:
    """Refresh RESOURCES.md on every active IA persistent worker bound to a
    task that belongs to this story.

    Returns the number of workers refreshed. Used by the orchestrator
    endpoints alongside `refresh_story_resources` so IA workers see new
    resources mid-task without waiting for a phase boundary.
    """
    from app.openclaw import gateway_admin

    _log.info(
        "refresh_task_workers_for_story START goal=%s story=%s",
        goal_id, story_id,
    )
    pool = get_pool()

    workers = await pool.fetch(
        """SELECT ai.gateway_agent_id, ai.parent_task_id, ai.template_id, t.id AS task_id
             FROM agent_instances ai
             JOIN tasks t ON t.id = ai.parent_task_id
            WHERE ai.status = 'active'
              AND ai.worker_type = 'persistent'
              AND ai.goal_id = $1
              AND t.story_id = $2""",
        goal_id, story_id,
    )

    if not workers:
        _log.info(
            "refresh_task_workers_for_story: no active IA workers for goal=%s story=%s",
            goal_id, story_id,
        )
        return 0

    refreshed = 0
    for w in workers:
        gw_id = w["gateway_agent_id"]
        task_id = w["task_id"]
        template_id = w["template_id"]

        # Previously guarded by _gateway_agent_exists; see refresh_story_resources
        # for why that check was wrong (agents.list returns loaded agents,
        # not existing agents). Try the write directly; failures surface via
        # the inner try/except without trashing the DB row.

        # Generate fresh write-token for any repos the story needs.
        github_token = None
        try:
            git_repos = await pool.fetch(
                """SELECT gr.name, COALESCE(gr.clone_url, gr.uri) AS uri
                     FROM story_resources sr
                     JOIN goal_resources gr ON gr.id = sr.resource_id
                    WHERE sr.story_id = $1 AND gr.status = 'active'
                      AND gr.kind IN ('repo', 'branch')
                      AND gr.repo_name IS NOT NULL AND gr.repo_name != ''""",
                story_id,
            )
            repo_names = []
            for r in git_repos:
                rn = _extract_github_repo_name(r["uri"] or "")
                if rn and rn not in repo_names:
                    repo_names.append(rn)
            if repo_names:
                token_data = await generate_installation_token(
                    repo_names, {"contents": "write", "metadata": "read", "workflows": "write"},
                )
                github_token = token_data["token"]
        except Exception as exc:
            _log.warning(
                "refresh_task_workers_for_story: token gen failed for task %s: %s",
                task_id, exc,
            )

        # Pre-download docs into the IA workspace (story-scoped).
        try:
            documents = await download_documents_to_workspace(
                gateway_admin, gw_id, goal_id,
                story_id=story_id, agent_id=template_id,
            )
        except Exception as exc:
            _log.warning(
                "refresh_task_workers_for_story: doc download failed for task %s: %s",
                task_id, exc,
            )
            documents = []

        try:
            md = await generate_resources_md(
                template_id, goal_id, github_token=github_token,
                task_id=task_id, documents=documents,
            )
            async with _refresh_gate(goal_id, template_id):
                await gateway_admin.set_agent_file(gw_id, "RESOURCES.md", md)
            refreshed += 1
            _log.info(
                "refresh_task_workers_for_story: refreshed task=%s template=%s gw=%s docs=%d bytes=%d",
                task_id, template_id, gw_id, len(documents), len(md),
            )
        except Exception as exc:
            _log.warning(
                "refresh_task_workers_for_story: RESOURCES.md write failed for task %s: %s",
                task_id, exc,
            )

    _log.info(
        "refresh_task_workers_for_story DONE goal=%s story=%s refreshed=%d/%d",
        goal_id, story_id, refreshed, len(workers),
    )
    return refreshed


async def refresh_task_worker(goal_id: str, task_id: str) -> bool:
    """Refresh RESOURCES.md on the single IA persistent worker bound to a
    task. Called after a task-level allocation change (grant/revoke) so
    the IA sees the SHCA's additions or exclusions immediately.

    Returns True on success, False if no active worker or refresh failed.
    """
    from app.openclaw import gateway_admin

    pool = get_pool()
    worker = await pool.fetchrow(
        """SELECT ai.gateway_agent_id, ai.template_id, t.story_id
             FROM agent_instances ai
             JOIN tasks t ON t.id = ai.parent_task_id
            WHERE ai.status = 'active'
              AND ai.worker_type = 'persistent'
              AND ai.goal_id = $1
              AND ai.parent_task_id = $2""",
        goal_id, task_id,
    )
    if not worker:
        _log.info(
            "refresh_task_worker: no active worker for goal=%s task=%s",
            goal_id, task_id,
        )
        return False

    gw_id = worker["gateway_agent_id"]
    template_id = worker["template_id"]
    story_id = worker["story_id"]

    # See refresh_story_resources: agents.list-based guards are unreliable
    # and previously caused us to mark idle agents destroyed. Skip the
    # guard; set_agent_file's own error path handles truly missing agents.

    # Fresh write token for any repo the task's story touches.
    github_token = None
    if story_id:
        try:
            git_repos = await pool.fetch(
                """SELECT gr.name, COALESCE(gr.clone_url, gr.uri) AS uri
                     FROM story_resources sr
                     JOIN goal_resources gr ON gr.id = sr.resource_id
                    WHERE sr.story_id = $1 AND gr.status = 'active'
                      AND gr.kind IN ('repo', 'branch')
                      AND gr.repo_name IS NOT NULL AND gr.repo_name != ''""",
                story_id,
            )
            repo_names: list[str] = []
            for r in git_repos:
                rn = _extract_github_repo_name(r["uri"] or "")
                if rn and rn not in repo_names:
                    repo_names.append(rn)
            if repo_names:
                token_data = await generate_installation_token(
                    repo_names, {"contents": "write", "metadata": "read", "workflows": "write"},
                )
                github_token = token_data["token"]
        except Exception as exc:
            _log.warning(
                "refresh_task_worker: token gen failed for task %s: %s",
                task_id, exc,
            )

    try:
        documents = await download_documents_to_workspace(
            gateway_admin, gw_id, goal_id,
            story_id=story_id or None, agent_id=template_id,
        )
    except Exception as exc:
        _log.warning(
            "refresh_task_worker: doc download failed for task %s: %s",
            task_id, exc,
        )
        documents = []

    try:
        md = await generate_resources_md(
            template_id, goal_id, github_token=github_token,
            task_id=task_id, documents=documents,
        )
        async with _refresh_gate(goal_id, template_id):
            await gateway_admin.set_agent_file(gw_id, "RESOURCES.md", md)
        _log.info(
            "refresh_task_worker: refreshed task=%s template=%s gw=%s bytes=%d",
            task_id, template_id, gw_id, len(md),
        )
        return True
    except Exception as exc:
        _log.warning(
            "refresh_task_worker: RESOURCES.md write failed for task %s: %s",
            task_id, exc,
        )
        return False


async def remove_document_from_all_workspaces(
    goal_id: str, local_path: str,
) -> dict:
    """Delete `local_path` (e.g. "docs/my.pdf") from every live agent
    workspace in the goal (GO + SHCAs + IAs), then refresh each
    session's RESOURCES.md so the deleted resource no longer appears.

    Called on hard-delete of a goal resource — after the DB row is
    marked deleted and the GCS blob is removed.

    Returns the same summary shape as refresh_all_goal_sessions.
    """
    from app.openclaw import session_manager, gateway_admin

    pool = get_pool()
    summary = {"go": False, "shcas": 0, "ia_workers": 0}

    # 1) GO
    go_agent_id = await pool.fetchval(
        "SELECT go_agent_id FROM goals WHERE id = $1", goal_id,
    )
    if go_agent_id:
        try:
            await gateway_admin.delete_agent_file(go_agent_id, local_path)
        except Exception as exc:
            _log.info(
                "remove_document_from_all_workspaces: GO %s delete %s: %s",
                go_agent_id, local_path, exc,
            )
        try:
            docs = await download_documents_to_workspace(
                gateway_admin, go_agent_id, goal_id, agent_id=go_agent_id,
            )
            md = await generate_resources_md(
                go_agent_id, goal_id, github_token=None, documents=docs,
            )
            async with _refresh_gate(goal_id, go_agent_id):
                await gateway_admin.set_agent_file(go_agent_id, "RESOURCES.md", md)
            summary["go"] = True
        except Exception as exc:
            _log.warning(
                "remove_document_from_all_workspaces: GO refresh failed: %s", exc,
            )

    # 2) SHCAs
    shcas = await pool.fetch(
        """SELECT ai.gateway_agent_id, ai.template_id, ai.thread_id
             FROM agent_instances ai
             JOIN agents a ON a.id = ai.template_id
            WHERE ai.status = 'active'
              AND ai.goal_id = $1
              AND a.type = 'SHCA'""",
        goal_id,
    )
    for s in shcas:
        gw_id = s["gateway_agent_id"]
        template_id = s["template_id"]
        thread_id = s["thread_id"]
        try:
            await gateway_admin.delete_agent_file(gw_id, local_path)
        except Exception as exc:
            _log.info(
                "remove_document_from_all_workspaces: SHCA %s delete %s: %s",
                gw_id, local_path, exc,
            )
        try:
            story_id = await pool.fetchval("""
                SELECT s.id FROM stories s
                 WHERE s.active_thread_id = $1 LIMIT 1
            """, thread_id) or None
            docs = await download_documents_to_workspace(
                gateway_admin, gw_id, goal_id,
                story_id=story_id, agent_id=template_id,
            )
            md = await generate_resources_md(
                template_id, goal_id, github_token=None,
                story_id=story_id, documents=docs,
                # story-mode SHCA ⇒ pre-cloned repo/ (operator workspace)
                repo_precloned=bool(story_id),
            )
            async with _refresh_gate(goal_id, template_id, thread_id):
                await gateway_admin.set_agent_file(gw_id, "RESOURCES.md", md)
            summary["shcas"] += 1
        except Exception as exc:
            _log.warning(
                "remove_document_from_all_workspaces: SHCA refresh failed for %s: %s",
                gw_id, exc,
            )

    # 3) IA workers
    workers = await pool.fetch(
        """SELECT gateway_agent_id, parent_task_id, template_id, task_id
             FROM agent_instances
            WHERE status = 'active'
              AND worker_type = 'persistent'
              AND goal_id = $1""",
        goal_id,
    )
    for w in workers:
        gw_id = w["gateway_agent_id"]
        template_id = w["template_id"]
        task_id = w["parent_task_id"] or w["task_id"]
        try:
            await gateway_admin.delete_agent_file(gw_id, local_path)
        except Exception as exc:
            _log.info(
                "remove_document_from_all_workspaces: IA %s delete %s: %s",
                gw_id, local_path, exc,
            )
        try:
            story_id = await pool.fetchval(
                "SELECT story_id FROM tasks WHERE id = $1", task_id,
            ) or None
            docs = await download_documents_to_workspace(
                gateway_admin, gw_id, goal_id,
                story_id=story_id, agent_id=template_id,
            )
            md = await generate_resources_md(
                template_id, goal_id, github_token=None,
                task_id=task_id, documents=docs,
            )
            async with _refresh_gate(goal_id, template_id):
                await gateway_admin.set_agent_file(gw_id, "RESOURCES.md", md)
            summary["ia_workers"] += 1
        except Exception as exc:
            _log.warning(
                "remove_document_from_all_workspaces: IA refresh failed for %s: %s",
                gw_id, exc,
            )

    _log.info(
        "remove_document_from_all_workspaces DONE goal=%s path=%s go=%s shcas=%d ia_workers=%d",
        goal_id, local_path, summary["go"], summary["shcas"], summary["ia_workers"],
    )
    return summary


async def refresh_all_goal_sessions(goal_id: str) -> dict:
    """Refresh RESOURCES.md (and pre-download new docs) for EVERY active
    session in this goal — the GO, all active SHCAs, and all active IA
    persistent workers.

    Used when a goal-wide resource is added (e.g. user uploads a file from
    the chat page). The newly registered resource needs to appear in every
    agent's workspace immediately.

    Returns a summary dict: {go: bool, shcas: int, ia_workers: int}.
    """
    from app.openclaw import session_manager, gateway_admin

    _log.info("refresh_all_goal_sessions START goal=%s", goal_id)
    pool = get_pool()
    summary = {"go": False, "shcas": 0, "ia_workers": 0}

    # 1) GO session — gateway_agent_id is just go-{goal_id}
    go_agent_id = await pool.fetchval(
        "SELECT go_agent_id FROM goals WHERE id = $1", goal_id,
    )
    if go_agent_id:
        try:
            docs = await download_documents_to_workspace(
                gateway_admin, go_agent_id, goal_id, agent_id=go_agent_id,
            )
            md = await generate_resources_md(
                go_agent_id, goal_id, github_token=None, documents=docs,
            )
            async with _refresh_gate(goal_id, go_agent_id):
                await gateway_admin.set_agent_file(go_agent_id, "RESOURCES.md", md)
            summary["go"] = True
            _log.info(
                "refresh_all_goal_sessions: GO refreshed goal=%s docs=%d bytes=%d",
                goal_id, len(docs), len(md),
            )
        except Exception as exc:
            _log.warning("refresh_all_goal_sessions: GO refresh failed: %s", exc)

    # 2) SHCA instances — every active per-conversation instance in this goal
    shca_instances = await pool.fetch(
        """SELECT ai.gateway_agent_id, ai.template_id, ai.thread_id
             FROM agent_instances ai
             JOIN agents a ON a.id = ai.template_id
            WHERE ai.status = 'active'
              AND ai.goal_id = $1
              AND a.type = 'SHCA'""",
        goal_id,
    )
    for shca in shca_instances:
        gw_id = shca["gateway_agent_id"]
        template_id = shca["template_id"]
        thread_id = shca["thread_id"]

        # Resolve story_id (if this thread is a story-mode conversation)
        story_id = await pool.fetchval(
            "SELECT id FROM stories WHERE goal_id = $1 AND active_thread_id = $2",
            goal_id, thread_id,
        )

        # Don't gate on _gateway_agent_exists — it only reports loaded
        # agents and caused us to trash DB rows for idle SHCAs that were
        # still disk-resident. Let set_agent_file fail naturally if the
        # agent is truly gone.

        try:
            # Try to use the live ResourceLifecycle (token-aware) if there's
            # an active in-memory session for this instance.
            session_key = (goal_id, template_id, thread_id)
            session = session_manager._sessions.get(session_key)
            lifecycle = getattr(session, "_resource_lifecycle", None) if session else None

            if lifecycle:
                lifecycle._story_id = story_id
                docs = await lifecycle._download_documents()
                await lifecycle._write_resources_md(docs)
            else:
                docs = await download_documents_to_workspace(
                    gateway_admin, gw_id, goal_id,
                    story_id=story_id, agent_id=template_id,
                )
                md = await generate_resources_md(
                    template_id, goal_id, github_token=None,
                    story_id=story_id, documents=docs,
                    # story-mode SHCA ⇒ pre-cloned repo/ (operator workspace)
                    repo_precloned=bool(story_id),
                )
                async with _refresh_gate(goal_id, template_id, thread_id):
                    await gateway_admin.set_agent_file(gw_id, "RESOURCES.md", md)

            summary["shcas"] += 1
            _log.info(
                "refresh_all_goal_sessions: SHCA refreshed gw=%s story=%s docs=%d",
                gw_id, story_id or "-", len(docs),
            )
        except Exception as exc:
            _log.warning(
                "refresh_all_goal_sessions: SHCA refresh failed for %s: %s", gw_id, exc,
            )

    # 3) IA persistent workers — every active worker in this goal
    workers = await pool.fetch(
        """SELECT gateway_agent_id, parent_task_id, template_id, task_id
             FROM agent_instances
            WHERE status = 'active'
              AND worker_type = 'persistent'
              AND goal_id = $1""",
        goal_id,
    )
    for w in workers:
        gw_id = w["gateway_agent_id"]
        template_id = w["template_id"]
        task_id = w["parent_task_id"] or w["task_id"]

        # Don't gate on _gateway_agent_exists — see SHCA branch above.

        try:
            # Resolve story for this task
            story_id = await pool.fetchval(
                "SELECT story_id FROM tasks WHERE id = $1", task_id,
            ) or None
            # IA workers receive whatever docs are linked to their
            # task's parent story. GO/SHCA can explicitly attach a
            # binary resource (PDF spec, design doc, etc.) by linking
            # it to the story — download_documents_to_workspace picks
            # it up the same way it does for SHCAs.
            docs = await download_documents_to_workspace(
                gateway_admin, gw_id, goal_id,
                story_id=story_id, agent_id=template_id,
            )
            md = await generate_resources_md(
                template_id, goal_id, github_token=None,
                task_id=task_id, documents=docs,
            )
            async with _refresh_gate(goal_id, template_id):
                await gateway_admin.set_agent_file(gw_id, "RESOURCES.md", md)
            summary["ia_workers"] += 1
            _log.info(
                "refresh_all_goal_sessions: IA worker refreshed gw=%s task=%s docs=%d",
                gw_id, task_id, len(docs),
            )
        except Exception as exc:
            _log.warning(
                "refresh_all_goal_sessions: IA refresh failed for %s: %s", gw_id, exc,
            )

    _log.info(
        "refresh_all_goal_sessions DONE goal=%s go=%s shcas=%d ia_workers=%d",
        goal_id, summary["go"], summary["shcas"], summary["ia_workers"],
    )
    return summary


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_github_repo_name(uri: str) -> Optional[str]:
    """Extract the repo name (without org) from a GitHub URL.

    e.g. "https://github.com/org/my-repo" → "my-repo"
         "https://github.com/org/my-repo.git" → "my-repo"
    """
    if "github.com" not in uri:
        return None
    parts = uri.rstrip("/").rstrip(".git").split("/")
    if len(parts) >= 2:
        return parts[-1]
    return None
