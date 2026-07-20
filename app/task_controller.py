"""Task Controller — event-driven worker for dispatching and orchestrating tasks.

Event logging follows State Ledger spec v0.1 Section 4.1:
  - Every task mutation writes to ledger_events (source of truth) + task_activity (projection)
  - correlation_id = DECISION_RECORDED event_id that spawned the work tree
  - causation_ids = DAG linking each event to its direct causes
  - Artifacts (branches, files, test results) embedded in event payloads

Controller triggers:
  - task_created → check if ready (no deps) → dispatch
  - task_completed → resolve dependents → dispatch newly ready tasks
  - task_failed → cascade impact
  - human_responded → resume blocked task
"""
import asyncio
import json
import logging
import re
import uuid
from typing import Optional, Union

import asyncpg

from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError, make_task_session_key
from app.resource_provisioning import generate_resources_md
from app.github_tokens import generate_installation_token
from app.notifications import push_notification
from app.ledger.journal import append_event
from app.ledger.schema import (
    TASK_CREATED, TASK_ASSIGNED, TASK_STATUS_CHANGED, TASK_COMPLETED,
    TASK_FAILED, TASK_CANCELLED, TASK_DEPENDENCY_ADDED, TASK_DEPENDENCY_RESOLVED,
    TASK_PROGRESS_UPDATED, TASK_CONTEXT_UPDATED, TASK_HANDOFF,
    QUESTION_RAISED, QUESTION_ANSWERED, DECISION_RECORDED,
    AGENT_PROVISIONED, AGENT_DECOMMISSIONED, ASSESSMENT_COMPLETED,
    ESCALATION_REQUESTED, ESCALATION_TIMED_OUT,
)
from app.ledger.payloads import (
    actor_ref, artifact_ref,
    task_created_payload, task_assigned_payload, task_status_changed_payload,
    task_completed_payload, task_failed_payload, task_cancelled_payload,
    task_dependency_added_payload, task_dependency_resolved_payload,
    task_progress_updated_payload, task_context_updated_payload,
    agent_provisioned_payload, agent_decommissioned_payload,
    question_raised_payload, question_answered_payload,
    extract_artifacts_from_report, assessment_completed_payload,
)

_log = logging.getLogger(__name__)

# ── Actor & Artifact Helpers ────────────────────────────────────────────────

# One canonical, registered system actor — imported, not re-declared (this used
# to be an inline literal, one of ~20 divergent copies). Re-exported here because
# tasks.py imports SYSTEM_ACTOR_ID from this module. See app/ledger/actors.py.
from app.ledger.actors import SYSTEM_ACTOR_ID, instance_actor  # noqa: E402


def _actor_uuid(instance_id: str = "", agent_id: str = "") -> uuid.UUID:
    """Deterministic UUID fallback. Use instance_actor_id() for real UUIDs."""
    if instance_id:
        return uuid.uuid5(uuid.NAMESPACE_DNS, f"instance:{instance_id}")
    if agent_id:
        return uuid.uuid5(uuid.NAMESPACE_DNS, f"agent:{agent_id}")
    return SYSTEM_ACTOR_ID


async def instance_actor_id(task_id: str = "", gateway_agent_id: str = "") -> uuid.UUID:
    """Get the real, *registered* UUID for an agent instance.

    Delegates to app.ledger.actors.instance_actor, which resolves by task_id then
    gateway_agent_id, and — critically — on a miss falls back to the task's story
    SHCA instance ("the assistant it runs on") and finally the system actor. It
    never mints an unregistered uuid5, so every emitted actor_id resolves to an
    `actors` row. (The type it returns is ignored here; call instance_actor
    directly when you also need the actor_type.)"""
    from app.ledger.actors import instance_actor as _resolve
    actor_id, _actor_type = await _resolve(task_id=task_id, gateway_agent_id=gateway_agent_id)
    return actor_id


# Re-exported from payloads module for backward compat (tasks.py imports these)
build_actor_ref = actor_ref
build_artifact_ref = artifact_ref


# ── Core Event Emission ─────────────────────────────────────────────────────

async def emit_task_event(
    conn_or_pool: Union[asyncpg.Connection, asyncpg.Pool],
    *,
    event_type: str,
    task_id: str,
    goal_id: str,
    payload: dict,
    actor_id: uuid.UUID = SYSTEM_ACTOR_ID,
    actor_type: str = "system",
    correlation_id: str = "",
    causation_ids: list[str] | None = None,
    context_conversation_id: str = "",
    # For task_activity projection (backward compat):
    summary: str = "",
    content: str = "",
    agent_id: str = "",
    instance_id: str = "",
) -> dict | None:
    """Dual-write: ledger_events (source of truth) + task_activity (projection).

    If conn_or_pool is a Connection (caller owns transaction), use it directly.
    If it's a Pool, acquire a connection and create a transaction.

    context_conversation_id: the thread_id for this task's chat. Auto-resolved
    from tasks.thread_id if not provided and task_id is set.

    Returns the append_event result dict (with event_id) or None.
    """
    if isinstance(conn_or_pool, asyncpg.pool.Pool):
        async with conn_or_pool.acquire() as conn:
            async with conn.transaction():
                return await _emit_inner(
                    conn, event_type=event_type, task_id=task_id, goal_id=goal_id,
                    payload=payload, actor_id=actor_id, actor_type=actor_type,
                    correlation_id=correlation_id, causation_ids=causation_ids,
                    context_conversation_id=context_conversation_id,
                    summary=summary, content=content, agent_id=agent_id,
                    instance_id=instance_id,
                )
    else:
        return await _emit_inner(
            conn_or_pool, event_type=event_type, task_id=task_id, goal_id=goal_id,
            payload=payload, actor_id=actor_id, actor_type=actor_type,
            correlation_id=correlation_id, causation_ids=causation_ids,
            context_conversation_id=context_conversation_id,
            summary=summary, content=content, agent_id=agent_id,
            instance_id=instance_id,
        )


async def _emit_inner(
    conn: asyncpg.Connection,
    *,
    event_type: str,
    task_id: str,
    goal_id: str,
    payload: dict,
    actor_id: uuid.UUID,
    actor_type: str,
    correlation_id: str,
    causation_ids: list[str] | None,
    context_conversation_id: str,
    summary: str,
    content: str,
    agent_id: str,
    instance_id: str,
) -> dict | None:
    """Inner dual-write (must be called within a transaction)."""
    # Auto-resolve conversation_id from the task's thread_id if not provided
    conversation_id = context_conversation_id
    if not conversation_id and task_id:
        conversation_id = await conn.fetchval(
            "SELECT thread_id FROM tasks WHERE id = $1", task_id,
        ) or ""

    # 1. Ledger event (hash-chained, authoritative audit log)
    result = await append_event(
        conn,
        event_type=event_type,
        payload=payload,
        actor_id=actor_id,
        actor_type=actor_type,
        correlation_id=correlation_id or None,
        causation_ids=causation_ids,
        context_task_id=task_id,
        context_goal_id=goal_id,
        context_conversation_id=conversation_id,
    )

    # 2. Task activity projection (denormalized, fast queries)
    if task_id:
        await conn.execute(
            """INSERT INTO task_activity
                   (task_id, goal_id, agent_id, instance_id, type, summary, content, metadata)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            task_id, goal_id, agent_id, instance_id,
            event_type, summary or payload.get("summary", event_type),
            content[:4000] if content else "",
            json.dumps(payload),
        )

    return result


# ── Causation Helpers ───────────────────────────────────────────────────────

async def get_last_event_id(conn_or_pool, task_id: str) -> str | None:
    """Return the event_id of the most recent ledger event for a task."""
    if isinstance(conn_or_pool, asyncpg.pool.Pool):
        row = await conn_or_pool.fetchval(
            """SELECT event_id FROM ledger_events
               WHERE context_task_id = $1
               ORDER BY sequence_number DESC LIMIT 1""",
            task_id,
        )
    else:
        row = await conn_or_pool.fetchval(
            """SELECT event_id FROM ledger_events
               WHERE context_task_id = $1
               ORDER BY sequence_number DESC LIMIT 1""",
            task_id,
        )
    return str(row) if row else None


async def get_dep_completion_event_ids(
    conn_or_pool, dep_task_ids: list[str],
) -> list[str]:
    """Find TASK_COMPLETED event_ids for the given tasks (for causation on dep resolution)."""
    if not dep_task_ids:
        return []
    q = conn_or_pool if isinstance(conn_or_pool, asyncpg.pool.Pool) else conn_or_pool
    rows = await q.fetch(
        """SELECT DISTINCT ON (context_task_id) event_id
           FROM ledger_events
           WHERE context_task_id = ANY($1) AND event_type = $2
           ORDER BY context_task_id, sequence_number DESC""",
        dep_task_ids, TASK_COMPLETED,
    )
    return [str(r["event_id"]) for r in rows]


async def find_dependent_tasks(conn_or_pool, goal_id: str, task_id: str) -> list[str]:
    """Find task IDs that depend on the given task (for cascade_impact)."""
    q = conn_or_pool if isinstance(conn_or_pool, asyncpg.pool.Pool) else conn_or_pool
    rows = await q.fetch(
        "SELECT id FROM tasks WHERE goal_id = $1 AND $2 = ANY(depends_on) AND status = 'active'",
        goal_id, task_id,
    )
    return [r["id"] for r in rows]


async def get_task_correlation_id(conn_or_pool, task_id: str) -> str:
    """Get the correlation_id (decision_event_id) for a task."""
    q = conn_or_pool if isinstance(conn_or_pool, asyncpg.pool.Pool) else conn_or_pool
    row = await q.fetchrow(
        "SELECT decision_event_id, correlation_id FROM tasks WHERE id = $1", task_id,
    )
    if not row:
        return ""
    return row["decision_event_id"] or str(row["correlation_id"] or "") or ""


# ── Task Activity Query (backward compat) ───────────────────────────────────

async def get_task_activities(task_id: str, types: list[str] = None, limit: int = 20) -> list[dict]:
    """Query task_activity rows for a task."""
    pool = get_pool()
    if types:
        rows = await pool.fetch(
            """SELECT * FROM task_activity
               WHERE task_id = $1 AND type = ANY($2)
               ORDER BY created_at DESC LIMIT $3""",
            task_id, types, limit,
        )
    else:
        rows = await pool.fetch(
            """SELECT * FROM task_activity
               WHERE task_id = $1
               ORDER BY created_at DESC LIMIT $2""",
            task_id, limit,
        )
    return [dict(r) for r in rows]


# ── Kept for backward compat — callers migrating incrementally ──────────────

async def log_activity(
    task_id: str,
    goal_id: str,
    activity_type: str,
    summary: str,
    content: str = "",
    agent_id: str = "",
    instance_id: str = "",
    metadata: dict = None,
) -> None:
    """DEPRECATED — use emit_task_event() for new code.

    Kept for backward compat during incremental migration.
    Dual-writes to task_activity + ledger_events.
    """
    pool = get_pool()
    actor_id = _actor_uuid(instance_id, agent_id)
    actor_type = "independent_agent" if (instance_id or agent_id) else "system"
    meta = metadata or {}
    corr_id = await get_task_correlation_id(pool, task_id) if task_id else ""

    await emit_task_event(
        pool,
        event_type=activity_type,
        task_id=task_id,
        goal_id=goal_id,
        payload={
            "summary": summary,
            "content": content[:2000] if content else "",
            **({"metadata": meta} if meta else {}),
        },
        actor_id=actor_id,
        actor_type=actor_type,
        correlation_id=corr_id,
        summary=summary,
        content=content,
        agent_id=agent_id,
        instance_id=instance_id,
    )


# ── Instance Manager ─────────────────────────────────────────────────────────

async def create_instance(task: dict) -> Optional[str]:
    """Create a gateway agent instance from a template for this task."""
    pool = get_pool()
    template_id = task.get("template_id") or task.get("assigned_to")
    if not template_id:
        _log.warning("Task %s has no template_id — cannot create instance", task["id"])
        return None

    goal_id = task["goal_id"]
    task_id = task["id"]
    instance_id = f"{template_id}-{goal_id[:8]}-{task_id}"

    template = await pool.fetchrow(
        "SELECT * FROM agents WHERE id = $1 AND status = 'active'", template_id,
    )
    if not template:
        _log.warning("Template %s not found for task %s", template_id, task_id)
        return None

    template = dict(template)
    workspace = f"~/.openclaw/workspace-{instance_id}"

    # Resolve tool allow/deny lists through the registry (profiles + direct tools +
    # cancellation). This is the same resolver used by provision.py — the dispatch
    # path must go through it so admin tool edits and profile changes take effect
    # on the next task run without requiring a manual template re-provision.
    try:
        from app.services.tool_registry import get_template_profiles, resolve_tool_allowlist
        profiles = await get_template_profiles(pool, template_id)
        raw_allow = list(template.get("tools_allow") or [])
        raw_deny = list(template.get("tools_deny") or [])
        effective_allow, effective_deny = await resolve_tool_allowlist(
            pool, profiles, raw_allow, raw_deny,
        )
        _log.info(
            "IA dispatch tools resolved: template=%s allow=%d deny=%d",
            template_id, len(effective_allow), len(effective_deny),
        )
    except Exception as _exc:
        # Non-fatal: fall back to the raw column values so dispatch isn't blocked
        # by a resolver failure (stale profile, DB hiccup, import error).
        _log.warning(
            "IA dispatch tool resolver failed for template=%s, using raw column: %s",
            template_id, _exc,
        )
        effective_allow = list(template.get("tools_allow") or [])
        effective_deny = list(template.get("tools_deny") or [])

    tools_param = {}
    if effective_allow:
        tools_param["allow"] = effective_allow
    if effective_deny:
        tools_param["deny"] = effective_deny

    try:
        gw_result = await gateway_admin.call("agents.create", {
            "name": instance_id,
            "workspace": workspace,
            "emoji": template.get("emoji", ""),
            "model": template.get("model", "claude-cli/sonnet"),
            **({"tools": tools_param} if tools_param else {}),
        })
        gw_agent_id = gw_result.get("agentId", instance_id)
        _log.info("Created instance %s for task %s (model + tools in one call)", gw_agent_id, task_id)
    except GatewayError as exc:
        if "already exists" in str(exc):
            gw_agent_id = instance_id
            _log.info("Instance %s already exists on gateway — reusing for task %s", gw_agent_id, task_id)
        else:
            _log.error("Failed to create instance for task %s: %s", task_id, exc)
            return None
    except Exception as exc:
        _log.error("Failed to create instance for task %s: %s", task_id, exc)
        return None

    # Copy template files (no config reload — direct file writes to workspace)
    agent_files = await pool.fetch(
        "SELECT filename, content FROM agent_files WHERE agent_id = $1", template_id,
    )
    for f in agent_files:
        try:
            await gateway_admin.set_agent_file(gw_agent_id, f["filename"], f["content"])
        except Exception as exc:
            _log.warning("Failed to write %s to %s: %s", f["filename"], gw_agent_id, exc)

    # Provision RESOURCES.md (no config reload — direct file write).
    # IA resources resolve via tasks.story_id → story_resources → goal_resources.
    # Legacy `task_resources JOIN resources` is no longer consulted.
    try:
        from app.resource_provisioning import download_documents_to_workspace

        # Review tasks (RA) inherit their resource access from the
        # reviewed task. The RA's own task row has no story_id /
        # task_resource_deps of its own; we resolve everything against
        # the work task it's reviewing so the RA sees the same repo,
        # branch, and credentials the planner / dev agents had.
        resource_task_id = task_id
        review_target_id = await pool.fetchval(
            "SELECT review_target_id FROM tasks WHERE id = $1", task_id,
        )
        if review_target_id:
            resource_task_id = review_target_id
            _log.info(
                "IA provision: review task %s → resolving resources against "
                "reviewed task %s",
                task_id, review_target_id,
            )

        story_id_for_task = await pool.fetchval(
            "SELECT story_id FROM tasks WHERE id = $1", resource_task_id,
        ) or ""
        _log.info(
            "IA provision: task=%s template=%s gw=%s resolved story=%s",
            task_id, template_id, gw_agent_id,
            story_id_for_task or "(none — falling back to goal scope)",
        )

        github_token = None
        if story_id_for_task:
            git_repos = await pool.fetch(
                """SELECT gr.name, COALESCE(gr.clone_url, gr.uri) AS uri
                     FROM story_resources sr
                     JOIN goal_resources gr ON gr.id = sr.resource_id
                    WHERE sr.story_id = $1 AND gr.status = 'active'
                      AND gr.kind IN ('repo', 'branch')
                      AND gr.repo_name IS NOT NULL AND gr.repo_name != ''""",
                story_id_for_task,
            )
        else:
            git_repos = await pool.fetch(
                """SELECT name, COALESCE(clone_url, uri) AS uri
                     FROM goal_resources
                    WHERE goal_id = $1 AND status = 'active'
                      AND kind IN ('repo', 'branch')
                      AND repo_name IS NOT NULL AND repo_name != ''""",
                goal_id,
            )

        repo_names = []
        for r in git_repos:
            uri = r["uri"] or ""
            if "github.com" in uri:
                name = uri.rstrip("/").rstrip(".git").split("/")[-1]
                if name and name not in repo_names:
                    repo_names.append(name)
        _log.info(
            "IA provision: task=%s git_repos=%d repo_names=%s",
            task_id, len(git_repos), repo_names,
        )

        if repo_names:
            try:
                # Read-write by default — matches user policy: "for repos and
                # branches let there be read-write access by default everywhere".
                token_data = await generate_installation_token(
                    repo_names,
                    {"contents": "write", "metadata": "read", "workflows": "write"},
                )
                github_token = token_data["token"]
            except Exception as exc:
                _log.warning("Failed to generate GitHub token for instance %s: %s", gw_agent_id, exc)

        documents = await download_documents_to_workspace(
            gateway_admin, gw_agent_id, goal_id,
            story_id=story_id_for_task or None, agent_id=template_id,
        )
        resources_md = await generate_resources_md(
            template_id, goal_id, github_token=github_token,
            task_id=resource_task_id, documents=documents,
        )
        await gateway_admin.set_agent_file(gw_agent_id, "RESOURCES.md", resources_md)
        _log.info(
            "IA provision: wrote RESOURCES.md task=%s gw=%s bytes=%d docs=%d",
            task_id, gw_agent_id, len(resources_md), len(documents),
        )
    except Exception as exc:
        _log.warning("Failed to provision RESOURCES.md for %s: %s", gw_agent_id, exc)

    # Wait for gateway config reload to complete.
    # agents.create writes to disk, but the gateway reloads config ASYNCHRONOUSLY.
    # The agent method uses in-memory config (loadConfig()) — if we dispatch before
    # reload, the agent method can't find the agent and silently drops the message.
    # Poll agents.list to confirm the agent is in memory before proceeding.
    agent_ready = False
    for attempt in range(10):
        await asyncio.sleep(1)
        try:
            agents_result = await gateway_admin.call("agents.list")
            agent_ids = [a.get("id", "") for a in agents_result.get("agents", [])]
            if gw_agent_id in agent_ids:
                _log.info("Agent %s confirmed in gateway memory (attempt %d)", gw_agent_id, attempt + 1)
                agent_ready = True
                break
        except Exception:
            pass
    if not agent_ready:
        _log.error("Agent %s not found in gateway after 10s — aborting dispatch", gw_agent_id)
        return None

    await pool.execute(
        "UPDATE tasks SET instance_id = $1 WHERE id = $2", gw_agent_id, task_id,
    )

    # Register in agent_instances table → gets a real UUID
    instance_row = await pool.fetchrow(
        """INSERT INTO agent_instances
               (template_id, gateway_agent_id, task_id, goal_id, workspace, model)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        template_id, gw_agent_id, task_id, goal_id,
        workspace, template.get("model", ""),
    )
    instance_uuid = instance_row["id"]

    # Eagerly register this instance as a ledger `actors` row BEFORE the first
    # instance-attributed emit (AGENT_PROVISIONED below uses it as actor_id), so
    # attribution resolves immediately instead of relying on the lazy backstop.
    try:
        from app.ledger.actors import register_agent_instance_actor
        await register_agent_instance_actor(instance_uuid)
    except Exception:
        pass

    # Emit IAM_AGENT_PROVISIONED with real instance UUID as actor_id
    corr_id = await get_task_correlation_id(pool, task_id)
    last_evt = await get_last_event_id(pool, task_id)
    await emit_task_event(
        pool,
        event_type=AGENT_PROVISIONED,
        task_id=task_id, goal_id=goal_id,
        payload=agent_provisioned_payload(
            instance_id=gw_agent_id, template_id=template_id,
            workspace=workspace, model=template.get("model", ""),
            spawned_by=actor_ref(SYSTEM_ACTOR_ID, "system", "task_controller"),
        ),
        actor_id=instance_uuid, actor_type="independent_agent",
        correlation_id=corr_id,
        causation_ids=[last_evt] if last_evt else None,
        summary=f"Created instance {gw_agent_id} from {template_id}",
        instance_id=gw_agent_id, agent_id=template_id,
    )
    return gw_agent_id


