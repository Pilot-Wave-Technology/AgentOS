"""Plan-approval DAG ingestion (SHCA Phase 4 Stage 3-f.2).

When the human approves a plan:
  1. The control panel fetches `docs/plans/<name>.md` from the story
     workspace via `shca.workspace.readFile`.
  2. `dag_parser.parse_dag_from_markdown` validates the YAML DAG block.
  3. THIS module inserts:
       * One `plan_versions` row (audit + reference for stories.current_plan_id)
       * One parent "DAG task" row with executor='claude-code-dag'
       * N subtask rows with executor='claude-code-phase' (one per DAG node)
       * System-gate tasks (executor='system-gate') injected around integration-tester:
           G1 pre-integration  — inserted between unit-tester and integration-tester
           G2 integrate        — inserted between integration-tester and any subsequent phases
     all in a single transaction.
  4. Returns the parent task id so the caller can update
     `stories.current_plan_id` + flip phase to 'executing'.

Gate injection rules:
  - G1 (pre-integration) is injected if and only if integration-tester is present in the DAG.
  - G2 (integrate) is injected if and only if integration-tester is present in the DAG.
  - If integration-tester is skipped (not in DAG), no gates are injected.
  - When G2 is injected, diff_review_via_gate=TRUE is set on the parent DAG task so
    _on_dag_tasks_complete skips the end-of-DAG diff review (already done by G2).
  - See documentation/story-integration/FLOW.md and SYSTEM_GATE.md for the full design.
"""
from __future__ import annotations

import json as _json
import logging
import secrets
import uuid

from app.database import get_pool
from app.ledger.journal import append_event
from app.services import claude_phases as cp_svc
from app.services.dag_parser import (
    DagParseError,
    ParsedDag,
    parse_dag_from_markdown,
)
from app.services.story_workspace_layout import resolve_workspace_clones, write_clones


async def _is_local_story(pool, story_id: str) -> bool:
    """Is this story being executed in a local editor rather than the gateway?

    Used only to skip repo-alias resolution — see the comment at the call site for
    why local dispatch must not receive a `Repository: cd <subdir>/` instruction.
    """
    mode = await pool.fetchval(
        "SELECT execution_mode FROM stories WHERE id = $1", story_id,
    )
    return (mode or "") == "local"


