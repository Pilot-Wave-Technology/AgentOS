"""Goal resource management — CRUD, upload, locking, scanning.

Resources are tracked per goal: repos, branches, documents, files, etc.
Humans can add resources, upload files, lock/unlock for write access.
GO agent uses tools to read summaries and scan resources.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import html as _html_lib
import json
import logging
import os
import time as _time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user, get_goal_role
from app.roles import SystemRole, GoalRole
from app.database import get_pool
from app.ledger.journal import append_event
from app.ledger.schema import (
    GOAL_RESOURCE_REGISTERED, GOAL_RESOURCE_LOCKED, GOAL_RESOURCE_UNLOCKED,
    GOAL_RESOURCE_ARCHIVED,
)
from app.notifications import push_notification
from app.services.task_runner_trigger import trigger_task_runner

_log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

from app.template_filters import register_filters as _register_filters  # noqa: E402
_register_filters(templates.env)

# File types we accept for upload
ALLOWED_FILE_TYPES = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "yaml": "application/x-yaml",
    "yml": "application/x-yaml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB


# ── Resource browser page ────────────────────────────────────────────────────

@router.get("/goals/{pid}/resources/browser")
async def resource_browser_redirect(pid: str):
    """Redirect legacy /resources/browser URL to /resources."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/goals/{pid}/resources", status_code=301)


@router.get("/goals/{pid}/resources", response_class=HTMLResponse)
async def resource_browser(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Resource browser — tree view of all goal resources."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1 AND status = 'active'", pid)
    if not goal:
        raise HTTPException(status_code=404)

    # `?show_archived=1` includes archived rows so the operator can find
    # and unarchive a resource they accidentally archived. Off by default
    # to keep the default browser view clean.
    show_archived = (request.query_params.get("show_archived") or "").lower() in (
        "1", "true", "yes", "on",
    )

    # Fetch all resources for this goal.
    if show_archived:
        status_filter = "AND gr.status IN ('active', 'archived')"
    else:
        status_filter = "AND gr.status = 'active'"
    rows = await pool.fetch(f"""
        SELECT gr.*,
               rs.content AS summary_preview,
               rs.scanned_at AS last_scan_time
        FROM goal_resources gr
        LEFT JOIN LATERAL (
            SELECT content, scanned_at FROM resource_summaries
            WHERE resource_id = gr.id ORDER BY created_at DESC LIMIT 1
        ) rs ON true
        WHERE gr.goal_id = $1 {status_filter}
        ORDER BY gr.kind, gr.name
    """, pid)
    resources = [dict(r) for r in rows]
    # WHICH ROWS HAVE BYTES A PERSON CAN TAKE AWAY. Worked out here and
    # not in the template, because "downloadable" is two different facts
    # -- an uploaded document has a `gcs_uri`, and a catalogued artifact
    # has none and lives in the artifact store under an id derived from
    # its own -- and a template that had to know both would be a third
    # place to keep that in step.
    for r in resources:
        r["downloadable"] = bool(r.get("gcs_uri")) or             str(r.get("id") or "").startswith("art_")

    # Build tree (group by parent). An orphan -- a row whose parent is not
    # in this result set -- is RE-ROOTED rather than dropped; see
    # `resource_tree` for the goal that lost three lanes to the old rule.
    from app.services.resource_tree import build_tree
    root_resources, children_map = build_tree(resources)

    # A REPO WHOSE LANES CARRY THE SUMMARIES IS NOT A REPO MISSING ONE.
    # `scan_resource` skips the repo-level summary once the goal has
    # declared trunk lanes -- work happens at branch level and a repo
    # card can only show one lane, so it would contradict the cards
    # beneath it. The panel had one word for both cases: "no summary",
    # with the tooltip "open the resource and click Rescan". Clicking it
    # calls `refresh_identity`, which declines for the same reason, so
    # the pill asked for something nothing would ever do.
    laned_repos = {
        r["parent_id"] for r in resources
        if r.get("kind") == "branch"
        and (r.get("role") or "") == "trunk"
        and r.get("parent_id")
    }

    # Fetch resources from user's OTHER goals (for "Link Existing" dropdown)
    other_resources = await pool.fetch("""
        SELECT gr.id, gr.name, gr.kind, gr.goal_id, g.name AS goal_name,
               gr.repo_owner, gr.repo_name, gr.branch_name, gr.gcs_uri, gr.file_type
        FROM goal_resources gr
        JOIN goals g ON g.id = gr.goal_id
        JOIN goal_members gm ON gm.goal_id = gr.goal_id
        WHERE gm.uid = $1 AND gr.goal_id != $2
          AND gr.status = 'active' AND g.status = 'active'
        ORDER BY g.name, gr.name
    """, user["uid"], pid)
    linkable_resources = [dict(r) for r in other_resources]

    # Check impersonation permissions for all GCP SA resources in one pass.
    # Runs concurrently so 2 SAs = 2 parallel IAM calls, not sequential.
    # sa_status_map: resource_id → "ok" | "no_permission" | "error" | "unknown"
    sa_status_map: dict[str, str] = {}
    sa_rows = [r for r in resources if r.get("kind") in ("gcp_test_sa", "gcp_deploy_sa")]
    if sa_rows:
        from app.gcp_credentials import get_self_sa_email  # noqa: PLC0415
        self_email = await get_self_sa_email()
        if self_email:
            import asyncio as _asyncio  # noqa: PLC0415
            import json as _json  # noqa: PLC0415

            def _extract_sa_email(r: dict) -> str:
                meta = r.get("metadata")
                if isinstance(meta, str) and meta:
                    try:
                        meta = _json.loads(meta)
                    except Exception:
                        return ""
                return (meta or {}).get("sa_email", "")

            checks = await _asyncio.gather(*[
                _check_sa_impersonation(_extract_sa_email(r), self_email)
                for r in sa_rows
            ])
            sa_status_map = {r["id"]: chk["status"] for r, chk in zip(sa_rows, checks)}
            _log.info(
                "resource_browser: SA permission checks for goal %s: %s",
                pid, sa_status_map,
            )

    # Goal documents — one pinned read-only entry (Feature Overview) at the
    # top of the browser. Work Documentation (doc_type='code') isn't surfaced
    # in the UI yet — see the hidden switch-link comment in goals/summary.html.
    import os as _os
    import datetime as _dt
    _interval_minutes = int(_os.environ.get("GOAL_SUMMARY_INTERVAL_MINUTES", 25))

    doc_rows = await pool.fetch(
        "SELECT doc_type, generated_at, requested_at, length(content) AS chars "
        "FROM goal_documents WHERE goal_id = $1",
        pid,
    )
    _now = _dt.datetime.now(_dt.timezone.utc)

    def _doc_info(row):
        if not row:
            return {"generated_at": None, "requested_at": None, "chars": None, "next_trigger": None}
        requested_at = row["requested_at"]
        next_trigger = None
        if requested_at:
            next_due = requested_at + _dt.timedelta(minutes=_interval_minutes)
            delta = (next_due - _now).total_seconds()
            if delta <= 60:
                next_trigger = "due soon"
            else:
                mins = int(delta / 60)
                next_trigger = f"~{mins} min"
        return {
            "generated_at": row["generated_at"],
            "requested_at": row["requested_at"],
            "chars": row["chars"],
            "next_trigger": next_trigger,
        }

    doc_by_type = {r["doc_type"]: r for r in doc_rows}
    goal_doc_progress = _doc_info(doc_by_type.get("progress"))

    from app.services.tour_progress import check_and_mark_milestone_if_enabled
    auto_fire_tour = await check_and_mark_milestone_if_enabled(user, "resources-intro")

    return templates.TemplateResponse(
        "resources/browser.html",
        {
            "request": request,
            "user": user,
            "goal": dict(goal),
            "pid": pid,
            "role": role or GoalRole.GOAL_ADMIN,
            "resources": root_resources,
            "children_map": children_map,
            "laned_repos": laned_repos,
            "resource_count": len(resources),
            "linkable_resources": linkable_resources,
            "show_archived": show_archived,
            "sa_status_map": sa_status_map,
            "goal_doc_progress": goal_doc_progress,
            "auto_fire_tour": auto_fire_tour,
        },
    )