async def destroy_instance(task: dict) -> None:
    """Delete the gateway agent instance and workspace.

    Best-effort: if the gateway agent is already gone (expired, manually
    deleted), we log a warning and continue with DB cleanup. This must
    never raise — callers rely on it being safe to call unconditionally.
    """
    gw_instance_id = task.get("instance_id")
    if not gw_instance_id:
        return

    # Delete the session first (conversation transcript)
    session_key = make_task_session_key(gw_instance_id, task['id'])
    try:
        await gateway_admin.delete_session(session_key, delete_transcript=True)
        _log.info("Deleted session %s", session_key)
    except Exception as exc:
        _log.debug("Session delete for %s: %s", session_key, exc)

    # Then delete the agent (definition + workspace). IA agents are
    # per-task ephemeral — by design their memory + transcripts are NOT
    # archived to GCS (only SHCA + GO go through archive-and-delete).
    try:
        await gateway_admin.delete_agent(gw_instance_id, delete_files=True)
        _log.info("Destroyed instance %s", gw_instance_id)
    except GatewayError as exc:
        if "not found" in str(exc).lower():
            _log.info("Gateway agent already gone: %s", gw_instance_id)
        else:
            _log.warning("Failed to delete gateway agent %s: [%s] %s", gw_instance_id, exc.code, exc)
    except Exception as exc:
        _log.error("Unexpected error deleting gateway agent %s: %s", gw_instance_id, exc)

    try:
        pool = get_pool()

        # Mark instance as destroyed in agent_instances table
        await pool.execute(
            """UPDATE agent_instances SET status = 'destroyed', destroyed_at = now()
               WHERE gateway_agent_id = $1 AND task_id = $2""",
            gw_instance_id, task["id"],
        )

        actor_id = await instance_actor_id(task_id=task["id"], gateway_agent_id=gw_instance_id)
        corr_id = await get_task_correlation_id(pool, task["id"])
        last_evt = await get_last_event_id(pool, task["id"])
        await emit_task_event(
            pool,
            event_type=AGENT_DECOMMISSIONED,
            task_id=task["id"], goal_id=task["goal_id"],
            payload=agent_decommissioned_payload(
                instance_id=gw_instance_id, reason="Task lifecycle cleanup",
            ),
            actor_id=actor_id, actor_type="independent_agent",
            correlation_id=corr_id,
            causation_ids=[last_evt] if last_evt else None,
            summary=f"Destroyed instance {gw_instance_id}",
            instance_id=gw_instance_id,
        )
    except Exception as exc:
        _log.warning("DB cleanup for instance %s failed (non-fatal): %s", gw_instance_id, exc)


# ── Dispatch Message Builder ─────────────────────────────────────────────────

async def _build_review_dispatch_message(task: dict, pool) -> str:
    """Build a tight, role-clear dispatch prompt for an RA task.

    The AGENTS.md / SOUL.md the RA loads at startup already contain the
    role instructions, step-by-step flow, tool usage and report schema.
    This dispatch only needs to hand the RA the *specific case*: which
    task it's reviewing, what type of review it is, and the verdict
    schema reminder anchored on the right task id.

    Two flavours by the reviewed task's `plan_content`:

    - **code-review**: plan_content is set → there is a planner-produced
      plan and (presumably) committed work to clone and diff.
    - **non-coding-review**: no plan_content → assess the reported
      outcome against the acceptance criteria.

    Keeps the message under ~2 KB for non-coding reviews and ~3 KB for
    code reviews (no plan inlining — the RA reads PLAN.md from the
    cloned repo, same as it does for PROGRESS.md).
    """
    review_task_id = task["id"]
    review_target_id = task.get("review_target_id", "")

    reviewed = await pool.fetchrow(
        "SELECT * FROM tasks WHERE id = $1", review_target_id,
    )
    if not reviewed:
        return (
            f"## ERROR — reviewed task not found\n\n"
            f"**YOUR TASK ID:** `{review_task_id}`\n"
            f"**REVIEWING TASK:** `{review_target_id}` (not found)\n\n"
            f"Call your `task_report` tool with:\n"
            f"  task_id: `{review_task_id}`\n"
            f"  status: `failed`\n"
            f"  summary: `Reviewed task not found`\n"
            f"  details: `id={review_target_id}`\n"
        )
    reviewed = dict(reviewed)

    # All reviews are treated as code reviews for now — the coding-vs-
    # non-coding heuristic is deferred until we have a stronger signal
    # (e.g. task_resource_deps showing a git_repo). The RA always clones
    # the repo and reviews the diff.
    plan_content = (reviewed.get("plan_content") or "").strip()
    is_coding = True
    review_type = "code-review"

    title = reviewed.get("title", "(no title)")
    description = reviewed.get("description") or "(no description)"
    acceptance = (
        reviewed.get("acceptance_criteria")
        or "(none specified — assess against the task description)"
    )

    # Compact per-work-subtask summary — 250 chars each, no full result dumps.
    work_children = await pool.fetch(
        """SELECT id, title, template_id, state, result
             FROM tasks
            WHERE parent_task_id = $1 AND status = 'active'
              AND review_target_id IS NULL
            ORDER BY created_at""",
        review_target_id,
    )
    child_lines = []
    for c in [dict(r) for r in work_children]:
        result_preview = (c.get("result") or "").replace("\n", " ")[:250]
        child_lines.append(
            f"- {c['title']} ({c.get('template_id') or '?'}) — "
            f"{c['state']}: {result_preview}"
        )
    child_block = "\n".join(child_lines) if child_lines else "(no work subtasks)"

    header = (
        f"# Task Reviewer Dispatch\n\n"
        f"**REVIEW TYPE:** `{review_type}`\n"
        f"**YOUR TASK ID:** `{review_task_id}`   ← use this in every "
        f"`task_report` and `message_assistant` tool call\n"
        f"**REVIEWING TASK:** `{review_target_id}` — \"{title}\"\n"
    )

    reviewed_block = (
        f"## What you're reviewing\n\n"
        f"**Description:**\n{description}\n\n"
        f"**Acceptance criteria:**\n{acceptance}\n\n"
        f"**What each work subtask reported:**\n{child_block}\n"
    )

    steps_block = (
        "## How to review\n\n"
        "1. Read `RESOURCES.md` → find the repo URL and working branch.\n"
        "2. `git clone <repo>` then `git checkout <working-branch>`.\n"
        "3. Read `.plan/<namespace>/plan.md` inside the clone (path is in\n"
        "   RESOURCES.md / your AGENTS.md) — that's what the work agents\n"
        "   were supposed to implement.\n"
        "4. Read the files the plan said would be created or modified.\n"
        "   Verify the plan was actually implemented — present, working,\n"
        "   meeting the acceptance criteria.\n"
        "5. Make a decision: approve or reject.\n\n"
        "Use `message_assistant` to send progress updates as you go (\"cloned\n"
        "the repo\", \"reading plan.md\", \"checking calculator.py\"). It's\n"
        "non-blocking and tells the team you're alive.\n"
    )

    report_block = (
        "## Reporting completion\n\n"
        f"Call the `task_report` tool **once**, as your final action. "
        f"Your task id is `{review_task_id}` (NOT `{review_target_id}`).\n\n"
        "**Your turn ends the moment you call `task_report`.** No further "
        "messages, no closing prose, no follow-up tool calls. The system marks "
        "your review complete the instant the call lands.\n\n"
        "Tool call arguments:\n\n"
        "```json\n"
        "{\n"
        f"  \"task_id\": \"{review_task_id}\",\n"
        "  \"status\": \"completed\",\n"
        "  \"verdict\": \"approve|request_changes|block\",\n"
        "  \"score\": 0-100,\n"
        "  \"summary\": \"<one-sentence verdict>\",\n"
        "  \"details\": \"<your reasoning: what you checked, what you saw, why this verdict>\",\n"
        "  \"review\": {\n"
        "    \"verdict\": \"<same as top-level>\",\n"
        "    \"summary\": \"<same as top-level summary>\",\n"
        "    \"critical_issues\": [\"<specific blocking problem with why>\"],\n"
        "    \"suggestions\":    [\"<specific non-blocking improvement>\"],\n"
        "    \"strengths\":      [\"<what was done well>\"]\n"
        "  }\n"
        "}\n"
        "```\n\n"
        "**Verdicts:**\n"
        "- `approve` — the plan was implemented, the acceptance criteria are met.\n"
        "- `request_changes` — implementation is partial or doesn't match the plan; specify what's missing.\n"
        "- `block` — fundamentally broken or missing required behaviour.\n\n"
        "If you can't reach a verdict (no repo access, files missing, plan unreadable): "
        "call `task_report` with `status: \"blocked\"` and what you need.\n"
        "On unrecoverable error: `status: \"failed\"`.\n\n"
        "Use the native `task_report` tool — do NOT shell out to a "
        "`task-report` binary. Full role instructions are in your AGENTS.md.\n"
    )

    return "\n\n".join([header, reviewed_block, steps_block, report_block])


async def build_dispatch_message(task: dict) -> str:
    """Assemble full context for the agent."""
    pool = get_pool()

    # Review tasks (RA) get a leaner, role-specific prompt. They review work,
    # they don't do work — so the "Cloning repo / Editing file / Running
    # pytest" progress examples and the full sibling-result dump aren't
    # appropriate. The dedicated builder also branches on coding vs
    # non-coding based on whether the reviewed task has a stored
    # plan_content (= a planner ran and produced a PLAN.md).
    if task.get("review_target_id"):
        return await _build_review_dispatch_message(task, pool)

    # 1. Parent task goal
    parent_section = ""
    parent_id = task.get("parent_task_id")
    if parent_id:
        parent = await pool.fetchrow(
            "SELECT title, description FROM tasks WHERE id = $1", parent_id,
        )
        if parent:
            parent_section = f"## Overall Goal\n**{parent['title']}**\n{parent['description']}\n"

    # 2. Completed sibling subtasks
    sibling_sections = []
    if parent_id:
        siblings = await pool.fetch(
            """SELECT id, title, assigned_to, template_id, state, result
               FROM tasks WHERE parent_task_id = $1 AND id != $2 AND status = 'active'
               ORDER BY created_at""",
            parent_id, task["id"],
        )
        for sib in [dict(s) for s in siblings]:
            agent = sib.get("template_id") or sib.get("assigned_to") or "?"
            if sib["state"] in ("done", "approved") and sib.get("result"):
                sibling_sections.append(f"### {sib['title']} ({agent}) — COMPLETED\n{sib['result']}")
            elif sib["state"] == "in_progress":
                sibling_sections.append(f"### {sib['title']} ({agent}) — IN PROGRESS")
            elif sib["state"] == "cancelled":
                sibling_sections.append(f"### {sib['title']} ({agent}) — CANCELLED")
            else:
                sibling_sections.append(f"### {sib['title']} ({agent}) — {sib['state'].upper()}")

    # 3. Direct dependency results
    dep_sections = []
    for dep_id in (task.get("depends_on") or []):
        dep = await pool.fetchrow(
            "SELECT title, assigned_to, template_id, result FROM tasks WHERE id = $1", dep_id,
        )
        if dep and dep["result"]:
            agent = dep.get("template_id") or dep.get("assigned_to") or "?"
            dep_sections.append(f"### {dep['title']} ({agent})\n{dep['result']}")

    # 4. Agent roster (for planner tasks only)
    template_id = task.get("template_id") or task.get("assigned_to", "")
    goal_id = task["goal_id"]
    agent_roster = ""
    if "planner" in template_id.lower():
        agents_in_goal = await pool.fetch(
            """SELECT a.id, a.name, a.type, a.model, a.notes
               FROM agent_contexts ac JOIN agents a ON a.id = ac.agent_id
               WHERE ac.goal_id = $1 AND a.status = 'active' AND a.type IN ('IA', 'RA')""",
            goal_id,
        )
        lines = [
            "## Available Agent Templates",
            "Use these template_id values when defining subtasks.",
            "Use `human` for subtasks requiring human action.",
            "",
        ]
        for a in [dict(x) for x in agents_in_goal]:
            lines.append(f"- **{a['name']}** (`{a['id']}`) — {a['type']}, model: {a['model']}")
        agent_roster = "\n".join(lines)

    # 5. Resources — task-scoped so the spec reflects exactly what the IA
    # will see in its RESOURCES.md (story-inherited + task-level grants,
    # minus task-level exclusions; see app/resource_provisioning.py).
    resources_md = ""
    try:
        resources_md = await generate_resources_md(
            template_id, goal_id, task_id=task["id"],
        )
    except Exception:
        pass

    # Build message — CRITICAL: reporting instruction goes FIRST
    parts = []

    parts.append(f"""## CRITICAL — READ THIS FIRST

You MUST call `task-report` via exec when you finish. Without this, the task pipeline stops.

**When done:**
```bash
task-report "{task['id']}" '{{"status":"completed","summary":"<what you did>","branch":"<branch name>","files_changed":["<files>"],"details":"<detailed output>"}}'
```

**If you need human input (single question):**
```bash
task-report "{task['id']}" '{{"status":"blocked","summary":"<brief question>","details":"<full context>"}}'
```

**If you need human input (multiple requests):**
```bash
task-report "{task['id']}" '{{"status":"blocked","summary":"Need multiple things","requests":[{{"type":"question","message":"<question>"}},{{"type":"access","message":"<what access you need>"}},{{"type":"approval","message":"<what needs approval>"}},{{"type":"action","message":"<what the human needs to do>"}}]}}'
```
Request types: question, access, approval, action.

**On error:**
```bash
task-report "{task['id']}" '{{"status":"failed","summary":"<error>","details":"<details>"}}'
```

Do NOT finish without calling task-report. This is not optional.

---

## Progress Updates (REQUIRED)

Send progress updates to the project manager for EVERY significant action.
Use `message-assistant` — it does NOT block your task.

```bash
message-assistant "{task['id']}" "progress" "Cloning repo and reading plan"
message-assistant "{task['id']}" "progress" "Read spec — 4 files to modify"
message-assistant "{task['id']}" "progress" "Editing calculator.py — adding num3 parameter"
message-assistant "{task['id']}" "progress" "Committed: feat: add three-number support"
message-assistant "{task['id']}" "progress" "Running pytest tests/ -v"
message-assistant "{task['id']}" "progress" "All 48 tests passing"
message-assistant "{task['id']}" "fyi" "Found a pre-existing bug in utils.py — not in scope"
```

Send an update BEFORE and AFTER every major step (clone, edit, commit, test).
This is how the team tracks your work in real-time.
""")

    if parent_section:
        parts.append(parent_section)

    # Plan location + prior work — resolved via the shared helper so
    # control-panel plan fetches (routers/tasks.py) and Slack refetches
    # (slack/actions.py) produce the same paths.
    from app.plan_paths import resolve_plan_paths, list_prior_chains_on_branch, render_prior_chains_section
    paths = await resolve_plan_paths(task["id"])
    plan_dir     = paths["plan_dir"]
    plan_md      = paths["plan_md"]
    progress_md  = paths["progress_md"]
    review_md    = paths["review_md"]
    is_story_scoped = bool(paths["story_id"])

    if is_story_scoped:
        parts.append(f"""## Plan location

Your plan and progress for this task chain live under **`{plan_dir}/`** on the working branch:

- `{plan_md}` — implementation plan (planner writes, others read)
- `{progress_md}` — append-only log; every agent adds a timestamped entry
- `{review_md}` — reviewer's findings (written by code-review-ia)

This folder is **specific to your task chain**. Other task chains — whether in your story or a sibling story on the same branch — have their own `.plan/story-<sid>/task-<root-tid>/` folders. See "Prior work on this branch" below.
""")
    else:
        parts.append(f"""## Plan location

Your plan and progress for this task chain live under **`{plan_dir}/`** on the working branch:

- `{plan_md}` — implementation plan (planner writes, others read)
- `{progress_md}` — append-only progress log
- `{review_md}` — reviewer's findings

This is a non-story task chain; the path is namespaced by the root task id rather than a story id.
""")

    # Prior work — every other root task on the same branch (any story).
    prior = await list_prior_chains_on_branch(goal_id, task["id"])
    prior_section = render_prior_chains_section(
        prior, plan_dir, task.get("title", "<untitled>"),
    )
    if prior_section:
        parts.append(prior_section)

    parts.append(f"""## Your Task: {task['title']}

### Description
{task.get('description', '')}

### Acceptance Criteria
{task.get('acceptance_criteria', 'Not specified — use your best judgment.')}
""")

    # If this is a re-dispatch with human feedback (plan change request)
    feedback = task.get("blocked_reason", "")
    if feedback and feedback.startswith("HUMAN FEEDBACK:"):
        human_feedback = feedback.replace("HUMAN FEEDBACK: ", "")
        previous_plan = task.get("plan_content", "")

        # Fetch what agents already completed (from cancelled/done sibling tasks)
        completed_siblings = await pool.fetch("""
            SELECT title, template_id, state, result
              FROM tasks
             WHERE parent_task_id = $1
               AND state IN ('done', 'approved', 'in_progress')
               AND status = 'active'
             ORDER BY created_at
        """, task["id"])
        completed_summary = ""
        if completed_siblings:
            lines = []
            for sib in completed_siblings:
                status_icon = "✅" if sib["state"] in ("done", "approved") else "🔄"
                result_preview = (sib["result"] or "")[:200].replace("\n", " ")
                lines.append(
                    f"- {status_icon} **{sib['title']}** (`{sib['template_id']}`) — "
                    f"{sib['state']}: {result_preview}"
                )
            completed_summary = "\n".join(lines)

        revision_parts = [f"""## ⚠️ REVISION REQUESTED

The human reviewed the current plan mid-execution and requested changes:

> {human_feedback}

**Important:** Some agents may have already completed work on the branch. Their code
is committed and pushed. Check `{plan_dir}/PROGRESS.md` in the repo for what was actually
done. Your revised plan should build on completed work, not redo it.
"""]

        if completed_summary:
            revision_parts.append(f"""### Work already completed (do NOT redo this)

{completed_summary}

Read `{plan_dir}/PROGRESS.md` on the branch for full details of what each agent did.
""")

        if previous_plan:
            revision_parts.append(f"""### Your Previous `{plan_dir}/PLAN.md` (for reference)

```markdown
{previous_plan[:16000]}
```

Revise this plan based on the feedback above. Update `{plan_dir}/PLAN.md` on the branch and
report with the revised subtask plan. Keep completed work — only change what's needed.
""")
        else:
            revision_parts.append(f"""
Your previous `{plan_dir}/PLAN.md` is on the branch. Clone the repo, read it, read
`{plan_dir}/PROGRESS.md` for what's already done, revise based on the feedback, push
the updated `{plan_dir}/PLAN.md`, and report with the revised subtask plan.
""")
        parts.append("\n".join(revision_parts))
        _log.info(
            "REPLAN dispatch: task=%s feedback_len=%d prev_plan_len=%d completed_siblings=%d",
            task["id"], len(human_feedback), len(previous_plan), len(completed_siblings),
        )

    if dep_sections:
        parts.append("## What Has Been Done (your dependencies)\n\n" + "\n\n".join(dep_sections))
    if sibling_sections:
        parts.append("## Other Subtasks\n\n" + "\n\n".join(sibling_sections))
    if agent_roster:
        parts.append(agent_roster)
    if resources_md:
        parts.append(f"## Resources\n\n{resources_md}")

    # Planner-specific: output format for subtasks
    if "planner" in template_id.lower():
        parts.append(f"""## IMPORTANT: Report Format

After creating `{plan_md}` and pushing to the branch, call task-report with:
- **summary**: brief description of what you planned
- **branch**: the working branch from RESOURCES.md
- **files_changed**: must include `"{plan_md}"`
- **details**: the subtask JSON block (NOT the plan content — we fetch that from the repo)

IMPORTANT: You MUST include "repo" in your report JSON payload. Example:
```bash
task-report "TASK_ID" '{{"status":"completed","summary":"Created plan for X","repo":"multi_agent_dummy_repo","branch":"<working branch>","files_changed":["{plan_md}"],"details":"```json:subtasks\\n[...]\\n```"}}'
```

The "repo" field tells the system which repository to fetch the plan from.
The human will see your `{plan_md}` in an approval card. They approve before coding starts.

### Plan file conventions

Your plan lives at **`{plan_dir}/`** in the repo (on the working branch):
- `{plan_md}` — the implementation plan you write
- `{progress_md}` — dev and test agents append timestamped entries here (you don't write this; see the PROGRESS.md convention in their AGENTS.md)
- `{review_md}` — code-review-ia writes the final review here
- If the repo root already has a `CLAUDE.md` — that is the repo's project instructions. READ it for coding conventions and project context, but do NOT overwrite it. Your plan goes in `{plan_md}`.

Example subtask format (copy the path verbatim into each subtask description):

```json:subtasks
[
  {{
    "title": "Implement backend logic",
    "template_id": "backend-dev-ia",
    "description": "Implement the feature per {plan_md} on <working branch>",
    "acceptance_criteria": "All functions working, no lint errors",
    "depends_on": [],
    "priority": "high"
  }},
  {{
    "title": "Write unit tests",
    "template_id": "test-ia",
    "description": "Write tests per {plan_md} on <working branch>",
    "acceptance_criteria": "All tests passing, >80% coverage",
    "depends_on": [0],
    "priority": "medium"
  }}
]
```

Rules:
- depends_on uses 0-based indices referring to other subtasks in THIS list
- template_id must be one of the available agent templates listed above
- Do NOT add a final human review step. A Reviewer Agent runs automatically after
  each task completes. Plans should end with the last substantive work task.

CRITICAL — Branch & Dependency Rules:
- Each agent gets its OWN isolated workspace (separate git clone)
- If task B reads or modifies code that task A writes, B MUST depend on A
- Testing tasks MUST depend on ALL coding tasks they test
- Code review MUST depend on ALL coding + testing tasks
- Only tasks on DIFFERENT repos can run in parallel
- Tasks on the SAME branch are ALWAYS sequential: code → test → review
- NEVER let a tester and coder run in parallel on the same branch
""")

    parts.append(f"""## REMINDER
Your task ID is: `{task['id']}`
- **When done/failed/blocked:** call `task-report` (MANDATORY)
- **Progress updates:** call `message-assistant` (REQUIRED for every major step)
{"- **Planner:** Write `.plan/PLAN.md` (not CLAUDE.md), include it in files_changed, report via task-report" if "planner" in template_id.lower() else ""}
""")

    return "\n\n".join(parts)


