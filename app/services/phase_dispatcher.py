"""Task dispatcher for `executor='claude-code-phase'` tasks (Stage 3-g).

The classic task_controller flow creates a new openclaw agent per IA
task. claude-code-phase tasks are different: each task is a new TURN on
an EXISTING claude-cli session (via runtime="cli" + resumeSessionId).
The session lives on disk in `~/.claude/projects/<...>/<sid>.jsonl` and
holds the full conversation context (plan, prior tasks, Q&A history).

This module:
  1. Picks ready claude-code-phase tasks (one at a time per story to
     avoid concurrent edits on the same session)
  2. Builds the [DISPATCH] or [DISPATCH-RESUME] / [ANSWER] message
     with the phase's system_prompt inlined
  3. Calls sessions_spawn(runtime="cli", resumeSessionId, task=...)
  4. Hands the response to sentinel_parser for state-machine updates
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from app.database import get_pool
from app.openclaw import gateway_admin, GatewayError
from app.services import claude_phases as cp_svc


_log = logging.getLogger(__name__)

# Credential injection is delegated to app.services.credential_brokers.
# PHASES_NEEDING_CREDENTIALS lives there as the single source of truth.


def _phase_prompt_inline(phase_row: dict) -> str:
    """Render a phase's system_prompt as the inline behavioural block
    we paste into a [DISPATCH] message. Keep it tight."""
    name = phase_row["name"]
    desc = (phase_row.get("description") or "").strip()
    sp = (phase_row.get("system_prompt") or "").strip()

    parts = [f"You are now in the **{name}** role for this task only."]
    if desc:
        parts.append(f"(Role description: {desc})")
    parts.append("")
    parts.append(sp)
    return "\n".join(parts)


# Phase-specific reminder appended near the top of the dispatch message,
# above the standing system_prompt. Defense in depth: the rule already
# lives in claude_phases.system_prompt, but agents have skipped past
# the standing prompt to rationalise narrower scope. The short reminder
# here is the second mention from a different angle so the rule is
# harder to miss. Kept cloud-agnostic — provider-specific constraints
# stay in the per-phase system_prompt.
_PHASE_REMINDERS: dict[str, str] = {
    "integration-tester": (
        "**Reminder before you start:** integration-tester cannot pass "
        "without exercising the deliverable's **primary effect** (the thing "
        "the goal asked it to do) against real infrastructure. CLI "
        "scaffolding (`--help`, missing-arg errors, file-not-found "
        "pre-flight) is necessary but NEVER sufficient. If the primary "
        "effect needs cloud / external credentials and the task message "
        "has no `## Cloud Credentials` (or `## GCP Credentials …`) block, "
        "emit `PHASE_TASK_BLOCKED` asking for them — do NOT silently shrink "
        "the scope to whatever your environment allows. "
        "If a cloud credential block IS present, use ONLY the exported "
        "environment variables it specifies — `CLOUDSDK_AUTH_ACCESS_TOKEN` "
        "for `gcloud`/`gsutil`, `GOOGLE_APPLICATION_CREDENTIALS` for Python/"
        "Node SDKs. **NEVER run `gcloud auth login`, "
        "`gcloud auth activate-service-account`, "
        "`gcloud config set account`, or "
        "`gcloud auth application-default login`.** "
        "These overwrite the VM's own persistent service account "
        "configuration for ALL operations on the machine."
    ),
    "deployment": (
        "**Reminder before you start:** if the repo contains any CI/CD "
        "config file (`.github/workflows/`, `cloudbuild.yaml`, "
        "`.gitlab-ci.yml`, `Jenkinsfile`, etc.), do NOT push or run any "
        "deploy command yet. Emit `PHASE_TASK_BLOCKED` first asking the "
        "operator whether a deploy-on-push automation is already live for "
        "this target — pushing on an assumption either double-deploys or "
        "silently no-ops. "
        "If a `## GCP Credentials` (or other cloud credential) block is "
        "present, use ONLY the exported environment variables it instructs "
        "— specifically `CLOUDSDK_AUTH_ACCESS_TOKEN` for `gcloud`/`gsutil` "
        "CLI commands. **NEVER run `gcloud auth login`, "
        "`gcloud auth activate-service-account`, "
        "`gcloud config set account`, or "
        "`gcloud auth application-default login`.** "
        "These commands overwrite the VM's persistent service account "
        "configuration for ALL operations on the machine, not just this "
        "task, and will break the VM's own identity."
    ),
}


def _phase_reminder(phase_name: str) -> str:
    """One-paragraph callout placed above the inlined system_prompt for
    selected phases. Empty string for phases without a reminder."""
    return _PHASE_REMINDERS.get(phase_name, "")


async def build_dispatch_message(
    task: dict, *, resume_with_answer: Optional[str] = None,
) -> str:
    """Build the message body sent to Claude for a phase task.

    If `resume_with_answer` is provided, this is a continuation after a
    PHASE_TASK_BLOCKED — format the answer alongside the original
    question (defends against session compaction).
    """
    phase_name = task.get("phase") or ""

    task_id = task["id"]
    title = task.get("title") or ""
    description = task.get("description") or ""
    acceptance = task.get("acceptance_criteria") or ""

    if resume_with_answer is not None:
        # Resume / [ANSWER] turns don't need the phase prompt — the
        # message body is generic boilerplate. Skip the registry lookup
        # so callers (like diff-review rejection) can pass a synthetic
        # task built from the DAG parent, which has no phase.
        prior_q = task.get("last_blocker_question") or "(prior question lost)"
        return (
            f"[ANSWER T{task_id}]\n"
            f"You asked: \"{prior_q}\"\n"
            f"Human answered: \"{resume_with_answer}\"\n"
            f"Continue executing task T{task_id} from where you left off. "
            f"You still have full session context (the plan, prior task "
            f"results, the workspace files). End with one of:\n"
            f"  PHASE_TASK_DONE T{task_id} — <summary>\n"
            f"  PHASE_TASK_BLOCKED T{task_id} — <one specific question>\n"
            f"  PHASE_TASK_FAILED T{task_id} — <reason>\n"
        )

    phase = await cp_svc.get_phase(phase_name)
    if not phase:
        raise ValueError(f"phase {phase_name!r} not registered in claude_phases")

    inline = _phase_prompt_inline(phase)
    reminder = _phase_reminder(phase_name)
    reminder_block = f"{reminder}\n\n" if reminder else ""
    return (
        f"[DISPATCH] Task T{task_id} · phase={phase_name}\n"
        f"Title: {title}\n"
        f"Description:\n{description}\n\n"
        f"Acceptance: {acceptance}\n\n"
        f"{reminder_block}"
        f"{inline}\n\n"
        f"## Finishing this task (this section OVERRIDES any earlier formatting)\n\n"
        f"Your reply's LAST line MUST be EXACTLY one of these sentinels, on its own "
        f"line, with NOTHING after it. Put ALL evidence, criteria checklists, error "
        f"output and tracebacks ABOVE the sentinel — never below it. A turn that ends "
        f"without a sentinel as its final line is rejected and you'll be asked to "
        f"re-emit it; if it is still missing, the task is failed. The sentinels:\n\n"
        f"- `PHASE_TASK_DONE T{task_id} — <one-line summary>` — use this "
        f"when the work in the description is finished to the acceptance "
        f"criterion. Default outcome.\n"
        f"- `PHASE_TASK_BLOCKED T{task_id} — <one specific question>` — "
        f"use this when you hit a real decision that the description "
        f"doesn't answer AND you'd risk making the wrong choice. "
        f"Examples: ambiguous behaviour for an edge case, choice between "
        f"two equally valid conventions, missing acceptance detail. "
        f"Do NOT use BLOCKED for trivia (file naming, formatting, "
        f"library choice when the repo's existing style is clear) — "
        f"make the call and note it in your DONE summary.\n"
        f"- `PHASE_TASK_FAILED T{task_id} — <reason>` — use this when "
        f"there's a hard blocker you can't recover from (missing "
        f"dependency, tool not installed, fundamentally impossible). "
        f"Distinct from BLOCKED: FAILED gives up; BLOCKED asks a "
        f"question.\n\n"
        f"After a BLOCKED, the human will answer and you'll get a "
        f"`[ANSWER T{task_id}]` turn to continue. You can BLOCK multiple "
        f"times on the same task — ask one question per round.\n\n"
        f"Reminder: do NOT commit or push during this task — the diff "
        f"review gate handles that at the end."
    )


async def find_next_dispatchable_task() -> Optional[dict]:
    """Return one ready claude-code-phase task whose story has no other
    in_progress / dispatching claude-code-phase sibling.

    We dispatch one task per story at a time to avoid concurrent turns
    on the same Claude session (the session is single-threaded).
    """
    pool = get_pool()

    # Kill switch — SYSTEM freeze short-circuit (defence-in-depth; _tick also
    # short-circuits before calling this).
    from app.services import execution_control  # noqa: PLC0415
    if await execution_control.is_system_paused():
        _log.info("phase_dispatcher: selection suppressed — SYSTEM execution paused")
        return None

    row = await pool.fetchrow(
        """
        WITH busy_stories AS (
            -- A story is busy if it has an active phase task OR an active system-gate.
            -- Prevents a phase task from dispatching while a gate is running for the
            -- same story (and vice versa — gate selector uses the same CTE).
            SELECT DISTINCT story_id FROM tasks
             WHERE story_id IS NOT NULL
               AND (
                   (executor = 'claude-code-phase'
                    AND state IN ('in_progress', 'dispatching', 'blocked'))
                   OR
                   (executor = 'system-gate'
                    AND state IN ('in_progress', 'dispatching', 'awaiting_approval'))
               )
        )
        SELECT t.*
          FROM tasks t
          JOIN stories s ON s.id = t.story_id
         WHERE t.executor = 'claude-code-phase'
           AND t.state = 'ready'
           AND t.story_id IS NOT NULL
           -- Deleted-goal guard: goal delete is soft and doesn't cascade to
           -- tasks/stories, so without this a dead goal's ready phase task would
           -- re-spawn a Claude session. Skip goals that are no longer active.
           AND EXISTS (
               SELECT 1 FROM goals g
                WHERE g.id = t.goal_id AND g.status = 'active'
           )
           AND t.story_id NOT IN (SELECT story_id FROM busy_stories)
           -- Operator-mode replan flag (migration 054): a human paused this
           -- story's execution. Separate, independent feature from the kill
           -- switch below — both gates must be clear to dispatch.
           AND s.execution_paused = FALSE
           -- Kill switch — GOAL freeze gate (the execution_pauses table is the
           -- kill switch's own store, never mixed with stories.execution_paused).
           AND NOT EXISTS (
               SELECT 1 FROM execution_pauses ep
                WHERE ep.scope = 'goal' AND ep.scope_id = t.goal_id
           )
         ORDER BY t.created_at ASC
         LIMIT 1
        """,
    )
    return dict(row) if row else None


def _parse_jsonb_metadata(row, story_id) -> dict:
    """Decode the `metadata` column of a goal_resources row into a dict.
    asyncpg returns JSONB as dict already; if a driver hands back a str
    we parse it. On parse failure, return {} and log a warning.
    """
    raw = row["metadata"] if row else None
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            _log.warning(
                "phase_dispatcher: malformed metadata JSON for goal_resource "
                "(story=%s) — treating as empty",
                story_id,
            )
            return {}
    return {}


async def _build_gcp_credential_block(task: dict, story_id: int) -> str:
    """Return a GCP credential section to append to the dispatch message.

    Reads the goal's GCP resources and dispatches to the GcpBroker for
    the actual credential work. The broker owns the dispatch_block format
    and any cleanup handle persisted on the task row.

    Resource lookup follows the repo + branch pattern:
      • `gcp_environment` carries project/region context (optional)
      • `gcp_test_sa` / `gcp_deploy_sa` each carry one SA email (the
        per-phase attachment). A goal can have either or both.
      • Legacy fallback: a `gcp_environment` row with
        `test_service_account` / `deploy_service_account` in its
        metadata still works without forcing a resource migration.

    Returns an empty string if the phase doesn't need credentials, the
    resource is missing, or the SA isn't configured. Otherwise returns
    the broker's dispatch_block.
    """
    from app.services.credential_brokers import (  # noqa: PLC0415
        PHASES_NEEDING_CREDENTIALS,
    )
    from app.services.credential_brokers.gcp import GcpBroker  # noqa: PLC0415

    phase_name = task.get("phase") or ""
    if phase_name not in PHASES_NEEDING_CREDENTIALS:
        return ""

    task_id = task["id"]

    # Resource lookup follows the repo + branch pattern: gcp_environment
    # carries project/region (the "container"), gcp_test_sa / gcp_deploy_sa
    # carry one SA each (the "branch-like" attachment). The dispatcher
    # joins them.
    #
    # Backwards-compat: a legacy gcp_environment row may still carry
    # test_service_account / deploy_service_account in its metadata. If
    # no dedicated gcp_test_sa / gcp_deploy_sa row exists, the dispatcher
    # falls back to those legacy fields so existing goals keep working
    # without forcing a resource migration.
    if phase_name == "integration-tester":
        role = "test"
        sa_resource_kind = "gcp_test_sa"
        legacy_field = "test_service_account"
        role_label = "integration testing"
    else:
        role = "deploy"
        sa_resource_kind = "gcp_deploy_sa"
        legacy_field = "deploy_service_account"
        role_label = "deployment"

    pool = get_pool()

    # 1. gcp_environment (project/region context). Optional — without it
    #    the dispatch block just omits project/region, and the SDK uses
    #    whatever's in the SA key file.
    env_row = await pool.fetchrow(
        """
        SELECT gr.metadata
          FROM goal_resources gr
          JOIN stories s ON s.goal_id = gr.goal_id
         WHERE s.id = $1
           AND gr.kind = 'gcp_environment'
         LIMIT 1
        """,
        story_id,
    )
    env_metadata = _parse_jsonb_metadata(env_row, story_id) if env_row else {}

    # 2. Per-SA resource for this phase (preferred).
    sa_row = await pool.fetchrow(
        """
        SELECT gr.metadata
          FROM goal_resources gr
          JOIN stories s ON s.goal_id = gr.goal_id
         WHERE s.id = $1
           AND gr.kind = $2
         LIMIT 1
        """,
        story_id, sa_resource_kind,
    )
    sa_metadata_row = _parse_jsonb_metadata(sa_row, story_id) if sa_row else {}

    # 3. Pick the SA email — new resource first, legacy field second.
    sa_email = (sa_metadata_row.get("sa_email") or "").strip()
    if not sa_email and env_metadata:
        sa_email = (env_metadata.get(legacy_field) or "").strip()
        if sa_email:
            _log.info(
                "phase_dispatcher: using legacy %s from gcp_environment for "
                "%s task T%s — operator should migrate to %s resource",
                legacy_field, phase_name, task_id, sa_resource_kind,
            )

    if not sa_email:
        _log.warning(
            "phase_dispatcher: %s task T%s has no SA configured for story %s "
            "(neither %s resource nor gcp_environment.%s)",
            phase_name, task_id, story_id, sa_resource_kind, legacy_field,
        )
        return (
            f"\n## GCP Credentials (this task only)\n\n"
            f"⚠️ No service account configured for **{phase_name}**.\n\n"
            f"Add a `{sa_resource_kind}` resource to the goal (with `sa_email` "
            f"set to the target service account), or — for legacy goals — set "
            f"the `{legacy_field}` field on the goal's `gcp_environment` resource.\n\n"
            f"Emit: `PHASE_TASK_BLOCKED T{task_id} — No {role_label} service "
            f"account configured. Goal operator must add a `{sa_resource_kind}` "
            f"resource.`\n"
        )

    # Build the per-SA metadata dict the broker expects. Target project/region
    # can come from the SA resource itself (per-SA override) OR from
    # gcp_environment. SA resource wins if both have a value.
    def _pick(field: str) -> str:
        return (
            (sa_metadata_row.get(field) or "").strip()
            or (env_metadata.get(field) or "").strip()
        )

    broker = GcpBroker(role=role)
    sa_metadata = {
        "sa_email":          sa_email,
        "target_project_id": _pick("target_project_id"),
        "target_region":     _pick("target_region"),
    }

    story_row = await pool.fetchrow(
        "SELECT shca_instance_id, workspace_path FROM stories WHERE id = $1",
        story_id,
    )
    workspace_path = (story_row["workspace_path"] if story_row else "") or ""
    instance_id = (story_row["shca_instance_id"] if story_row else "") or ""

    bundle = await broker.provision(
        resource_metadata=sa_metadata,
        phase=phase_name,
        task_id=task_id,
        story_id=str(story_id),
        workspace_path=workspace_path,
        instance_id=instance_id,
    )

    if bundle is None:
        # Broker rejected the resource (e.g. malformed SA email). Surface
        # a BLOCKED guidance block so the agent stops cleanly instead of
        # silently running with no creds.
        _log.warning(
            "phase_dispatcher: GcpBroker.provision returned None for %s task T%s "
            "(sa_email=%r) — resource rejected as malformed",
            phase_name, task_id, sa_email,
        )
        return (
            f"\n## GCP Credentials (this task only)\n\n"
            f"⚠️ Service account email `{sa_email}` is malformed; the broker "
            f"refused to provision credentials.\n\n"
            f"Emit: `PHASE_TASK_BLOCKED T{task_id} — Malformed {role_label} "
            f"service account email in goal resource. Operator must fix it.`\n"
        )

    # Persist the cleanup_handle on the task row so the strict-cleanup
    # hook (and the watchdog sweep) can revoke whatever provision()
    # created when the task reaches a terminal state. Empty handle means
    # nothing to clean up (provision failed before writing any state).
    if bundle.get("cleanup_handle"):
        await pool.execute(
            "UPDATE tasks SET credential_cleanup_handle = $1, updated_at = NOW() "
            "WHERE id = $2",
            json.dumps(bundle["cleanup_handle"]), task_id,
        )

    return bundle["dispatch_block"]


async def dispatch_phase_task(task: dict, already_claimed: bool = False) -> dict:
    """Send a claude-code-phase task to Claude via sessions_spawn.

    `already_claimed=True` means the DAG scheduler (enforce path) already
    CAS-claimed this task to 'dispatching' under the admission lock, so the
    internal CAS below is skipped. The claim UPDATE it performs is identical
    (state->'dispatching', dispatched_at=NOW()), so crash recovery via the
    stale-dispatch reaper is unchanged either way.

    Marks task 'dispatching' → 'in_progress' → ... and lets
    sentinel_parser take over once the response comes back.

    Returns: { ok, task_id, claude_response_text, sentinel } or
             { ok: False, error }
    """
    from app.services.sentinel_parser import handle_phase_task_response

    pool = get_pool()
    task_id = task["id"]
    story_id = task["story_id"]

    # Kill switch — re-check system/goal before sending. Closes the small
    # select->create_task window in _tick where a pause could flip after
    # find_next_dispatchable_task() returned this task.
    from app.services import execution_control  # noqa: PLC0415
    _blk = await execution_control.dispatch_block(task.get("goal_id") or "")
    if _blk:
        _log.info("phase_dispatcher: dispatch skipped for T%s — %s paused (reason=%s)",
                  task_id, _blk.scope, _blk.reason or "<none>")
        return {"ok": False, "error": f"execution paused ({_blk.scope})"}

    # Resolve story → instance_id, workspace_path, execution_paused, and the LIVE
    # claude session (SSOT — see C5 below).
    story = await pool.fetchrow(
        """SELECT shca_instance_id, workspace_path, execution_paused,
                  current_claude_session_id
             FROM stories WHERE id = $1""",
        story_id,
    )
    if not story:
        return {"ok": False, "error": "story not found"}
    # M9: re-check story pause in the select→create_task→dispatch window. The
    # SQL selector filters execution_paused=FALSE but a pause can flip in the
    # gap between find_next_dispatchable_task() returning and this coroutine
    # running its CAS claim.
    if story["execution_paused"]:
        _log.info(
            "phase_dispatcher: T%s skipped — story S%s paused in dispatch window",
            task_id, story_id,
        )
        return {"ok": False, "error": "story execution paused"}
    instance_id = story["shca_instance_id"]
    ws_path = story["workspace_path"]
    if not instance_id or not ws_path:
        return {"ok": False, "error": "story workspace not provisioned"}

    # C5: use the story's LIVE claude session (SSOT), not the task's frozen copy —
    # a fork/kill/reset updates the story column, never the frozen task copy.
    # Empty or the __prespawn_ sentinel ⇒ no live session: do NOT dispatch
    # (resuming a killed/deleted transcript or forking fresh mid-DAG loses context).
    session_id = (story["current_claude_session_id"] or "").strip()
    if not session_id or session_id.startswith("__prespawn_"):
        return {"ok": False, "error": "story has no live claude session (reset/killed/provisioning)"}

    # CAS claim — bail if another dispatch already claimed this task (state no
    # longer 'ready'). Guards the within-instance second-tick race where
    # asyncio.create_task hasn't yet yielded to run this coroutine's UPDATE
    # before _tick fires again and re-selects the same 'ready' task.
    # Skipped when already_claimed: the DAG scheduler performed this exact CAS
    # under the admission lock, so the task is already 'dispatching'.
    if not already_claimed:
        tag = await pool.execute(
            "UPDATE tasks SET state = 'dispatching', dispatched_at = NOW(), "
            "updated_at = NOW() WHERE id = $1 AND state = 'ready'",
            task_id,
        )
        if tag == "UPDATE 0":
            _log.info(
                "phase_dispatcher: T%s already claimed by another dispatch, skipping",
                task_id,
            )
            return {"ok": False, "error": "task already claimed"}

    # Build message and append phase-scoped GCP credentials if applicable.
    try:
        message = await build_dispatch_message(task)
        gcp_block = await _build_gcp_credential_block(task, story_id)
        if gcp_block:
            message = message + gcp_block
    except ValueError as exc:
        # Build-time failure (bad phase / credential block) before the agent ran.
        # Fail + cascade deterministically (was: failed without cascade → DAG stalled).
        await _fail_phase_dispatch(task, f"phase dispatch build error: {exc}")
        return {"ok": False, "error": str(exc)}

    # Mark in_progress, then spawn.
    await pool.execute(
        "UPDATE tasks SET state = 'in_progress', "
        "started_at = COALESCE(started_at, NOW()), updated_at = NOW() "
        "WHERE id = $1",
        task_id,
    )

    # shca.claude.invoke is the gateway-level wrapper around spawnCliDirect.
    # (sessions_spawn is an agent tool — not callable directly from the
    # control panel via gateway_admin.) The gateway pulls
    # CONTROL_PANEL_URL + OPENCLAW_GATEWAY_TOKEN from its own env to set
    # the hook env vars, so we don't need to pass them here.
    rpc_params = {
        "agentId": instance_id,
        "storyId": story_id,
        # goalId lets the gateway mint a goal-scoped hook token for the claude
        # child instead of injecting the shared gateway admin token (shared-
        # token exfiltration fix — see openclaw cli-spawn.ts).
        "goalId": task.get("goal_id"),
        "cwd": ws_path,
        "resumeSessionId": session_id,
        "task": message,
        "taskId": task_id,
    }

    _log.info(
        "phase_dispatcher: dispatching T%s phase=%s story=%s session=%s msg_chars=%d",
        task_id, task.get("phase"), story_id, session_id[:8], len(message),
    )

    try:
        result = await gateway_admin.call(
            "shca.claude.invoke", rpc_params, timeout=1800,  # 30-min cap
        )
    except GatewayError as exc:
        # RPC error (incl. connection drop / retryable). The turn's outcome is unknown
        # and the DAG runs sequentially on one session, so fail deterministically +
        # cascade rather than parking 'blocked' (a silent dead-end the Q&A relay and
        # janitor both ignore). Retry/replan is human-gated via the failure card, so
        # there is no auto double-run risk.
        _log.warning("phase dispatch RPC failed (task=%s retryable=%s): %s", task_id, exc.retryable, exc)
        if exc.retryable:
            reason = (
                f"Gateway connection lost during the phase invoke — the turn's outcome "
                f"is unknown. Verify no Claude session is still running on the VM for "
                f"this task, then retry or replan from the task board. ({exc})"
            )
        else:
            reason = f"phase invoke failed: {exc}"
        await _fail_phase_dispatch(task, reason)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        # Timeout (30-min cap) / crash / unexpected — NEVER leave the task silently
        # in_progress for the 40-min janitor. Deterministic fail + cascade.
        _log.warning("phase dispatch unexpected error (task=%s): %s", task_id, exc)
        await _fail_phase_dispatch(task, f"phase invoke error (timeout/crash): {exc}")
        return {"ok": False, "error": str(exc)}

    # C3: the gateway per-story turn lock returned "busy" — another claude turn is
    # already in flight for this story. The result carries no real turn (empty
    # text), so do NOT hand it to the sentinel parser (that would look
    # off-protocol and strand the task in_progress). Revert to ready so the
    # controller re-dispatches on a later tick, and release the creds provisioned
    # for this attempt.
    if result.get("status") == "busy":
        await pool.execute(
            "UPDATE tasks SET state = 'ready', updated_at = NOW() "
            "WHERE id = $1 AND state IN ('dispatching','in_progress')",
            task_id,
        )
        from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
        await release_credentials_safe(task_id, "phase_dispatcher:busy_reject")
        _log.info("phase_dispatcher: T%s busy (per-story lock held) — reverted to ready for retry", task_id)
        return {"ok": False, "busy": True, "task_id": task_id}

    # Persist sessionId — belt-and-suspenders; cli-spawn also POSTs to
    # /internal/shca/stories/<sid>/claude-session, but doing it here
    # too means a single point of truth for the column.
    returned_sid = (result.get("sessionId") or "").strip()
    if returned_sid and returned_sid != session_id:
        await pool.execute(
            "UPDATE stories SET current_claude_session_id = $1, "
            "updated_at = NOW() WHERE id = $2",
            returned_sid, story_id,
        )

    # Hand off to sentinel parser.
    text = result.get("text") or ""
    await handle_phase_task_response(task, text, result)
    return {"ok": True, "task_id": task_id, "text_len": len(text)}


async def _fail_phase_dispatch(task: dict, reason: str) -> None:
    """Fail a phase task at dispatch time (RPC error / invoke timeout / build error) and
    cascade-fail its DAG — deterministic, never a silent in_progress/blocked.

    Phase 2a: previously a retryable RPC drop parked the task 'blocked' with no question
    (which the Q&A relay and janitor both ignore → silent dead-end), and the ValueError /
    unexpected-error paths marked the task 'failed' but did NOT cascade, so the DAG stalled
    (dependents never became ready). Both are now failed + cascaded so the failure surfaces
    (failure card) and the human can retry/replan."""
    from app.services.sentinel_parser import _fail_dag_cascade  # noqa: PLC0415
    from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
    pool = get_pool()
    task_id = task["id"]
    await pool.execute(
        "UPDATE tasks SET state = 'failed', blocked_reason = $1, completed_at = NOW(), "
        "updated_at = NOW() WHERE id = $2 AND state NOT IN ('halted','cancelled','done')",
        reason, task_id,
    )
    await release_credentials_safe(task_id, "phase_dispatcher:dispatch_failed")
    await _fail_dag_cascade(
        task["goal_id"], task.get("story_id"), task.get("parent_task_id"), task_id, reason,
    )


async def dispatch_resume_with_answer(
    task: dict, human_answer: str,
) -> dict:
    """Re-enter a blocked task with the human's answer.

    Same shape as dispatch_phase_task but builds a [ANSWER T<id>] message
    instead of [DISPATCH]. State goes blocked → in_progress.
    """
    from app.services.sentinel_parser import handle_phase_task_response

    pool = get_pool()
    task_id = task["id"]
    story_id = task["story_id"]
    # Capture the pending question in-memory BEFORE the pre-invoke UPDATE NULLs
    # it. The human's answer is a fire-and-forget arg that is never persisted
    # (chat.py M17), so if this resume can't run (busy), we must restore this
    # question and leave the task 'blocked' — otherwise the answer is lost and a
    # plain phase turn re-dispatches. See the busy branch below.
    blocker_question = task.get("last_blocker_question")

    # Kill switch — the Q&A relay (chat.py) calls this directly when a human
    # answers a blocked phase task; it bypasses the controller tick, so this is
    # the only place a system/goal freeze can be enforced for the resume turn.
    from app.services import execution_control  # noqa: PLC0415
    _blk = await execution_control.dispatch_block(task.get("goal_id") or "")
    if _blk:
        _log.info("phase_dispatcher: resume skipped for T%s — %s paused (reason=%s)",
                  task_id, _blk.scope, _blk.reason or "<none>")
        return {"ok": False, "error": f"execution paused ({_blk.scope})"}

    story = await pool.fetchrow(
        """SELECT shca_instance_id, workspace_path, current_claude_session_id
             FROM stories WHERE id = $1""",
        story_id,
    )
    if not story:
        return {"ok": False, "error": "story not found"}
    instance_id = story["shca_instance_id"]
    ws_path = story["workspace_path"]
    # C5: resume the story's LIVE session (SSOT), not the task's frozen copy.
    # Empty / __prespawn_ sentinel ⇒ no live session to answer into.
    session_id = (story["current_claude_session_id"] or "").strip()
    if not session_id or session_id.startswith("__prespawn_"):
        return {"ok": False, "error": "story has no live claude session to resume"}

    message = await build_dispatch_message(task, resume_with_answer=human_answer)
    # Always inject fresh GCP credentials on resume — the original token from the
    # initial dispatch may have expired during the BLOCKED wait (1h lifetime).
    gcp_block = await _build_gcp_credential_block(task, story_id)
    if gcp_block:
        message = message + gcp_block

    await pool.execute(
        """UPDATE tasks SET
               state = 'in_progress',
               started_at = COALESCE(started_at, NOW()),
               last_blocker_question = NULL,
               updated_at = NOW()
           WHERE id = $1""",
        task_id,
    )

    # The human chose to ANSWER (resume) rather than use the blocked recovery card's
    # Retry/Replan/Abandon buttons. Stamp that card decided so its now-stale buttons can't
    # later cancel/replan a DAG that has already resumed on the answer.
    try:
        from app.services.story_recovery import mark_recovery_cards_decided  # noqa: PLC0415
        if task.get("parent_task_id"):
            await mark_recovery_cards_decided(task["parent_task_id"], "answered")
    except Exception:
        _log.warning("resume: could not stamp blocked recovery card decided (task=%s)", task_id)

    rpc_params = {
        "agentId": instance_id,
        "storyId": story_id,
        # goalId lets the gateway mint a goal-scoped hook token for the claude
        # child instead of injecting the shared gateway admin token (shared-
        # token exfiltration fix — see openclaw cli-spawn.ts).
        "goalId": task.get("goal_id"),
        "cwd": ws_path,
        "resumeSessionId": session_id,
        "task": message,
        "taskId": task_id,
    }

    _log.info(
        "phase_dispatcher: RESUMING T%s with answer (chars=%d)",
        task_id, len(human_answer),
    )

    try:
        result = await gateway_admin.call(
            "shca.claude.invoke", rpc_params, timeout=1800,
        )
    except GatewayError as exc:
        # Symmetric with dispatch_phase_task: an RPC error / timeout on the resume turn
        # (incl. retryable connection drop) FAILS + cascades deterministically rather
        # than parking 'blocked' (the silent dead-end this refactor removes). The
        # human's answer was already NULLed by the pre-invoke UPDATE and the turn's
        # outcome is unknown, so recovery is a human-gated retry/replan (no double-run).
        _log.warning("phase resume RPC failed (task=%s retryable=%s): %s", task_id, exc.retryable, exc)
        if exc.retryable:
            reason = (
                f"Gateway connection lost during the resume invoke — the turn's outcome "
                f"is unknown. Verify no Claude session is still running on the VM for "
                f"this task, then retry or replan from the task board. ({exc})"
            )
        else:
            reason = f"resume invoke failed: {exc}"
        await _fail_phase_dispatch(task, reason)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        # Timeout / crash / unexpected — NEVER leave the resumed task silently
        # in_progress (it's fired fire-and-forget from chat.py, so an uncaught error
        # would otherwise strand it for the 40-min janitor). Deterministic fail + cascade.
        _log.warning("phase resume unexpected error (task=%s): %s", task_id, exc)
        await _fail_phase_dispatch(task, f"resume invoke error (timeout/crash): {exc}")
        return {"ok": False, "error": str(exc)}

    # C3: per-story turn lock is busy (another claude turn in flight). No real
    # turn happened. Unlike dispatch_phase_task, we must NOT revert to 'ready':
    # the human's answer was never persisted (fire-and-forget arg) and the
    # pre-invoke UPDATE already NULLed last_blocker_question, so a 'ready' task
    # would re-dispatch a plain [DISPATCH] turn and silently DROP the answer.
    # Instead revert to 'blocked' and RESTORE the question so the task stays a
    # pending Q&A; the human re-sends their answer (which re-fires the relay in
    # chat.py, gated on state='blocked' AND last_blocker_question IS NOT NULL).
    # Skip the sentinel parser; release the resume creds.
    # TODO(followup): surface a "Claude Code busy — resend your answer" notice to
    # the human here (phase_dispatcher has no thread-message helper today; the
    # relay is fire-and-forget so the return value isn't consumed by chat.py).
    if result.get("status") == "busy":
        await pool.execute(
            "UPDATE tasks SET state = 'blocked', last_blocker_question = $2, "
            "updated_at = NOW() "
            "WHERE id = $1 AND state IN ('dispatching','in_progress')",
            task_id, blocker_question,
        )
        from app.services.credential_brokers import release_credentials_safe  # noqa: PLC0415
        await release_credentials_safe(task_id, "phase_dispatcher:busy_reject")
        _log.info("phase_dispatcher: resume T%s busy (per-story lock held) — reverted to blocked, answer not consumed (human must resend)", task_id)
        return {"ok": False, "busy": True, "task_id": task_id}

    text = result.get("text") or ""
    await handle_phase_task_response(task, text, result)
    return {"ok": True, "task_id": task_id, "text_len": len(text)}


async def resume_blocked_task_safe(task: dict, human_answer: str) -> None:
    """Fire-and-forget-safe wrapper around dispatch_resume_with_answer.

    The Q&A relay (chat.py) launches the resume via asyncio.create_task with no await,
    so an uncaught exception ANYWHERE in the resume (credential build, invoke, post-
    processing) would be a lost background-task exception that strands the task
    'in_progress' for the 40-min janitor — exactly the silent stall this refactor
    removes. This guarantees any such crash instead fails the task + cascades. The
    normal error/timeout/busy arms inside dispatch_resume_with_answer still handle their
    cases; this only catches what escapes them."""
    try:
        await dispatch_resume_with_answer(task, human_answer)
    except Exception as exc:
        _log.warning("resume_blocked_task_safe: resume crashed (task=%s): %s", task.get("id"), exc)
        try:
            await _fail_phase_dispatch(task, f"resume crashed: {exc}")
        except Exception:
            _log.exception(
                "resume_blocked_task_safe: _fail_phase_dispatch also failed for %s",
                task.get("id"),
            )