# ── Add resource ─────────────────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/add")
async def add_resource(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Add a resource to the goal (repo, document, service, etc.)."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    form = await request.form()
    kind = form.get("kind", "other")
    name = form.get("name", "").strip()
    uri = form.get("uri", "").strip()
    human_context = form.get("human_context", "").strip()

    # The "Cloud Resource" dropdown is the operator-facing entry; the
    # actual storage kind depends on the selected provider. For now only
    # GCP is wired up — picking GCP creates a `gcp_environment` row plus
    # 0/1 optional `gcp_test_sa` and 0/1 optional `gcp_deploy_sa` child
    # rows (see _create_cloud_resource_with_sas below).
    if kind == "cloud_resource":
        provider = (form.get("cloud_provider", "gcp") or "gcp").strip().lower()
        if provider == "gcp":
            return await _create_gcp_resource_with_sas(
                pid=pid, user=user, form=form, name=name,
                human_context=human_context,
            )
        raise HTTPException(
            status_code=400,
            detail=f"Cloud provider {provider!r} is not supported yet — only 'gcp' is wired up.",
        )
    # role applies to kind='branch' — default 'feature'. Accepts 'trunk',
    # 'feature', 'release'. For kind='repo', this is read from the
    # working_branch_is_trunk checkbox (handled below).
    role = (form.get("role", "feature") or "feature").strip().lower()
    if role not in ("trunk", "feature", "release"):
        role = "feature"

    _log.info(
        "Add resource: kind=%s name=%s uri=%s branch_name=%s parent_repo_id=%s role=%s "
        "working_branch=%s",
        kind, name, uri, form.get("branch_name", ""),
        form.get("parent_repo_id", ""), role,
        form.get("working_branch", ""),
    )

    pool = get_pool()
    resource_id = str(uuid.uuid4())[:12]

    # Build metadata JSONB for kinds that carry structured config.
    # Other kinds leave metadata as NULL (the column default).
    metadata: dict | None = None
    if kind == "gcp_environment":
        metadata = {
            "target_project_id": form.get("gcp_target_project_id", "").strip(),
            "target_region": form.get("gcp_target_region", "").strip(),
            "test_service_account": form.get("gcp_test_service_account", "").strip(),
            "deploy_service_account": form.get("gcp_deploy_service_account", "").strip(),
        }

    # Parse repo-specific fields
    repo_owner = ""
    repo_name = ""
    default_branch = "main"
    clone_url = ""
    parent_id = None
    branch_name = ""
    base_branch = ""

    # When kind='repo', optionally also create a branch row in one go.
    working_branch = ""
    working_branch_is_trunk = False
    working_branch_id = ""  # filled in after the branch row insert
    # Same reason, and it is load-bearing: the scan below reads it on
    # every repo path, and G5 only assigns it when no working branch was
    # named. Undefined here is a NameError in the success case.
    trunk_row_id = ""
    existing_repo = None    # the goal's existing row for this repo, if any
    revive_lane = None      # a retired lane row for this branch, to bring back

    if kind == "repo":
        if not name and uri:
            # Auto-name from URL
            parts = uri.rstrip("/").split("/")
            name = parts[-1].replace(".git", "") if parts else ""
        if not name:
            raise HTTPException(status_code=400, detail="Resource name required")
        if uri:
            parts = uri.rstrip("/").split("/")
            if "github.com" in uri and len(parts) >= 2:
                repo_name = parts[-1].replace(".git", "")
                repo_owner = parts[-2]
                clone_url = uri
                default_branch = form.get("default_branch", "main") or "main"

        # Optional one-shot: also add a branch resource for the working
        # branch (typically 'dev' or similar). If the trunk checkbox is
        # set, the branch row is created with role='trunk' so the GO/
        # planner uses it as the source for feature branches and the
        # merge target in review stories.
        working_branch = (form.get("working_branch", "") or "").strip()
        working_branch_is_trunk = (
            form.get("working_branch_is_trunk", "") in ("on", "true", "1", "yes")
        )
        if not working_branch:
            raise HTTPException(status_code=400, detail="Working branch required")

        # THE SAME REPO AGAIN IS A NEW TRUNK, NOT A DUPLICATE. The
        # branch-first flow registers "repo + this branch as trunk" in
        # one act; governing a second branch of a repo the goal already
        # holds must reuse the existing repo row as the parent -- the
        # old behaviour was a unique-constraint 400 that made
        # multi-trunk monorepos impossible to register at all.
        if repo_owner and repo_name:
            existing_repo = await pool.fetchrow(
                "SELECT id FROM goal_resources WHERE goal_id = $1 "
                "AND kind = 'repo' AND repo_owner = $2 AND repo_name = $3",
                pid, repo_owner, repo_name)
            # A SECOND LANE IS WELCOME; A DUPLICATE IS NOT. Each branch
            # registered here is its own trunk LANE -- the engine keys
            # nodes per lane (git_nodes.trunk_branch, git_repo_trunks per
            # (repo, trunk)), and a story lands on the lane GO granted
            # it. Only the exact same branch twice is refused.
            if working_branch_is_trunk:
                # A RETIRED LANE IS RE-ADDABLE. This check had no status
                # filter, so a lane that had been deleted or archived went
                # on refusing its own branch for ever -- and `unarchive`
                # only accepts status='archived', so a DELETED lane could
                # not be restored either. Between them, deleting a trunk
                # lane was irreversible through the UI: exactly the dead
                # end the operator in goal GOAL1 was in. Found on dev the
                # first time anyone deleted a lane and tried again
                # (MREP1, 2026-10-06).
                #
                # The retired row is REVIVED rather than duplicated. Its id
                # is what `story_resources` grants and `parent_id` chains
                # point at, so bringing it back is both cheaper and more
                # honest than minting a second row for the same branch --
                # and the INSERT below carries
                # `ON CONFLICT (goal_id, kind, name) DO NOTHING`, so a
                # duplicate would have silently produced no row at all.
                dup = await pool.fetchval(
                    "SELECT 1 FROM goal_resources "
                    "WHERE goal_id = $1 AND kind = 'branch' "
                    "AND repo_owner = $2 AND repo_name = $3 "
                    "AND role = 'trunk' AND branch_name = $4 "
                    "AND coalesce(status, 'active') = 'active'",
                    pid, repo_owner, repo_name, working_branch)
                if not dup:
                    revive_lane = await pool.fetchval(
                        "SELECT id FROM goal_resources "
                        "WHERE goal_id = $1 AND kind = 'branch' "
                        "AND repo_owner = $2 AND repo_name = $3 "
                        "AND branch_name = $4 "
                        "AND coalesce(status, 'active') <> 'active' "
                        "ORDER BY created_at DESC LIMIT 1",
                        pid, repo_owner, repo_name, working_branch)
                if dup:
                    raise HTTPException(
                        status_code=400,
                        detail=(f"'{working_branch}' is already a trunk "
                                f"lane of {repo_name} in this goal."))

    elif kind == "branch":
        parent_repo_id = form.get("parent_repo_id", "").strip()
        branch_name = form.get("branch_name", "").strip()
        base_branch = form.get("base_branch", "main").strip()
        branch_uri = uri.strip()

        if parent_repo_id:
            # Branch under an existing repo resource
            parent = await pool.fetchrow(
                "SELECT * FROM goal_resources WHERE id = $1 AND goal_id = $2 AND kind = 'repo'",
                parent_repo_id, pid,
            )
            if parent:
                parent_id = parent_repo_id
                repo_owner = parent.get("repo_owner", "")
                repo_name = parent.get("repo_name", "")
                clone_url = parent.get("clone_url", "")

        if branch_uri:
            # Parse repo info from URL (works with or without parent)
            # Supports: https://github.com/owner/repo or https://github.com/owner/repo/tree/branch
            parts = branch_uri.rstrip("/").split("/")
            if "github.com" in branch_uri and len(parts) >= 2:
                if "/tree/" in branch_uri:
                    tree_idx = parts.index("tree")
                    repo_name = parts[tree_idx - 1].replace(".git", "")
                    repo_owner = parts[tree_idx - 2]
                    if not branch_name:
                        branch_name = "/".join(parts[tree_idx + 1:])
                else:
                    repo_name = parts[-1].replace(".git", "")
                    repo_owner = parts[-2]
                clone_url = f"https://github.com/{repo_owner}/{repo_name}"
                uri = clone_url  # store clean repo URL

        if not branch_name:
            raise HTTPException(status_code=400, detail="Branch name required")

        # Auto-generate display name: "repo:branch"
        if repo_name and branch_name:
            name = name or f"{repo_name}:{branch_name}"
        else:
            name = name or branch_name
    elif kind == "gcp_environment":
        if not name:
            raise HTTPException(status_code=400, detail="Resource name required")
    else:
        if not name:
            raise HTTPException(status_code=400, detail="Resource name required")

    # Pre-check for duplicate (goal_id, kind, name) — uq_goal_resource_kind_name.
    # Surface a friendly 400 instead of letting the constraint produce a 500.
    #
    # EXCEPT for a repo the goal already holds: the branch-first flow adds
    # the same repo again to register ANOTHER trunk lane, and the existing
    # repo row is deliberately REUSED as the parent (see existing_repo
    # below). This pre-check firing first is exactly how adding
    # cal_backend after cal_frontend died with "already registered" —
    # the lane dedupe never got to run.
    existing = await pool.fetchrow(
        """SELECT id, status FROM goal_resources
            WHERE goal_id = $1 AND kind = $2 AND name = $3""",
        pid, kind, name,
    )
    if existing and kind == "repo" and existing_repo:
        existing = None  # the reuse path owns this case
    if existing:
        _log.info(
            "Resource already exists: goal=%s kind=%s name=%s rid=%s status=%s",
            pid, kind, name, existing["id"], existing["status"],
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"A {kind} named '{name}' is already registered for this goal "
                f"(resource id {existing['id']}, status={existing['status']}). "
                f"If you want a different one, give it a different display name."
            ),
        )

    # THE EVENT NAMES THE ROW THAT ENDS UP EXISTING. The dedupe below can
    # rebind `resource_id` to the goal's existing repo row, and the event
    # used to be appended BEFORE that happened -- so a re-registration
    # logged `resource.goal.registered` for a freshly minted id that was
    # then thrown away, with no `goal_resources` row and no
    # `resource_events` row to match. Goal GOAL1 has one such ghost
    # (`01b84264-b1a`, 2026-10-05 12:42). Deciding the id first makes the
    # ledger describe what happened: a reuse, with the id reused.
    reused = bool(kind == "repo" and existing_repo)
    if reused:
        resource_id = existing_repo["id"]

    import asyncpg
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    event_type=GOAL_RESOURCE_REGISTERED,
                    payload={
                        "resource_id": resource_id,
                        "kind": kind,
                        "name": name,
                        "uri": uri,
                        "branch_name": branch_name,
                        "parent_id": parent_id or "",
                        "reused": reused,
                    },
                    actor_id=user["actor_id"],
                    context_goal_id=pid,
                )

                # default_writable TRUE for code. It is the CEILING --
                # whether any story in this goal may EVER write this --
                # not a grant: a story still holds it read-only until GO
                # grants otherwise. A repository is attached to a goal
                # in order to be worked on, so a false ceiling here made
                # every repo added through this page permanently
                # unwritable, and the executor's own criteria
                # unsatisfiable. Documents and environments keep the
                # column's false default, which is right for them.
                if reused:
                    # Reuse the goal's existing repo row as the parent.
                    # `resource_id` was rebound above, before the event.
                    pass
                else:
                    await conn.execute("""
                    INSERT INTO goal_resources
                        (id, goal_id, kind, name, uri, created_by, human_context,
                         repo_owner, repo_name, default_branch, clone_url,
                         parent_id, branch_name, base_branch, role, metadata,
                         summary_stale, default_writable)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, true, true)
                    """, resource_id, pid, kind, name, uri, user["uid"], human_context,
                    repo_owner, repo_name, default_branch, clone_url,
                    parent_id, branch_name, base_branch,
                    # For kind='repo' the role column is unused (the
                    # column belongs to branches). Store 'feature' as
                    # a no-op default so the constraint is satisfied.
                    role if kind == "branch" else "feature",
                    json.dumps(metadata) if metadata else None)

                    await conn.execute("""
                        INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
                        VALUES ($1, $2, 'created', $3, $4)
                    """, resource_id, pid, user["uid"],
                        json.dumps({"kind": kind, "name": name,
                                    "branch_name": branch_name, "role": role}))

                # If a working branch was supplied alongside a repo, also
                # create the branch row in the same transaction. Mirrors
                # what the user would do as a separate "add a branch"
                # step — but folded into one operation so the common
                # "this repo + work on this branch" flow is one click.
                # G5: a repo with NO working branch named still gets its
                # trunk DESIGNATED, as a one-act registration. The trunk
                # row is role='trunk' at the repo's default branch --
                # provisioning already excludes trunk-role rows from
                # story mounts, so this designates without granting.
                # Kills the ritual of answering the trunk question on
                # every addition; override by adding a branch explicitly.
                if kind == "repo" and not working_branch and repo_owner and repo_name:
                    trunk_row_id = str(uuid.uuid4())[:12]
                    await conn.execute("""
                        INSERT INTO goal_resources
                            (id, goal_id, kind, name, uri, created_by,
                             human_context, repo_owner, repo_name,
                             default_branch, clone_url, parent_id,
                             branch_name, base_branch, role, summary_stale)
                        VALUES ($1, $2, 'branch', $3, $4, $5,
                                'auto-designated trunk', $6, $7, $8, $9,
                                $10, $11, $8, 'trunk', false)
                        ON CONFLICT (goal_id, kind, name) DO NOTHING
                    """, trunk_row_id, pid, f"{repo_name}:{default_branch}",
                        clone_url, user["uid"], repo_owner, repo_name,
                        default_branch, clone_url, resource_id,
                        default_branch)

                if kind == "repo" and working_branch and repo_owner and repo_name:
                    working_branch_id = str(uuid.uuid4())[:12]
                    branch_display_name = f"{repo_name}:{working_branch}"
                    branch_role = "trunk" if working_branch_is_trunk else "feature"
                    if revive_lane:
                        # Bring the retired row back rather than minting a
                        # second one for the same branch. Re-points it at
                        # the repo row in play and restores the role, in
                        # case it was retired as something else.
                        working_branch_id = revive_lane
                        await conn.execute(
                            "UPDATE goal_resources SET status = 'active', "
                            "  role = $3, parent_id = $4, base_branch = $5, "
                            "  updated_at = now() "
                            " WHERE id = $1 AND goal_id = $2",
                            revive_lane, pid, branch_role, resource_id,
                            default_branch)
                        await conn.execute("""
                            INSERT INTO resource_events
                                (resource_id, goal_id, event_type, actor, details)
                            VALUES ($1, $2, 'created', $3, $4)
                        """, revive_lane, pid, user["uid"],
                            json.dumps({
                                "kind": "branch", "name": branch_display_name,
                                "branch_name": working_branch,
                                "role": branch_role, "revived": True,
                                "created_with_repo": resource_id,
                            }))
                        _log.info(
                            "Revived retired trunk lane %s (%s) under repo %s "
                            "on goal %s", revive_lane, branch_display_name,
                            resource_id, pid,
                        )
                    else:
                        await conn.execute("""
                        INSERT INTO goal_resources
                            (id, goal_id, kind, name, uri, created_by, human_context,
                             repo_owner, repo_name, default_branch, clone_url,
                             parent_id, branch_name, base_branch, role,
                             summary_stale, default_writable)
                        VALUES ($1, $2, 'branch', $3, $4, $5, $6, $7, $8, $9, $10,
                                $11, $12, $13, $14, true, true)
                        ON CONFLICT (goal_id, kind, name) DO NOTHING
                    """, working_branch_id, pid, branch_display_name,
                        clone_url, user["uid"], "",
                        repo_owner, repo_name, default_branch, clone_url,
                        resource_id, working_branch, default_branch, branch_role)
                        await conn.execute("""
                            INSERT INTO resource_events
                                (resource_id, goal_id, event_type, actor, details)
                            VALUES ($1, $2, 'created', $3, $4)
                        """, working_branch_id, pid, user["uid"],
                            json.dumps({
                                "kind": "branch", "name": branch_display_name,
                                "branch_name": working_branch,
                                "role": branch_role,
                                "created_with_repo": resource_id,
                            }))
                        _log.info(
                            "Auto-created branch resource %s (%s, role=%s) "
                            "under repo %s", working_branch_id,
                            branch_display_name, branch_role, resource_id,
                        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        # Race against the pre-check (or a hidden constraint we forgot about).
        _log.warning(
            "Resource insert hit unique constraint: goal=%s kind=%s name=%s — %s",
            pid, kind, name, exc,
        )
        raise HTTPException(
            status_code=400,
            detail="A resource with these details already exists for this goal.",
        )

    # Trigger initial scan in background
    if kind == "repo" and repo_owner and repo_name:
        import asyncio
        asyncio.create_task(_background_scan(resource_id, pid, repo_owner,
                                             repo_name))
        # THE AUTO-DESIGNATED TRUNK ROW IS SCANNED TOO, and without this
        # the ordinary flow ends with NO summary anywhere.
        #
        # G5 above creates a `role='trunk'` row at the repo's default
        # branch the moment a repo is added, so from that instant the
        # goal HAS a declared trunk -- which means `scan_resource` skips
        # the repo-level summary, correctly, because the lane is
        # supposed to carry it. Nothing scheduled the lane. Repo card
        # skipped, lane card never built, and the row is inserted with
        # `summary_stale = false` so the panel does not even show a
        # "no summary" pill to say so.
        #
        # The skip and the scan that makes it harmless were added in
        # different places; this is the second half.
        if trunk_row_id:
            asyncio.create_task(_background_branch_scan(
                trunk_row_id, pid, repo_owner, repo_name,
                default_branch, default_branch,
            ))
        # If we also created a working-branch row, scan it too.
        if working_branch_id and working_branch:
            asyncio.create_task(_background_branch_scan(
                working_branch_id, pid, repo_owner, repo_name,
                working_branch, default_branch,
            ))
    elif kind == "branch" and repo_owner and repo_name and branch_name:
        # Branch scan = compare against base branch
        import asyncio
        asyncio.create_task(_background_branch_scan(
            resource_id, pid, repo_owner, repo_name, branch_name, base_branch))

    # Sync RESOURCES.md for all agents in this goal
    try:
        await _sync_goal_resources_md(pid)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md for goal %s: %s", pid, exc)

    # Notify GO
    try:
        from app.services.go_events import on_resource_registered
        await on_resource_registered(pid, resource_id, user["uid"])
    except Exception:
        pass

    return RedirectResponse(url=f"/goals/{pid}/resources/browser", status_code=303)


# ── Cloud resource creation (env + optional test/deploy SAs in one form) ─────

async def _create_gcp_resource_with_sas(
    *, pid: str, user: dict, form, name: str, human_context: str,
) -> RedirectResponse:
    """One-shot creation for a GCP cloud resource:
      • Always creates a `gcp_environment` row (project/region).
      • Creates a `gcp_test_sa` child row IF `gcp_test_service_account` is provided.
      • Creates a `gcp_deploy_sa` child row IF `gcp_deploy_service_account` is provided.

    Each row gets its own id; the SAs link to the env via `parent_id` so the
    browser UI nests them under the env (same visual pattern as branch under
    repo).

    Operator can add/remove SAs later from the env detail page — see the
    `/resources/<env_id>/sa/...` endpoints.
    """
    if not name:
        raise HTTPException(status_code=400, detail="Resource name required for cloud environment.")

    target_project = (form.get("gcp_target_project_id", "") or "").strip()
    target_region = (form.get("gcp_target_region", "") or "").strip()
    test_sa = (form.get("gcp_test_service_account", "") or "").strip()
    deploy_sa = (form.get("gcp_deploy_service_account", "") or "").strip()

    if not target_project:
        raise HTTPException(status_code=400, detail="Target GCP Project ID is required.")

    _log.info(
        "Cloud resource (GCP) form submit: goal=%s name=%s project=%s region=%s "
        "test_sa=%s deploy_sa=%s",
        pid, name, target_project, target_region or "-",
        test_sa or "(none)", deploy_sa or "(none)",
    )

    # Defence-in-depth: run the same registry validation the agent path
    # uses. Catches typos in operator input the same way it catches GO
    # hallucinations. validate_registration returns operator-readable
    # error strings; surface them as 400 so the form can show them.
    from app.resource_kinds import validate_registration  # noqa: PLC0415
    env_metadata = {
        "target_project_id": target_project,
        "target_region": target_region,
    }
    err = validate_registration(
        kind="gcp_environment", metadata=env_metadata, parent_metadata=None,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    if test_sa:
        err = validate_registration(
            kind="gcp_test_sa",
            metadata={"sa_email": test_sa},
            parent_metadata=env_metadata,
        )
        if err:
            raise HTTPException(status_code=400, detail=f"Test SA: {err}")
    if deploy_sa:
        err = validate_registration(
            kind="gcp_deploy_sa",
            metadata={"sa_email": deploy_sa},
            parent_metadata=env_metadata,
        )
        if err:
            raise HTTPException(status_code=400, detail=f"Deploy SA: {err}")

    pool = get_pool()
    env_id = str(uuid.uuid4())[:12]

    # Pre-check for duplicate env name (same constraint as the main path).
    existing = await pool.fetchrow(
        """SELECT id, status FROM goal_resources
            WHERE goal_id = $1 AND kind = $2 AND name = $3""",
        pid, "gcp_environment", name,
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A gcp_environment named '{name}' already exists for this goal "
                f"(resource id {existing['id']}). Pick a different display name, "
                f"or open the existing environment to attach SAs to it."
            ),
        )

    import asyncpg
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. The gcp_environment row.
                await append_event(
                    conn,
                    event_type=GOAL_RESOURCE_REGISTERED,
                    payload={
                        "resource_id": env_id,
                        "kind": "gcp_environment",
                        "name": name,
                        "uri": "",
                        "branch_name": "",
                        "parent_id": "",
                    },
                    actor_id=user["actor_id"],
                    context_goal_id=pid,
                )
                await conn.execute(
                    """
                    INSERT INTO goal_resources
                        (id, goal_id, kind, name, uri, created_by, human_context,
                         repo_owner, repo_name, default_branch, clone_url,
                         parent_id, branch_name, base_branch, role, metadata, summary_stale)
                    VALUES ($1, $2, 'gcp_environment', $3, '', $4, $5,
                            '', '', 'main', '',
                            NULL, '', '', 'feature', $6, true)
                    """,
                    env_id, pid, name, user["uid"], human_context,
                    json.dumps(env_metadata),
                )
                await conn.execute(
                    """
                    INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
                    VALUES ($1, $2, 'created', $3, $4)
                    """,
                    env_id, pid, user["uid"],
                    json.dumps({"kind": "gcp_environment", "name": name}),
                )

                # 2. Optional Test SA.
                if test_sa:
                    await _insert_sa_child(
                        conn, pid=pid, env_id=env_id, env_name=name, user=user,
                        sa_kind="gcp_test_sa", sa_email=test_sa,
                    )

                # 3. Optional Deploy SA.
                if deploy_sa:
                    await _insert_sa_child(
                        conn, pid=pid, env_id=env_id, env_name=name, user=user,
                        sa_kind="gcp_deploy_sa", sa_email=deploy_sa,
                    )
    except asyncpg.exceptions.UniqueViolationError as exc:
        _log.warning(
            "Cloud-resource insert hit unique constraint: goal=%s env=%s — %s",
            pid, name, exc,
        )
        raise HTTPException(
            status_code=400,
            detail="A resource with these details already exists for this goal.",
        )

    try:
        await _sync_goal_resources_md(pid)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md for goal %s: %s", pid, exc)

    try:
        from app.services.go_events import on_resource_registered
        await on_resource_registered(pid, env_id, user["uid"])
    except Exception:
        pass

    return RedirectResponse(url=f"/goals/{pid}/resources/browser", status_code=303)


async def _insert_sa_child(
    conn, *, pid: str, env_id: str, env_name: str, user: dict,
    sa_kind: str, sa_email: str,
) -> str:
    """Insert one gcp_test_sa or gcp_deploy_sa row, linked to env_id via
    parent_id. Display name is `<env_name>:<role>` so the operator can see
    which env it's attached to in the resource list. Returns the new id.
    Caller handles the surrounding transaction + duplicate detection.

    Metadata stores only `sa_email` — target_project_id and target_region
    are read from the parent env on demand (see ResourceKindSpec for
    `gcp_test_sa` / `gcp_deploy_sa`). Avoids denormalised duplication
    that could drift if the parent's project/region is ever updated.
    """
    sa_id = str(uuid.uuid4())[:12]
    role_label = "test" if sa_kind == "gcp_test_sa" else "deploy"
    display_name = f"{env_name}:{role_label}-sa"
    metadata = {"sa_email": sa_email}
    await append_event(
        conn,
        event_type=GOAL_RESOURCE_REGISTERED,
        payload={
            "resource_id": sa_id,
            "kind": sa_kind,
            "name": display_name,
            "uri": "",
            "branch_name": "",
            "parent_id": env_id,
        },
        actor_id=user["actor_id"],
        context_goal_id=pid,
    )
    await conn.execute(
        """
        INSERT INTO goal_resources
            (id, goal_id, kind, name, uri, created_by, human_context,
             repo_owner, repo_name, default_branch, clone_url,
             parent_id, branch_name, base_branch, role, metadata, summary_stale)
        VALUES ($1, $2, $3, $4, '', $5, '',
                '', '', 'main', '',
                $6, '', '', 'feature', $7, true)
        ON CONFLICT (goal_id, kind, name) DO NOTHING
        """,
        sa_id, pid, sa_kind, display_name, user["uid"], env_id,
        json.dumps(metadata),
    )
    await conn.execute(
        """
        INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
        VALUES ($1, $2, 'created', $3, $4)
        """,
        sa_id, pid, user["uid"],
        json.dumps({"kind": sa_kind, "name": display_name, "sa_email": sa_email}),
    )
    _log.info(
        "Auto-created %s resource %s (%s) under env %s",
        sa_kind, sa_id, sa_email, env_id,
    )
    return sa_id


# ── Link existing resource from another goal ─────────────────────────────────

@router.post("/goals/{pid}/resources/link")
async def link_existing_resource(
    pid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Link an existing resource from another goal to this goal."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    form = await request.form()
    source_id = form.get("source_resource_id", "").strip()
    if not source_id:
        raise HTTPException(status_code=400, detail="Source resource ID required")

    pool = get_pool()

    # Fetch the source resource
    source = await pool.fetchrow(
        "SELECT * FROM goal_resources WHERE id = $1 AND status = 'active'", source_id,
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source resource not found")

    # Check user has access to source goal
    source_role = await get_goal_role(user["uid"], source["goal_id"])
    if source_role is None:
        raise HTTPException(status_code=403, detail="No access to source resource's goal")

    # Check not already linked
    existing = await pool.fetchval("""
        SELECT id FROM goal_resources
        WHERE goal_id = $1 AND source_resource_id = $2 AND status = 'active'
    """, pid, source_id)
    if existing:
        return RedirectResponse(url=f"/goals/{pid}/resources/browser", status_code=303)

    # Name-conflict pre-check — same (goal_id, kind, name) uniqueness rule as
    # add_resource. Without this, the INSERT below could hit the DB's
    # uq_goal_resource_kind_name constraint directly: an uncaught
    # asyncpg.UniqueViolationError (500), unlike add_resource which has
    # always guarded against this.
    name_conflict = await pool.fetchrow(
        """SELECT id, status FROM goal_resources
            WHERE goal_id = $1 AND kind = $2 AND name = $3""",
        pid, source["kind"], source["name"],
    )
    if name_conflict:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A {source['kind']} named '{source['name']}' is already registered for "
                f"this goal (resource id {name_conflict['id']}, status={name_conflict['status']}). "
                f"Rename the source resource in its home goal if you want to link it here."
            ),
        )

    resource_id = str(uuid.uuid4())[:12]

    async with pool.acquire() as conn:
        async with conn.transaction():
            await append_event(
                conn,
                event_type=GOAL_RESOURCE_REGISTERED,
                payload={
                    "resource_id": resource_id,
                    "kind": source["kind"],
                    "name": source["name"],
                    "linked_from": source_id,
                    "linked_from_goal": source["goal_id"],
                },
                actor_id=user["actor_id"],
                context_goal_id=pid,
            )

            # Copy resource data, link back to source
            await conn.execute("""
                INSERT INTO goal_resources
                    (id, goal_id, kind, name, uri, created_by, human_context,
                     repo_owner, repo_name, default_branch, clone_url,
                     branch_name, base_branch, file_type, file_size_bytes,
                     gcs_uri, source_resource_id, summary_stale)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, false)
            """, resource_id, pid, source["kind"], source["name"],
                source.get("uri", ""), user["uid"], source.get("human_context", ""),
                source.get("repo_owner", ""), source.get("repo_name", ""),
                source.get("default_branch", "main"), source.get("clone_url", ""),
                source.get("branch_name", ""), source.get("base_branch", ""),
                source.get("file_type", ""), source.get("file_size_bytes", 0),
                source.get("gcs_uri", ""), source_id)

            # Copy latest summary from source
            source_summary = await conn.fetchrow("""
                SELECT content, summary_type, metadata, scanned_at, previous_scan_sha
                FROM resource_summaries
                WHERE resource_id = $1 ORDER BY created_at DESC LIMIT 1
            """, source_id)
            if source_summary:
                await conn.execute("""
                    INSERT INTO resource_summaries
                        (resource_id, goal_id, summary_type, content, metadata,
                         scanned_at, previous_scan_sha)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                """, resource_id, pid, source_summary["summary_type"],
                    source_summary["content"], source_summary["metadata"],
                    source_summary["scanned_at"], source_summary.get("previous_scan_sha", ""))

                await conn.execute("""
                    UPDATE goal_resources SET last_scanned_at = $1, last_summary_at = $1
                    WHERE id = $2
                """, source_summary["scanned_at"], resource_id)

            await conn.execute("""
                INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
                VALUES ($1, $2, 'created', $3, $4)
            """, resource_id, pid, user["uid"],
                json.dumps({"linked_from": source_id, "source_goal": source["goal_id"]}))

    # Notify GO
    try:
        from app.services.go_events import on_resource_registered
        await on_resource_registered(pid, resource_id, user["uid"])
    except Exception:
        pass

    return RedirectResponse(url=f"/goals/{pid}/resources/browser", status_code=303)


# ── Upload file attachment ───────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/upload")
async def upload_resource_file(
    pid: str,
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(""),
    human_context: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """Upload a file (PDF, Excel, Word, Markdown) as a goal resource."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # Determine file type
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_FILE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: .{ext}")

    # Read file
    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)")

    resource_name = name.strip() or file.filename
    resource_id = str(uuid.uuid4())[:12]
    content_hash = hashlib.sha256(data).hexdigest()

    pool = get_pool()

    # Pre-check: surface a friendly 400 instead of a DB constraint 500.
    existing = await pool.fetchrow(
        "SELECT id, status FROM goal_resources "
        "WHERE goal_id = $1 AND kind = 'document' AND name = $2",
        pid, resource_name,
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A document named '{resource_name}' is already registered for this goal "
                f"(resource id {existing['id']}, status={existing['status']}). "
                f"Archive or delete the existing one, or upload with a different name."
            ),
        )

    # Upload to shared resource bucket (goal-independent)
    from app.storage import upload_resource_file
    gcs_uri = await upload_resource_file(
        resource_id, file.filename, data,
        content_type=ALLOWED_FILE_TYPES.get(ext, "application/octet-stream"),
    )

    import asyncpg as _asyncpg
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await append_event(
                    conn,
                    event_type=GOAL_RESOURCE_REGISTERED,
                    payload={
                        "resource_id": resource_id,
                        "kind": "document",
                        "name": resource_name,
                        "file_type": ext,
                        "file_size": len(data),
                    },
                    actor_id=user["actor_id"],
                    context_goal_id=pid,
                )

                await conn.execute("""
                    -- `uri` IS THE LOCATOR, and writing only `gcs_uri`
                    -- makes the resource INVISIBLE TO THE KERNEL: no
                    -- mount, no egress host, and nothing raises.
                    -- `test_resource_uri_is_one_column` names this
                    -- exact failure as the one thing its removal of
                    -- `gcs_uri` from kernel/ depends on -- "every
                    -- writer populates `uri`" -- and this writer did
                    -- not. GOAL1 story 4bb5e544: three documents
                    -- uploaded from the resources page, an empty
                    -- `/workspace/docs/`, and a WORKSPACE.md telling
                    -- the executor to read them.
                    INSERT INTO goal_resources
                        (id, goal_id, kind, name, uri, created_by,
                         human_context, file_type, file_size_bytes,
                         gcs_uri, summary_stale)
                    VALUES ($1, $2, 'document', $3, $4, $5, $6, $7, $8, $9,
                            true)
                """, resource_id, pid, resource_name, gcs_uri, user["uid"],
                    human_context, ext, len(data), gcs_uri)

                await conn.execute("""
                    INSERT INTO resource_attachments
                        (resource_id, goal_id, filename, file_type, file_size_bytes,
                         gcs_uri, uploaded_by, content_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """, resource_id, pid, file.filename, ext, len(data),
                    gcs_uri, user["uid"], content_hash)

                await conn.execute("""
                    INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
                    VALUES ($1, $2, 'created', $3, $4)
                """, resource_id, pid, user["uid"],
                    json.dumps({"filename": file.filename, "type": ext, "size": len(data)}))
    except _asyncpg.exceptions.UniqueViolationError:
        _log.warning(
            "upload: unique constraint race for goal=%s name=%r — resource already exists",
            pid, resource_name,
        )
        raise HTTPException(
            status_code=400,
            detail=f"A document named '{resource_name}' already exists for this goal.",
        )

    # Trigger document scan in background
    import asyncio
    asyncio.create_task(_background_doc_scan(resource_id, pid, gcs_uri, ext, resource_name))

    # For renderable types: insert a pending render row and fire the Cloud Run Job.
    # The job writes rendered HTML to resource_renders; the detail page reads it.
    # DOCX/DOC files above _DOCX_RENDER_LIMIT are skipped — the browser would
    # receive too much HTML. They fall back to the download button.
    if ext in _RENDERABLE_TYPES:
        file_size_bytes = len(data)
        if ext in ("docx", "doc") and file_size_bytes > _DOCX_RENDER_LIMIT:
            _log.info(
                "upload: DOCX too large to render inline "
                "(%d bytes > %d) — skipping render row for %s",
                file_size_bytes, _DOCX_RENDER_LIMIT, resource_id,
            )
        else:
            try:
                await pool.execute(
                    """INSERT INTO resource_renders (resource_id, render_status)
                       VALUES ($1, 'pending')
                       ON CONFLICT (resource_id) DO NOTHING""",
                    resource_id,
                )
            except Exception:
                _log.warning("upload: failed to insert pending render row for %s", resource_id)
            asyncio.create_task(trigger_task_runner(resource_id))
            _log.info("upload: render job triggered for %s (file_type=%s)", resource_id, ext)

    # Notify GO
    try:
        from app.services.go_events import on_resource_registered
        await on_resource_registered(pid, resource_id, user["uid"])
    except Exception:
        pass

    # Push the new doc into every live agent session (GO + SHCAs; IAs
    # skipped inside download_documents_to_workspace). Matches the
    # chat-paperclip UX so a browser upload is immediately available.
    try:
        from app.resource_provisioning import refresh_all_goal_sessions
        asyncio.create_task(refresh_all_goal_sessions(pid))
    except Exception:
        pass

    return RedirectResponse(url=f"/goals/{pid}/resources/browser", status_code=303)


# ── GCP impersonation check ──────────────────────────────────────────────────

# Caller identity token used by _check_sa_impersonation, cached across calls
# since it does not depend on the target SA being checked. Re-derived only
# once expired, instead of on every resource-page load.
_caller_creds_cache = None
_caller_creds_lock = asyncio.Lock()

# Fetching/refreshing the caller token was previously uncached and unbounded:
# a stalled google.auth call blocked the whole page GET indefinitely, and
# every browser refresh just re-triggered the same hang. Bound it with a
# timeout and cache the result per SA to avoid repeating the round-trip on
# every load.
_CALLER_TOKEN_TIMEOUT_SECS = 5.0
_IMPERSONATION_CACHE_TTL_SECS = 5 * 60
_impersonation_cache: dict[str, tuple[float, dict]] = {}


async def _ensure_valid_caller_creds() -> str:
    """Populate/refresh _caller_creds_cache under lock and return its token."""
    global _caller_creds_cache
    import google.auth  # noqa: PLC0415
    import google.auth.transport.requests  # noqa: PLC0415

    async with _caller_creds_lock:
        if _caller_creds_cache is None:
            _caller_creds_cache, _ = await asyncio.to_thread(
                google.auth.default,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        if not _caller_creds_cache.valid:
            await asyncio.to_thread(
                _caller_creds_cache.refresh, google.auth.transport.requests.Request()
            )
        return _caller_creds_cache.token


async def _get_caller_token() -> str:
    """Return a valid access token for this process's identity, refreshing only when expired.

    Bounded by _CALLER_TOKEN_TIMEOUT_SECS overall — a stalled metadata server or
    credential refresh raises asyncio.TimeoutError instead of hanging the caller.
    """
    return await asyncio.wait_for(_ensure_valid_caller_creds(), timeout=_CALLER_TOKEN_TIMEOUT_SECS)


async def _check_sa_impersonation(sa_email: str, cloud_run_sa_email: str) -> dict:
    """Check whether the Cloud Run SA can impersonate the given target SA.

    Calls the IAM testIamPermissions API to verify
    iam.serviceAccounts.getAccessToken on the target SA.  This is equivalent
    to checking roles/iam.serviceAccountTokenCreator without enumerating the
    full IAM policy (which would require iam.serviceAccounts.getIamPolicy).

    Result is cached per sa_email for _IMPERSONATION_CACHE_TTL_SECS so repeated
    page loads (e.g. the operator hitting refresh) don't redo the full
    credential-fetch + IAM round-trip every time.

    Returns a dict with keys:
      status  — "ok" | "no_permission" | "error" | "unknown"
      message — human-readable detail when not "ok"
    """
    if not cloud_run_sa_email:
        return {"status": "unknown", "message": "Service account email could not be determined (local dev or metadata server unavailable)"}

    cached = _impersonation_cache.get(sa_email)
    if cached and (_time.monotonic() - cached[0]) < _IMPERSONATION_CACHE_TTL_SECS:
        return cached[1]

    try:
        import httpx  # noqa: PLC0415

        token = await _get_caller_token()
        project = sa_email.split("@")[1].split(".iam.")[0]
        url = (
            f"https://iam.googleapis.com/v1/projects/{project}"
            f"/serviceAccounts/{sa_email}:testIamPermissions"
        )
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                json={"permissions": ["iam.serviceAccounts.getAccessToken"]},
                headers={"Authorization": f"Bearer {token}"},
            )

        if resp.status_code == 200:
            granted = "iam.serviceAccounts.getAccessToken" in resp.json().get("permissions", [])
            result = {"status": "ok" if granted else "no_permission", "message": ""}
        else:
            result = {"status": "error", "message": f"IAM API returned HTTP {resp.status_code}"}

    except ImportError:
        result = {"status": "unknown", "message": "google-auth not available in this environment (expected in local dev)"}
    except asyncio.TimeoutError:
        _log.warning("_check_sa_impersonation: timed out fetching caller token for %s", sa_email)
        result = {"status": "error", "message": "Timed out determining caller identity (metadata server unresponsive)"}
    except Exception as exc:
        result = {"status": "error", "message": str(exc)[:300]}

    _impersonation_cache[sa_email] = (_time.monotonic(), result)
    return result


# ── Render job integration ────────────────────────────────────────────────────

# Types whose content is pre-rendered by the Cloud Run render job.
# Images and plain-text types are still handled inline at page load.
_RENDERABLE_TYPES = frozenset({"pdf", "docx", "doc", "csv"})
_RENDER_MAX_RETRIES = 5
# A pending row older than this means the job was triggered but never wrote back —
# treat it as stale and re-trigger rather than waiting forever.
_RENDER_PENDING_TIMEOUT_SECS = 15 * 60  # 15 minutes
# DOCX/DOC files above this size are not rendered inline — extracted HTML would be
# too large for the browser. Files over this limit get a download button only.
_DOCX_RENDER_LIMIT = 2 * 1024 * 1024  # 2 MB


# ── Resource detail ──────────────────────────────────────────────────────────

@router.get("/goals/{pid}/resources/{rid}", response_class=HTMLResponse)
async def resource_detail(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Resource detail page with summary, metadata, and event history."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    resource = await pool.fetchrow(
        "SELECT * FROM goal_resources WHERE id = $1 AND goal_id = $2", rid, pid,
    )
    if not resource:
        raise HTTPException(status_code=404)

    # Latest summary
    summary = await pool.fetchrow("""
        SELECT * FROM resource_summaries
        WHERE resource_id = $1 ORDER BY created_at DESC LIMIT 1
    """, rid)

    # Event history
    events = await pool.fetch("""
        SELECT * FROM resource_events
        WHERE resource_id = $1 ORDER BY created_at DESC LIMIT 20
    """, rid)

    # Children (for repos)
    children = await pool.fetch("""
        SELECT * FROM goal_resources
        WHERE parent_id = $1 AND status = 'active'
        ORDER BY kind, name
    """, rid)

    goal = await pool.fetchrow("SELECT * FROM goals WHERE id = $1", pid)

    # asyncpg returns the JSONB metadata column as a string. The
    # gcp_environment detail template uses `resource.metadata.target_project_id`
    # in the attach-SA modal placeholder; without parsing it stays a
    # string and Jinja attribute access falls back to Undefined.
    resource_dict = dict(resource)
    raw_meta = resource_dict.get("metadata")
    if isinstance(raw_meta, str) and raw_meta:
        try:
            resource_dict["metadata"] = json.loads(raw_meta)
        except Exception:
            resource_dict["metadata"] = None

    from app.gcp_credentials import get_self_sa_email  # noqa: PLC0415
    gcp_vm_sa_email = await get_self_sa_email()

    # For GCP SA resources: check impersonation and fetch parent environment
    impersonation_check = None
    parent_resource = None
    is_gcp_sa = resource_dict.get("kind") in ("gcp_test_sa", "gcp_deploy_sa")
    if is_gcp_sa:
        sa_email = (resource_dict.get("metadata") or {}).get("sa_email", "")
        if sa_email:
            impersonation_check = await _check_sa_impersonation(sa_email, gcp_vm_sa_email)
            _log.info(
                "resource_detail: SA impersonation check for %s → %s",
                sa_email, impersonation_check.get("status"),
            )
        parent_id = resource_dict.get("parent_id")
        if parent_id:
            parent_row = await pool.fetchrow(
                "SELECT * FROM goal_resources WHERE id = $1", parent_id
            )
            if parent_row:
                parent_dict = dict(parent_row)
                raw = parent_dict.get("metadata")
                if isinstance(raw, str) and raw:
                    try:
                        parent_dict["metadata"] = json.loads(raw)
                    except Exception:
                        parent_dict["metadata"] = None
                parent_resource = parent_dict

    # ── Parse summary metadata (asyncpg may return JSONB as str in some envs) ─
    summary_dict = None
    scan_stats = None
    if summary:
        summary_dict = dict(summary)
        raw_sm = summary_dict.get("metadata")
        if isinstance(raw_sm, str) and raw_sm:
            try:
                raw_sm = json.loads(raw_sm)
            except Exception:
                raw_sm = None
        if isinstance(raw_sm, dict):
            summary_dict["metadata"] = raw_sm
            scan_stats = raw_sm
        elif raw_sm is None:
            summary_dict["metadata"] = None

    # ── GitHub live data (repo and branch only) ───────────────────────────────
    from app.services.git_graph.github_ops import (  # noqa: PLC0415
        mint_repo_token, get_repo_overview, compare, get_branch_pr,
    )
    github_live = None
    kind = resource_dict.get("kind")

    if kind == "repo":
        owner = resource_dict.get("repo_owner")
        repo_name = resource_dict.get("repo_name")
        if owner and repo_name:
            try:
                gh_token = await mint_repo_token(
                    f"{owner}/{repo_name}", {"metadata": "read", "contents": "read"}
                )
                github_live = await get_repo_overview(gh_token, owner, repo_name)
                _log.info("resource_detail: GitHub overview fetched for %s/%s", owner, repo_name)
            except Exception:
                _log.warning(
                    "resource_detail: GitHub overview failed for %s/%s",
                    owner, repo_name, exc_info=True,
                )

    elif kind == "branch":
        parent_id = resource_dict.get("parent_id")
        if parent_id:
            repo_row = await pool.fetchrow(
                "SELECT * FROM goal_resources WHERE id = $1 AND kind = 'repo'", parent_id
            )
            if repo_row:
                owner = repo_row["repo_owner"]
                repo_name = repo_row["repo_name"]
                branch_name = resource_dict.get("branch_name")
                base = resource_dict.get("base_branch") or repo_row.get("default_branch") or "main"
                if owner and repo_name and branch_name:
                    try:
                        gh_token = await mint_repo_token(
                            f"{owner}/{repo_name}",
                            {"metadata": "read", "contents": "read"},
                        )
                        cmp, pr = await asyncio.gather(
                            compare(gh_token, owner, repo_name, base, branch_name),
                            get_branch_pr(gh_token, owner, repo_name, owner, branch_name),
                            return_exceptions=True,
                        )
                        github_live = {
                            "ahead_by": cmp.get("ahead_by") if isinstance(cmp, dict) else None,
                            "behind_by": cmp.get("behind_by") if isinstance(cmp, dict) else None,
                            "compare_status": cmp.get("status") if isinstance(cmp, dict) else None,
                            "pr": pr if isinstance(pr, dict) else None,
                            "repo_owner": owner,
                            "repo_name": repo_name,
                            "base_branch": base,
                        }
                        _log.info(
                            "resource_detail: branch GitHub data fetched for %s (ahead=%s behind=%s)",
                            branch_name,
                            github_live.get("ahead_by"),
                            github_live.get("behind_by"),
                        )
                    except Exception:
                        _log.warning(
                            "resource_detail: GitHub data failed for branch %s",
                            resource_dict.get("branch_name"), exc_info=True,
                        )

    # ── Document / file: viewer URL + render cache lookup ────────────────────
    from app.storage import download_gcs_uri_bytes  # noqa: PLC0415
    viewer_url = None
    viewer_url_is_external = False
    doc_text_content = None
    doc_rendered_content = None   # populated from resource_renders when status='done'
    render_pending = False         # render job running or just triggered (includes backoff)
    render_failed = False          # permanently failed after _RENDER_MAX_RETRIES attempts
    _TEXT_PREVIEW_TYPES = {
        "md", "txt", "py", "js", "ts", "jsx", "tsx", "json",
        "yaml", "yml", "toml", "sql", "sh",
    }

    if kind in ("document", "file"):
        gcs_uri = resource_dict.get("gcs_uri") or ""
        uri = resource_dict.get("uri") or ""
        file_type = (resource_dict.get("file_type") or "").lower().lstrip(".")
        file_size = resource_dict.get("file_size_bytes") or 0

        # Infer file_type from GCS URI for older resources where it was not stored.
        if not file_type and gcs_uri and "." in gcs_uri:
            inferred = gcs_uri.rsplit(".", 1)[-1].lower()
            if inferred in ALLOWED_FILE_TYPES:
                file_type = inferred
                _log.info(
                    "resource_detail: inferred file_type '%s' from gcs_uri for %s", file_type, rid
                )

        _log.info(
            "resource_detail: doc viewer — rid=%s file_type='%s' file_size=%d gcs_uri=%s",
            rid, file_type, file_size, bool(gcs_uri),
        )

        if gcs_uri:
            # Proxy URL for the download button and image src — auth-gated, no
            # GCS signed URL required (avoids Cloud Run IAM signing complexity).
            viewer_url = f"/goals/{pid}/resources/{rid}/content"

            # DOCX/DOC files above the render limit produce too much HTML for the browser.
            # Exclude them from the render path — they fall back to the download button.
            _should_render = (
                file_type in _RENDERABLE_TYPES
                and not (file_type in ("docx", "doc") and file_size > _DOCX_RENDER_LIMIT)
            )
            if file_type in _RENDERABLE_TYPES and not _should_render:
                _log.info(
                    "resource_detail: DOCX too large to render inline "
                    "(%d bytes > %d) — download button only for %s",
                    file_size, _DOCX_RENDER_LIMIT, rid,
                )
            if _should_render:
                # For heavy types: check the resource_renders cache populated by the
                # Cloud Run render job. Never extract inline at page-load time.
                render_row = await pool.fetchrow(
                    "SELECT rendered_html, render_status, retry_count, retry_after, updated_at, error_message "
                    "FROM resource_renders WHERE resource_id = $1",
                    rid,
                )
                _log.info(
                    "resource_detail: render check — rid=%s file_type=%s "
                    "row_status=%s retry_count=%s retry_after=%s row_age=%s",
                    rid, file_type,
                    render_row["render_status"] if render_row else None,
                    render_row["retry_count"] if render_row else None,
                    render_row["retry_after"] if render_row else None,
                    (datetime.now(timezone.utc) - render_row["updated_at"]) if render_row else None,
                )
                if render_row is None:
                    # No render row — first time this resource has been viewed.
                    render_pending = True
                    try:
                        await pool.execute(
                            """INSERT INTO resource_renders (resource_id, render_status)
                               VALUES ($1, 'pending')
                               ON CONFLICT (resource_id) DO NOTHING""",
                            rid,
                        )
                    except Exception:
                        _log.warning(
                            "resource_detail: could not insert pending render row for %s", rid
                        )
                    _log.info(
                        "resource_detail: no render row — scheduling job trigger "
                        "rid=%s file_type=%s gcs_uri=%s",
                        rid, file_type, gcs_uri,
                    )
                    asyncio.create_task(trigger_task_runner(rid))
                elif render_row["render_status"] == "done":
                    if file_type != "pdf":
                        # PDFs use the native browser <object> viewer — don't surface
                        # OCR text as the primary display. The extracted text stays in
                        # resource_renders for the resource scanner / agent context.
                        doc_rendered_content = render_row["rendered_html"]
                    _log.info(
                        "resource_detail: render done — "
                        "rid=%s file_type=%s html_len=%d serving_to_ui=%s",
                        rid, file_type, len(render_row["rendered_html"]),
                        file_type != "pdf",
                    )
                elif render_row["render_status"] == "pending":
                    pending_age = datetime.now(timezone.utc) - render_row["updated_at"]
                    if pending_age.total_seconds() > _RENDER_PENDING_TIMEOUT_SECS:
                        # Job was triggered but never wrote back — assume it died silently.
                        # Reset to failed so the backoff path picks it up on the next check.
                        _log.warning(
                            "resource_detail: stale pending — resetting to failed and re-triggering "
                            "rid=%s pending_age=%s",
                            rid, pending_age,
                        )
                        render_pending = True
                        try:
                            await pool.execute(
                                """UPDATE resource_renders
                                   SET render_status = 'failed', updated_at = now()
                                   WHERE resource_id = $1""",
                                rid,
                            )
                        except Exception:
                            _log.warning(
                                "resource_detail: could not reset stale pending row — rid=%s",
                                rid, exc_info=True,
                            )
                        asyncio.create_task(trigger_task_runner(rid))
                    else:
                        # Job is running — just show the pending card.
                        render_pending = True
                        _log.info(
                            "resource_detail: render pending — waiting for job "
                            "rid=%s pending_since=%s age=%s",
                            rid, render_row["updated_at"], pending_age,
                        )
                else:
                    # status = 'failed' — render job writes retry_count=999 for all code
                    # errors so this is always permanent. User can retry via the UI button.
                    render_failed = True
                    _log.warning(
                        "resource_detail: render failed (permanent) — "
                        "rid=%s retry_count=%s error=%r",
                        rid,
                        render_row["retry_count"],
                        (render_row["error_message"] or "")[:120],
                    )

            elif file_type in _TEXT_PREVIEW_TYPES and 0 < file_size < 100_000:
                try:
                    raw_bytes = await download_gcs_uri_bytes(gcs_uri)
                    doc_text_content = raw_bytes.decode("utf-8", errors="replace")
                    _log.info(
                        "resource_detail: text preview loaded for %s (%d bytes)", rid, len(raw_bytes)
                    )
                except Exception:
                    _log.warning(
                        "resource_detail: text preview fetch failed for %s", rid, exc_info=True
                    )

        elif uri.startswith("http"):
            # Externally-hosted file — no GCS, pass URL straight through.
            viewer_url = uri
            viewer_url_is_external = True
            _log.info("resource_detail: using external URI as viewer URL for %s", rid)

    # A repo page says where work lands. Cheap, and only for repos: the
    # question is meaningless for a document and the two queries would be
    # paid on every resource view.
    lane_summary = None
    if resource_dict.get("kind") == "repo" and resource_dict.get("repo_name"):
        from app.services.git_graph.trunk_lane import lane_summary as _lanes
        async with pool.acquire() as _c:
            lane_summary = await _lanes(
                _c, pid, resource_dict.get("repo_owner") or "",
                resource_dict["repo_name"])

    return templates.TemplateResponse(
        "resources/detail.html",
        {
            "request": request,
            "user": user,
            "goal": dict(goal),
            "pid": pid,
            "role": role,
            "resource": resource_dict,
            # Where this goal's work on the repo actually lands, declared
            # or not -- see trunk_lane.lane_summary.
            "lane_summary": lane_summary,
            "summary": summary_dict,
            "scan_stats": scan_stats,
            "events": [dict(e) for e in events],
            "children": [dict(c) for c in children],
            # Passed to the gcp_environment modal so CLI setup commands can be
            # pre-filled with the real Cloud Run SA email.
            "gcp_vm_sa_email": gcp_vm_sa_email,
            # GCP SA-specific
            "impersonation_check": impersonation_check,
            "parent_resource": parent_resource,
            "is_gcp_sa": is_gcp_sa,
            # GitHub live data (repo / branch only — None for other kinds)
            "github_live": github_live,
            # Document / file viewer
            "viewer_url": viewer_url,
            "viewer_url_is_external": viewer_url_is_external,
            "doc_text_content": doc_text_content,
            "doc_rendered_content": doc_rendered_content,
            "render_pending": render_pending,
            "render_failed": render_failed,
        },
    )


# ── Render retry ─────────────────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/{rid}/retry-render")
async def retry_render(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> Response:
    """Reset a permanently-failed render row back to pending and re-trigger the job.

    Called from the 'Try again' button on the render_failed card. The user explicitly
    opts in — no automatic retries happen.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    import asyncio as _asyncio
    pool = get_pool()
    _log.info("retry_render: user=%s re-triggering render for rid=%s", user["uid"], rid)
    try:
        await pool.execute(
            """UPDATE resource_renders
               SET render_status = 'pending',
                   retry_count   = 0,
                   retry_after   = NULL,
                   error_message = NULL,
                   updated_at    = now()
               WHERE resource_id = $1""",
            rid,
        )
        _asyncio.create_task(trigger_task_runner(rid))
        _log.info("retry_render: row reset to pending and job re-triggered for rid=%s", rid)
    except Exception:
        _log.error("retry_render: failed to reset render row for rid=%s", rid, exc_info=True)
    return RedirectResponse(f"/goals/{pid}/resources/{rid}", status_code=303)


# ── Resource content proxy ────────────────────────────────────────────────────

@router.get("/goals/{pid}/resources/{rid}/content")
async def resource_content(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Stream a GCS-hosted resource file through the server.

    Avoids GCS signed URLs (which require specific IAM setup on Cloud Run).
    Auth-gated: only goal members can fetch the file.
    """
    from fastapi.responses import Response as HTTPResponse  # noqa: PLC0415
    from app.storage import download_gcs_uri_bytes  # noqa: PLC0415

    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id, gcs_uri, file_type, name FROM goal_resources "
        " WHERE id = $1 AND goal_id = $2", rid, pid,
    )
    if not row:
        raise HTTPException(status_code=404)

    file_type = (row["file_type"] or "").lower().lstrip(".")
    content_type = ALLOWED_FILE_TYPES.get(file_type, "application/octet-stream")

    if row["gcs_uri"]:
        try:
            data = await download_gcs_uri_bytes(row["gcs_uri"])
        except Exception:
            _log.error("resource_content: failed to fetch %s from GCS",
                       row["gcs_uri"], exc_info=True)
            raise HTTPException(
                status_code=502, detail="Could not fetch file from storage")
    else:
        # A CATALOGUED ARTIFACT HAS NO `gcs_uri`. Its bytes are in the
        # content-addressed artifact store, and its resource id is
        # `art_` + the first twelve characters of the artifact id -- see
        # the join in `app/routers/artifacts.py`. That is a TRUNCATION,
        # so the full id has to be looked up rather than reconstructed.
        #
        # SERVED FROM HERE rather than sending the page at a second
        # endpoint: one download URL per resource row means the template
        # does not have to know where any particular resource keeps its
        # bytes, and a new kind of store is one branch here instead of a
        # new branch in every page that lists resources.
        data, content_type = await _artifact_bytes(pool, pid, rid,
                                                   content_type)

    want_download = (request.query_params.get("download") or "") not in ("", "0")
    # ATTACHMENT WHEN ASKED. The default stays `inline` because this
    # route also backs the in-page viewer, where a PDF must render
    # rather than land in the downloads folder. An HTML `download`
    # attribute alone is not enough: browsers ignore it in cases a
    # person cannot predict, and the failure looks like a broken button.
    disposition = "attachment" if want_download else "inline"
    _log.info("resource_content: served %s (%d bytes, %s, %s)",
              rid, len(data), content_type, disposition)
    from app.services.content_disposition import (  # noqa: PLC0415
        content_disposition)

    return HTTPResponse(
        content=data,
        media_type=content_type,
        headers={
            # NOT AN f-STRING WITH THE NAME IN IT. HTTP headers are
            # latin-1, and an em dash in a report title -- which is what
            # an LLM writes unprompted -- made this raise
            # `UnicodeEncodeError` inside Starlette, several frames from
            # anything mentioning a filename. Seen as a 500 on
            # `art_a60f320cfa72`.
            "Content-Disposition": content_disposition(disposition,
                                                       row["name"]),
            "Cache-Control": "private, max-age=3600",
        },
    )


async def _artifact_bytes(pool, pid: str, rid: str, fallback_type: str):
    """The bytes of a catalogued artifact, scoped to the goal."""
    from kernel.artifacts.aio import aget                 # noqa: PLC0415
    from kernel.artifacts.store import ArtifactNotFound   # noqa: PLC0415

    prefix = rid[4:]
    art = await pool.fetchrow(
        """
        SELECT m.artifact_id, m.media_type
          FROM kernel.artifact_manifest m
          LEFT JOIN kernel.executions e ON e.id = m.execution_id
         WHERE left(m.artifact_id, 12) = $1
           AND COALESCE(e.goal_id, m.goal_id) = $2
         LIMIT 1
        """, prefix, pid)
    if art is None:
        # SCOPED BY THE JOIN: the artifact store is content-addressed
        # and goal-blind, so this is what stops one goal reading
        # another's bytes by guessing a resource id.
        raise HTTPException(status_code=404)
    try:
        return (await aget(art["artifact_id"]),
                art["media_type"] or fallback_type)
    except (ArtifactNotFound, OSError) as exc:
        _log.warning("resource_content: %s is catalogued and not in the "
                     "store: %s", rid, exc)
        raise HTTPException(
            status_code=410,
            detail="recorded but the content is no longer in the store")             from None


# ── Lock / Unlock ────────────────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/{rid}/lock")
async def lock_resource(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Lock a resource for exclusive write access."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    form = await request.form()
    for_task = form.get("for_task", "")

    pool = get_pool()
    result = await pool.execute("""
        UPDATE goal_resources
        SET locked_by = $1, locked_for = $2, locked_at = now()
        WHERE id = $3 AND goal_id = $4 AND locked_by IS NULL
    """, user["uid"], for_task, rid, pid)

    if result == "UPDATE 0":
        # Already locked
        resource = await pool.fetchrow(
            "SELECT locked_by, locked_for FROM goal_resources WHERE id = $1", rid,
        )
        if resource and resource["locked_by"]:
            raise HTTPException(
                status_code=409,
                detail=f"Resource locked by {resource['locked_by']} for task {resource['locked_for']}",
            )
        raise HTTPException(status_code=404)

    await pool.execute("""
        INSERT INTO resource_events (resource_id, goal_id, event_type, actor, details)
        VALUES ($1, $2, 'locked', $3, $4)
    """, rid, pid, user["uid"], json.dumps({"for_task": for_task}))

    await append_event(
        pool,
        event_type=GOAL_RESOURCE_LOCKED,
        payload={"resource_id": rid, "locked_by": user["uid"], "for_task": for_task},
        actor_id=user["actor_id"],
        context_goal_id=pid,
    )

    return RedirectResponse(url=f"/goals/{pid}/resources/{rid}", status_code=303)


@router.post("/goals/{pid}/resources/{rid}/unlock")
async def unlock_resource(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Release a resource lock. Only the member who holds the lock, or a
    goal.manager, may release it — otherwise any member could steal another
    member's in-progress exclusive edit."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    pool = get_pool()
    resource = await pool.fetchrow(
        "SELECT locked_by FROM goal_resources WHERE id = $1 AND goal_id = $2", rid, pid,
    )
    if not resource:
        raise HTTPException(status_code=404)
    if resource["locked_by"] and resource["locked_by"] != user["uid"] and role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403, detail="Only the lock holder or a goal admin can unlock this resource")

    await pool.execute("""
        UPDATE goal_resources
        SET locked_by = NULL, locked_for = NULL, locked_at = NULL
        WHERE id = $1 AND goal_id = $2
    """, rid, pid)

    await pool.execute("""
        INSERT INTO resource_events (resource_id, goal_id, event_type, actor)
        VALUES ($1, $2, 'unlocked', $3)
    """, rid, pid, user["uid"])

    await append_event(
        pool,
        event_type=GOAL_RESOURCE_UNLOCKED,
        payload={"resource_id": rid, "unlocked_by": user["uid"]},
        actor_id=user["actor_id"],
        context_goal_id=pid,
    )

    return RedirectResponse(url=f"/goals/{pid}/resources/{rid}", status_code=303)


# ── Archive resource ─────────────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/{rid}/archive")
async def archive_resource(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Archive (soft-delete) a resource.

    ARCHIVE IS AS STRONG AS DELETE, FOR EVERYTHING THAT MATTERS HERE. Every
    lane and provisioning query filters `status = 'active'`, so an archived
    trunk lane is as absent from the model as a deleted one -- it simply
    stays recoverable. This used to be a blind UPDATE with no checks at
    all, which made the Archive button a one-click bypass of the guard on
    the Delete button beside it.
    """
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT kind, name FROM goal_resources WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Resource not found")

    from app.services.resource_dependents import (
        blocking_dependents, describe_stories,
    )
    deps = await blocking_dependents(pool, pid, rid, row["kind"] or "")
    if deps["stories"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{row['name']}' is still granted to "
                f"{describe_stories(deps['stories'])}. An archived resource is "
                f"invisible to the engine, so this would move their work to "
                f"whichever branch the fallback picks. Re-grant those stories "
                f"first (your assistant can: revoke_resource / grant_resource)."
            ),
        )

    await pool.execute(
        "UPDATE goal_resources SET status = 'archived', updated_at = now() "
        " WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )

    await append_event(
        pool,
        event_type=GOAL_RESOURCE_ARCHIVED,
        payload={"resource_id": rid},
        actor_id=user["actor_id"],
        context_goal_id=pid,
    )

    return RedirectResponse(url=f"/goals/{pid}/resources/browser", status_code=303)


# ── Unarchive resource ───────────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/{rid}/unarchive")
async def unarchive_resource(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Restore an archived resource to active status.

    Fails with 400 if another *active* row has stolen the same
    (goal_id, kind, name) since the archive — operator must rename or
    delete the conflicting row first.

    Triggers the same `on_resource_registered` hook the add path uses so
    `RESOURCES.md` regenerates for any active stories referencing this
    resource — keeps the unarchive flow symmetric with create.
    """
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()

    row = await pool.fetchrow(
        "SELECT kind, name, status FROM goal_resources "
        "WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Resource not found.")
    if row["status"] != "archived":
        return RedirectResponse(
            url=f"/goals/{pid}/resources/{rid}", status_code=303,
        )

    # Uniqueness pre-check: the constraint is (goal_id, kind, name) across
    # all rows including archived, but if another *active* row has the same
    # tuple, unarchive would still create two "live" rows that downstream
    # code can't disambiguate. Refuse gracefully.
    conflict = await pool.fetchrow(
        """SELECT id FROM goal_resources
            WHERE goal_id = $1 AND kind = $2 AND name = $3
              AND status = 'active' AND id != $4""",
        pid, row["kind"], row["name"], rid,
    )
    if conflict:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Another active {row['kind']} named {row['name']!r} already "
                f"exists (id {conflict['id']}). Rename or archive that one first, "
                f"then retry the unarchive."
            ),
        )

    await pool.execute(
        "UPDATE goal_resources SET status = 'active', updated_at = NOW() "
        "WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )

    await append_event(
        pool,
        event_type=GOAL_RESOURCE_REGISTERED,  # re-use the "registered" event
        payload={
            "resource_id": rid,
            "kind": row["kind"],
            "name": row["name"],
            "unarchived": True,
        },
        actor_id=user["actor_id"],
        context_goal_id=pid,
    )

    # Same downstream hook as add: regenerates RESOURCES.md for affected
    # stories, notifies GO. Best-effort — failures here don't undo the
    # unarchive itself.
    try:
        from app.services.go_events import on_resource_registered
        await on_resource_registered(pid, rid, user["uid"])
    except Exception:
        _log.warning(
            "unarchive: on_resource_registered hook failed for resource %s — "
            "RESOURCES.md may be stale until next refresh",
            rid, exc_info=True,
        )

    _log.info(
        "Resource unarchived: goal=%s rid=%s kind=%s name=%s",
        pid, rid, row["kind"], row["name"],
    )

    return RedirectResponse(url=f"/goals/{pid}/resources/{rid}", status_code=303)


_TRUTHY = ("1", "true", "yes", "on")


async def wants_cascade(request: Request) -> bool:
    """Did this request ask to cascade? Query string first, then the body.

    THE BODY READ MUST NOT BE ABLE TO FAIL. The Delete form carries no input
    fields -- it is a button and two `data-confirm` attributes -- and the
    shared interceptor in static/js/app.js submits `new FormData(form)`. For
    a field-less form that is an EMPTY multipart body: just the closing
    boundary, with no leading CRLF. python-multipart rejects it with
    `MultipartParseError: Did not find CR at end of boundary`, so a plain
    `await request.form()` here turned every delete into a 500 -- including
    the ones the dependency guard was about to refuse with a useful message,
    which is how it was found (dev, goal MREP1, 2026-10-06).

    Query string first so `?cascade=1` works whatever the body is, and the
    body read is tolerant because an unreadable body means "no flag", never
    "crash the endpoint".
    """
    if (request.query_params.get("cascade") or "").lower() in _TRUTHY:
        return True
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 -- any parse failure means "not asked for"
        return False
    return (str(form.get("cascade") or "")).lower() in _TRUTHY


# ── Delete resource (hard) ───────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/{rid}/delete")
async def delete_resource(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Permanently delete a resource from the goal.

    Unlike archive (which just flips status), delete:
      - Sets status='deleted' in goal_resources (soft-delete in DB so the
        ledger / attachments audit trail stays intact).
      - Removes the GCS blob.
      - Removes the file from every live agent workspace's docs/ folder
        (GO + active SHCAs + active IA workers).
      - Refreshes each agent's RESOURCES.md so the deleted resource no
        longer appears.
    """
    role = await get_goal_role(user["uid"], pid)
    if role != GoalRole.GOAL_ADMIN:
        raise HTTPException(status_code=403)

    pool = get_pool()
    resource = await pool.fetchrow(
        "SELECT * FROM goal_resources WHERE id = $1 AND goal_id = $2", rid, pid,
    )
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    name = resource.get("name") or ""
    gcs_uri = resource.get("gcs_uri") or resource.get("uri") or ""
    kind = resource.get("kind") or ""

    # 0. SAY WHAT IS ATTACHED BEFORE DETACHING IT. A repo resource carries
    #    trunk lanes, and the lanes are what stories are granted. Deleting
    #    one used to succeed in silence and strand them (goal GOAL1,
    #    2026-10-05). `cascade=1` is the operator confirming they meant it,
    #    and archives the children with it rather than leaving them parented
    #    to a row that is gone.
    from app.services.resource_dependents import (
        blocking_dependents, describe_children, describe_stories,
    )
    cascade = await wants_cascade(request)
    deps = await blocking_dependents(pool, pid, rid, kind)

    # A STORY GRANT HAS NO CASCADE. Removing what a story points at
    # re-routes its work to whatever the fallback happens to be -- the
    # original failure in miniature -- so this refuses outright rather
    # than offering a confirmation that silently does it anyway.
    if deps["stories"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{name}' is still granted to {describe_stories(deps['stories'])}. "
                f"Re-grant those stories to the branch they should land on "
                f"first (your assistant can do this with revoke_resource / "
                f"grant_resource); deleting this would move their work to "
                f"whichever branch the fallback picks."
            ),
        )
    if deps["children"] and not cascade:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{name}' still has {describe_children(deps['children'])}. "
                f"Archive them first, or re-submit with cascade=1 to archive "
                f"them along with it."
            ),
        )

    # 1. Soft-delete the DB row (keeps audit trail + allows future re-upload
    #    via the upload_and_register_resource revive path).
    await pool.execute(
        "UPDATE goal_resources SET status = 'deleted', updated_at = now() "
        "WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )
    await append_event(
        pool,
        event_type="resource.goal.deleted",
        payload={"resource_id": rid, "name": name, "kind": kind},
        actor_id=user["actor_id"],
        context_goal_id=pid,
    )

    # 1b. Cascade: ARCHIVE the children, never delete them. Archive is
    #     reversible from the UI and a lane the goal may still want is not
    #     the operator's to destroy as a side effect of removing its
    #     parent. Each one gets its own event so the trail names what went.
    if cascade and deps["children"]:
        for child in deps["children"]:
            await pool.execute(
                "UPDATE goal_resources SET status = 'archived', updated_at = now() "
                " WHERE id = $1 AND goal_id = $2", child["id"], pid,
            )
            await append_event(
                pool,
                event_type="resource.goal.archived",
                payload={
                    "resource_id": child["id"], "name": child.get("name") or "",
                    "kind": child.get("kind") or "", "cascaded_from": rid,
                },
                actor_id=user["actor_id"],
                context_goal_id=pid,
            )
        _log.info(
            "Delete cascade archived %d child resource(s) of %s on goal %s",
            len(deps["children"]), rid, pid,
        )

    # 2. Remove the GCS blob (best-effort; if it fails the row is still
    #    soft-deleted and the workspace cleanup continues).
    if gcs_uri.startswith("gs://"):
        try:
            from app.storage import get_gcs_client
            parts = gcs_uri[5:].split("/", 1)
            if len(parts) == 2:
                bucket_name, blob_path = parts
                client = get_gcs_client()
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(blob_path)
                try:
                    blob.delete()
                    _log.info(
                        "Deleted GCS blob for resource %s: %s", rid, gcs_uri,
                    )
                except Exception as exc:
                    _log.warning(
                        "Failed to delete GCS blob %s: %s", gcs_uri, exc,
                    )
        except Exception as exc:
            _log.warning(
                "GCS cleanup failed for resource %s: %s", rid, exc,
            )

    # 3. Remove from every live agent workspace + refresh their RESOURCES.md.
    if kind in ("document", "file") and name:
        from app.resource_provisioning import (
            _safe_doc_filename, remove_document_from_all_workspaces,
        )
        local_path = f"docs/{_safe_doc_filename(name)}"
        import asyncio as _asyncio
        _asyncio.create_task(
            remove_document_from_all_workspaces(pid, local_path)
        )

    _log.info(
        "Deleted resource goal=%s rid=%s name=%r kind=%s",
        pid, rid, name, kind,
    )
    return RedirectResponse(
        url=f"/goals/{pid}/resources/browser", status_code=303,
    )


# ── Attach Test / Deploy SA to existing gcp_environment ──────────────────────

@router.post("/goals/{pid}/resources/{rid}/attach-sa")
async def attach_sa(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Attach a test or deploy SA to an existing gcp_environment.

    Mirrors the "+ Add Branch" pattern on repo detail — operator can fix
    up an env they registered without the SA, instead of archiving the
    env and re-creating it from scratch (`/goals/<pid>/resources/browser`
    + Cloud Resource form). The detail page shows the buttons only when
    that role isn't already attached, so the duplicate-role guard below
    is defence in depth against form replay / race.
    """
    role = await get_goal_role(user["uid"], pid)
    if role is None:
        raise HTTPException(status_code=403)

    form = await request.form()
    sa_role = (form.get("role", "") or "").strip().lower()
    sa_email = (form.get("sa_email", "") or "").strip()

    if sa_role not in ("test", "deploy"):
        raise HTTPException(
            status_code=400,
            detail="role must be 'test' or 'deploy'.",
        )
    if not sa_email:
        raise HTTPException(
            status_code=400, detail="sa_email is required.",
        )

    sa_kind = "gcp_test_sa" if sa_role == "test" else "gcp_deploy_sa"

    pool = get_pool()
    env = await pool.fetchrow(
        "SELECT id, kind, name, status, metadata FROM goal_resources "
        "WHERE id = $1 AND goal_id = $2",
        rid, pid,
    )
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found.")
    if env["kind"] != "gcp_environment":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Resource {rid} is a {env['kind']!r}, not a gcp_environment. "
                f"SAs can only be attached to gcp_environment resources."
            ),
        )
    if env["status"] != "active":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Environment is {env['status']!r} — unarchive it first before "
                f"attaching SAs."
            ),
        )

    # asyncpg returns JSONB as str by default; tolerate both.
    raw_meta = env["metadata"]
    if isinstance(raw_meta, str):
        try:
            parent_metadata = json.loads(raw_meta)
        except Exception:
            parent_metadata = {}
    else:
        parent_metadata = dict(raw_meta or {})

    # Same validator the form path + agent path use — catches bad SA
    # format and project-binding mismatch (SA's project portion must
    # equal parent env's target_project_id).
    from app.resource_kinds import validate_registration  # noqa: PLC0415
    err = validate_registration(
        kind=sa_kind,
        metadata={"sa_email": sa_email},
        parent_metadata=parent_metadata,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Refuse if an active SA of the same role is already attached. The
    # detail-page button is hidden in that case, but a stale tab or form
    # replay could still hit us — so check inside the transaction below
    # too (race protection).
    duplicate = await pool.fetchrow(
        """SELECT id, name FROM goal_resources
            WHERE goal_id = $1 AND parent_id = $2 AND kind = $3
              AND status = 'active'""",
        pid, rid, sa_kind,
    )
    if duplicate:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A {sa_role} SA is already attached to this environment "
                f"(resource {duplicate['id']}, name {duplicate['name']!r}). "
                f"Archive the existing one first if you want to replace it."
            ),
        )

    # _insert_sa_child uses ON CONFLICT DO NOTHING on the
    # (goal_id, kind, name) constraint. If an archived row with this
    # auto-generated name exists (e.g. operator attached + archived
    # earlier), the insert would silently no-op and the operator would
    # see a misleading success redirect. Catch that here and point them
    # at the unarchive flow instead.
    expected_name = f"{env['name']}:{sa_role}-sa"
    stale = await pool.fetchrow(
        """SELECT id, status FROM goal_resources
            WHERE goal_id = $1 AND kind = $2 AND name = $3""",
        pid, sa_kind, expected_name,
    )
    if stale:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A {sa_role} SA with display name {expected_name!r} already "
                f"exists for this goal (resource {stale['id']}, status "
                f"{stale['status']!r}). "
                + (
                    "Unarchive it from the resources browser (?show_archived=1) "
                    "if you want to reuse it, "
                    if stale["status"] == "archived"
                    else ""
                )
                + "or delete it permanently from its detail page."
            ),
        )

    import asyncpg
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _insert_sa_child(
                    conn, pid=pid, env_id=rid, env_name=env["name"],
                    user=user, sa_kind=sa_kind, sa_email=sa_email,
                )
    except asyncpg.exceptions.UniqueViolationError as exc:
        # Concurrent attach lost the race against the unique constraint
        # (goal_id, kind, name). Pre-checks above narrow when this fires.
        _log.warning(
            "attach-sa hit unique constraint: goal=%s env=%s role=%s — %s",
            pid, rid, sa_role, exc,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"A resource with the auto-generated name "
                f"{env['name']}:{sa_role}-sa already exists. Archive or "
                f"rename the existing one first."
            ),
        )

    # Same downstream hooks as the env-creation path — regenerate
    # RESOURCES.md so any live agent session (GO + active SHCAs/IAs) sees
    # the new SA without manual refresh.
    try:
        await _sync_goal_resources_md(pid)
    except Exception as exc:
        _log.warning("Failed to sync RESOURCES.md for goal %s: %s", pid, exc)

    try:
        from app.services.go_events import on_resource_registered
        await on_resource_registered(pid, rid, user["uid"])
    except Exception:
        _log.warning(
            "attach-sa: on_resource_registered hook failed for env %s",
            rid, exc_info=True,
        )

    _log.info(
        "Attached %s SA to env: goal=%s env=%s sa_email=%s",
        sa_role, pid, rid, sa_email,
    )

    return RedirectResponse(
        url=f"/goals/{pid}/resources/{rid}", status_code=303,
    )


# ── Trigger scan ─────────────────────────────────────────────────────────────

@router.post("/goals/{pid}/resources/{rid}/scan")
async def trigger_scan(
    pid: str, rid: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Manually trigger a resource scan/analysis."""
    role = await get_goal_role(user["uid"], pid)
    if role is None:  # Must be a goal member
        raise HTTPException(status_code=403)

    pool = get_pool()
    resource = await pool.fetchrow(
        "SELECT * FROM goal_resources WHERE id = $1 AND goal_id = $2", rid, pid,
    )
    if not resource:
        raise HTTPException(status_code=404)

    import asyncio
    if resource["kind"] == "repo" and resource.get("repo_owner"):
        # No branch: `scan_resource` decides which branch, and whether
        # to scan the repo row at all -- a goal with declared trunk
        # branches gets its summaries from those, not from here.
        asyncio.create_task(_background_scan(
            rid, pid, resource["repo_owner"], resource["repo_name"],
        ))
    elif resource.get("gcs_uri"):
        asyncio.create_task(_background_doc_scan(
            rid, pid, resource["gcs_uri"],
            resource.get("file_type", ""), resource["name"],
        ))

    return RedirectResponse(url=f"/goals/{pid}/resources/{rid}", status_code=303)


# ── Background helpers ───────────────────────────────────────────────────────

async def _background_scan(resource_id, goal_id, repo_owner, repo_name):
    """Background: scan a repo resource.

    THROUGH `scan_resource`, NOT `scan_repo_full`. This used to call the
    repo scanner directly with a branch the CALLER chose, which is how
    it came to pass `default_branch` -- GitHub's `main` -- for years.
    Going through the one entry point means the branch, and whether to
    scan at all, are decided in one place: a goal that has declared
    trunk branches gets no repo-level summary, because each of those
    branches is its own resource with its own summary and a repo card
    can only ever show one of them.

    Duplicating that rule here is how the two would drift, and the
    version that drifts is this one -- the UI path, which nobody runs in
    a test.
    """
    try:
        from app.services.resource_scanner import scan_resource
        out = await scan_resource(goal_id, resource_id) or {}
        if out.get("skipped"):
            _log.info("Repo scan skipped for %s/%s: %s",
                      repo_owner, repo_name, out.get("reason"))
        else:
            _log.info("Background repo scan complete: %s/%s",
                      repo_owner, repo_name)
    except Exception as exc:
        _log.error("Background repo scan failed: %s", exc)


async def _background_branch_scan(resource_id, goal_id, repo_owner, repo_name, branch_name, base_branch):
    """Background: full scan of a branch (what's in it, not just diff)."""
    try:
        from app.services.resource_scanner import scan_repo_full
        from app.routers.go_internal import _get_github_token
        token = await _get_github_token(goal_id, repo_name)
        # Full scan of the branch — GO needs to know what's IN the branch
        await scan_repo_full(
            resource_id, goal_id, repo_owner, repo_name,
            branch_name, token,
        )
        _log.info("Background branch scan complete: %s/%s:%s", repo_owner, repo_name, branch_name)
    except Exception as exc:
        _log.error("Background branch scan failed: %s", exc)


async def _background_doc_scan(resource_id, goal_id, gcs_uri, file_type, filename):
    """Background: scan a document."""
    try:
        from app.services.resource_scanner import scan_document
        await scan_document(resource_id, goal_id, gcs_uri, file_type, filename)
        _log.info("Background doc scan complete: %s", filename)
    except Exception as exc:
        _log.error("Background doc scan failed: %s", exc)


async def _sync_goal_resources_md(goal_id: str):
    """Sync RESOURCES.md for all agents in a goal after resource changes."""
    from app.resource_provisioning import sync_resources_md
    from app.openclaw import gateway_admin
    pool = get_pool()

    agents = await pool.fetch("""
        SELECT agent_id FROM agent_contexts WHERE goal_id = $1
    """, goal_id)

    for a in agents:
        try:
            await sync_resources_md(gateway_admin, a["agent_id"], goal_id)
        except Exception as exc:
            _log.debug("RESOURCES.md sync failed for %s: %s", a["agent_id"], exc)