# ── Phase Message Builder (Persistent Worker) ────────────────────────────────

async def _build_phase_message(subtask: dict, parent_task_id: str) -> str:
    """Build a phase-specific prompt for a persistent worker.

    Unlike build_dispatch_message() which includes full context for a fresh
    agent, this is concise — the agent already has context from previous
    phases via --resume.
    """
    pool = get_pool()

    # Count phases for progress indicator
    all_subtasks = await pool.fetch(
        """SELECT id, title, template_id, state
           FROM tasks WHERE parent_task_id = $1 AND status = 'active'
           ORDER BY created_at ASC""",
        parent_task_id,
    )
    total_phases = len(all_subtasks)
    phase_number = 0
    for i, st in enumerate(all_subtasks):
        if st["id"] == subtask["id"]:
            phase_number = i + 1
            break

    # Completed phases summary (brief — agent has full context via --resume)
    completed = [
        f"- {st['title']} ({st['template_id'] or '?'}) — DONE"
        for st in all_subtasks
        if st["state"] in ("done", "approved") and st["id"] != subtask["id"]
    ]
    completed_section = ""
    if completed:
        completed_section = f"""## Completed Phases
{chr(10).join(completed)}
"""

    template_id = subtask.get("template_id") or subtask.get("assigned_to", "")

    # Map template_id to a role name
    role_map = {
        "planner": "PLANNER",
        "backend": "BACKEND DEVELOPER",
        "frontend": "FRONTEND DEVELOPER",
        "test": "TESTER",
        "review": "CODE REVIEWER",
    }
    role = "DEVELOPER"
    for key, val in role_map.items():
        if key in template_id.lower():
            role = val
            break

    return f"""=== PHASE {phase_number} of {total_phases} ===
ROLE: {role}

Your workspace files (AGENTS.md, SOUL.md, RESOURCES.md) have been updated for this phase.
Read AGENTS.md for your role-specific instructions. Read RESOURCES.md for current credentials.

## Workspace Check (do this FIRST)
1. Find the repo: `find . -name .git -type d -maxdepth 3 2>/dev/null`
2. `cd` into the repo directory
3. Run `git status` and `git log --oneline -5` to see current state
4. Run `git pull` to ensure you have the latest changes from previous phases
5. Verify you are on the correct feature branch

{completed_section}## Your Task: {subtask['title']}

{subtask.get('description', '')}

### Acceptance Criteria
{subtask.get('acceptance_criteria', 'Not specified — use your best judgment.')}

### Rules
- You have full context from previous phases in your session history
- Work in the existing workspace — do NOT re-clone
- Build on top of what was done in previous phases
- Send progress updates via message-assistant for every major step
- When done, call task-report with status "completed"
- If blocked, call message-assistant then task-report with status "blocked"

Your task ID is: `{subtask['id']}`
"""


# ── Planner Output Parser ────────────────────────────────────────────────────