async def _resolve_story_repo_aliases(pool, story_id: str) -> tuple[set[str], dict]:
    """Repo identifiers a story's plan may name → canonical workspace subdir.

    Returns (allowed_aliases, subdir_by_alias). Several spellings map to the same
    clone on purpose: the planner reads CLAUDE.md / RESOURCES.md, which show the
    directory (`repos/acme__web`), while it may naturally write the repo name
    (`acme/web` or `web`). Rejecting a correct-but-differently-spelled repo would be a
    pointless failure, so all three forms resolve to the same subdir.

    Only WRITE repos are offered: a task cannot be scoped to a read-only context
    clone, because it could not commit its work there.

    Returns (set(), {}) when the story has no node rows — a single-repo or legacy
    workspace — which makes the parser skip `repo:` validation entirely.
    """
    try:
        is_local = await _is_local_story(pool, story_id)
        # Write repos only: a task cannot be scoped to a read-only context clone,
        # because it could never commit its work there.
        rows = write_clones(await resolve_workspace_clones(pool, story_id))

        # SINGLE-repo LOCAL stories skip alias resolution on purpose.
        # `local_workspace_nodes` records their one clone with subdir='repo' (or, for a
        # path-contract-capable client, `repos/<owner>__<name>`) so /internal/git-
        # credential and the write-repo count work — but the extension opens that
        # clone directory ITSELF as the workspace folder, so the agent's cwd is
        # already inside it. Resolving an alias here could set `tasks.repo_subdir` and
        # make `build_dispatch_message` emit "Repository: `repo/` — cd repo/ ...",
        # telling the agent to cd into a directory it is already in. A single-repo
        # local checkout has nothing to disambiguate, so skipping keeps its dispatch
        # prompts byte-identical to before nodes were recorded.
        #
        # A MULTI-repo local checkout (research/NREPO_LOCAL_EXECUTION_PLAN.md, C8) is
        # the opposite: it has extra repos the agent's cwd is NOT already inside, so a
        # task now genuinely needs to know which one it targets — the same reason
        # gateway/operator multi-repo stories were never exempted from this. Detected
        # by repo COUNT, not execution_mode alone: a local story with exactly one write
        # repo must still see the byte-identical prompt above.
        if is_local and len(rows) <= 1:
            return set(), {}
    except Exception as exc:
        _log.debug(
            "dag_ingest: could not load workspace nodes for story=%s (%s) — "
            "skipping repo validation", story_id, exc,
        )
        return set(), {}

    # Unambiguous aliases first: the subdir and owner/name are unique per repo.
    subdir_by_alias: dict[str, str] = {}
    for row in rows:
        for alias in (row["subdir"], f"{row['repo_owner']}/{row['repo_name']}"):
            if alias:
                subdir_by_alias[alias] = row["subdir"]

    # The BARE repo name is only offered when it identifies exactly one repo. Two
    # granted repos can share a name across owners (acme/web + other/web); registering
    # both would let the last row silently win — in a row order Postgres does not
    # guarantee — and bind a task to the wrong checkout. Ambiguous bare names are simply
    # not accepted, so the planner gets a clear "not one of this story's repos" error
    # listing the unambiguous forms instead of being quietly mis-resolved.
    bare_counts: dict[str, int] = {}
    for row in rows:
        if row["repo_name"]:
            bare_counts[row["repo_name"]] = bare_counts.get(row["repo_name"], 0) + 1
    for row in rows:
        name = row["repo_name"]
        if name and bare_counts.get(name) == 1 and name not in subdir_by_alias:
            subdir_by_alias[name] = row["subdir"]
    ambiguous = sorted(n for n, c in bare_counts.items() if c > 1)
    if ambiguous:
        _log.info(
            "dag_ingest: story=%s has repos sharing a bare name %s — tasks must use "
            "the subdir or owner/name form", story_id, ambiguous,
        )
    return set(subdir_by_alias), subdir_by_alias


_log = logging.getLogger(__name__)
from app.ledger.actors import SYSTEM_ACTOR_ID, story_shca_actor  # canonical actors