def parse_planner_subtasks(output: str) -> Optional[list[dict]]:
    """Extract the subtask JSON from planner output."""
    pattern = r'```json:subtasks\s*\n(.*?)```'
    match = re.search(pattern, output, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            _log.warning("Failed to parse planner subtasks JSON: %s", exc)
            return None

    pattern = r'```json\s*\n(\[.*?\])```'
    match = re.search(pattern, output, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, list) and data and "template_id" in data[0]:
                return data
        except json.JSONDecodeError:
            pass

    return None


async def create_subtasks_from_planner(parent_task: dict, planner_output: str) -> list[str]:
    """Parse planner output and create subtasks in the database.

    Emits spec-compliant events:
    - TASK_PROGRESS_UPDATED on parent (planning complete)
    - TASK_CREATED for each subtask (individual, with parent_task_id in payload)
    - TASK_DEPENDENCY_ADDED for each dependency
    """
    subtasks = parse_planner_subtasks(planner_output)
    if not subtasks:
        _log.info("No subtask JSON found in planner output for task %s", parent_task["id"])
        return []

    pool = get_pool()
    parent_id = parent_task["id"]
    goal_id = parent_task["goal_id"]
    source_thread_id = parent_task.get("source_thread_id", "")
    corr_id = await get_task_correlation_id(pool, parent_id)

    # Emit TASK_PROGRESS_UPDATED on parent (planning phase complete)
    parent_last_evt = await get_last_event_id(pool, parent_id)
    progress_evt = await emit_task_event(
        pool,
        event_type=TASK_PROGRESS_UPDATED,
        task_id=parent_id, goal_id=goal_id,
        payload=task_progress_updated_payload(
            summary=f"Planning complete — {len(subtasks)} subtasks defined",
        ),
        actor_id=await instance_actor_id(
            task_id=parent_id,
            gateway_agent_id=parent_task.get("instance_id", ""),
        ),
        actor_type="independent_agent",
        correlation_id=corr_id,
        causation_ids=[parent_last_evt] if parent_last_evt else None,
        summary=f"Planning complete — {len(subtasks)} subtasks defined",
        instance_id=parent_task.get("instance_id", ""),
        agent_id=parent_task.get("template_id", ""),
    )
    progress_evt_id = progress_evt["event_id"] if progress_evt else None

    # Find the highest existing subtask index under this parent so re-plan
    # subtasks get incremental IDs (e.g. if task-3 was cancelled, new ones
    # start from task-4).
    max_existing = await pool.fetchval("""
        SELECT max(
            CASE WHEN id LIKE $1 || '-%'
                 THEN cast(split_part(id, '-', array_length(string_to_array(id, '-'), 1)) AS int)
                 ELSE 0
            END
        ) FROM tasks WHERE parent_task_id = $1 AND review_target_id IS NULL
    """, parent_id) or 0
    _log.info(
        "create_subtasks_from_planner: parent=%s existing_max_index=%d new_count=%d",
        parent_id, max_existing, len(subtasks),
    )

    # Create subtasks in DB + emit events
    subtask_ids = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for i, st in enumerate(subtasks):
                st_id = f"{parent_id}-{max_existing + i + 1}"
                st_thread_id = str(uuid.uuid4())
                st_template = st.get("template_id", "")
                st_title = st.get("title", f"Subtask {i+1}")

                # Resolve depends_on indices → task IDs
                raw_deps = st.get("depends_on", [])
                resolved_deps = []
                for dep in raw_deps:
                    if isinstance(dep, int) and 0 <= dep < len(subtask_ids):
                        resolved_deps.append(subtask_ids[dep])
                    elif isinstance(dep, str):
                        resolved_deps.append(dep)

                await conn.execute(
                    """INSERT INTO threads (id, goal_id, agent_id, task_id, created_by, title)
                       VALUES ($1, $2, $3, $4, 'system', $5)""",
                    st_thread_id, goal_id, st_template, st_id, st_title,
                )

                await conn.execute(
                    """INSERT INTO tasks
                           (id, goal_id, title, description, assigned_to, template_id,
                            priority, depends_on, state, created_by, thread_id,
                            correlation_id, parent_task_id, acceptance_criteria,
                            source_thread_id, decision_event_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending','system',$9,$10,$11,$12,$13,$14)""",
                    st_id, goal_id, st_title, st.get("description", ""),
                    st_template, st_template,
                    st.get("priority", "medium"), resolved_deps,
                    st_thread_id, corr_id, parent_id,
                    st.get("acceptance_criteria", ""), source_thread_id,
                    corr_id,  # decision_event_id = same correlation
                )

                # The planner (the parent task's agent instance) is the creator
                # of these subtasks; system fallback if it can't be resolved.
                planner_actor, planner_atype = await instance_actor(task_id=parent_id)
                # Emit TASK_CREATED for this subtask
                assignee_ref = None
                if st_template:
                    st_actor_id = await pool.fetchval(
                        "SELECT actor_id FROM agents WHERE id = $1", st_template,
                    ) or _actor_uuid(agent_id=st_template)
                    assignee_ref = actor_ref(
                        st_actor_id,
                        "independent_agent" if st_template != "human" else "human",
                        st_template,
                    )

                created_evt = await _emit_inner(
                    conn,
                    event_type=TASK_CREATED,
                    task_id=st_id, goal_id=goal_id,
                    payload=task_created_payload(
                        title=st_title,
                        description=st.get("description", ""),
                        acceptance_criteria=st.get("acceptance_criteria", ""),
                        assignee=assignee_ref,
                        priority=st.get("priority", "medium"),
                        parent_task_id=parent_id,
                        dependencies=resolved_deps,
                    ),
                    actor_id=planner_actor, actor_type=planner_atype,
                    correlation_id=corr_id,
                    causation_ids=[progress_evt_id] if progress_evt_id else None,
                    context_conversation_id=st_thread_id,
                    summary=f"Subtask created: {st_title}",
                    content="", agent_id=st_template, instance_id="",
                )
                created_evt_id = created_evt["event_id"] if created_evt else None

                # Emit TASK_DEPENDENCY_ADDED for each dependency
                for dep_id in resolved_deps:
                    await _emit_inner(
                        conn,
                        event_type=TASK_DEPENDENCY_ADDED,
                        task_id=st_id, goal_id=goal_id,
                        payload=task_dependency_added_payload(
                            depends_on=dep_id,
                        ),
                        actor_id=planner_actor, actor_type=planner_atype,
                        correlation_id=corr_id,
                        causation_ids=[created_evt_id] if created_evt_id else None,
                        context_conversation_id=st_thread_id,
                        summary=f"Dependency: {st_id} blocked on {dep_id}",
                        content="", agent_id="", instance_id="",
                    )

                subtask_ids.append(st_id)

                # Notify platform channels (Notion, Slack, etc.) of new task
                try:
                    import asyncio as _asyncio
                    from app.channels.base import relay_lifecycle_event as _rle
                    _asyncio.create_task(_rle(goal_id, "task.created", {
                        "task_id": st_id,
                        "task_title": st_title,
                        "story_id": str(task.get("story_id") or ""),
                        "state": "pending",
                    }))
                except Exception as _exc:
                    _log.debug("relay task.created failed task=%s: %s", st_id, _exc)

    _log.info("Created %d subtasks from planner output for task %s: %s",
              len(subtask_ids), parent_id, subtask_ids)

    # Inherit resources from parent to each subtask
    from app.routers.tasks import _inherit_task_resources
    for st_id in subtask_ids:
        st_row = await pool.fetchrow("SELECT template_id FROM tasks WHERE id = $1", st_id)
        st_template = st_row["template_id"] if st_row else ""
        await _inherit_task_resources(
            pool, parent_id, st_id, goal_id, st_template, SYSTEM_ACTOR_ID, corr_id,
        )

    # Trigger dependency resolution for newly created subtasks
    await task_controller.trigger()

    return subtask_ids


# ── Re-planning Support ──────────────────────────────────────────────────────

async def cancel_subtask(
    task_id: str,
    reason: str,
    cancelled_by_actor: uuid.UUID = SYSTEM_ACTOR_ID,
    cancelled_by_type: str = "system",
    cancelled_by_name: str = "system",
    cascade: bool = True,
) -> list[str]:
    """Cancel a subtask. If cascade=True, also cancel dependents.

    Emits TASK_CANCELLED + IAM_AGENT_DECOMMISSIONED per cancelled task.
    Returns list of all cancelled task IDs.
    """
    pool = get_pool()
    task = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if not task:
        return []
    task = dict(task)
    if task["state"] in ("done", "approved", "cancelled"):
        return []

    goal_id = task["goal_id"]
    corr_id = await get_task_correlation_id(pool, task_id)
    last_evt = await get_last_event_id(pool, task_id)

    # Cancel this task
    await pool.execute(
        "UPDATE tasks SET state = 'cancelled' WHERE id = $1", task_id,
    )
    # No-op if no credentials were provisioned; cheap to call uniformly.
    from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
    await release_credentials_safe(task_id, "task_controller:cancel_task")

    dependents = await find_dependent_tasks(pool, goal_id, task_id) if cascade else []

    await emit_task_event(
        pool,
        event_type=TASK_CANCELLED,
        task_id=task_id, goal_id=goal_id,
        payload=task_cancelled_payload(
            reason=reason,
            cancelled_by=actor_ref(cancelled_by_actor, cancelled_by_type, cancelled_by_name),
            cascade_impact=dependents,
        ),
        actor_id=cancelled_by_actor, actor_type=cancelled_by_type,
        correlation_id=corr_id,
        causation_ids=[last_evt] if last_evt else None,
        summary=f"Cancelled: {reason}",
        instance_id=task.get("instance_id", ""),
        agent_id=task.get("template_id", ""),
    )

    # Decommission instance if running
    if task.get("instance_id"):
        await destroy_instance(task)

    # Cascade to dependents
    cancelled = [task_id]
    if cascade:
        for dep_id in dependents:
            sub_cancelled = await cancel_subtask(
                dep_id, f"Dependency {task_id} cancelled",
                cancelled_by_actor, cancelled_by_type, cancelled_by_name,
                cascade=True,
            )
            cancelled.extend(sub_cancelled)

    return cancelled


async def check_parent_completion(parent_task_id: str) -> bool:
    """If all children of parent are done/cancelled, auto-complete the parent.

    Aggregates artifacts from child TASK_COMPLETED events.
    Returns True if parent was completed.
    """
    pool = get_pool()
    parent = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", parent_task_id)
    if not parent:
        return False
    parent = dict(parent)

    # Don't fire if parent is already in a terminal or review-pending state.
    if parent["state"] in ("done", "approved", "automated_review", "awaiting_approval", "rejected", "cancelled", "failed"):
        return False

    # claude-code DAG parents are reviewed via the DIFF-REVIEW flow — the SAME path the
    # PHASE_TASK_DONE sentinel takes (DAG → awaiting_approval, story → awaiting_review,
    # an Approve/Reject diff-review card on the story's existing Claude session) — NOT a
    # spawned RA/IA review task. A human marking a subtask DONE on the board reaches this
    # generic completion path (the sentinel path only fires when CLAUDE emits the done
    # sentinel); without this gate the RA-review INSERTs default to executor='ia' and the
    # legacy IA dispatch loop spawns a fresh task-scoped agent — the "next task sent as an
    # IA task" bug. Delegate to the claude-code DAG finisher and leave the Claude session
    # alive for the human's Approve/Reject. (_on_dag_tasks_complete re-checks all subtasks
    # are terminal, so this is safe + idempotent.)
    if (parent.get("executor") or "") == "claude-code-dag":
        # NOTE: this function was renamed _maybe_finish_dag -> _on_dag_tasks_complete
        # (documentation/story-integration/CODE_INTEGRATION.md); the old name here raised an
        # ImportError that was swallowed by the caller's try/except, silently breaking
        # the board "mark done" -> diff-review rollup for claude-code DAGs.
        from app.services.sentinel_parser import _on_dag_tasks_complete
        await _on_dag_tasks_complete(parent["goal_id"], parent.get("story_id"), parent_task_id)
        # Not "completed" in this function's sense — the DAG goes to awaiting_approval
        # (the diff-review card), not 'done'. Return False so the caller doesn't log a
        # misleading "auto-completed parent". (_on_dag_tasks_complete no-ops if not all done.)
        return False

    # Only consider real work children — exclude review tasks (review_target_id IS NOT NULL)
    # so that a pending/in-progress RA task doesn't block parent from moving to
    # awaiting_approval, and so that review task completion doesn't re-trigger this path.
    children = await pool.fetch(
        """SELECT id, state FROM tasks
           WHERE parent_task_id = $1 AND status = 'active'
             AND review_target_id IS NULL""",
        parent_task_id,
    )
    if not children:
        return False

    # All children must be in terminal states. halted is included so a
    # force-killed subtask doesn't pin the parent in_progress forever (M13).
    terminal = {"done", "approved", "cancelled", "halted"}
    all_terminal = all(r["state"] in terminal for r in children)
    if not all_terminal:
        return False

    # Check there's at least one non-cancelled child
    non_cancelled = [r for r in children if r["state"] != "cancelled"]
    if not non_cancelled:
        return False

    # Aggregate artifacts from child TASK_COMPLETED events
    child_ids = [r["id"] for r in non_cancelled]
    artifact_rows = await pool.fetch(
        """SELECT payload FROM ledger_events
           WHERE context_task_id = ANY($1) AND event_type = $2
           ORDER BY sequence_number""",
        child_ids, TASK_COMPLETED,
    )
    all_artifacts = []
    for r in artifact_rows:
        p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        all_artifacts.extend(p.get("artifacts_produced", []))

    # All work subtasks are done — spawn an RA review task and move the
    # parent to 'automated_review' while the RA runs. If no RA is
    # configured, fall back to 'awaiting_approval' for human sign-off.
    goal_id = parent["goal_id"]
    corr_id = await get_task_correlation_id(pool, parent_task_id)
    child_completion_evts = await get_dep_completion_event_ids(pool, child_ids)

    previous_state = parent["state"]

    new_state = "automated_review"

    await pool.execute(
        "UPDATE tasks SET state = $1, result = $2 WHERE id = $3",
        new_state, f"All {len(non_cancelled)} subtasks completed — pending review", parent_task_id,
    )

    await emit_task_event(
        pool,
        event_type=TASK_STATUS_CHANGED,
        task_id=parent_task_id, goal_id=goal_id,
        payload=task_status_changed_payload(
            previous_status=previous_state, new_status=new_state,
            reason="All subtasks completed — spawning automated review",
        ),
        actor_id=SYSTEM_ACTOR_ID, actor_type="system",
        correlation_id=corr_id,
        causation_ids=child_completion_evts or None,
        summary=f"All subtasks done → {new_state}",
    )

    _log.info("Parent task %s → %s (%d children done)", parent_task_id, new_state, len(non_cancelled))

    await _spawn_review_task(pool, parent_task_id, parent, force=True)

    # Clean up ALL agent instances in the task tree now that work is complete
    await cleanup_parent_task(parent_task_id)

    # Notify GO + dispatching SHCA. on_task_state_changed handles the
    # wake-up gating (root-only) — without this call, an auto-completed
    # root sends no message to anyone, which is the symptom users hit
    # after /human-review or after the last subtask reports completion.
    try:
        from app.services.go_events import on_task_state_changed
        await on_task_state_changed(
            goal_id, parent_task_id, previous_state, new_state, "system",
        )
    except Exception as exc:
        _log.warning(
            "Parent auto-complete: GO/SHCA notification failed for %s: %s",
            parent_task_id, exc,
        )

    return True


# ── Task Controller (Event-Driven) ───────────────────────────────────────────

# Advisory lock key for task-controller leader election. Must differ from
# git_graph/engine.py's key so the two locks don't interfere.
_TC_LEADER_KEY = "task-controller-leader-v1"


class TaskController:
    """Event-driven task controller.

    Only one Cloud Run instance may dispatch tasks at a time. Leader election
    is implemented via pg_try_advisory_lock on _TC_LEADER_KEY, mirroring the
    pattern in git_graph/engine.py. The leader holds a dedicated pinned
    asyncpg connection for the lock's lifetime; a heartbeat SELECT 1 detects
    idle-reap / Cloud SQL proxy restarts and re-acquires on loss.
    """

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._event = asyncio.Event()
        self._leader_conn = None   # pinned connection that holds the advisory lock
        self._is_leader = False
        self._last_stale_freeze_warn_at: float = 0.0  # monotonic, throttles M16 warning

    def start(self):
        if not self._running:
            self._running = True
            self._event = asyncio.Event()
            self._task = asyncio.create_task(self._run_forever())
            self._task.add_done_callback(self._done_callback)
            _log.info("TaskController started (event-driven)")

    async def stop(self):
        self._running = False
        self._event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._release_leader()
        _log.info("TaskController stopped")

    # ── leader election ───────────────────────────────────────────────────────

    async def _acquire_leader(self) -> bool:
        """Become (or remain) the sole dispatching leader.

        Holds a dedicated pinned connection for the advisory lock. Returns True
        iff this instance now holds the lock. Mirrors git_graph/engine.py.
        """
        if self._is_leader and self._leader_conn is not None:
            # Heartbeat: keep the pinned connection alive so Cloud SQL / VPC
            # idle-reap doesn't silently drop the session-scoped lock.
            try:
                if not self._leader_conn.is_closed():
                    await self._leader_conn.execute("SELECT 1")
                    return True
            except Exception as exc:
                _log.warning("TaskController: leader connection lost (%s); re-acquiring", exc)
        # Lost / never held — (re)acquire.
        await self._release_leader()
        pool = get_pool()
        conn = None
        try:
            conn = await pool.acquire()
            got = bool(await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1)::bigint)", _TC_LEADER_KEY))
            if got:
                self._leader_conn = conn
                self._is_leader = True
                _log.info("TaskController: acquired leader lock")
                return True
            await pool.release(conn)
            return False
        except Exception as exc:
            _log.warning("TaskController: leader acquire failed: %s", exc)
            if conn is not None:
                try:
                    await pool.release(conn)
                except Exception:
                    pass
            return False

    async def _release_leader(self) -> None:
        conn = self._leader_conn
        self._leader_conn = None
        self._is_leader = False
        if conn is None:
            return
        try:
            if not conn.is_closed():
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext($1)::bigint)", _TC_LEADER_KEY)
        except Exception:
            pass
        try:
            await get_pool().release(conn)
        except Exception:
            pass

    async def trigger(self):
        """Wake the controller to process tasks."""
        self._event.set()

    @staticmethod
    def _done_callback(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            _log.error("TaskController crashed: %s", exc, exc_info=exc)

    async def _run_forever(self):
        await asyncio.sleep(3)
        _log.info("TaskController loop ready (event-driven, 15s idle poll)")

        # Wrap the initial tick so a first-pass exception (e.g. transient DB
        # permission issue at boot) doesn't kill the worker permanently.
        if await self._acquire_leader():
            try:
                await self._tick()
                _log.info("TaskController initial tick completed")
            except Exception as exc:
                _log.error("TaskController initial tick error (loop continuing): %s",
                           exc, exc_info=exc)
        else:
            _log.info("TaskController: not leader at boot, skipping initial tick")

        tick_count = 0
        loop = asyncio.get_event_loop()
        while self._running:
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=15)
                wake_reason = "event"
            except asyncio.TimeoutError:
                wake_reason = "poll"
            if not self._running:
                return
            # Only the leader instance dispatches tasks. Non-leaders still wake
            # on the timeout so they can re-contest the lock when the leader dies.
            if not await self._acquire_leader():
                _log.debug("TaskController: not leader, skipping tick")
                continue
            tick_count += 1
            t_start = loop.time()
            try:
                _log.debug("TaskController tick #%d start (wake=%s)", tick_count, wake_reason)
                await self._tick()
                elapsed_ms = int((loop.time() - t_start) * 1000)
                _log.debug("TaskController tick #%d done in %dms", tick_count, elapsed_ms)
            except Exception as exc:
                elapsed_ms = int((loop.time() - t_start) * 1000)
                _log.error("TaskController tick #%d error after %dms: %s",
                           tick_count, elapsed_ms, exc, exc_info=exc)

    async def _tick(self):
        """One pass: check completions + detect stale agents + resolve deps + dispatch ready."""
        pool = get_pool()

        # Snapshot of task-state distribution at tick entry — gives a quick
        # signal of whether the controller is seeing the work it should be.
        state_counts = await pool.fetch(
            """SELECT state, COUNT(*) AS n
                 FROM tasks
                WHERE status = 'active'
                GROUP BY state""",
        )
        if state_counts:
            distribution = ", ".join(f"{r['state']}={r['n']}" for r in state_counts)
            _log.info("TaskController tick: active tasks by state — %s", distribution)
        else:
            _log.debug("TaskController tick: no active tasks in DB")

        await self._check_completions(pool)
        await self._check_stale_dispatches(pool)
        await self._check_stale_dispatching_nulls(pool)
        await self._check_stale_blocked(pool)
        await self._check_stuck_after_pause(pool)
        await self._resolve_dependencies(pool)
        await self._check_stale_pauses(pool)

        # Kill switch — SYSTEM freeze short-circuit. Completion/dependency
        # housekeeping above still runs so in-flight turns drain gracefully and
        # finished work is recorded; we only skip the two DISPATCH blocks below
        # so no NEW agent work starts while the system is frozen. Goal and
        # agent-instance scopes are enforced closer to each dispatch path.
        from app.services import execution_control  # noqa: PLC0415
        _sys_block = await execution_control.is_system_paused()
        if _sys_block:
            _log.info(
                "TaskController tick: dispatch suppressed — SYSTEM execution "
                "paused (reason=%s). Housekeeping ran; no new dispatch.",
                _sys_block.reason or "<none>",
            )
            return

        # claude-code-phase dispatch (Phase 4 Stage 3-g). One per story
        # at a time because the underlying claude-cli session is
        # single-threaded.
        try:
            from app.services.phase_dispatcher import (
                find_next_dispatchable_task,
                dispatch_phase_task,
            )
            # DAG concurrency ceiling (TASK_DAG_CEILING_PLAN.md). ENFORCE: the
            # scheduler selects+claims the next phase under an admission lock and
            # returns a task already 'dispatching' (or None to hold work). SHADOW
            # (default): legacy selection, log-only, dispatch unchanged.
            import asyncio as _asyncio  # noqa: PLC0415
            try:
                from app.services.dag_scheduler import (  # noqa: PLC0415
                    admit_and_claim_next_phase, get_ceiling_config, shadow_review,
                )
                _dag_n, _dag_enforce = await get_ceiling_config()
            except Exception:
                _dag_enforce = False
                _log.debug("dag_scheduler config read failed (non-fatal)", exc_info=True)

            if _dag_enforce:
                try:
                    claimed = await admit_and_claim_next_phase()
                except Exception:
                    claimed = None
                    _log.exception("dag_scheduler admit failed (non-fatal)")
                if claimed:
                    _log.info(
                        "TaskController: dispatching claude-code-phase task %s "
                        "(phase=%s story=%s) [dag-ceiling ENFORCE]",
                        claimed["id"], claimed.get("phase"), claimed.get("story_id"),
                    )
                    _asyncio.create_task(dispatch_phase_task(claimed, already_claimed=True))
            else:
                next_phase_task = await find_next_dispatchable_task()
                try:
                    await shadow_review(next_phase_task)
                except Exception:
                    _log.debug("dag_scheduler shadow_review failed (non-fatal)", exc_info=True)
                if next_phase_task:
                    _log.info(
                        "TaskController: dispatching claude-code-phase task %s "
                        "(phase=%s story=%s)",
                        next_phase_task["id"], next_phase_task.get("phase"),
                        next_phase_task.get("story_id"),
                    )
                    _asyncio.create_task(dispatch_phase_task(next_phase_task))
        except Exception:
            _log.exception("phase dispatch loop crashed (non-fatal)")

        # system-gate dispatch — one per story at a time (same single-flight rule as
        # phase tasks; the busy_stories CTE in both selectors covers cross-executor
        # conflicts). Gates are driven entirely by the platform — no Claude session.
        try:
            from app.services.merge_orchestrator.dispatch import (  # noqa: PLC0415
                find_next_dispatchable_gate,
                dispatch_system_gate,
            )
            next_gate_task = await find_next_dispatchable_gate()
            if next_gate_task:
                _log.info(
                    "TaskController: dispatching system-gate %s "
                    "(gate_kind=%s story=%s)",
                    next_gate_task["id"], next_gate_task.get("gate_kind"),
                    next_gate_task.get("story_id"),
                )
                import asyncio as _asyncio  # noqa: PLC0415
                _asyncio.create_task(dispatch_system_gate(next_gate_task))
        except Exception:
            _log.exception("system-gate dispatch loop crashed (non-fatal)")

        # Scope dispatch to IA-executor tasks only (post-migration 039).
        # Tasks owned by SHCA, Claude Code, or pending human action are NOT
        # dispatched by this controller — SHCA's operator loop handles those.
        # Default executor='ia' on existing rows preserves prior behavior.
        # Kill switch — GOAL freeze gate: skip ready tasks whose goal is paused.
        # SYSTEM scope is already handled by the short-circuit above;
        # agent-instance scope is enforced in _dispatch_task once the worker is
        # resolved. The anti-join keeps paused-goal tasks out of the LIMIT 5
        # window so they can't starve runnable tasks in other goals.
        ready_tasks = await pool.fetch(
            """SELECT t.* FROM tasks t
               WHERE t.state = 'ready' AND t.status = 'active'
                 AND (t.executor = 'ia' OR t.executor LIKE 'ia:%')
                 -- Deleted-goal guard: goal delete is soft (goals.status='deleted')
                 -- and does NOT cascade to tasks, so without this a dead goal's
                 -- ready task would provision a fresh IA agent. Skip them.
                 AND EXISTS (
                     SELECT 1 FROM goals g
                      WHERE g.id = t.goal_id AND g.status = 'active'
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM execution_pauses ep
                      WHERE ep.scope = 'goal' AND ep.scope_id = t.goal_id
                 )
               ORDER BY t.priority DESC, t.created_at ASC
               LIMIT 5""",
        )
        if ready_tasks:
            _log.info("TaskController tick: %d ready task(s) to dispatch — ids=%s",
                      len(ready_tasks),
                      ",".join(t["id"] for t in ready_tasks))
        for row in ready_tasks:
            task_dict = dict(row)
            try:
                await self._dispatch_task(pool, task_dict)
            except Exception as exc:
                _log.error("Dispatch failed for task %s (template=%s, goal=%s): %s",
                           task_dict.get("id"),
                           task_dict.get("template_id") or task_dict.get("assigned_to"),
                           task_dict.get("goal_id"),
                           exc, exc_info=exc)

    async def _check_completions(self, pool):
        """Poll in_progress tasks via gateway session preview.

        Scoped to IA-executor tasks only. SHCA-owned / Claude-Code tasks
        report completion through SHCA's own event-handling loop, not via
        the gateway session preview path used here.
        """
        in_progress = await pool.fetch(
            """SELECT * FROM tasks
               WHERE state = 'in_progress' AND status = 'active'
               AND instance_id IS NOT NULL AND instance_id != ''
               AND (executor = 'ia' OR executor LIKE 'ia:%')
               LIMIT 10""",
        )
        for row in in_progress:
            task = dict(row)
            instance_id = task["instance_id"]
            task_id = task["id"]
            session_key = make_task_session_key(instance_id, task_id)

            try:
                preview = await gateway_admin.call("sessions.preview", {
                    "keys": [session_key],
                    "limit": 5,
                    "maxChars": 8000,
                })

                previews = preview.get("previews", [])
                if not previews:
                    continue

                session_data = previews[0] if previews else {}
                messages = session_data.get("messages", [])
                if not messages:
                    continue

                last_assistant = None
                last_role = ""
                for msg in reversed(messages):
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role", "")
                    if not last_role:
                        last_role = role
                    if role == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            text_parts = [p.get("text", "") for p in content
                                          if isinstance(p, dict) and p.get("type") == "text"]
                            content = "\n".join(text_parts)
                        elif not isinstance(content, str):
                            content = str(content)
                        if content.strip():
                            last_assistant = content
                        break

                if not last_assistant:
                    continue

                if last_role == "assistant":
                    _log.info("Task %s completed (session preview, %d chars)", task_id, len(last_assistant))
                    await handle_task_completion(task_id, last_assistant)

            except Exception as exc:
                _log.info("Session preview for task %s failed: %s", task_id, exc)

    async def _check_stuck_after_pause(self, pool):
        """Kill switch — surface agents that won't stop after a pause.

        Graceful pause lets an in-flight turn finish, but a corrupted/looping
        agent may never finish. For any task still running past a per-executor
        grace window whose system, goal, OR own agent-instance is paused, emit a
        one-shot `agent.stuck_after_pause` ledger event. The admin control panel
        reads these to show a "force-kill?" affordance. Fires once per task
        (deduped against the ledger) so we don't spam.

        All three pause scopes are covered: an operator who graceful-stops a
        single instance must still be told if that instance's turn hangs forever
        — that narrow case is exactly when the stuck affordance matters most.
        """
        from app.config import get_settings  # noqa: PLC0415
        settings = get_settings()
        phase_min = settings.stuck_after_pause_phase_minutes
        ia_min = settings.stuck_after_pause_ia_minutes

        try:
            stuck = await pool.fetch(
                """
                SELECT t.id, t.goal_id, t.instance_id, t.executor,
                       EXTRACT(EPOCH FROM (now() - t.dispatched_at)) / 60.0 AS age_min
                  FROM tasks t
                 WHERE t.status = 'active'
                   AND t.state IN ('in_progress', 'dispatching')
                   AND t.dispatched_at IS NOT NULL
                   AND (
                        (t.executor = 'claude-code-phase'
                         AND t.dispatched_at < now() - make_interval(mins => $1))
                     OR (t.executor IS DISTINCT FROM 'claude-code-phase'
                         AND t.dispatched_at < now() - make_interval(mins => $2))
                   )
                   AND (
                        EXISTS (SELECT 1 FROM execution_pauses ep WHERE ep.scope = 'system')
                     OR EXISTS (SELECT 1 FROM execution_pauses ep
                                 WHERE ep.scope = 'goal' AND ep.scope_id = t.goal_id)
                     OR EXISTS (SELECT 1 FROM execution_pauses ep
                                 WHERE ep.scope = 'agent_instance'
                                   AND ep.scope_id = t.instance_id)
                        -- M18: also surface phase tasks stuck under a story-level pause
                        -- (stories.execution_paused is independent of execution_pauses).
                     OR EXISTS (SELECT 1 FROM stories s
                                 WHERE s.id = t.story_id AND s.execution_paused = TRUE)
                   )
                   AND NOT EXISTS (
                        -- M11: dedup is scoped to the CURRENT pause window.
                        -- If the operator resumed and re-paused, the existing
                        -- alert was from a prior window (before the current
                        -- paused_at) and should not suppress a new one.
                        -- For tasks only paused via stories.execution_paused
                        -- (no execution_pauses row), COALESCE falls back to
                        -- epoch so any prior alert still dedupes (one-shot,
                        -- since we have no paused_at timestamp to correlate).
                        SELECT 1 FROM ledger_events le
                         WHERE le.event_type = 'agent.stuck_after_pause'
                           AND le.context_task_id = t.id
                           AND le.timestamp >= COALESCE(
                               (SELECT MIN(ep2.paused_at)
                                  FROM execution_pauses ep2
                                 WHERE ep2.scope = 'system'
                                    OR (ep2.scope = 'goal'
                                        AND ep2.scope_id = t.goal_id)
                                    OR (ep2.scope = 'agent_instance'
                                        AND ep2.scope_id = t.instance_id)),
                               '1970-01-01 00:00:00+00'::timestamptz
                           )
                   )
                 LIMIT 10
                """,
                phase_min, ia_min,
            )
        except Exception as exc:
            _log.warning("stuck-after-pause check query failed (non-fatal): %s", exc)
            return

        for row in stuck:
            task = dict(row)
            age = round(float(task.get("age_min") or 0), 1)
            _log.warning(
                "Agent STUCK after pause: task=%s goal=%s executor=%s instance=%s "
                "still running %.1f min after pause — surfacing force-kill affordance",
                task["id"], task["goal_id"], task.get("executor"),
                task.get("instance_id") or "-", age,
            )
            try:
                await emit_task_event(
                    pool,
                    event_type="agent.stuck_after_pause",
                    task_id=task["id"], goal_id=task["goal_id"],
                    payload={
                        "executor": task.get("executor") or "ia",
                        "instance_id": task.get("instance_id") or "",
                        "minutes_in_flight": age,
                    },
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                    summary=f"Agent stuck {age} min after pause — force-kill recommended",
                    instance_id=task.get("instance_id") or "",
                )
            except Exception as exc:
                _log.warning("stuck-after-pause emit failed for task %s: %s", task["id"], exc)

            # Notify all goal admins so they don't wait blindly for a turn
            # that will never finish — force-kill is the escape hatch.
            try:
                admin_uids = await pool.fetch(
                    """SELECT uid FROM goal_members
                        WHERE goal_id = $1 AND role = 'admin'""",
                    task["goal_id"],
                )
                for m in admin_uids:
                    await push_notification(
                        m["uid"],
                        type="agent_stuck",
                        title="Agent stuck after pause",
                        body=(
                            f"A {task.get('executor','ia')} agent has been running "
                            f"for {age} min since the goal was paused. "
                            "Open Execution Control to force-kill it."
                        ),
                        goal_id=task["goal_id"],
                        task_id=task["id"],
                        link=f"/admin/controls",
                    )
            except Exception as exc:
                _log.warning("stuck-after-pause notification failed for task %s: %s", task["id"], exc)

    async def _check_stale_dispatches(self, pool):
        """Detect tasks stuck in_progress where the gateway agent no longer exists.

        CLI agents produce output through stdio, NOT through the gateway session
        transcript. So we can't check session messages — an empty session is normal.

        Instead, check if the agent definition still exists on the gateway.
        If the agent is gone (deleted, expired), the CLI process is dead.
        """
        # Kill switch — don't churn instances during a freeze. While the SYSTEM is
        # paused, skip stale-dispatch recovery entirely: the dispatch gate already
        # blocks new work, so retrying here would tear down and re-queue instances
        # pointlessly. Per-goal pauses are handled per-row inside the loop.
        from app.services import execution_control  # noqa: PLC0415
        if await execution_control.is_system_paused():
            return

        stale = await pool.fetch(
            """SELECT * FROM tasks
               WHERE state = 'in_progress' AND status = 'active'
               AND instance_id IS NOT NULL AND instance_id != ''
               AND dispatched_at IS NOT NULL
               AND dispatched_at < now() - interval '5 minutes'
               LIMIT 5""",
        )
        for row in stale:
            task = dict(row)
            task_id = task["id"]
            instance_id = task["instance_id"]

            # Kill switch — if this task's goal or instance is paused, leave it alone.
            # An agent_instance pause is a graceful stop: the operator wants the
            # current turn to finish naturally. Destroying the instance here would
            # defeat that intent and re-queue the task unexpectedly.
            if await execution_control.dispatch_block(task["goal_id"], instance_id=instance_id):
                continue

            try:
                # Check if the agent still exists on the gateway
                agents_result = await gateway_admin.call("agents.list")
                agent_ids = [a.get("id", "") for a in agents_result.get("agents", [])]
                agent_exists = instance_id in agent_ids

                # Agent still exists on gateway → assume it's working
                if agent_exists:
                    continue

                # Agent gone from gateway → CLI process is dead
                retry_count = task.get("retry_count", 0)
                max_retries = task.get("max_retries", 1)

                if retry_count < max_retries:
                    _log.warning(
                        "Task %s has empty session after 2+ min — retrying dispatch (%d/%d)",
                        task_id, retry_count + 1, max_retries,
                    )
                    corr_id = await get_task_correlation_id(pool, task_id)
                    last_evt = await get_last_event_id(pool, task_id)
                    await emit_task_event(
                        pool,
                        event_type=TASK_STATUS_CHANGED,
                        task_id=task_id, goal_id=task["goal_id"],
                        payload=task_status_changed_payload(
                            previous_status="in_progress", new_status="ready",
                            reason=f"Agent never started — retry {retry_count + 1}/{max_retries}",
                        ),
                        actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                        correlation_id=corr_id,
                        causation_ids=[last_evt] if last_evt else None,
                        summary=f"Stale dispatch retry {retry_count + 1}/{max_retries}",
                        instance_id=instance_id,
                    )
                    await pool.execute(
                        "UPDATE tasks SET state = 'ready', retry_count = $1 WHERE id = $2",
                        retry_count + 1, task_id,
                    )
                    # Destroy the dead instance so create_instance starts fresh
                    await destroy_instance(task)
                else:
                    _log.error(
                        "Task %s failed after %d dispatch retries — agent never started",
                        task_id, max_retries,
                    )
                    await pool.execute(
                        "UPDATE tasks SET state = 'failed' "
                        "WHERE id = $1 AND state IN ('dispatching','in_progress')",
                        task_id,
                    )
                    from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
                    await release_credentials_safe(task_id, "task_controller:dispatch_retries_exhausted")
                    corr_id = await get_task_correlation_id(pool, task_id)
                    last_evt = await get_last_event_id(pool, task_id)
                    await emit_task_event(
                        pool,
                        event_type=TASK_FAILED,
                        task_id=task_id, goal_id=task["goal_id"],
                        payload=task_failed_payload(
                            failure_reason=f"Agent never started after {max_retries} retries",
                            recovery_suggestion="Check OpenClaw gateway logs and agent configuration",
                        ),
                        actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                        correlation_id=corr_id,
                        causation_ids=[last_evt] if last_evt else None,
                        summary=f"Agent never started ({max_retries} retries exhausted)",
                        instance_id=instance_id,
                    )
                    await destroy_instance(task)

            except Exception as exc:
                _log.debug("Stale check for task %s failed: %s", task_id, exc)

    async def _check_stale_dispatching_nulls(self, pool) -> None:
        """Recover tasks stranded in 'dispatching' with NULL dispatched_at (M10).

        Crash window: the CAS claim sets state='dispatching'; dispatched_at is only
        written after the gateway agent is created and the first message sent. A
        process crash in this window leaves a zombie invisible to
        _check_stale_dispatches (which requires dispatched_at IS NOT NULL). Reset
        these back to 'ready' after 5 minutes so the next tick can re-dispatch.
        """
        zombies = await pool.fetch(
            """SELECT id, goal_id FROM tasks
                WHERE state = 'dispatching' AND dispatched_at IS NULL
                  AND status = 'active'
                  AND updated_at < now() - interval '5 minutes'
                LIMIT 10""",
        )
        for row in zombies:
            task_id, goal_id = row["id"], row["goal_id"]
            result = await pool.execute(
                "UPDATE tasks SET state = 'ready', updated_at = NOW() "
                "WHERE id = $1 AND state = 'dispatching' AND dispatched_at IS NULL",
                task_id,
            )
            if result != "UPDATE 0":
                _log.warning(
                    "task_controller: zombie dispatching task=%s goal=%s reset to ready "
                    "(crashed between CAS claim and dispatched_at write)",
                    task_id, goal_id,
                )

    async def _check_stale_pauses(self, pool) -> None:
        """Warn operators when any pause has been active for more than 4 hours (M16).

        Throttled to at most once per hour to avoid log spam during an extended freeze.
        """
        import time
        now = time.monotonic()
        if now - self._last_stale_freeze_warn_at < 3600:
            return
        try:
            stale = await pool.fetch(
                """SELECT scope, scope_id, reason, paused_at
                     FROM execution_pauses
                    WHERE paused_at < now() - interval '4 hours'""",
            )
            if stale:
                self._last_stale_freeze_warn_at = now
                for p in stale:
                    _log.warning(
                        "execution_control: STALE FREEZE — scope=%s id=%s has been paused "
                        "since %s (reason=%s) — operator action may be required",
                        p["scope"], p["scope_id"] or "-",
                        p["paused_at"], p["reason"] or "<none>",
                    )
        except Exception as exc:
            _log.debug("task_controller: _check_stale_pauses query failed: %s", exc)

    async def _check_stale_blocked(self, pool):
        """Emit esc.timed_out for tasks stuck in blocked beyond the timeout threshold.

        Only fires once per task — skips tasks that already have an esc.timed_out
        ledger event. Does not change task state; that remains a human decision.
        """
        stale = await pool.fetch(
            """SELECT t.id, t.goal_id, t.title, t.story_id
                 FROM tasks t
                WHERE t.state = 'blocked'
                  AND t.status = 'active'
                  AND t.updated_at < now() - interval '24 hours'
                  AND NOT EXISTS (
                      SELECT 1 FROM ledger_events le
                       WHERE le.event_type = 'esc.timed_out'
                         AND le.context_task_id = t.id
                  )
                LIMIT 10""",
        )
        for row in stale:
            task_id = row["id"]
            goal_id = row["goal_id"]
            try:
                corr_id = await get_task_correlation_id(pool, task_id)
                blocked_hours = await pool.fetchval(
                    "SELECT EXTRACT(EPOCH FROM (now() - updated_at)) / 3600 FROM tasks WHERE id = $1",
                    task_id,
                )
                await emit_task_event(
                    pool,
                    event_type=ESCALATION_TIMED_OUT,
                    task_id=task_id, goal_id=goal_id,
                    payload={
                        "task_title": row["title"] or "",
                        "story_id": row["story_id"] or "",
                        "blocked_hours": round(float(blocked_hours or 0), 1),
                    },
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                    correlation_id=corr_id,
                    causation_ids=None,
                    summary=f"Escalation timed out after {round(float(blocked_hours or 0))}h blocked",
                )
                _log.warning(
                    "Task %s (goal %s) has been blocked for %.1fh — esc.timed_out emitted",
                    task_id, goal_id, float(blocked_hours or 0),
                )
            except Exception as exc:
                _log.debug("Stale blocked check for task %s failed: %s", task_id, exc)

    async def _resolve_dependencies(self, pool):
        """Move pending tasks to ready if all dependencies are met.

        Emits TASK_DEPENDENCY_RESOLVED per resolved dep, then TASK_STATUS_CHANGED.
        """
        # Tasks with dependencies
        pending_with_deps = await pool.fetch(
            """SELECT * FROM tasks
               WHERE state = 'pending' AND status = 'active'
               AND depends_on != '{}' AND depends_on IS NOT NULL""",
        )
        for row in pending_with_deps:
            task = dict(row)
            depends_on = task.get("depends_on") or []
            if not depends_on:
                continue

            # Check dep states — a failed dep cascades failure immediately so
            # downstream tasks (e.g. integration-tester after a failed G1 gate)
            # never become ready and run against an unprepared workspace.
            dep_rows = await pool.fetch(
                "SELECT id, state FROM tasks WHERE id = ANY($1)",
                depends_on,
            )
            dep_state_map = {r["id"]: r["state"] for r in dep_rows}
            failed_dep_ids = [tid for tid, st in dep_state_map.items() if st == "failed"]
            if failed_dep_ids:
                _log.warning(
                    "task_controller: task %s has failed dep(s) %s — cascading failure",
                    task["id"], failed_dep_ids,
                )
                await pool.execute(
                    "UPDATE tasks SET state = 'failed', updated_at = NOW() WHERE id = $1",
                    task["id"],
                )
                await emit_task_event(
                    pool,
                    event_type=TASK_STATUS_CHANGED,
                    task_id=task["id"], goal_id=task["goal_id"],
                    payload=task_status_changed_payload(
                        previous_status="pending", new_status="failed",
                        reason=f"Dependency {failed_dep_ids[0]} failed — cascading failure",
                    ),
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                )
                continue

            # M12: halted dependency — the dependency was force-killed and can
            # never complete. Cancel the dependent so it doesn't strand in pending.
            halted_dep_ids = [tid for tid, st in dep_state_map.items() if st == "halted"]
            if halted_dep_ids:
                _log.warning(
                    "task_controller: task %s has halted dep(s) %s — cascading cancellation",
                    task["id"], halted_dep_ids,
                )
                await pool.execute(
                    "UPDATE tasks SET state = 'cancelled', blocked_reason = $1, "
                    "updated_at = NOW() WHERE id = $2",
                    f"Dependency {halted_dep_ids[0]} was force-killed — task cannot run",
                    task["id"],
                )
                await emit_task_event(
                    pool,
                    event_type=TASK_STATUS_CHANGED,
                    task_id=task["id"], goal_id=task["goal_id"],
                    payload=task_status_changed_payload(
                        previous_status="pending", new_status="cancelled",
                        reason=f"Dependency {halted_dep_ids[0]} was force-killed",
                    ),
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                )
                continue

            done_dep_ids = [tid for tid, st in dep_state_map.items() if st in ("done", "approved")]

            if len(done_dep_ids) == len(depends_on):
                # All deps resolved — emit events
                corr_id = await get_task_correlation_id(pool, task["id"])
                dep_completion_evts = await get_dep_completion_event_ids(pool, done_dep_ids)

                # Emit TASK_DEPENDENCY_RESOLVED for each resolved dep
                resolved_evt_ids = []
                for dep_id in done_dep_ids:
                    dep_evts = await get_dep_completion_event_ids(pool, [dep_id])
                    evt = await emit_task_event(
                        pool,
                        event_type=TASK_DEPENDENCY_RESOLVED,
                        task_id=task["id"], goal_id=task["goal_id"],
                        payload=task_dependency_resolved_payload(dependency_id=dep_id),
                        actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                        correlation_id=corr_id,
                        causation_ids=dep_evts or None,
                        summary=f"Dependency {dep_id} resolved",
                    )
                    if evt:
                        resolved_evt_ids.append(evt["event_id"])

                # Transition pending → ready
                await pool.execute("UPDATE tasks SET state = 'ready' WHERE id = $1", task["id"])
                await emit_task_event(
                    pool,
                    event_type=TASK_STATUS_CHANGED,
                    task_id=task["id"], goal_id=task["goal_id"],
                    payload=task_status_changed_payload(
                        previous_status="pending", new_status="ready",
                        reason=f"All {len(depends_on)} dependencies resolved",
                    ),
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                    correlation_id=corr_id,
                    causation_ids=resolved_evt_ids or None,
                    summary="All dependencies met → ready",
                )
                _log.info("Task %s → ready (deps met)", task["id"])

        # Tasks with NO dependencies and a template → ready (bulk, no individual events for efficiency)
        bulk_ready = await pool.execute(
            """UPDATE tasks SET state = 'ready'
               WHERE state = 'pending' AND status = 'active'
               AND (depends_on = '{}' OR depends_on IS NULL)
               AND template_id IS NOT NULL AND template_id != ''""",
        )
        # asyncpg returns status string like "UPDATE 3"; log when non-zero
        if bulk_ready and not bulk_ready.endswith(" 0"):
            _log.info("Bulk pending→ready (template already set): %s", bulk_ready)

        # Tasks with NO template and NO parent → auto-assign planner-ia
        # Skip tasks created by GO (created_by starts with 'go-') — those are human-owned
        updated = await pool.fetch(
            """UPDATE tasks SET template_id = 'planner-ia', assigned_to = 'planner-ia'
               WHERE state = 'pending' AND status = 'active'
               AND (depends_on = '{}' OR depends_on IS NULL)
               AND (template_id IS NULL OR template_id = '')
               AND parent_task_id IS NULL
               AND (created_by IS NULL OR NOT created_by LIKE 'go-%')
               RETURNING id, goal_id""",
        )
        if updated:
            _log.info("Auto-assigned planner-ia to %d task(s) — ids=%s",
                      len(updated),
                      ",".join(r["id"] for r in updated))
        for row in updated:
            corr_id = await get_task_correlation_id(pool, row["id"])
            last_evt = await get_last_event_id(pool, row["id"])
            planner_actor = await pool.fetchval(
                "SELECT actor_id FROM agents WHERE id = 'planner-ia'",
            ) or _actor_uuid(agent_id="planner-ia")
            await emit_task_event(
                pool,
                event_type=TASK_ASSIGNED,
                task_id=row["id"], goal_id=row["goal_id"],
                payload=task_assigned_payload(
                    assignee=actor_ref(planner_actor, "independent_agent", "planner-ia"),
                    assignment_reason="Auto-assigned: no template_id, top-level task needs planning",
                ),
                actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary="Auto-assigned to planner-ia",
                agent_id="planner-ia",
            )
            _log.info("Task %s auto-assigned to planner-ia", row["id"])

    async def _dispatch_task(self, pool, task: dict):
        """Create instance and dispatch, or handle human checkpoint.

        For subtasks with a parent, reuses the parent's persistent worker
        instance — all subtasks execute sequentially in the same workspace
        with Claude CLI --resume providing session continuity.
        """
        task_id = task["id"]
        goal_id = task["goal_id"]
        template_id = task.get("template_id") or task.get("assigned_to", "")
        corr_id = await get_task_correlation_id(pool, task_id)
        last_evt = await get_last_event_id(pool, task_id)

        # Kill switch — defence-in-depth guard. The IA ready-task query already
        # filters paused goals, but a tick may have selected this task just
        # before a pause flipped. Re-check system/goal (and agent-instance if the
        # task already carries one) BEFORE the atomic claim below so we leave the
        # task in 'ready' — never stranding a 'dispatching' row. Agent-instance
        # scope for a reused persistent worker is re-checked after resolution.
        from app.services import execution_control  # noqa: PLC0415
        _blk = await execution_control.dispatch_block(goal_id, task.get("instance_id") or None)
        if _blk:
            _log.info(
                "Dispatch skipped for task %s — %s paused (id=%s reason=%s); "
                "task stays 'ready' for a later tick.",
                task_id, _blk.scope, _blk.scope_id or "-", _blk.reason or "<none>",
            )
            return

        # Human checkpoint
        if template_id == "human":
            await pool.execute(
                "UPDATE tasks SET state = 'blocked', blocked_reason = $1 WHERE id = $2",
                task.get("description", "Human action required"), task_id,
            )
            await emit_task_event(
                pool,
                event_type=TASK_STATUS_CHANGED,
                task_id=task_id, goal_id=goal_id,
                payload=task_status_changed_payload(
                    previous_status="ready", new_status="blocked",
                    reason=f"Human checkpoint: {task.get('title', '')}",
                ),
                actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary=f"Human checkpoint: {task.get('title', '')}",
            )
            await emit_task_event(
                pool,
                event_type=ESCALATION_REQUESTED,
                task_id=task_id, goal_id=goal_id,
                payload={
                    "kind": "human_checkpoint",
                    "task_title": task.get("title", ""),
                    "story_id": task.get("story_id", "") or "",
                },
                actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary=f"Escalation: human checkpoint — {task.get('title', '')}",
            )
            _log.info("Task %s → blocked (human checkpoint)", task_id)
            return

        # Dispatching — atomically claim the task to prevent double-dispatch.
        # If another tick already moved it out of 'ready', skip it.
        result = await pool.execute(
            "UPDATE tasks SET state = 'dispatching' WHERE id = $1 AND state = 'ready'", task_id,
        )
        if result == "UPDATE 0":
            _log.info("Task %s already claimed by another tick — skipping", task_id)
            return

        # Relay task-dispatched to all connected thread channels (Slack, Notion SHCA page, etc.)
        try:
            from app.channels.base import relay_to_thread_channel
            _src_thread = task.get("source_thread_id") or ""
            if _src_thread:
                _title = task.get("title", "Task")
                _msg = f"🔄 *Task dispatched:* {_title} → agent `{template_id}`"
                await relay_to_thread_channel(goal_id, _src_thread, _msg, username="Task Controller")
        except Exception as exc:
            _log.warning("Channel thread relay failed for task-dispatched %s: %s", task_id, exc)

        # Notify the GO so it knows what's being worked on (title +
        # description + agent + story context). Fire-and-forget; if GO
        # session isn't active the handler no-ops.
        try:
            from app.services.go_events import on_task_dispatched
            await on_task_dispatched(goal_id, task_id)
        except Exception as exc:
            _log.warning("on_task_dispatched failed for %s: %s", task_id, exc)

        # ── Persistent Worker: reuse parent's worker for subtasks ──
        parent_task_id = task.get("parent_task_id")
        instance_id = None
        is_persistent_phase = False

        # Review tasks always get their own fresh instance — never reuse a
        # worker from the parent task tree, which may already be destroyed.
        if parent_task_id and not task.get("review_target_id"):
            # Check if parent already has a persistent worker
            worker = await pool.fetchrow("""
                SELECT gateway_agent_id, cli_session_id
                FROM agent_instances
                WHERE parent_task_id = $1 AND status = 'active' AND worker_type = 'persistent'
                LIMIT 1
            """, parent_task_id)

            if worker:
                # Reuse existing worker — this is a subsequent phase
                instance_id = worker["gateway_agent_id"]
                is_persistent_phase = True
                _log.info(
                    "PERSISTENT WORKER: reusing %s for subtask %s (parent %s, template %s)",
                    instance_id, task_id, parent_task_id, template_id,
                )
                # Point subtask at the shared worker
                await pool.execute(
                    "UPDATE tasks SET instance_id = $1 WHERE id = $2",
                    instance_id, task_id,
                )

                # Emit TASK_HANDOFF — tracks phase switch in ledger
                await emit_task_event(
                    pool,
                    event_type=TASK_HANDOFF,
                    task_id=task_id, goal_id=goal_id,
                    payload={
                        "handoff_type": "persistent_worker_phase",
                        "worker_instance_id": instance_id,
                        "phase_template_id": template_id,
                        "parent_task_id": parent_task_id,
                        "cli_session_id": worker.get("cli_session_id", ""),
                    },
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                    correlation_id=corr_id,
                    causation_ids=[last_evt] if last_evt else None,
                    summary=f"Phase handoff to {template_id} on worker {instance_id}",
                    instance_id=instance_id, agent_id=template_id,
                )

        if not instance_id:
            # No persistent worker exists yet — create one
            instance_id = await create_instance(task)
            if not instance_id:
                await pool.execute("UPDATE tasks SET state = 'failed' WHERE id = $1", task_id)
                from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
                await release_credentials_safe(task_id, "task_controller:create_instance_failed")
                cascade = await find_dependent_tasks(pool, goal_id, task_id)
                await emit_task_event(
                    pool,
                    event_type=TASK_FAILED,
                    task_id=task_id, goal_id=goal_id,
                    payload=task_failed_payload(
                        failure_reason="Failed to create agent instance",
                        recovery_suggestion="Check agent template and retry",
                        cascade_impact=cascade,
                    ),
                    actor_id=SYSTEM_ACTOR_ID, actor_type="system",
                    correlation_id=corr_id,
                    causation_ids=[last_evt] if last_evt else None,
                    summary="Failed to create instance",
                )
                return

            # Register as persistent worker if this is a subtask
            # (or a top-level planner that will have subtasks later).
            # Review tasks are never persistent workers — they run once and exit.
            if parent_task_id and not task.get("review_target_id"):
                await pool.execute("""
                    UPDATE agent_instances
                    SET worker_type = 'persistent', parent_task_id = $1
                    WHERE gateway_agent_id = $2 AND status = 'active'
                """, parent_task_id, instance_id)
                _log.info(
                    "PERSISTENT WORKER: created %s for parent task %s (first phase: %s)",
                    instance_id, parent_task_id, template_id,
                )
            elif not task.get("parent_task_id"):
                # Top-level task (planner) — register with its own ID as parent
                await pool.execute("""
                    UPDATE agent_instances
                    SET worker_type = 'persistent', parent_task_id = $1
                    WHERE gateway_agent_id = $2 AND status = 'active'
                """, task_id, instance_id)
                _log.info(
                    "PERSISTENT WORKER: created %s for top-level task %s (planner)",
                    instance_id, task_id,
                )

        # Kill switch — AGENT-INSTANCE graceful stop. If an operator stopped the
        # (possibly reused persistent) worker we just resolved, don't dispatch its
        # next subtask. The task was claimed 'dispatching' above; return it to
        # 'ready' so it picks up once the instance is resumed. Only the reuse path
        # can trip this — a freshly created instance has no pause row.
        _inst_blk = await execution_control.dispatch_block(goal_id, instance_id)
        if _inst_blk and _inst_blk.scope == "agent_instance":
            _log.info(
                "Dispatch skipped for task %s — agent instance %s stopped by "
                "operator; returning task to 'ready'.",
                task_id, instance_id,
            )
            await pool.execute("UPDATE tasks SET state = 'ready' WHERE id = $1", task_id)
            return

        # Swap workspace files before each phase — each role has its own
        # AGENTS.md, SOUL.md, and RESOURCES.md with correct access levels
        if is_persistent_phase:
            # Copy this subtask's template files to the worker workspace
            phase_template_id = task.get("template_id") or task.get("assigned_to", "")
            if phase_template_id:
                phase_files = await pool.fetch(
                    "SELECT filename, content FROM agent_files WHERE agent_id = $1",
                    phase_template_id,
                )
                for f in phase_files:
                    try:
                        await gateway_admin.set_agent_file(
                            instance_id, f["filename"], f["content"])
                    except Exception as exc:
                        _log.warning(
                            "Failed to write %s for phase %s: %s",
                            f["filename"], phase_template_id, exc,
                        )
                if phase_files:
                    _log.info(
                        "Swapped %d workspace files to %s for phase %s",
                        len(phase_files), phase_template_id, task_id,
                    )

        # Refresh RESOURCES.md before each phase. Resources resolve via the
        # task's story (story_resources → goal_resources). Legacy
        # `task_resources JOIN resources` is no longer consulted.
        if is_persistent_phase:
            try:
                from app.resource_provisioning import download_documents_to_workspace

                story_id_for_phase = await pool.fetchval(
                    "SELECT story_id FROM tasks WHERE id = $1", task_id,
                ) or ""
                _log.info(
                    "IA phase refresh: task=%s template=%s instance=%s story=%s",
                    task_id, template_id, instance_id,
                    story_id_for_phase or "(none — falling back to goal scope)",
                )

                github_token = None
                if story_id_for_phase:
                    git_repos = await pool.fetch(
                        """SELECT gr.name, COALESCE(gr.clone_url, gr.uri) AS uri
                             FROM story_resources sr
                             JOIN goal_resources gr ON gr.id = sr.resource_id
                            WHERE sr.story_id = $1 AND gr.status = 'active'
                              AND gr.kind IN ('repo', 'branch')
                              AND gr.repo_name IS NOT NULL AND gr.repo_name != ''""",
                        story_id_for_phase,
                    )
                else:
                    git_repos = await pool.fetch(
                        """SELECT name, COALESCE(clone_url, uri) AS uri
                             FROM goal_resources
                            WHERE goal_id = $1 AND status = 'active'
                              AND kind IN ('repo', 'branch')
                              AND repo_name IS NOT NULL AND repo_name != ''""",
                        goal_id,
                    )

                repo_names = []
                for r in git_repos:
                    uri = r["uri"] or ""
                    if "github.com" in uri:
                        nm = uri.rstrip("/").rstrip(".git").split("/")[-1]
                        if nm and nm not in repo_names:
                            repo_names.append(nm)

                if repo_names:
                    try:
                        token_data = await generate_installation_token(
                            repo_names,
                            {"contents": "write", "metadata": "read", "workflows": "write"},
                        )
                        github_token = token_data["token"]
                    except Exception as exc:
                        _log.warning("Failed to generate GitHub token for phase: %s", exc)

                documents = await download_documents_to_workspace(
                    gateway_admin, instance_id, goal_id,
                    story_id=story_id_for_phase or None, agent_id=template_id,
                )
                resources_md = await generate_resources_md(
                    template_id, goal_id, github_token=github_token,
                    task_id=task_id, documents=documents,
                )
                # Gate the rewrite through the live session (if any) so a
                # phase-dispatched message landing concurrently waits for
                # the new RESOURCES.md. IA persistent workers are keyed
                # (goal_id, template_id) in session_manager.
                from app.resource_provisioning import _refresh_gate
                async with _refresh_gate(goal_id, template_id):
                    await gateway_admin.set_agent_file(
                        instance_id, "RESOURCES.md", resources_md,
                    )
                _log.info(
                    "IA phase refresh DONE: task=%s instance=%s repos=%d docs=%d bytes=%d",
                    task_id, instance_id, len(repo_names), len(documents), len(resources_md),
                )
            except Exception as exc:
                _log.warning("Failed to refresh RESOURCES.md for phase: %s", exc)

        # Build phase-aware message for persistent workers
        if is_persistent_phase:
            message = await _build_phase_message(task, parent_task_id)
        else:
            message = await build_dispatch_message(task)

        # Session key uses parent_task_id for persistent workers
        # so all phases share the same OpenClaw session (enabling --resume).
        # Review tasks always use their own task_id — they run independently.
        if parent_task_id and not task.get("review_target_id"):
            session_key = make_task_session_key(instance_id, parent_task_id)
        else:
            session_key = make_task_session_key(instance_id, task_id)

        # Use agent method for dispatch — supports extraSystemPrompt, returns runId.
        # Backend clients are now treated as INTERNAL_MESSAGE_CHANNEL in the OpenClaw
        # fork, so tools are not disabled by group-chat context detection.
        try:
            # 6 min ceiling — sized to cover the gateway's own 5-min
            # compaction-aggregate-timeout (attempt.ts:COMPACTION_RETRY_AGGREGATE_TIMEOUT_MS).
            # If the IA is mid-compaction when this dispatch arrives,
            # pi-embedded-runner blocks on waitForCompactionRetry up to
            # that window; a shorter client timeout would falsely fail
            # the task while the gateway is still working through the
            # compaction LLM-summary. Timeout is a ceiling, not a floor.
            dispatch_result = await gateway_admin.call("agent", {
                "agentId": instance_id,
                "sessionKey": session_key,
                "message": message,
                "idempotencyKey": f"dispatch-{task_id}",
            }, timeout=360)
            run_id = dispatch_result.get("runId", "")
            phase_label = " (persistent phase)" if is_persistent_phase else ""
            _log.info(
                "Dispatched task %s → %s (runId=%s)%s",
                task_id, instance_id, run_id[:12] if run_id else "?", phase_label,
            )
        except Exception as exc:
            _log.error("Dispatch failed for %s: %s", task_id, exc)
            await pool.execute("UPDATE tasks SET state = 'failed' WHERE id = $1", task_id)
            from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
            await release_credentials_safe(task_id, "task_controller:dispatch_exception")
            cascade = await find_dependent_tasks(pool, goal_id, task_id)
            await emit_task_event(
                pool,
                event_type=TASK_FAILED,
                task_id=task_id, goal_id=goal_id,
                payload=task_failed_payload(
                    failure_reason=f"Dispatch failed: {exc}",
                    recovery_suggestion="Check gateway connectivity and retry",
                    cascade_impact=cascade,
                ),
                actor_id=await instance_actor_id(task_id=task_id, gateway_agent_id=instance_id),
                actor_type="independent_agent",
                correlation_id=corr_id,
                causation_ids=[last_evt] if last_evt else None,
                summary=f"Dispatch failed: {exc}",
                instance_id=instance_id,
            )
            return

        await pool.execute(
            "UPDATE tasks SET state = 'in_progress', dispatched_at = now(), started_at = COALESCE(started_at, now()) WHERE id = $1", task_id,
        )

        # Emit TASK_STATUS_CHANGED ready → in_progress. Attributed to the
        # dispatched instance (the agent that now owns the task), not system.
        dispatch_last = await get_last_event_id(pool, task_id)
        _disp_actor, _disp_type = await instance_actor(task_id=task_id, gateway_agent_id=instance_id)
        await emit_task_event(
            pool,
            event_type=TASK_STATUS_CHANGED,
            task_id=task_id, goal_id=goal_id,
            payload=task_status_changed_payload(
                previous_status="ready", new_status="in_progress",
                reason=f"Dispatched to {instance_id}",
            ),
            actor_id=_disp_actor, actor_type=_disp_type,
            correlation_id=corr_id,
            causation_ids=[dispatch_last] if dispatch_last else None,
            summary=f"Dispatched to {instance_id}",
            instance_id=instance_id, agent_id=template_id,
        )

        # Notify platform channels of task dispatch
        try:
            import asyncio as _asyncio
            from app.channels.base import relay_lifecycle_event as _rle
            _task_row = await pool.fetchrow("SELECT title, story_id FROM tasks WHERE id=$1", task_id)
            _asyncio.create_task(_rle(goal_id, "task.state_changed", {
                "task_id": task_id,
                "task_title": (_task_row["title"] if _task_row else "") or "",
                "story_id": str((_task_row["story_id"] if _task_row else "") or ""),
                "old_state": "ready",
                "new_state": "in_progress",
            }))
        except Exception as _exc:
            _log.debug("relay task.state_changed in_progress failed task=%s: %s", task_id, _exc)


# ── Review Task Helpers ──────────────────────────────────────────────────────

def _review_sibling_parent(task: dict) -> str | None:
    """Return parent_task_id for review tasks (siblings or children of work task)."""
    return task.get("parent_task_id") or task["id"]


async def _spawn_ra_review_task(task_id: str, task: dict, ra_agent_id: str) -> None:
    pool = get_pool()
    reviewed = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if not reviewed:
        _log.warning("_spawn_ra_review_task: task %s not found, skipping", task_id)
        return
    from app.plan_paths import resolve_plan_paths
    paths = await resolve_plan_paths(task_id)

    description = f"""You are reviewing the outcome of a completed task.

### Task Being Reviewed: {reviewed['title']}

**Description:**
{reviewed.get('description', '')}

### Where to Find the Work

- `{paths['plan_md']}` — implementation plan
- `{paths['progress_md']}` — step-by-step progress log

Read these files and any relevant code in the repository using your file tools.
Do NOT form a verdict without examining the actual implementation.

Call `message-assistant` with a concise human-readable summary of your findings.
Then call `task-report` with your verdict."""

    review_task_id = str(uuid.uuid4())[:8]

    acceptance_criteria = f"""Reviewed task acceptance criteria:
{reviewed.get('acceptance_criteria') or '(none specified — assess completeness and quality of the stated objective)'}

When calling task_report, set the review field with:
- `verdict`: one of `"approve"`, `"request_changes"`, `"block"`
- `critical_issues`: list of blocking problems (empty if approving)
- `suggestions`: list of non-blocking improvements"""
    parent_id = _review_sibling_parent(task)
    corr_id = task.get("correlation_id", "")
    # Both assigned_to and template_id are set to ra_agent_id (the gateway instance
    # ID, e.g. "task-reviewer-ia-bnkdz-350afe17") — NOT a templates.id. This is
    # intentional: the dispatch path reads template_id to know which agent to assign
    # work to, and here we want to route to a specific already-provisioned RA instance.
    # Side effect: resolving the agent type for this task must NOT query
    # `agents WHERE id = template_id` (RA instances have no agents rows).
    # Use `agent_instances JOIN templates` instead — see handle_task_completion.
    await pool.execute(
        """INSERT INTO tasks (id, goal_id, title, description, acceptance_criteria,
                              assigned_to, template_id, priority, depends_on, state,
                              created_by, thread_id, story_id, review_target_id,
                              correlation_id, source_thread_id, decision_event_id, parent_task_id)
           VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,'pending','system',$9,$10,$11,$12,$13,NULL,$14)
           ON CONFLICT (id) DO NOTHING""",
        review_task_id, task["goal_id"],
        f"RA Review: {task['title']}", description, acceptance_criteria,
        ra_agent_id, "medium", [],
        task.get("thread_id", ""), task.get("story_id", ""),
        task_id, corr_id, task.get("source_thread_id", ""), parent_id,
    )
    await task_controller.trigger()