def _short_id(length: int = 8) -> str:
    """Hierarchical-id-safe short token (matches existing task id style)."""
    return secrets.token_hex(length // 2)


async def ingest_approved_plan(
    *,
    goal_id: str,
    story_id: str,
    plan_path: str,
    plan_md: str,
    claude_session_id: str,
    approved_by_uid: str,
    content_hash: str = "",
) -> dict:
    """Insert the plan_version + DAG task tree on plan approval.

    `content_hash` (C3) fingerprints the approved plan markdown so a plan executed
    end-to-end can't be silently re-run (the local plan gate refuses a re-approval whose
    hash matches an already-'executed' version). Optional — gateway approvals leave it ''.

    Returns: {ok, dag_task_id, plan_version_id, task_count} on success.
    Raises DagParseError on validation failure (caller surfaces to UI).
    """
    pool = get_pool()

    # 1. Resolve allowed phases from the DB (active rows only).
    phases = await cp_svc.list_phases(only_active=True)
    allowed = {p["name"] for p in phases}

    # 1b. Resolve which repos this story may target (multi-repo plan M4.3). Sourced
    # from what provisioning actually cloned, and accepted in BOTH the subdir and the
    # bare-repo-name form because the planner sees both in its workspace docs. Empty
    # for a single-repo/legacy story, which makes the parser ignore `repo:` entirely.
    # NOTE: pass the MAPPING to the parser. The alias set is 3 entries per repo, so
    # handing it a set made every single-repo story look multi-repo (see dag_parser).
    _, subdir_by_alias = await _resolve_story_repo_aliases(pool, story_id)

    # 2. Parse + validate the DAG block.
    parsed: ParsedDag = parse_dag_from_markdown(
        plan_md, allowed_phases=allowed, repo_aliases=subdir_by_alias,
    )

    # 3. Generate ids.
    dag_task_id = _short_id(8)
    plan_version_id = uuid.uuid4()

    # Map DAG ids (T1, T2, ...) → DB task ids (dag_task_id-T1, ...)
    db_ids: dict[str, str] = {
        t.id: f"{dag_task_id}-{t.id}" for t in parsed.tasks
    }

    # Gate injection disabled — merge-and-push flow is handled in the dev branch.
    # Re-enable by restoring: repo_info = await _resolve_repo_info_for_story(...)
    # and gate_inserts = _plan_gate_injection(parsed.tasks, dag_task_id, db_ids, repo_info=repo_info)
    gate_inserts: list = []
    has_integrate_gate = False

    # Structured DAG payload stored in plan_versions.task_dag (JSONB).
    task_dag_json = {
        "tasks": [
            {
                "id": t.id,
                "phase": t.phase,
                "title": t.title,
                "description": t.description,
                "acceptance": t.acceptance,
                "depends_on": t.depends_on,
            }
            for t in parsed.tasks
        ],
        "gates": gate_inserts,
    }

    # 4. Insert plan_versions row, DAG task, and subtasks atomically.
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Determine next version number for this story.
            existing_version = await conn.fetchval(
                """SELECT COALESCE(MAX(version), 0) FROM plan_versions
                    WHERE story_id = $1""",
                story_id,
            )
            next_version = (existing_version or 0) + 1

            await conn.execute(
                """INSERT INTO plan_versions (
                       id, story_id, version, plan_doc_path,
                       task_dag, proposed_by, claude_session_id,
                       status, approved_at, approved_by, content_hash
                   ) VALUES (
                       $1, $2, $3, $4,
                       $5::JSONB, 'shca', $6,
                       'approved', NOW(), $7, $8
                   )""",
                plan_version_id,
                story_id,
                next_version,
                plan_path,
                _json.dumps(task_dag_json),
                claude_session_id,
                approved_by_uid,
                content_hash or "",
            )

            # Parent DAG task. Not dispatched directly; rolls up children.
            # diff_review_via_gate=TRUE when an integrate gate is present — suppresses
            # the unconditional end-of-DAG diff review in _on_dag_tasks_complete.
            await conn.execute(
                """INSERT INTO tasks (
                       id, goal_id, story_id, parent_task_id,
                       title, description,
                       executor, phase,
                       claude_session_id,
                       state,
                       acceptance_criteria,
                       depends_on,
                       diff_review_via_gate,
                       created_by, created_at, updated_at
                   ) VALUES (
                       $1, $2, $3, NULL,
                       $4, $5,
                       'claude-code-dag', NULL,
                       $6,
                       'in_progress',
                       'All subtasks complete + diff approved',
                       '{}'::TEXT[],
                       $7,
                       $8, NOW(), NOW()
                   )""",
                dag_task_id,
                goal_id,
                story_id,
                f"DAG · {plan_path} · v{next_version}",
                f"Execution of {len(parsed.tasks)} phase tasks per plan {plan_path} (v{next_version}).",
                claude_session_id,
                has_integrate_gate,
                approved_by_uid,
            )

            # Subtasks. Map depends_on to DB ids, accounting for gate rewiring.
            # gate_inserts may have overridden some tasks' depends_on (e.g. integration-tester
            # now depends on G1 rather than unit-tester). The rewired_deps dict captures that.
            # rewired_deps: task_db_id → new depends_on list after gate injection.
            # For G1: integration-tester must wait for G1, not for G1's predecessors.
            #         g["id"] is G1's own DB task id; g["depends_on"] is G1's deps
            #         (unit-tester) — do NOT use that as the rewired task's deps.
            # For rewire-only entries (post-integration tasks): g["depends_on"] already
            #         contains [g2_id, ...] constructed in _plan_gate_injection.
            rewired_deps: dict[str, list[str]] = {
                g["rewires_task_id"]: [g["id"]] if g.get("gate_kind") else g["depends_on"]
                for g in gate_inserts
                if g.get("rewires_task_id")
            }
            for t in parsed.tasks:
                child_id = db_ids[t.id]
                # Use gate-rewired deps if this task's dep was intercepted by a gate.
                db_deps = rewired_deps.get(child_id) or [db_ids[d] for d in t.depends_on]
                # 'ready' = dependencies satisfied (or none); 'pending' = waiting on deps.
                state = "ready" if not db_deps else "pending"
                await conn.execute(
                    """INSERT INTO tasks (
                           id, goal_id, story_id, parent_task_id,
                           title, description,
                           executor, phase,
                           claude_session_id,
                           state,
                           acceptance_criteria,
                           depends_on,
                           created_by, created_at, updated_at,
                           repo_subdir
                       ) VALUES (
                           $1, $2, $3, $4,
                           $5, $6,
                           'claude-code-phase', $7,
                           $8,
                           $9,
                           $10,
                           $11::TEXT[],
                           $12, NOW(), NOW(),
                           $13
                       )""",
                    child_id,
                    goal_id,
                    story_id,
                    dag_task_id,
                    t.title,
                    t.description,
                    t.phase,
                    claude_session_id,
                    state,
                    t.acceptance,
                    db_deps,
                    approved_by_uid,
                    # Normalize whatever alias the planner wrote (subdir or repo name)
                    # to the canonical workspace subdir, so executors get a path they
                    # can cd into without re-resolving. None for single-repo/legacy.
                    subdir_by_alias.get(t.repo) if t.repo else None,
                )

            # System-gate tasks (G1 pre-integration, G2 integrate).
            # Inserted after phase tasks so depends_on references are already in the table.
            # Rewire-only entries (gate_kind=None) carry dep-rewriting metadata for phase
            # tasks but are not inserted as tasks themselves.
            for gate in gate_inserts:
                if gate.get("gate_kind") is None:
                    continue  # rewire-only entry — not a task
                gate_state = "ready" if not gate["depends_on"] else "pending"
                await conn.execute(
                    """INSERT INTO tasks (
                           id, goal_id, story_id, parent_task_id,
                           title, description,
                           executor, gate_kind, gate_payload,
                           state,
                           depends_on,
                           created_by, created_at, updated_at
                       ) VALUES (
                           $1, $2, $3, $4,
                           $5, $6,
                           'system-gate', $7, $8::JSONB,
                           $9,
                           $10::TEXT[],
                           $11, NOW(), NOW()
                       )""",
                    gate["id"], goal_id, story_id, dag_task_id,
                    gate["title"], gate["description"],
                    gate["gate_kind"], _json.dumps(gate["gate_payload"]),
                    gate_state,
                    gate["depends_on"],
                    approved_by_uid,
                )

            # Story phase + plan-id pointer.
            await conn.execute(
                """UPDATE stories SET
                       current_plan_id = $1,
                       current_phase   = 'executing',
                       updated_at      = NOW()
                   WHERE id = $2""",
                str(plan_version_id), story_id,
            )

            # Ledger: DAG_INITIALIZED with full payload (for replay /
            # debugging — the DAG can be reconstructed from this event).
            # Materializes the SHCA's plan → attribute to the SHCA instance.
            _dag_actor, _dag_type = await story_shca_actor(story_id)
            await append_event(
                conn,
                actor_id=_dag_actor, actor_type=_dag_type,
                event_type="dag.initialized",
                payload={
                    "story_id": story_id,
                    "dag_task_id": dag_task_id,
                    "plan_version_id": plan_version_id,
                    "plan_path": plan_path,
                    "version": next_version,
                    "claude_session_id": claude_session_id,
                    "tasks": [
                        {
                            "id": db_ids[t.id],
                            "dag_id": t.id,
                            "phase": t.phase,
                            "title": t.title,
                            "depends_on": [db_ids[d] for d in t.depends_on],
                        }
                        for t in parsed.tasks
                    ],
                },
                context_goal_id=goal_id,
            )

    _log.info(
        "dag_ingest: created %d phase tasks + %d gates under DAG %s for story %s (plan v%d, diff_review_via_gate=%s)",
        len(parsed.tasks), len(gate_inserts), dag_task_id, story_id, next_version, has_integrate_gate,
    )

    return {
        "ok": True,
        "dag_task_id": dag_task_id,
        "plan_version_id": str(plan_version_id),
        "version": next_version,
        "task_count": len(parsed.tasks),
        "gate_count": len(gate_inserts),
        "task_ids": list(db_ids.values()),
        "gate_ids": [g["id"] for g in gate_inserts],
    }


def _plan_gate_injection(
    tasks,
    dag_task_id: str,
    db_ids: dict[str, str],
    repo_info: dict | None = None,
) -> list[dict]:
    """Compute gate tasks to inject based on which phases are present.

    Returns a list of gate descriptors. Each descriptor includes:
      id, gate_kind, title, description, gate_payload, depends_on (DB ids),
      rewires_task_id (the phase task whose depends_on this gate intercepts).

    Injection rules (see FLOW.md):
      - If 'integration-tester' is NOT in the DAG: no gates injected.
      - If 'integration-tester' IS present:
          G1 (pre-integration): depends on unit-tester (or last non-integration phase);
                                 integration-tester now depends on G1 instead.
          G2 (integrate):       depends on integration-tester;
                                 any subsequent phases (deployment) now depend on G2.
    """
    phase_ids = {t.phase: t.id for t in tasks}  # phase name → DAG id (T1, T2, ...)

    if "integration-tester" not in phase_ids:
        _log.debug("dag_ingest: integration-tester not in DAG — no gates injected")
        return []

    if repo_info is None:
        _log.warning(
            "dag_ingest: integration-tester present but story has no branch assignment in "
            "story_resources — skipping gate injection. Assign a branch resource first."
        )
        return []

    integration_tester_id = phase_ids["integration-tester"]
    integration_tester_db_id = db_ids[integration_tester_id]

    # Find what unit-tester (or the last phase before integration-tester) depends on,
    # to thread G1 into the dependency chain correctly.
    integration_task = next(t for t in tasks if t.id == integration_tester_id)
    # G1 inherits integration-tester's original depends_on (the task(s) before it).
    g1_depends_on = [db_ids[d] for d in integration_task.depends_on]

    g1_id = f"{dag_task_id}-G1"
    g2_id = f"{dag_task_id}-G2"

    # Find phases that come after integration-tester (e.g. deployment).
    # They currently depend on integration-tester; they will be rewired to depend on G2.
    post_integration_tasks = [
        t for t in tasks
        if integration_tester_id in t.depends_on
        and t.id != integration_tester_id
    ]
    g2_rewired_task_db_ids = {db_ids[t.id] for t in post_integration_tasks}

    gates = [
        {
            "id": g1_id,
            "gate_kind": "pre-integration",
            "title": "Gate G1 · pre-integration",
            "description": (
                "Resolve merge target, build local merge preview (merge target into feature), "
                "detect conflicts, generate merge report. Automated; only escalates to human on conflict."
            ),
            "gate_payload": repo_info or {},
            "depends_on": g1_depends_on,
            # G1 intercepts integration-tester's upstream deps:
            # integration-tester now depends on G1, not directly on unit-tester.
            "rewires_task_id": integration_tester_db_id,
        },
        {
            "id": g2_id,
            "gate_kind": "integrate",
            "title": "Gate G2 · integrate",
            "description": (
                "Single human approval gate: review net diff + merge report, choose landing mode "
                "(Direct Push or Create MR). Executes push + merge on approval."
            ),
            "gate_payload": repo_info or {},
            "depends_on": [integration_tester_db_id],
            "rewires_task_id": None,  # G2 doesn't intercept a specific task — all post-integration tasks rewire
        },
    ]

    # Rewire all post-integration tasks to depend on G2 instead of integration-tester.
    # The rewired_deps dict in ingest_approved_plan uses rewires_task_id for single-task
    # rewiring; for the G2 case we return each post-integration task as its own entry.
    for task in post_integration_tasks:
        task_db_id = db_ids[task.id]
        # Replace integration-tester dep with G2; keep any other deps.
        new_deps = [
            (g2_id if d == integration_tester_db_id else d)
            for d in [db_ids[dep] for dep in task.depends_on]
        ]
        gates.append({
            "id": None,  # not a gate task; this entry is just a rewire instruction
            "gate_kind": None,
            "title": None, "description": None, "gate_payload": None,
            "depends_on": new_deps,
            "rewires_task_id": task_db_id,
        })

    # Filter out the rewire-only entries from the gate_inserts list for DB insertion.
    actual_gates = [g for g in gates if g["gate_kind"] is not None]
    _log.info(
        "dag_ingest: injecting %d gates (G1, G2) — integration-tester=%s, post-integration tasks=%s",
        len(actual_gates), integration_tester_db_id, [db_ids[t.id] for t in post_integration_tasks],
    )
    return gates  # caller separates actual gates (gate_kind != None) from rewire instructions


async def _resolve_repo_info_for_story(pool, story_id: str) -> dict | None:
    """Look up gate_payload fields (repo resource + branch names) for a story.

    Returns a dict ready to embed as gate_payload, or None if no branch assignment found.

    Resolution order:
      1. story_resources WHERE relation='produces' AND goal_resources.kind='branch'
         — the story explicitly produces a branch (canonical, live path).
         The branch resource's parent repo is found via goal_resources WHERE kind='repo'
         on the same goal.
      2. No produces-branch assignment → return None (no gates for this story).

    story_branches (migration 039) is deprecated — empty in dev+feature. It is
    NOT consulted here; gate_payload relies entirely on goal_resources + story_resources.

    gate_payload fields:
      repo_goal_resource_id — goal_resources.id of the repo (TEXT)
      source_branch         — the feature branch name (from the branch resource metadata)
      target_branch         — the trunk branch to merge into (repo's default_branch)
      provider              — 'github' | 'gitlab' | '' (from repo's metadata)
    """
    # Look up the branch resource this story works on.
    # relation='produces' is the canonical value for coding stories (set by create_branch
    # and grant_resource with relation='produces').
    # relation='needs' on a branch is also accepted as a fallback for stories created
    # before the produces/needs distinction was enforced (pre-June 2026 goals).
    # Review-only stories that reference a branch with 'needs' but never push to it will
    # also match here, but they will NOT have integration-tester in their DAG and so will
    # not receive G1/G2 gates (gate injection is still gated on integration-tester presence).
    branch_row = await pool.fetchrow(
        """SELECT gr.id AS branch_resource_id,
                  gr.branch_name,
                  gr.metadata AS branch_metadata,
                  gr.goal_id
             FROM story_resources sr
             JOIN goal_resources gr ON gr.id = sr.resource_id
            WHERE sr.story_id = $1
              AND sr.relation IN ('produces', 'needs')
              AND gr.kind = 'branch'
            ORDER BY
                -- prefer produces over needs when both exist
                CASE WHEN sr.relation = 'produces' THEN 0 ELSE 1 END,
                gr.created_at ASC
            LIMIT 1""",
        story_id,
    )

    if not branch_row:
        _log.info(
            "dag_ingest: story %s has no branch story_resource (produces/needs) — no gates injected",
            story_id,
        )
        return None

    # branch_name is a dedicated column; metadata is a fallback for legacy rows.
    branch_metadata = branch_row["branch_metadata"] or {}
    if isinstance(branch_metadata, str):
        branch_metadata = _json.loads(branch_metadata)

    source_branch = (
        branch_row["branch_name"]
        or branch_metadata.get("branch_name")
        or branch_metadata.get("name")
        or ""
    )

    if not source_branch:
        _log.warning(
            "dag_ingest: story %s branch resource %s has no branch_name — cannot determine source_branch",
            story_id, branch_row["branch_resource_id"],
        )
        return None

    # Find the repo resource for this goal (the branch lives on the repo).
    goal_id = branch_row["goal_id"]
    repo_row = await pool.fetchrow(
        """SELECT id, default_branch, metadata FROM goal_resources
            WHERE goal_id = $1 AND kind = 'repo'
            ORDER BY created_at ASC LIMIT 1""",
        goal_id,
    )
    if not repo_row:
        _log.warning(
            "dag_ingest: story %s goal %s has no repo goal_resource — cannot build gate_payload",
            story_id, goal_id,
        )
        return None

    repo_metadata = repo_row["metadata"] or {}
    if isinstance(repo_metadata, str):
        repo_metadata = _json.loads(repo_metadata)

    # default_branch is a dedicated column; metadata is a fallback for legacy rows.
    target_branch = repo_row["default_branch"] or repo_metadata.get("default_branch") or "main"
    provider = repo_metadata.get("provider", "")

    _log.info(
        "dag_ingest: resolved repo_info for story %s — repo=%s source=%s target=%s provider=%s",
        story_id, repo_row["id"], source_branch, target_branch, provider,
    )
    return {
        "repo_goal_resource_id": repo_row["id"],
        "source_branch": source_branch,
        "target_branch": target_branch,
        "provider": provider,
    }