async def _spawn_human_review_task(
    task_id: str, task: dict, ra_review_task_id: str | None
) -> None:
    pool = get_pool()
    review_task_id = f"{task_id}-human-review-{uuid.uuid4().hex[:8]}"
    parent_id = _review_sibling_parent(task)
    depends_on = [ra_review_task_id] if ra_review_task_id else [task_id]
    corr_id = task.get("correlation_id", "")
    await pool.execute(
        """INSERT INTO tasks (id, goal_id, title, description, assigned_to, template_id,
                              priority, depends_on, state, created_by, thread_id, story_id,
                              review_target_id, correlation_id, source_thread_id,
                              decision_event_id, parent_task_id)
           VALUES ($1,$2,$3,$4,'human','human',$5,$6,'pending','system',$7,$8,$9,$10,$11,NULL,$12)
           ON CONFLICT (id) DO NOTHING""",
        review_task_id, task["goal_id"],
        f"Human Review: {task['title']}",
        f"Human approval required for task {task_id}",
        "medium",
        depends_on,
        task.get("thread_id", ""),
        task.get("story_id", ""),
        task_id,
        corr_id,
        task.get("source_thread_id", ""),
        parent_id,
    )
    await task_controller.trigger()


async def _spawn_review_task(pool, task_id: str, task: dict, *, force: bool = False) -> None:
    """Spawn an RA (or human fallback) review task for a completed work task.

    Skips review tasks, planner tasks, and human-assigned tasks.
    Pass force=True to bypass the template filter (e.g. when all subtasks of a
    planner task complete — the planner is the spec holder but we still want review).
    Dedup: no-ops if a review task already exists for this task_id.
    """
    if task.get("review_target_id"):
        return
    # Platform Improvement (__platform__) work is never reviewed — its tasks are
    # small no-code integration ops executed directly by the platform assistant.
    if task.get("goal_id") == "__platform__":
        return
    template_id = task.get("template_id") or ""
    if not force and ("planner" in template_id.lower() or template_id == "human"):
        return
    existing = await pool.fetchval(
        """SELECT id FROM tasks WHERE review_target_id = $1 AND status = 'active'
           AND state NOT IN ('done', 'failed', 'cancelled') LIMIT 1""",
        task_id,
    )
    if existing:
        return
    # Step 1: goal-specific RA (allows per-goal override)
    ra_agent_id = await pool.fetchval(
        """SELECT a.id FROM agents a
           JOIN goal_agent_memberships gam ON gam.agent_id = a.id
           WHERE gam.goal_id = $1 AND gam.status = 'active'
             AND a.type = 'RA' AND a.scope = 'task' AND a.status = 'active'
           LIMIT 1""",
        task["goal_id"],
    )
    # Step 2: global fallback — one RA serves all goals
    if not ra_agent_id:
        ra_agent_id = await pool.fetchval(
            """SELECT id FROM agents
               WHERE type = 'RA' AND scope = 'task' AND status = 'active'
               ORDER BY created_at ASC LIMIT 1"""
        )

    if ra_agent_id:
        await _spawn_ra_review_task(task_id, task, ra_agent_id)
    else:
        from app.cards import progress_card
        from app.assistant_relay import post_card_to_assistant_thread
        card = progress_card(
            task_id=task_id,
            agent_template="system",
            agent_name="Task Controller",
            title="Human Review Required",
            body=(
                f"No review agent is configured. Please review "
                f"**{task.get('title', task_id)}** manually on the task board."
            ),
        )
        try:
            await post_card_to_assistant_thread(
                task=task,
                card=card,
                content_markdown=f"Human review required for: {task.get('title', task_id)}",
                sender="system",
                notify_type="review_required",
                notify_title=f"Review needed: {task.get('title', task_id)[:50]}",
            )
        except Exception as exc:
            _log.warning("Failed to post human review card for task %s: %s", task_id, exc)
        await _spawn_human_review_task(task_id, task, ra_review_task_id=None)


def _parse_review_verdict(
    result: str,
) -> tuple[str, int, list[str], list[str], str, str]:
    """Parse RA result for verdict, score, findings, recommendations, updated_spec, summary.

    Binary verdict policy: only an explicit approval keyword
    (approve / approved / complete / completed / pass / passed,
    case-insensitive) in `report.review.verdict` or `report.verdict` yields
    "approved". Every other label — request_changes, block, rejected,
    empty, unrecognised — collapses to "rejected". This keeps routing
    deterministic and avoids the prior fragile keyword fallback that
    could mis-classify approved reviews as rejected.
    """
    try:
        data = json.loads(result) if result.strip().startswith("{") else {}
        if not data:
            # Look for JSON block inside fenced code
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result, re.DOTALL)
            if m:
                data = json.loads(m.group(1))
    except (json.JSONDecodeError, AttributeError):
        data = {}

    # Binary verdict: only an explicit approval keyword counts as "approved".
    # Anything else — including request_changes, block, an empty verdict, or
    # an unrecognised label — collapses to "rejected" so the main task lands
    # in a deterministic state.
    _APPROVED_KEYWORDS = {
        "approve", "approved",
        "complete", "completed",
        "pass", "passed",
    }
    raw_verdict = (data.get("review") or {}).get("verdict") or data.get("verdict") or ""
    raw_verdict_lc = raw_verdict.strip().lower()
    if any(kw in raw_verdict_lc for kw in _APPROVED_KEYWORDS):
        # An approval keyword appears anywhere in the verdict string — e.g.
        # "approve", "Approved", "complete and passing tests" all count.
        verdict = "approved"
    elif raw_verdict_lc:
        # Verdict is set but contains no approval keyword (request_changes,
        # block, rejected, anything else) → rejected.
        verdict = "rejected"
    else:
        # No structured verdict at all — fall back to scanning the raw
        # result text for an approval keyword as a last-resort signal.
        approved_re = re.compile(
            r"\b(APPROVED|APPROVE|COMPLETED|COMPLETE|PASSED|PASS)\b",
            re.IGNORECASE,
        )
        verdict = "approved" if approved_re.search(result or "") else "rejected"

    review_block = data.get("review") or {}

    score = data.get("quality_score") or data.get("score")
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 75 if verdict == "approved" else 50

    findings = review_block.get("critical_issues") or data.get("findings") or []
    if not isinstance(findings, list):
        findings = [str(findings)]

    recommendations = review_block.get("suggestions") or data.get("recommendations") or []
    if not isinstance(recommendations, list):
        recommendations = [str(recommendations)]

    updated_spec = data.get("updated_spec", "") or ""
    summary = data.get("summary", "") or result[:200]

    return verdict, score, findings, recommendations, updated_spec, summary


async def _build_ia_work_context(pool, original_task_id: str) -> str:
    """Build a human-readable summary of the IA's completed work for the assistant action message.

    Pulls structured artifacts (branch, files_changed, test_results) from the
    TASK_COMPLETED ledger event so the human has full context when approving.
    Falls back gracefully — any missing field is simply omitted.
    """
    lines = []

    # Structured artifacts from the ledger TASK_COMPLETED event
    artifact_row = await pool.fetchrow(
        """SELECT payload FROM ledger_events
           WHERE context_task_id = $1 AND event_type = $2
           ORDER BY sequence_number DESC LIMIT 1""",
        original_task_id, TASK_COMPLETED,
    )
    artifacts = []
    if artifact_row:
        p = json.loads(artifact_row["payload"]) if isinstance(artifact_row["payload"], str) else (artifact_row["payload"] or {})
        artifacts = p.get("artifacts_produced", [])

    branch_artifact = next((a for a in artifacts if a.get("artifact_type") == "branch"), None)
    test_artifact = next((a for a in artifacts if a.get("artifact_type") == "test_results"), None)

    if branch_artifact:
        meta = branch_artifact.get("metadata", {})
        branch_name = meta.get("branch_name", branch_artifact.get("storage_ref", ""))
        files = meta.get("files_changed", [])
        if branch_name:
            lines.append(f"🌿 **Branch:** `{branch_name}`")
        if files:
            file_list = "\n".join(f"  - `{f}`" for f in files[:20])
            lines.append(f"📁 **Files changed:**\n{file_list}")
            if len(files) > 20:
                lines.append(f"  _(and {len(files) - 20} more)_")

    if test_artifact:
        meta = test_artifact.get("metadata", {})
        passed = meta.get("passed")
        failed = meta.get("failed")
        coverage = meta.get("coverage")
        parts = []
        if passed is not None:
            parts.append(f"{passed} passed")
        if failed is not None:
            parts.append(f"{failed} failed")
        if coverage:
            parts.append(f"coverage {coverage}")
        if parts:
            icon = "✅" if not failed else "❌"
            lines.append(f"{icon} **Tests:** {', '.join(parts)}")

    return "\n".join(lines)



async def _spawn_human_review_task_after_ra(
    task_id: str,
    task: dict,
    ra_review_task_id: str,
    score: int,
    findings: list,
    recommendations: list,
    summary: str,
    ra_full_result: str,
) -> None:
    """Create a human review subtask directly in awaiting_approval, pre-filled with RA context."""
    pool = get_pool()
    review_task_id = f"{task_id}-human-review-{uuid.uuid4().hex[:8]}"
    parent_id = _review_sibling_parent(task)
    corr_id = task.get("correlation_id", "")

    acceptance_criteria = (task.get("acceptance_criteria") or "").strip()
    work_context = await _build_ia_work_context(pool, task_id)

    desc_parts = [f"RA reviewed this task and approved it (score {score}/100)."]
    if summary:
        desc_parts.append(f"\n**RA summary:** {summary}")
    if work_context:
        desc_parts.append(f"\n{work_context}")
    if acceptance_criteria:
        desc_parts.append(f"\n**Acceptance criteria:**\n{acceptance_criteria}")
    if findings:
        desc_parts.append("\n**RA findings (non-blocking):**\n" + "\n".join(f"- {f}" for f in findings))
    if recommendations:
        desc_parts.append("\n**RA recommendations:**\n" + "\n".join(f"- {r}" for r in recommendations))
    description = "\n".join(desc_parts)

    # Created in 'blocked' state — the subtask is parked waiting on human
    # action. The main reviewed task carries the awaiting_approval state.
    # When the human approves/rejects (UI or Slack), complete_human_review
    # flips this subtask to 'done' and the main task to 'done' or 'rejected'.
    await pool.execute(
        """INSERT INTO tasks (id, goal_id, title, description, assigned_to, template_id,
                              priority, depends_on, state, blocked_reason, created_by, thread_id, story_id,
                              review_target_id, correlation_id, source_thread_id,
                              decision_event_id, parent_task_id)
           VALUES ($1,$2,$3,$4,'human','human',$5,$6,'blocked',$7,'system',$8,$9,$10,$11,$12,NULL,$13)
           ON CONFLICT (id) DO NOTHING""",
        review_task_id, task["goal_id"],
        f"Human Review: {task['title']}",
        description,
        "medium", [],
        f"Awaiting human approve/reject — RA verdict approved (score {score}/100)",
        task.get("thread_id", ""),
        task.get("story_id", ""),
        task_id,
        corr_id,
        task.get("source_thread_id", ""),
        parent_id,
    )
    await emit_task_event(
        pool,
        event_type=ESCALATION_REQUESTED,
        task_id=review_task_id, goal_id=task["goal_id"],
        payload={
            "kind": "ra_review",
            "task_title": task.get("title", ""),
            "story_id": task.get("story_id", "") or "",
            "review_target_id": task_id,
            "score": score,
        },
        actor_id=SYSTEM_ACTOR_ID, actor_type="system",
        correlation_id=corr_id,
        causation_ids=None,
        summary=f"Escalation: RA review — {task.get('title', '')}",
    )


async def _notify_assistant_ra_rejection(
    original_task: dict,
    verdict: str,
    score: int,
    findings: list,
    recommendations: list,
    summary: str,
    updated_spec: str,
) -> None:
    """Post full RA findings to the assistant and ask it to create a revised spec."""
    from app.cards import progress_card
    from app.assistant_relay import post_card_to_assistant_thread

    task_id = original_task["id"]
    task_title = original_task.get("title", task_id)
    verdict_label = {"changes_required": "Changes Required", "rejected": "Rejected"}.get(verdict, verdict)

    msg = f"## RA Review: {verdict_label} — {task_title}\n\n"
    msg += f"**Score:** {score}/100\n\n"
    if summary:
        msg += f"**Summary:** {summary}\n\n"
    if findings:
        msg += "**Blocking issues:**\n" + "\n".join(f"- {f}" for f in findings) + "\n\n"
    if recommendations:
        msg += "**Recommendations:**\n" + "\n".join(f"- {r}" for r in recommendations) + "\n\n"
    if updated_spec:
        msg += f"**RA proposed revised spec:**\n{updated_spec}\n\n"
    msg += (
        "The task has been marked **rejected**. "
        "Please review the findings above and create a new task with an updated spec "
        "that addresses the blocking issues."
    )

    try:
        from app.cards import review_card
        card = review_card(
            task_id=task_id,
            agent_template="task-reviewer-ia",
            agent_name="Task Reviewer",
            reviewed_task_title=task_title,
            verdict=verdict,
            quality_score=score,
            findings=findings,
            recommendations=recommendations,
            summary=summary or msg[:300],
            updated_spec=updated_spec,
        )
        await post_card_to_assistant_thread(
            task=original_task,
            card=card,
            content_markdown=msg,
            sender="system",
            notify_type="review_rejected",
            notify_title=f"Task rejected after review: {task_title[:50]}",
        )
    except Exception as exc:
        _log.warning("Failed to post RA rejection message for task %s: %s", task_id, exc)


async def complete_human_review(
    review_task_id: str,
    action: str,
    feedback: str,
    actor_uid: str,
) -> dict:
    """Unified completion for a human-review subtask, used by both the UI
    approve endpoint and the Slack /human-review command.

    Flow:
      - review subtask:  blocked → done   (the gate has been cleared)
      - main task:       awaiting_approval → done      (action="approve")
                                          → rejected  (action="reject")

    Both rows are updated in one transaction. Ledger events are emitted
    for each, the controller is triggered, and GO is notified of the
    main task transition.

    Returns {"ok": bool, "message": str, "main_task_id": str, "main_state": str}.
    """
    pool = get_pool()
    act = (action or "").strip().lower()
    if act not in ("approve", "reject"):
        return {"ok": False, "message": f"Invalid action {action!r} — expected 'approve' or 'reject'"}

    hr = await pool.fetchrow(
        "SELECT * FROM tasks WHERE id = $1 AND status = 'active'",
        review_task_id,
    )
    if not hr:
        return {"ok": False, "message": f"Review task {review_task_id} not found"}
    hr = dict(hr)

    if (hr.get("template_id") or "") != "human" or not hr.get("review_target_id"):
        return {"ok": False, "message": "Not a human-review task"}

    if hr["state"] == "done":
        # Idempotent — already cleared. Tell the caller it's fine.
        main_id = hr["review_target_id"]
        main_row = await pool.fetchval("SELECT state FROM tasks WHERE id = $1", main_id)
        return {
            "ok": True, "idempotent": True,
            "message": "Review already done",
            "main_task_id": main_id, "main_state": main_row or "",
        }

    if hr["state"] not in ("blocked", "awaiting_approval"):
        # awaiting_approval is accepted for back-compat with rows created
        # before this flow change. Anything else is unexpected.
        return {
            "ok": False,
            "message": f"Review is in state {hr['state']}, not awaiting human action",
        }

    main_task_id = hr["review_target_id"]
    main = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", main_task_id)
    if not main:
        return {"ok": False, "message": f"Reviewed task {main_task_id} not found"}
    main = dict(main)

    new_main_state = "done" if act == "approve" else "rejected"
    goal_id = hr["goal_id"]

    actor_id = await pool.fetchval(
        "SELECT actor_id FROM users WHERE uid = $1", actor_uid,
    ) or SYSTEM_ACTOR_ID

    summary = (
        f"Human {'approved' if act == 'approve' else 'rejected'} review"
        + (f": {feedback}" if feedback else "")
    )

    corr_hr = await get_task_correlation_id(pool, review_task_id)
    corr_main = await get_task_correlation_id(pool, main_task_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            last_hr = await get_last_event_id(conn, review_task_id)
            last_main = await get_last_event_id(conn, main_task_id)

            # 1. Review subtask → done
            await conn.execute(
                """UPDATE tasks SET state = 'done', result = $1
                   WHERE id = $2 AND state IN ('blocked', 'awaiting_approval')""",
                (feedback or summary)[:32000], review_task_id,
            )
            await _emit_inner(
                conn, event_type=TASK_STATUS_CHANGED,
                task_id=review_task_id, goal_id=goal_id,
                payload=task_status_changed_payload(
                    previous_status=hr["state"], new_status="done",
                    reason=summary,
                ),
                actor_id=actor_id, actor_type="human",
                correlation_id=corr_hr,
                causation_ids=[last_hr] if last_hr else None,
                summary=summary, content="", agent_id="human", instance_id="",
                context_conversation_id="",
            )

            # 2. Main task → done or rejected
            await conn.execute(
                """UPDATE tasks SET state = $1
                   WHERE id = $2 AND state = 'awaiting_approval'""",
                new_main_state, main_task_id,
            )
            await _emit_inner(
                conn, event_type=TASK_STATUS_CHANGED,
                task_id=main_task_id, goal_id=goal_id,
                payload=task_status_changed_payload(
                    previous_status=main["state"], new_status=new_main_state,
                    reason=summary,
                ),
                actor_id=actor_id, actor_type="human",
                correlation_id=corr_main,
                causation_ids=[last_main] if last_main else None,
                summary=summary, content="", agent_id="human", instance_id="",
                context_conversation_id="",
            )

    # 3. Cleanup IA instances in the tree if approved
    if act == "approve":
        try:
            await cleanup_parent_task(main_task_id)
        except Exception as exc:
            _log.warning("cleanup_parent_task failed for %s: %s", main_task_id, exc)

    # 4. Wake the controller and notify GO of the main task transition
    await task_controller.trigger()
    try:
        from app.services.go_events import on_task_state_changed
        await on_task_state_changed(
            goal_id, main_task_id, "awaiting_approval", new_main_state, actor_uid,
        )
    except Exception as exc:
        _log.warning("GO event notification failed for %s: %s", main_task_id, exc)

    _log.info(
        "Human review completed: review=%s action=%s → main=%s state=%s by uid=%s",
        review_task_id, act, main_task_id, new_main_state, actor_uid,
    )

    return {
        "ok": True,
        "message": f"Main task → {new_main_state}",
        "main_task_id": main_task_id,
        "main_state": new_main_state,
    }


async def _handle_ra_review_task_completion(
    review_task_id: str, result: str, review_task: dict
) -> None:
    """Parse RA output, store review record, then branch on verdict:
    - approved  → parent transitions to awaiting_approval; human review subtask created
    - negative  → parent transitions to rejected; SHCA notified with full findings
    """
    pool = get_pool()
    original_task_id = review_task["review_target_id"]
    original_task = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", original_task_id)
    if not original_task:
        return

    original_task = dict(original_task)
    verdict, score, findings, recommendations, updated_spec, summary = _parse_review_verdict(result)

    await pool.execute(
        """INSERT INTO task_reviews
               (task_id, reviewer_agent_id, review_task_id, verdict, quality_score,
                findings, recommendations, updated_spec, summary, full_content)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
        original_task_id, review_task.get("template_id", ""),
        review_task_id, verdict, score,
        findings, recommendations, updated_spec, summary, result[:32000],
    )

    corr_id = await get_task_correlation_id(pool, original_task_id)
    await emit_task_event(
        pool, event_type=ASSESSMENT_COMPLETED,
        task_id=review_task_id, goal_id=review_task["goal_id"],
        payload=assessment_completed_payload(
            assessed_task_id=original_task_id, verdict=verdict,
            quality_score=score, findings=findings,
            recommendations=recommendations, summary=summary,
        ),
        actor_id=await instance_actor_id(
            task_id=review_task_id,
            gateway_agent_id=review_task.get("instance_id", ""),
        ),
        actor_type="independent_agent",
        correlation_id=corr_id,
        summary=f"Review: {verdict} (score {score})",
        content=result[:32000],
    )

    if verdict == "approved":
        # Parent stays in awaiting_approval; human review subtask carries the gate
        await pool.execute(
            """UPDATE tasks SET state = 'awaiting_approval'
               WHERE id = $1 AND state = 'automated_review'""",
            original_task_id,
        )
        await _spawn_human_review_task_after_ra(
            original_task_id, original_task, review_task_id,
            score, findings, recommendations, summary, result,
        )
        # Brief SHCA notification so the human knows action is needed
        try:
            from app.cards import review_card
            from app.assistant_relay import post_card_to_assistant_thread
            task_title = original_task.get("title", original_task_id)
            notify_md = (
                f"RA cleared **{task_title}** (score {score}/100). "
                f"A human review task is now awaiting your approval on the task board."
            )
            card = review_card(
                task_id=original_task_id,
                agent_template="task-reviewer-ia",
                agent_name="Task Reviewer",
                reviewed_task_title=task_title,
                verdict=verdict,
                quality_score=score,
                findings=findings,
                recommendations=recommendations,
                summary=summary or notify_md,
                updated_spec="",
            )
            await post_card_to_assistant_thread(
                task=original_task,
                card=card,
                content_markdown=notify_md,
                sender="system",
                notify_type="approval_required",
                notify_title=f"Review needed: {task_title[:50]}",
            )
        except Exception as exc:
            _log.warning("Failed to post RA approval notification for task %s: %s", original_task_id, exc)
    else:
        # Negative verdict — reject the original task and surface findings to SHCA
        await pool.execute(
            """UPDATE tasks SET state = 'rejected'
               WHERE id = $1 AND state = 'automated_review'""",
            original_task_id,
        )
        from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
        await release_credentials_safe(original_task_id, "task_controller:ra_verdict_rejected")
        await _notify_assistant_ra_rejection(
            original_task, verdict, score, findings, recommendations, summary, updated_spec,
        )

    # Tear down the RA's gateway instance immediately. The RA's work is
    # done the moment it submits a verdict; there's nothing for it to
    # do during the wait for human review (approved path) or after the
    # rejection notification (other path). Waiting for cleanup_parent_
    # task to fire on main-task terminal state could be seconds → days,
    # during which the RA holds ~150-300 MB of gateway RAM for no
    # reason. Destroy now.
    try:
        await destroy_instance({
            "id": review_task_id,
            "instance_id": review_task.get("instance_id", ""),
            "goal_id": review_task.get("goal_id", ""),
        })
        _log.info(
            "RA gateway instance destroyed immediately after verdict: review_task=%s",
            review_task_id,
        )
    except Exception as exc:
        _log.warning(
            "Failed to tear down RA gateway instance for %s (will be picked up by "
            "cleanup_parent_task or the orphan reconciler): %s",
            review_task_id, exc,
        )

    await task_controller.trigger()


# ── Task Completion Handler ──────────────────────────────────────────────────

async def handle_task_completion(task_id: str, result: str) -> None:
    """Called when an agent finishes a task. Stores result, emits TASK_COMPLETED,
    resolves deps, parses planner subtasks, checks parent completion."""
    pool = get_pool()
    task = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if not task:
        return
    task = dict(task)

    # Review task hook: run verdict handler before normal done flow.
    # Falls through — normal flow still sets this task to done, emits TASK_COMPLETED,
    # and calls check_parent_completion.
    if task.get("review_target_id"):
        # template_id on review tasks holds the gateway instance ID (ra_agent_id).
        # Resolve to agent type via agent_instances→templates — RA instances are
        # ephemeral and never written to the agents table.
        agent_type = await pool.fetchval(
            """SELECT t.type FROM agent_instances ai
               JOIN templates t ON t.id = ai.template_id
               WHERE ai.gateway_agent_id = $1
               LIMIT 1""",
            task.get("template_id", ""),
        ) or ""
        if agent_type == "RA":
            await _handle_ra_review_task_completion(task_id, result, task)
        elif task.get("template_id") == "human":
            # Human manually approved the review — transition reviewed task to approved.
            await pool.execute(
                """UPDATE tasks SET state = 'approved'
                   WHERE id = $1 AND state = 'awaiting_approval'""",
                task["review_target_id"],
            )

    # Planner completing ≠ parent task done.
    # The planner produces a plan (subtasks). The parent task stays in_progress
    # until all subtasks finish — check_parent_completion handles that.
    template_id = task.get("template_id") or ""
    is_planner = "planner" in template_id.lower()
    is_subtask = bool(task.get("parent_task_id"))
    needs_approval = task.get("approval_required", False)

    # Check if this task is part of a persistent worker
    _pw_row = await pool.fetchrow("""
        SELECT worker_type FROM agent_instances
        WHERE gateway_agent_id = $1 AND status = 'active'
    """, task.get("instance_id", ""))
    is_persistent = _pw_row and _pw_row["worker_type"] == "persistent"

    _log.info(
        "Task completion: %s (template=%s, planner=%s, subtask=%s, persistent=%s) → %s",
        task_id, template_id, is_planner, is_subtask, is_persistent,
        "awaiting_approval" if (is_planner and not is_subtask) else "done",
    )

    is_review_task = bool(task.get("review_target_id"))

    if is_planner and not is_subtask:
        # Planner done → awaiting_approval. The human must click "Approve Plan"
        # (in Slack or the web UI). card_responses.py handles the click:
        # creates subtasks and moves to in_progress.
        new_state = "awaiting_approval"
    elif is_review_task or (is_subtask and not needs_approval):
        # RA review tasks complete independently — they don't gate on human approval.
        new_state = "done"
    else:
        new_state = "awaiting_approval"

    await pool.execute(
        "UPDATE tasks SET state = $1, result = $2 WHERE id = $3",
        new_state, result[:32000], task_id,
    )

    # Extract artifacts from result (try parsing as JSON report)
    artifacts = []
    try:
        report = json.loads(result) if result.strip().startswith("{") else {}
        artifacts = extract_artifacts_from_report(report)
    except (json.JSONDecodeError, AttributeError):
        pass

    corr_id = await get_task_correlation_id(pool, task_id)
    last_evt = await get_last_event_id(pool, task_id)

    if is_planner and not is_subtask:
        # Planner done → post approval card and wait for human sign-off.
        # card_responses.py:handle_card_response handles the "approve" click:
        # it creates subtasks and moves the task to in_progress. The "changes"
        # click sends feedback back to the planner for a re-run.
        await emit_task_event(
            pool,
            event_type=TASK_PROGRESS_UPDATED,
            task_id=task_id, goal_id=task["goal_id"],
            payload=task_progress_updated_payload(
                summary="Planning complete — awaiting human approval",
            ),
            actor_id=await instance_actor_id(
                task_id=task_id,
                gateway_agent_id=task.get("instance_id", ""),
            ),
            actor_type="independent_agent",
            correlation_id=corr_id,
            causation_ids=[last_evt] if last_evt else None,
            summary="Plan ready — awaiting approval",
            content=result[:32000],
            instance_id=task.get("instance_id", ""),
            agent_id=task.get("template_id", ""),
        )

        parsed_subtasks = parse_planner_subtasks(result)
        if parsed_subtasks:
            plan_lines = []
            for i, st in enumerate(parsed_subtasks):
                deps = st.get("depends_on", [])
                dep_str = f" (depends on: {deps})" if deps else ""
                plan_lines.append(
                    f"{i+1}. **{st.get('title', 'Subtask')}** → `{st.get('template_id', '?')}`{dep_str}"
                )
            plan_body = "\n".join(plan_lines)

            from app.cards import approval_card
            from app.assistant_relay import post_card_to_assistant_thread, card_to_markdown

            # Use plan_content (.plan/PLAN.md fetched from repo) if available,
            # fall back to extracting from the planner's result text.
            plan_content = task.get("plan_content") or ""
            if not plan_content:
                plan_content = await pool.fetchval(
                    "SELECT plan_content FROM tasks WHERE id = $1", task_id,
                ) or ""
            if not plan_content:
                if "PLAN.md" in result or "CLAUDE.md" in result:
                    parts = result.split("```json:subtasks")
                    if len(parts) > 1:
                        plan_content = parts[0].strip()

            card = approval_card(
                task_id=task_id,
                title=f"Plan ready — {len(parsed_subtasks)} subtasks",
                body=plan_body,
                subtask_summary=[
                    {"title": st.get("title", ""), "template_id": st.get("template_id", ""),
                     "priority": st.get("priority", "medium"),
                     "description": st.get("description", "")[:200]}
                    for st in parsed_subtasks
                ],
                context=plan_content[:32000] or result[:32000],
                show_action_buttons=True,
            )
            await post_card_to_assistant_thread(
                task=task, card=card, content_markdown=card_to_markdown(card),
                sender=template_id,
                notify_type="plan_awaiting_approval",
                notify_title=f"Plan ready for approval: {task.get('title', task_id)[:40]}",
            )
            _log.info(
                "Planner task %s awaiting approval (%d subtasks)",
                task_id, len(parsed_subtasks),
            )
        else:
            _log.warning("Planner %s produced no parseable subtasks", task_id)
    else:
        await emit_task_event(
            pool,
            event_type=TASK_COMPLETED,
            task_id=task_id, goal_id=task["goal_id"],
            payload=task_completed_payload(
                outcome_summary=result[:2000],
                artifacts_produced=artifacts,
            ),
            actor_id=await instance_actor_id(
                task_id=task_id,
                gateway_agent_id=task.get("instance_id", ""),
            ),
            actor_type="independent_agent",
            correlation_id=corr_id,
            causation_ids=[last_evt] if last_evt else None,
            summary=f"Agent completed → {new_state}",
            content=result[:32000],
            instance_id=task.get("instance_id", ""),
            agent_id=task.get("template_id", ""),
        )

        # Notify platform channels of task completion or approval gate
        try:
            import asyncio as _asyncio
            from app.channels.base import relay_lifecycle_event as _rle
            _evt_type = "task.completed" if new_state == "done" else "task.state_changed"
            _asyncio.create_task(_rle(task["goal_id"], _evt_type, {
                "task_id": task_id,
                "task_title": task.get("title", ""),
                "story_id": str(task.get("story_id") or ""),
                "old_state": "in_progress",
                "new_state": new_state,
                "result_summary": result[:500] if new_state == "done" else "",
            }))
        except Exception as _exc:
            _log.debug("relay task completion failed task=%s new_state=%s: %s", task_id, new_state, _exc)

        # Only review root tasks — subtasks are reviewed via their parent
        # once all siblings complete (handled in check_parent_completion).
        if not task.get("parent_task_id"):
            await _spawn_review_task(pool, task_id, task)

    # Planner subtasks are NO LONGER auto-created here.
    # The plan approval card is posted above. Subtasks are created when the
    # human clicks "Approve" — handled by approve_plan() in tasks.py.

    # Don't destroy individual subtask instances here.
    # For persistent workers, the single instance stays alive until parent completes.
    # For standard workers, cleanup happens in cleanup_parent_task.

    # Store CLI session ID from result if available (for persistent worker tracking)
    if task.get("parent_task_id") and task.get("instance_id"):
        try:
            report = json.loads(result) if result.strip().startswith("{") else {}
            cli_sid = report.get("cli_session_id", "")
            if cli_sid:
                await pool.execute("""
                    UPDATE agent_instances SET cli_session_id = $1
                    WHERE gateway_agent_id = $2 AND status = 'active'
                """, cli_sid, task["instance_id"])
        except (json.JSONDecodeError, AttributeError):
            pass

    # Check if parent can auto-complete
    if task.get("parent_task_id"):
        await check_parent_completion(task["parent_task_id"])

    # Trigger controller to resolve newly unblocked dependencies
    await task_controller.trigger()

    # Auto-save task completion to goal memory
    try:
        from app.memory import auto_save_task_completed
        await auto_save_task_completed(pool, task)
    except Exception as exc:
        _log.warning("Failed to auto-save task completion memory: %s", exc)

    # Refresh MEMORY.md on any active assistant instances for this goal
    try:
        await _refresh_goal_assistant_instances(task["goal_id"])
    except Exception as exc:
        _log.warning("Failed to refresh assistant instances after task completion: %s", exc)


async def _refresh_goal_assistant_instances(goal_id: str) -> None:
    """Regenerate MEMORY.md for all active assistant instances in a goal.

    Called after task completions, plan approvals, and other goal data changes
    so that active assistant conversations see the latest state.
    """
    from app.memory import generate_memory_md

    pool = get_pool()
    active = await pool.fetch("""
        SELECT ai.gateway_agent_id, ai.thread_id, ai.template_id
        FROM agent_instances ai
        JOIN agents a ON a.id = ai.template_id
        WHERE ai.goal_id = $1 AND ai.status = 'active' AND a.type = 'SHCA'  -- DB constant, see agent_types.py
    """, goal_id)

    if not active:
        return

    from app.openclaw import gateway_admin
    for inst in active:
        try:
            md = await generate_memory_md(
                pool, inst["template_id"], goal_id, inst["thread_id"],
            )
            await gateway_admin.set_agent_file(
                inst["gateway_agent_id"], "MEMORY.md", md,
            )
        except Exception as exc:
            _log.debug("Failed to refresh MEMORY.md for %s: %s", inst["gateway_agent_id"], exc)

    _log.info("Refreshed MEMORY.md for %d active assistant instances in goal %s", len(active), goal_id)


# ── Cleanup ──────────────────────────────────────────────────────────────────

async def cleanup_parent_task(parent_task_id: str) -> None:
    """Destroy ALL agent instances in the task tree (parent + subtasks).

    Called once when the parent task completes (all subtasks done).
    For persistent workers, only one instance exists for the whole tree.
    Clears instance_id on each task to prevent duplicate cleanup.
    """
    pool = get_pool()
    destroyed_count = 0

    # Check for persistent worker first
    persistent_worker = await pool.fetchrow("""
        SELECT gateway_agent_id, task_id FROM agent_instances
        WHERE parent_task_id = $1 AND status = 'active' AND worker_type = 'persistent'
        LIMIT 1
    """, parent_task_id)

    if persistent_worker:
        # Persistent worker — one instance covers the whole task tree
        gw_id = persistent_worker["gateway_agent_id"]
        worker_task_id = persistent_worker["task_id"]

        # Build a fake task dict for destroy_instance
        worker_task = {"id": worker_task_id, "goal_id": "", "instance_id": gw_id}
        parent = await pool.fetchrow("SELECT goal_id FROM tasks WHERE id = $1", parent_task_id)
        if parent:
            worker_task["goal_id"] = parent["goal_id"]

        await destroy_instance(worker_task)
        destroyed_count = 1

        # Clear instance_id on parent + all subtasks
        await pool.execute("UPDATE tasks SET instance_id = NULL WHERE id = $1", parent_task_id)
        await pool.execute(
            "UPDATE tasks SET instance_id = NULL WHERE parent_task_id = $1",
            parent_task_id,
        )
    else:
        # Legacy: per-subtask instances (standard workers)
        parent = await pool.fetchrow("SELECT * FROM tasks WHERE id = $1", parent_task_id)
        if parent and parent.get("instance_id"):
            await destroy_instance(dict(parent))
            await pool.execute("UPDATE tasks SET instance_id = NULL WHERE id = $1", parent_task_id)
            destroyed_count += 1

        subtasks = await pool.fetch(
            "SELECT * FROM tasks WHERE parent_task_id = $1 AND instance_id IS NOT NULL AND instance_id != ''",
            parent_task_id,
        )
        for row in subtasks:
            await destroy_instance(dict(row))
            await pool.execute("UPDATE tasks SET instance_id = NULL WHERE id = $1", row["id"])
            destroyed_count += 1

    _log.info("Cleaned up %d instances for task tree %s", destroyed_count, parent_task_id)


# ── Singleton ────────────────────────────────────────────────────────────────

task_controller = TaskController()


async def trigger() -> None:
    """Module-level wake for the singleton controller.

    Callers that only want to nudge the dispatch loop can ``await trigger()``
    without reaching for the singleton object. This is the canonical wake entry
    point; several resume/approval paths import it by name. It MUST be awaited —
    ``trigger()`` unawaited just builds a coroutine that never runs (that latent
    bug silently defeated every kill-switch resume before this was added).
    """
    _log.debug("task_controller: module-level trigger() → waking singleton")
    await task_controller.trigger()
